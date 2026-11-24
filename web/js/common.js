// 모든 페이지 공통: rosbridge 연결, 로봇 목록(fleet), 상단바(연결·지연·배터리·로봇 선택), 전체 비상정지
(function () {
  'use strict';

  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } },
  };

  const defaultUrl = `ws://${location.hostname || 'localhost'}:9090`;
  const ros = new window.Ros(store.get('tb3.rosbridge', defaultUrl));

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (v, d = 2) => (typeof v === 'number' && Number.isFinite(v) ? v.toFixed(d) : '—');

  // ---------- toast ----------
  function toast(text, kind = 'info', ms = 3500) {
    let box = $('#toasts');
    if (!box) { box = document.createElement('div'); box.id = 'toasts'; box.setAttribute('role', 'status'); document.body.appendChild(box); }
    const t = document.createElement('div');
    t.className = `toast toast-${kind}`;
    t.textContent = text;
    box.appendChild(t);
    setTimeout(() => t.remove(), ms);
  }

  function toastAction(text, label, fn, kind = 'warn', ms = 6000) {
    toast(text, kind, ms);
    const t = $('#toasts').lastElementChild;
    const b = document.createElement('button');
    b.className = 'btn btn-sm';
    b.style.marginLeft = '10px';
    b.textContent = label;
    b.addEventListener('click', () => { t.remove(); fn(); });
    t.appendChild(b);
  }

  // ---------- 조종 권한 (여러 브라우저가 같은 로봇을 동시에 조종하지 못하게) ----------
  // 서버(/api/control)의 3초 lease. 조종 중 1초마다 갱신, 입력이 끝나면 놓음. 비상정지는 권한과 무관.
  const control = (() => {
    let clientId = '';
    try { clientId = sessionStorage.getItem('tb3.client') || ''; } catch (e) { /* ignore */ }
    if (!clientId) {
      clientId = Math.random().toString(36).slice(2, 10);
      try { sessionStorage.setItem('tb3.client', clientId); } catch (e) { /* ignore */ }
    }
    const label = `${/Mobi|Android/i.test(navigator.userAgent) || location.pathname.endsWith('mobile.html') ? '모바일' : 'PC'} ${clientId.slice(0, 4)}`;
    const mine = new Map();      // robot id -> renew timer
    const pending = new Set();
    const lastDenied = new Map();
    async function call(robot, action) {
      const r = await fetch('/api/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ robot: robot.id, client: clientId, label, action }) });
      if (!r.ok) throw new Error((await r.json()).error || r.status);
      return r.json();
    }
    function keep(robot) {
      if (mine.has(robot.id)) return;
      mine.set(robot.id, setInterval(async () => {
        try {
          const r = await call(robot, 'renew');
          if (!r.ok) { lost(robot, r.holder); }
        } catch (e) { /* 서버가 잠깐 안 되면 다음 갱신에서 다시 */ }
      }, 1000));
    }
    function lost(robot, holder) {
      clearInterval(mine.get(robot.id)); mine.delete(robot.id);
      toast(`${robot.name}: 조종 권한이 ${holder ? holder.label : '다른 사용자'} 에게 넘어갔습니다`, 'warn');
      document.dispatchEvent(new CustomEvent('tb3:control-lost', { detail: robot.id }));
    }
    async function acquire(robot, force) {
      pending.add(robot.id);
      try {
        const r = await call(robot, force ? 'force' : 'acquire');
        if (r.ok) { keep(robot); return true; }
        const now = performance.now();
        if (now - (lastDenied.get(robot.id) || 0) > 3000) {
          lastDenied.set(robot.id, now);
          toastAction(`${robot.name} 은(는) ${r.holder.label} 이(가) 조종 중입니다`, '넘겨받기', () => acquire(robot, true).then((ok) => ok && toast('조종 권한을 넘겨받았습니다. 다시 조종하세요.', 'ok')));
        }
        return false;
      } catch (e) {
        // 조종 권한 서버를 쓸 수 없으면(예: serve.py 없이 열었을 때) 조종은 허용
        keep.noServer = true;
        return true;
      } finally { pending.delete(robot.id); }
    }
    return {
      clientId, label,
      mine: (robot) => mine.has(robot.id) || keep.noServer === true,
      // 조종 루프에서 매번 호출: 권한이 있으면 true, 없으면 요청을 걸어 두고 false
      drive(robot) {
        if (mine.has(robot.id) || keep.noServer) return true;
        if (!pending.has(robot.id)) acquire(robot, false);
        return false;
      },
      release(robot) {
        if (!mine.has(robot.id)) return;
        clearInterval(mine.get(robot.id)); mine.delete(robot.id);
        call(robot, 'release').catch(() => {});
      },
      async holders() { try { return await (await fetch('/api/control', { cache: 'no-store' })).json(); } catch (e) { return {}; } },
    };
  })();

  // ---------- fleet ----------
  const FALLBACK = { mapTopic: '/map', robots: [{ id: 'burger_01', name: 'burger-01', type: 'turtlebot3', ns: '', tfTopic: '/tf', tfPrefix: '', color: '#2DD4BF' }] };
  let fleet = FALLBACK;
  let robots = [];
  let robot = null;               // 이 페이지가 다루는 로봇

  async function loadFleet() {
    try {
      const r = await fetch('/api/fleet', { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      fleet = await r.json();
      if (!fleet.robots || !fleet.robots.length) fleet = { ...fleet, robots: FALLBACK.robots };
    } catch (e) {
      fleet = FALLBACK;
      toast('로봇 목록(/api/fleet)을 읽지 못해 기본 로봇 1대로 동작합니다', 'warn', 5000);
    }
    robots = fleet.robots.map((cfg) => new window.Robot(ros, cfg));
    const want = new URLSearchParams(location.search).get('robot') || store.get('tb3.robot', '');
    robot = robots.find((r) => r.id === want) || robots[0];
    store.set('tb3.robot', robot.id);
  }

  // ---------- 관리자 토큰이 필요한 요청 (구역 저장, 지도 작성, 활성 지도 지정) ----------
  async function adminFetch(url, opts = {}, retry = true) {
    const token = store.get('tb3.adminToken', '');
    const r = await fetch(url, { ...opts, headers: { 'Content-Type': 'application/json', ...(opts.headers || {}), 'X-Admin-Token': token } });
    if (r.status === 401 && retry) {
      const t = prompt('관리자 토큰을 입력하세요.\nPC 에서: cat ~/turtlebot3/ros2-fleet-console/config/admin_token', '');
      if (!t) throw new Error('토큰이 없어 취소했습니다');
      store.set('tb3.adminToken', t.trim());
      return adminFetch(url, opts, false);
    }
    if (r.status === 401) store.set('tb3.adminToken', '');
    return r;
  }
  async function adminJson(url, opts) {
    const r = await adminFetch(url, opts);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
    return body;
  }
  // 활성 지도(지도 작성·구역·Nav2 기준) 지정: fleet.json 의 mapFile
  async function setActiveMap(name) {
    const body = { ...fleet, mapFile: name };
    if (!name) delete body.mapFile;
    fleet = await adminJson('/api/fleet', { method: 'PUT', body: JSON.stringify(body) });
    await loadZones();
    return fleet;
  }

  // ---------- 이동 구역 (활성 지도 fleet.mapFile 의 구역) ----------
  let zonesDoc = { map: '', zones: [] };
  async function loadZones() {
    zonesDoc = { map: fleet.mapFile || '', zones: [] };
    if (fleet.mapFile) {
      try {
        const r = await fetch(`/api/zones/${encodeURIComponent(fleet.mapFile)}`, { cache: 'no-store' });
        if (r.ok) zonesDoc = await r.json();
      } catch (e) { /* 구역 없이 동작 */ }
    }
    robots.forEach((r) => { r.zones = zonesDoc.zones.filter((z) => z.robots.includes('*') || z.robots.includes(r.id)); });
    return zonesDoc;
  }
  document.addEventListener('tb3:zone-block', (e) => toast(`${e.detail.name}: 이동 금지 구역이라 멈췄습니다`, 'warn', 2500));

  function robotUrl(id, page) {
    const u = new URL(page || location.pathname, location.href);
    u.searchParams.set('robot', id);
    return u.pathname + u.search;
  }

  // ---------- 진행 중인 액션 goal (비상정지 시 취소) ----------
  const activeGoals = new Set();
  function trackGoal(goal) {
    activeGoals.add(goal);
    goal.result.finally(() => activeGoals.delete(goal)).catch(() => {});
    return goal;
  }

  // 전체 비상정지: 등록된 모든 로봇에 0 속도 + 이 브라우저가 보낸 goal 모두 취소
  function estop() {
    activeGoals.forEach((g) => g.cancel());
    robots.forEach((r) => r.halt());
    // 경비 중이면 순찰 goal 은 서버의 patrol_node 가 보낸 것이라 위에서 취소되지 않음 → 모든 로봇 순찰 멈춤
    // (경비가 아니면 받는 노드가 없어 아무 일도 없음)
    ros.publish('/fleet/patrol_cmd', 'std_msgs/msg/String', { data: JSON.stringify({ cmd: 'pause', robot: '*' }) });
    // 이동 경로도 서버의 실행기가 보낸 goal → 토픽으로 바로 취소하고, 서버 DB 에도 멈춤으로 기록 (실행기가 꺼져 있어도)
    ros.publish('/fleet/route_cmd', 'std_msgs/msg/String', { data: JSON.stringify({ cmd: 'pause', robot: '*' }) });
    fetch('/api/assignments/pause-all', { method: 'POST' }).catch(() => {});
    const patrolling = document.querySelector('#mode-chip')?.dataset.mode === 'patrol';
    document.dispatchEvent(new CustomEvent('tb3:estop'));
    toast(`비상정지: 로봇 ${robots.length}대에 정지 명령을 보내고 진행 중인 goal 을 취소했습니다${patrolling ? ' (순찰도 멈춤 — 경비 화면에서 재개)' : ''} · 이동 경로도 멈춤 (경로 화면에서 이어서)`, 'danger');
  }

  // ---------- 상단바 ----------
  function setConn(state, text) {
    $$('[data-conn]').forEach((el) => { el.dataset.conn = state; });
    $$('[data-conn-text]').forEach((el) => { el.textContent = text; });
  }
  ros.on('connecting', () => setConn('wait', '연결 중…'));
  ros.on('connection', () => {
    setConn('ok', 'rosbridge 연결됨');
    robots.forEach((r) => { r.detectCmdVelType(); r.refreshServices(); });
  });
  ros.on('close', (was) => { setConn('bad', was ? '연결 끊김 · 재시도 중' : 'rosbridge 연결 실패'); $$('[data-latency]').forEach((el) => { el.textContent = ''; }); });

  async function pingLoop() {
    if (ros.connected) {
      const t0 = performance.now();
      try {
        await ros.callService('/rosapi/get_time', {}, { timeout: 3000 });
        const ms = Math.round(performance.now() - t0);
        $$('[data-latency]').forEach((el) => { el.textContent = ` · ${ms} ms`; });
      } catch (e) { $$('[data-latency]').forEach((el) => { el.textContent = ' · 응답 없음'; }); }
    }
    setTimeout(pingLoop, 3000);
  }

  function renderBattery() {
    if (!robot) return;
    const p = robot.batteryPercent();
    const stale = performance.now() - robot.battery.t > 15000;
    const low = robot.batteryLow();
    $$('[data-battery-text]').forEach((el) => { el.textContent = stale ? '배터리 정보 없음' : robot.batteryText(); });
    $$('[data-battery-pct]').forEach((el) => { el.textContent = stale ? '—%' : `${Number.isFinite(p) ? Math.round(p) : '—'}%`; });
    $$('[data-battery-fill]').forEach((el) => {
      el.setAttribute('width', String(Math.max(1, (Number.isFinite(p) && !stale ? p : 0) / 100 * Number(el.dataset.max || 15))));
      el.setAttribute('fill', low ? '#F87171' : '#2DD4BF');
    });
    $$('[data-battery]').forEach((el) => el.setAttribute('aria-label', `${robot.name} 배터리 ${robot.batteryText()}`));
  }

  function bindHeader() {
    $$('[data-estop]').forEach((b) => b.addEventListener('click', estop));
    $$('[data-conn]').forEach((el) => el.addEventListener('click', () => {
      const url = prompt('rosbridge WebSocket 주소', ros.url);
      if (url && url.trim()) { store.set('tb3.rosbridge', url.trim()); ros.connect(url.trim()); }
    }));
    $$('[data-ros-url]').forEach((el) => { el.textContent = ros.url; });
    // 로봇 선택 (<select data-robot-select>): 같은 페이지를 ?robot= 으로 다시 엶
    $$('[data-robot-select]').forEach((sel) => {
      sel.innerHTML = robots.map((r) => `<option value="${esc(r.id)}"${r === robot ? ' selected' : ''}>${esc(r.name)} · ${esc(r.label)}${r.ns ? ` (${esc(r.ns)})` : ''}</option>`).join('');
      sel.addEventListener('change', () => { location.href = robotUrl(sel.value); });
    });
    $$('[data-robot-name]').forEach((el) => { el.textContent = robot.name; });
    $$('[data-robot-color]').forEach((el) => { el.style.background = robot.color; });
    // 다른 페이지로 가는 링크에 현재 로봇 유지
    $$('a[data-keep-robot]').forEach((a) => { a.href = robotUrl(robot.id, a.getAttribute('href')); });
    window.addEventListener('keydown', (e) => { if (e.key === 'Escape') estop(); });
  }

  // ---------- 운영 모드 (운영 / 지도 만들기 / 경비) + 경보 배지 — 모든 페이지 상단 ----------
  const MODE_LABEL = { operate: '운영', mapping: '지도 만들기', patrol: '경비' };
  let modeState = { mode: 'operate' };
  let openAlerts = 0;
  function modeWidget() {
    const bar = $('.topbar') || $('header');
    if (!bar || $('#mode-chip')) return;
    const wrap = document.createElement('div');
    wrap.style.cssText = 'display: flex; gap: 8px; align-items: center';
    wrap.innerHTML = '<button class="mode-chip" id="mode-chip" title="운영 모드 바꾸기">모드 —</button>'
      + '<a class="alert-chip hidden" id="alert-chip" href="patrol.html" title="확인 안 한 경보"></a>';
    const spacer = bar.querySelector('.spacer');
    if (spacer) bar.insertBefore(wrap, spacer); else bar.appendChild(wrap);
    $('#mode-chip').addEventListener('click', openModeDialog);
  }
  function renderMode() {
    const c = $('#mode-chip');
    if (c) {
      // 경비 모드인데 감지 프로세스가 멈춰 있으면(서버가 다시 띄우는 중) 경고로 보여 줌
      const down = modeState.mode === 'patrol' && modeState.patrol && !modeState.patrol.running;
      c.textContent = `모드 · ${MODE_LABEL[modeState.mode] || modeState.mode}${down ? ' · 감지 멈춤' : ''}`;
      c.dataset.mode = modeState.mode;
      c.classList.toggle('down', !!down);
      c.title = down ? '경비 감지(patrol_node)가 멈췄습니다. 서버가 자동으로 다시 시작합니다. logs/patrol.log 확인' : '클릭하여 모드 변경';
    }
    const a = $('#alert-chip');
    if (a) { a.textContent = `확인 안 한 경보 ${openAlerts}`; a.classList.toggle('hidden', !openAlerts); }
    document.dispatchEvent(new CustomEvent('tb3:mode', { detail: modeState }));
  }
  async function pollMode() {
    try {
      modeState = await (await fetch('/api/mode', { cache: 'no-store' })).json();
      if (fleet.mapFile) {
        const r = await (await fetch(`/api/alerts?map=${encodeURIComponent(fleet.mapFile)}&status=open&limit=1`, { cache: 'no-store' })).json();
        openAlerts = (r.counts && r.counts.open) || 0;
      }
      renderMode();
    } catch (e) { /* serve.py 없이 열었을 때 */ }
  }
  // 경비 중 새 경보: 토스트 + 소리 + (허용했으면) 브라우저 알림
  function alertNotify(a) {
    const kind = a.kind === 'new' ? '새로 생긴 것' : '사라진 것';
    const who = (a.robots || []).map((id) => (robots.find((r) => r.id === id) || { name: id }).name).join(', ');
    toastAction(`경보 #${a.id} · ${kind} (${a.x.toFixed(1)}, ${a.y.toFixed(1)}) · ${who}`, '보기', () => { location.href = `patrol.html#alert-${a.id}`; }, 'danger', 8000);
    try {
      const ac = new (window.AudioContext || window.webkitAudioContext)();
      const o = ac.createOscillator(); const g = ac.createGain();
      o.frequency.value = 880; g.gain.value = 0.08; o.connect(g); g.connect(ac.destination); o.start(); o.stop(ac.currentTime + 0.25);
    } catch (e) { /* 소리 불가 */ }
    // 사용자가 화면을 한 번도 누르지 않았으면 브라우저가 막고 콘솔 오류를 남기므로 그때는 건너뜀
    if (navigator.vibrate && (!navigator.userActivation || navigator.userActivation.hasBeenActive)) navigator.vibrate([150, 80, 150]);
    if ('Notification' in window && Notification.permission === 'granted') {
      try { new Notification(`경보 #${a.id}: ${kind}`, { body: `${who} · (${a.x.toFixed(1)}, ${a.y.toFixed(1)})`, tag: `alert-${a.id}` }); } catch (e) { /* ignore */ }
    }
  }
  function watchAlerts() {
    ros.subscribe('/fleet/alerts', 'std_msgs/msg/String', (m) => {
      let ev;
      try { ev = JSON.parse(m.data); } catch (e) { return; }
      document.dispatchEvent(new CustomEvent('tb3:alert', { detail: ev }));
      if (ev.event === 'new') { openAlerts += 1; renderMode(); alertNotify(ev.alert); }
    }, { queue: 20 });
  }

  async function openModeDialog() {
    let dlg = $('#mode-dialog');
    if (!dlg) {
      dlg = document.createElement('dialog');
      dlg.id = 'mode-dialog';
      dlg.className = 'mode-dialog';
      document.body.appendChild(dlg);
    }
    const maps = await (await fetch('/api/maps', { cache: 'no-store' })).json().catch(() => []);
    let routes = 0;
    if (fleet.mapFile) {
      try { const p = await (await fetch(`/api/patrol/${encodeURIComponent(fleet.mapFile)}`)).json(); routes = Object.values(p.routes || {}).filter((r) => r.enabled && r.points.length).length; } catch (e) { /* ignore */ }
    }
    const online = robots.filter((r) => r.online).length;
    const chk = (ok, text, sub) => `<div class="check"><span class="${ok ? 'ok' : 'unk'}">${ok ? '✓' : '!'}</span><span>${esc(text)}${sub ? `<span class="muted" style="display: block; font-size: 11px">${esc(sub)}</span>` : ''}</span><span></span></div>`;
    const cur = modeState.mode;
    const opt = (m, title, desc) => `<label class="mode-opt${m === cur ? ' cur' : ''}"><input type="radio" name="mode" value="${m}" ${m === cur ? 'checked' : ''}><span><b>${title}</b>${m === cur ? ' <span class="badge">지금</span>' : ''}<br><span class="muted" style="font-size: 12px">${desc}</span></span></label>`;
    dlg.innerHTML = `<form method="dialog" class="col" style="gap: 12px">
      <div class="panel-head" style="justify-content: space-between"><h2 style="font-size: 17px">운영 모드 바꾸기</h2><button value="cancel" class="btn btn-icon" aria-label="닫기">✕</button></div>
      <div class="muted" style="font-size: 12px">지금: <b style="color: var(--text)">${MODE_LABEL[cur]}</b> · 로봇 ${robots.length}대 중 온라인 ${online}대</div>
      ${opt('operate', '운영', '사람이 목표를 주고 조종합니다. 자동 동작 없음.')}
      ${opt('mapping', '지도 만들기', '온라인 로봇이 함께 지도를 그립니다. 지도 만들기 화면에서 시작·저장합니다.')}
      ${opt('patrol', '경비', '로봇들이 순찰 경로를 돌며 기준 지도와 다른 곳을 찾아 관제실에 알립니다.')}
      <div class="col" style="gap: 6px; background: var(--bg); border-radius: 10px; padding: 10px" id="mode-checks">
        <div class="muted" style="font-size: 12px">경비 시작 전 확인</div>
        ${chk(!!fleet.mapFile, fleet.mapFile ? `기준 지도: ${fleet.mapFile}` : '기준(활성) 지도 없음', fleet.mapFile ? '' : '지도 만들기로 먼저 만드세요')}
        ${chk(routes > 0, `순찰 경로 ${routes}개`, routes ? '' : '경비 화면 > 순찰 경로에서 지정 (없으면 감지만 함)')}
        ${chk(online > 0, `온라인 로봇 ${online}대`, '로봇별 Nav2 와 위치 추정(초기 위치)이 필요합니다')}
        ${chk(maps.length > 0, `저장된 지도 ${maps.length}개`, '')}
      </div>
      <div style="display: flex; gap: 8px"><button value="cancel" class="btn" style="flex: 1 1 0; min-height: 44px">취소</button>
        <button value="ok" class="btn btn-primary" id="mode-ok" style="flex: 2 1 0; min-height: 44px">바꾸기</button></div>
      <div class="muted" style="font-size: 11px; text-align: center">바꾸면 진행 중인 지도 만들기·경비 프로세스는 정지됩니다</div>
    </form>`;
    dlg.showModal();
    dlg.onclose = async () => {
      if (dlg.returnValue !== 'ok') return;
      const m = (dlg.querySelector('input[name=mode]:checked') || {}).value;
      if (!m || m === cur) return;
      try {
        modeState = await adminJson('/api/mode', { method: 'POST', body: JSON.stringify({ mode: m }) });
        renderMode();
        toast(`모드: ${MODE_LABEL[m]}`, 'ok');
        if (m === 'mapping' && !location.pathname.endsWith('mapping.html')) location.href = 'mapping.html';
        if (m === 'patrol' && !location.pathname.endsWith('patrol.html')) location.href = 'patrol.html';
      } catch (e) { toast(`모드를 바꾸지 못했습니다: ${e.message}`, 'danger', 6000); }
    };
  }

  const App = {
    ros, store, $, $$, esc, fmt, toast, toastAction, estop, trackGoal, robotUrl, control,
    get mode() { return modeState; }, openModeDialog, pollMode,
    get fleet() { return fleet; },
    get zones() { return zonesDoc; },
    loadZones, adminFetch, adminJson, setActiveMap,
    get robots() { return robots; },
    get robot() { return robot; },
    // 이전 단일 로봇 코드 호환
    cmdVel: (lin, ang) => robot && robot.cmdVel(lin, ang),
    get LIMITS() { return robot ? robot.limits : { lin: 0.22, ang: 2.84 }; },
    get cmdVelType() { return robot ? robot.cmdVelType : '—'; },
  };
  App.ready = new Promise((resolve) => {
    document.addEventListener('DOMContentLoaded', async () => {
      await loadFleet();
      await loadZones();
      bindHeader();
      if (!document.body.dataset.noMode) { modeWidget(); pollMode(); setInterval(pollMode, 3000); watchAlerts(); }
      // 현재 로봇의 기본 상태 구독 (배터리 표시, 구역 판정용 지도 위치). 플릿 페이지는 직접 모든 로봇을 시작함
      if (!document.body.dataset.fleet) robot.start({ tf: true });
      robot.onChange(renderBattery);
      setInterval(renderBattery, 5000);
      ros.connect();
      pingLoop();
      resolve(App);
    });
  });
  window.App = App;
})();
