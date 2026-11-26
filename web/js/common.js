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

  // ---------- LiDAR 안전 구역 (가까운 물체 → 정지·서행). 설정: /api/safety (DB data/fleet.db, safety.py) ----------
  // 플릿 기본값(safety_settings) + 로봇별 덮어쓰기(robot_safety). 로봇마다 '적용 값' = 덮어쓰기가 있으면 그것, 없으면 기본값.
  // 웹 수동 조종은 Robot.safetyGate 가 r.safety 로, Nav2 주행은 collision_monitor 의 LidarStop·LidarSlow 가 같은 값으로 막음
  const SAFETY_KEYS = ['stop_dist', 'slow_dist', 'slow_speed'];
  let safety = { enabled: true, stop_dist: 0.20, slow_dist: 0.50, slow_speed: 0.05, since: 0 };   // 플릿 기본값
  let safetyRobots = {};           // robot id -> 적용 값 {enabled, stop_dist, slow_dist, slow_speed, source: 'robot'|'fleet'}
  let safetyChanges = [];          // 최근 변경 이력 (DB safety_changes)
  const cm = (s) => `${(s * 100).toFixed(0)} cm`;
  const sameVals = (a, b) => a.enabled === b.enabled && SAFETY_KEYS.every((k) => Math.abs(a[k] - b[k]) < 1e-6);
  const effOf = (r) => safetyRobots[r.id] || { ...safety, source: 'fleet' };
  function setSafety(snap) {
    const { changes, robots: per, ...fleetVals } = snap;
    safety = fleetVals;
    if (per) safetyRobots = per;
    if (changes) safetyChanges = changes;
    robots.forEach((r) => { r.safety = effOf(r); });
    renderSafety();
  }
  async function loadSafety() {
    try {
      const r = await fetch('/api/safety', { cache: 'no-store' });
      if (r.ok) setSafety(await r.json());
    } catch (e) { /* serve.py 없이 열었을 때: 기본값(켜짐)으로 */ }
  }
  function renderSafety() {
    const c = $('#safety-chip');
    if (!c) return;
    const short = !!c.closest('.m-head');   // 휴대폰 상단은 좁아서 짧게
    const effs = robots.map(effOf);
    const nOff = effs.filter((e) => !e.enabled).length;
    const nOwn = effs.filter((e) => e.source === 'robot').length;
    const vals = (e) => (e.enabled ? `${cm(e.stop_dist)} 정지 / ${cm(e.slow_dist)} 서행` : '꺼짐');
    if (!document.body.dataset.fleet && robot) {
      // 한 로봇 화면(대시보드·모바일): 이 로봇에 적용되는 값
      const e = effOf(robot);
      c.textContent = short ? `LiDAR · ${e.enabled ? '켜짐' : '꺼짐'}` : `LiDAR 안전 · ${vals(e)}${e.source === 'robot' ? ' (로봇별)' : ''}`;
      c.dataset.on = e.enabled ? '1' : '0';
      c.title = `${robot.name}: ${e.enabled ? `로봇 중심에서 ${cm(e.stop_dist)} 안 물체 → 정지, ${cm(e.slow_dist)} 안 → ${e.slow_speed} m/s 서행` : 'LiDAR 안전 구역 꺼짐'} (${e.source === 'robot' ? '로봇별 설정' : '기본값'}). 클릭하여 바꾸기`;
      return;
    }
    // 여러 로봇 화면: 기본값 + 로봇별 설정 수 + 꺼진 로봇 수
    const extra = [nOwn ? `로봇별 ${nOwn}` : '', nOff && safety.enabled ? `${nOff}대 꺼짐` : ''].filter(Boolean).join(' · ');
    c.textContent = short ? `LiDAR · ${nOff === 0 ? '켜짐' : nOff === effs.length ? '꺼짐' : '일부 꺼짐'}`
      : `LiDAR 안전 · ${vals(safety)}${extra ? ` · ${extra}` : ''}`;
    c.dataset.on = nOff === 0 ? '1' : nOff === effs.length ? '0' : 'part';
    c.title = `${robots.map((r) => `${r.name}: ${vals(effOf(r))}${effOf(r).source === 'robot' ? ' (로봇별)' : ''}`).join('\n')}\n클릭하여 바꾸기`;
  }
  const safetyEvText = {
    stop: (d) => `LiDAR 안전: ${cm(d.d)} 앞에 물체가 있어 멈췄습니다 (반대쪽으로는 움직일 수 있음)`,
    slow: (d) => `LiDAR 안전: ${cm(d.d)} 앞에 물체 → ${d.robot.safety.slow_speed} m/s 서행`,
    noscan: (d) => `LiDAR 안전: 스캔이 없어 ${d.robot.safety.slow_speed} m/s 서행으로 제한합니다`,
  };
  document.addEventListener('tb3:safety-limit', (e) => {
    const d = e.detail;
    const f = safetyEvText[d.state];
    if (f) toast(`${d.robot.name}: ${f(d)}`, d.state === 'stop' ? 'danger' : 'warn', 2500);
  });

  // 실행 중인 Nav2 collision_monitor 에 켜기·끄기를 바로 반영 (거리·속도는 Nav2 를 다시 띄워야 반영됨)
  const CM_PARAMS = ['LidarStop.enabled', 'LidarStop.radius', 'LidarSlow.enabled', 'LidarSlow.radius', 'LidarSlow.linear_limit'];
  async function nav2Safety(r) {
    try {
      const res = await ros.callService(r.topic('collision_monitor/get_parameters'), { names: CM_PARAMS },
        { type: 'rcl_interfaces/srv/GetParameters', timeout: 3000 });
      const v = (res.values || []).map((p) => (p.type === 1 ? p.bool_value : p.type === 3 ? p.double_value : null));
      if (v.length !== CM_PARAMS.length || v.some((x) => x === null)) return { state: 'old' };   // LidarStop 없는 파라미터로 뜬 Nav2
      return { state: 'ok', enabled: v[0] && v[2], stop_dist: v[1], slow_dist: v[3], slow_speed: v[4] };
    } catch (e) { return { state: 'absent' }; }
  }
  // 로봇마다 그 로봇의 적용 값으로 켜기·끄기. 반환: robot id -> 'ok' | 'old' | 'absent'
  async function applySafetyToNav2(list = robots) {
    const bool = (b) => ({ type: 1, bool_value: b });
    const out = {};
    await Promise.all(list.map(async (r) => {
      const en = effOf(r).enabled;
      try {
        const res = await ros.callService(r.topic('collision_monitor/set_parameters'),
          { parameters: [{ name: 'LidarStop.enabled', value: bool(en) }, { name: 'LidarSlow.enabled', value: bool(en) }] },
          { type: 'rcl_interfaces/srv/SetParameters', timeout: 3000 });
        out[r.id] = (res.results || []).length === 2 && res.results.every((x) => x.successful) ? 'ok' : 'old';
      } catch (e) { out[r.id] = 'absent'; }
    }));
    return out;
  }

  async function openSafetyDialog() {
    let dlg = $('#safety-dialog');
    if (!dlg) {
      dlg = document.createElement('dialog');
      dlg.id = 'safety-dialog';
      dlg.className = 'mode-dialog safety-dialog';
      document.body.appendChild(dlg);
    }
    await loadSafety();
    const s = safety;
    const before = Object.fromEntries(robots.map((r) => [r.id, effOf(r)]));
    // 켜기 + 정지·서행 거리(cm) + 서행 속도 입력 한 벌 (pre: id 앞부분)
    const fields = (v) => `<div class="sf-fields">
        <label class="sf-on"><input type="checkbox" data-k="enabled" ${v.enabled ? 'checked' : ''}> 켜기</label>
        <label>정지 <input type="number" data-k="stop_dist" min="5" max="100" step="1" value="${(v.stop_dist * 100).toFixed(0)}"> cm</label>
        <label>서행 <input type="number" data-k="slow_dist" min="10" max="200" step="1" value="${(v.slow_dist * 100).toFixed(0)}"> cm</label>
        <label><input type="number" data-k="slow_speed" min="0.01" max="0.22" step="0.01" value="${v.slow_speed}"> m/s</label>
      </div>`;
    const read = (box) => {
      const q = (k) => box.querySelector(`[data-k="${k}"]`);
      return { enabled: q('enabled').checked, stop_dist: Number(q('stop_dist').value) / 100,
        slow_dist: Number(q('slow_dist').value) / 100, slow_speed: Number(q('slow_speed').value) };
    };
    const nameOf = (id) => (id ? (robots.find((r) => r.id === id) || { name: id }).name : '기본값');
    dlg.innerHTML = `<form method="dialog" class="col" style="gap: 12px">
      <div class="panel-head" style="justify-content: space-between"><h2 style="font-size: 17px">LiDAR 안전 구역</h2><button value="cancel" class="btn btn-icon" aria-label="닫기">✕</button></div>
      <div class="muted" style="font-size: 12px">로봇 중심에서 정지 거리 안에 물체가 보이면 정지, 서행 거리 안이면 서행 속도로 줄입니다.</div>
      <div class="sf-box" id="sf-fleet"><div class="sf-title"><b>기본값</b><span class="muted">따로 설정하지 않은 로봇에 적용</span></div>${fields(s)}</div>
      <div class="col" style="gap: 8px">
        <div class="muted" style="font-size: 12px">로봇별 (Nav2 상태는 지금 실행 중인 collision_monitor 의 값)</div>
        ${robots.map((r) => {
          const e = effOf(r);
          return `<div class="sf-box" data-robot="${esc(r.id)}">
            <div class="sf-title"><span class="swatch" style="background: ${esc(r.color)}"></span><b>${esc(r.name)}</b>
              <select data-mode aria-label="${esc(r.name)} 설정 방식"><option value="fleet"${e.source === 'fleet' ? ' selected' : ''}>기본값 따름</option><option value="robot"${e.source === 'robot' ? ' selected' : ''}>따로 설정</option></select></div>
            <div data-own${e.source === 'robot' ? '' : ' hidden'}>${fields(e)}</div>
            <div class="sf-nav2 muted" data-nav2>Nav2 확인 중…</div>
          </div>`;
        }).join('') || '<span class="muted">등록된 로봇 없음</span>'}
      </div>
      <details style="font-size: 12px"${safetyChanges.length ? '' : ' hidden'}>
        <summary class="muted" style="cursor: pointer">최근 변경 ${safetyChanges.length}건</summary>
        <div class="col" style="gap: 3px; margin-top: 6px">${safetyChanges.slice(0, 10).map((c) => `<div style="display: flex; gap: 8px; flex-wrap: wrap">
          <span class="muted mono" style="min-width: 92px">${esc(new Date(c.t * 1000).toLocaleString([], { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }))}</span>
          <b>${esc(nameOf(c.robot))}</b>
          ${c.action === 'clear' ? '<span class="muted">기본값으로</span>' : ''}
          <span class="sf-${c.enabled ? 'ok' : 'warn'}">${c.enabled ? '켜짐' : '꺼짐'}</span>
          <span>${cm(c.stop_dist)} / ${cm(c.slow_dist)} · ${c.slow_speed} m/s</span><span class="muted">${esc(c.by || '')}</span></div>`).join('')}</div>
      </details>
      <div class="muted" style="font-size: 11px; line-height: 1.5">
        · 켜기·끄기는 실행 중인 Nav2 와 웹 조종에 바로 반영됩니다. 거리·속도를 바꾸면 웹 조종은 바로, Nav2 는 그 로봇의 Nav2 를 다시 실행할 때 반영됩니다.<br>
        · 웹 조종은 진행 방향 쪽 물체만 보고, 멈춘 뒤에도 반대쪽으로 빠져나가거나 제자리 회전할 수 있습니다. Nav2 는 어느 방향이든 정지합니다.<br>
        · 끄기와 값 바꾸기는 관리자 토큰이 필요합니다 (켜기만 하면 필요 없음). 다른 teleop 이 보내는 cmd_vel 은 막지 못합니다.
      </div>
      <div style="display: flex; gap: 8px"><button value="cancel" class="btn" style="flex: 1 1 0; min-height: 44px">취소</button>
        <button value="ok" class="btn btn-primary" style="flex: 2 1 0; min-height: 44px">저장하고 적용</button></div>
    </form>`;
    dlg.querySelectorAll('[data-robot]').forEach((box) => {
      box.querySelector('[data-mode]').addEventListener('change', (ev) => { box.querySelector('[data-own]').hidden = ev.target.value !== 'robot'; });
    });
    dlg.showModal();
    // 로봇별 Nav2 에 지금 들어가 있는 값 (그 로봇의 적용 값과 다르면 Nav2 다시 실행 필요)
    robots.forEach(async (r) => {
      const n = await nav2Safety(r);
      const el = dlg.querySelector(`[data-robot="${CSS.escape(r.id)}"] [data-nav2]`);
      if (!el) return;
      const e = before[r.id];
      let txt; let cls = 'muted';
      if (n.state === 'absent') txt = 'Nav2 없음 (다음 실행 때 이 로봇의 값으로 뜸)';
      else if (n.state === 'old') { txt = 'Nav2: 안전 구역 없는 파라미터로 실행됨 → 다시 실행 필요'; cls = 'warn'; }
      else {
        const same = SAFETY_KEYS.every((k) => Math.abs(n[k] - e[k]) < 1e-3);
        txt = `Nav2: ${n.enabled ? '켜짐' : '꺼짐'} · ${cm(n.stop_dist)} / ${cm(n.slow_dist)} · ${n.slow_speed} m/s${same ? '' : ' → 값이 달라 다시 실행 필요'}`;
        cls = same && n.enabled === e.enabled ? 'ok' : 'warn';
      }
      el.className = `sf-nav2 sf-${cls}`;
      el.textContent = txt;
    });
    dlg.onclose = async () => {
      if (dlg.returnValue !== 'ok') return;
      const by = control.label;   // 이력에 남길 바꾼 화면 (예: 'PC ab12')
      const fleetNew = read(dlg.querySelector('#sf-fleet'));
      // 보낼 요청: 기본값이 바뀌었으면 PUT, 로봇마다 따로 설정 저장(PUT)·기본값으로 되돌리기(DELETE)
      const reqs = [];
      if (!sameVals(fleetNew, s)) reqs.push(['기본값', '/api/safety', 'PUT', fleetNew]);
      dlg.querySelectorAll('[data-robot]').forEach((box) => {
        const id = box.dataset.robot;
        const mode = box.querySelector('[data-mode]').value;
        const e = before[id];
        const url = `/api/safety/robots/${encodeURIComponent(id)}`;
        if (mode === 'robot') {
          const v = read(box.querySelector('[data-own]'));
          if (e.source !== 'robot' || !sameVals(v, e)) reqs.push([nameOf(id), url, 'PUT', v]);
        } else if (e.source === 'robot') reqs.push([nameOf(id), `${url}?by=${encodeURIComponent(by)}`, 'DELETE', null]);
      });
      if (!reqs.length) { toast('바뀐 것이 없습니다', 'info', 2500); return; }
      const failed = [];
      for (const [label, url, method, body] of reqs) {
        try {
          await adminJson(url, { method, body: body ? JSON.stringify({ ...body, by }) : undefined });
        } catch (err) { failed.push(`${label}: ${err.message}`); if (/토큰/.test(err.message)) break; }
      }
      await loadSafety();
      // 적용 값이 바뀐 로봇만 Nav2 에 켜기·끄기를 보냄
      const changed = robots.filter((r) => !sameVals(effOf(r), before[r.id]));
      const res = await applySafetyToNav2(changed);
      const n = (st) => Object.values(res).filter((x) => x === st).length;
      const distChanged = changed.filter((r) => SAFETY_KEYS.some((k) => Math.abs(effOf(r)[k] - before[r.id][k]) > 1e-6));
      const parts = [`LiDAR 안전 저장 (적용 값이 바뀐 로봇 ${changed.length}대)`];
      if (n('ok')) parts.push(`Nav2 ${n('ok')}대에 켜기·끄기 바로 반영`);
      if (n('absent')) parts.push(`${n('absent')}대는 Nav2 없음`);
      if (n('old')) parts.push(`${n('old')}대는 Nav2 다시 실행 필요`);
      if (distChanged.some((r) => res[r.id] === 'ok')) parts.push(`거리·속도는 Nav2 를 다시 실행해야 반영 (${distChanged.filter((r) => res[r.id] === 'ok').map((r) => r.name).join(', ')})`);
      if (failed.length) toast(`저장하지 못한 것: ${failed.join(' / ')}`, 'danger', 8000);
      if (failed.length < reqs.length) toast(parts.join(' · '), 'ok', 7000);
    };
  }

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
    // 경비 역할의 순회 goal 은 서버의 경비 노드(patrol_node)가, 워커의 이동 경로 goal 은 실행기가 보낸 것이라
    // 위에서 취소되지 않음 → 토픽으로 바로 취소하고, 서버 DB 에도 멈춤으로 기록 (노드가 꺼져 있어도)
    ros.publish('/fleet/patrol_cmd', 'std_msgs/msg/String', { data: JSON.stringify({ cmd: 'pause', robot: '*' }) });
    ros.publish('/fleet/route_cmd', 'std_msgs/msg/String', { data: JSON.stringify({ cmd: 'pause', robot: '*' }) });
    ros.publish('/fleet/obstacle_cmd', 'std_msgs/msg/String', { data: JSON.stringify({ cmd: 'abort' }) });   // 장애물 처리 중이면 중단
    fetch('/api/assignments/pause-all', { method: 'POST' }).catch(() => {});
    document.dispatchEvent(new CustomEvent('tb3:estop'));
    toast(`비상정지: 로봇 ${robots.length}대에 정지 명령을 보내고 진행 중인 goal 을 취소했습니다 · 워커의 이동 경로와 경비도 모두 멈춤 (역할 화면에서 이어서·재개)`, 'danger');
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

  // ---------- 운영 모드 (운영 / 지도 만들기) + 경보 배지 + 경비 장애물 대기 알림 — 모든 페이지 상단 ----------
  // 운영 모드에서는 로봇마다 역할(워커·경비, roles.html)대로 움직이고, 지도 만들기 모드에서는 역할이 모두 멈춤
  const MODE_LABEL = { operate: '운영', mapping: '지도 만들기', obstacle: '장애물 처리' };
  let modeState = { mode: 'operate' };
  let openAlerts = 0;
  function modeWidget() {
    const bar = $('.topbar') || $('header');
    if (!bar || $('#mode-chip')) return;
    const wrap = document.createElement('div');
    wrap.style.cssText = 'display: flex; gap: 8px; align-items: center';
    wrap.innerHTML = '<button class="mode-chip" id="mode-chip" title="운영 모드 바꾸기">모드 —</button>'
      + '<button class="mode-chip safety-chip" id="safety-chip" data-on="1">LiDAR 안전 —</button>'
      + '<a class="alert-chip hidden" id="alert-chip" href="patrol.html" title="확인 안 한 경보"></a>'
      + '<a class="alert-chip alarm-chip hidden" id="alarm-chip" href="roles.html" title="지도에 없던 장애물 앞에서 멈춰 기다리는 경비 로봇"></a>';
    // 휴대폰 상단(.m-head)은 좁아서 칩들이 다음 줄로 내려갈 수 있게
    if (bar.matches('.m-head')) { bar.style.flexWrap = 'wrap'; wrap.style.flexWrap = 'wrap'; }
    const spacer = bar.querySelector('.spacer');
    if (spacer) bar.insertBefore(wrap, spacer); else bar.appendChild(wrap);
    $('#mode-chip').addEventListener('click', openModeDialog);
    $('#safety-chip').addEventListener('click', openSafetyDialog);
    renderSafety();
  }
  function renderMode() {
    const c = $('#mode-chip');
    if (c) {
      // 경비 중인 로봇이 있는데 경비 노드가 멈춰 있으면(서버가 다시 띄우는 중) 경고로 보여 줌
      const gd = modeState.guard || {};
      const down = modeState.mode === 'operate' && gd.active > 0 && !gd.running;
      c.textContent = `모드 · ${MODE_LABEL[modeState.mode] || modeState.mode}${down ? ' · 경비 노드 멈춤' : ''}`;
      c.dataset.mode = modeState.mode;
      c.classList.toggle('down', !!down);
      c.title = down ? '경비 노드(patrol_node)가 멈췄습니다. 서버가 자동으로 다시 시작합니다. logs/patrol.log 확인' : '클릭하여 모드 변경';
    }
    const a = $('#alert-chip');
    if (a) { a.textContent = `확인 안 한 경보 ${openAlerts}`; a.classList.toggle('hidden', !openAlerts); }
    document.dispatchEvent(new CustomEvent('tb3:mode', { detail: modeState }));
  }
  // ---------- 장애물 처리 (운영 중 경비가 새 장애물을 찾고 출구가 있으면 서버가 자동으로) — 모든 화면 위 배너 ----------
  let obstacleState = { run: null };
  let obstacleTimer = null;
  let obstacleBeepAt = 0;
  let obstacleSeenEnd = 0;          // 끝난 결과를 보여 준 처리 번호 (한 번만)
  async function loadObstacle() {
    try { obstacleState = await (await fetch('/api/obstacle', { cache: 'no-store' })).json(); } catch (e) { return; }
    renderObstacle();
    document.dispatchEvent(new CustomEvent('tb3:obstacle', { detail: obstacleState }));
  }
  function watchObstacle(on) {
    if (on && !obstacleTimer) { loadObstacle(); obstacleTimer = setInterval(loadObstacle, 1000); }
    if (!on && obstacleTimer) { clearInterval(obstacleTimer); obstacleTimer = null; loadObstacle(); }
  }
  function renderObstacle() {
    const run = obstacleState.run;
    let b = $('#obstacle-banner');
    const active = run && ['gathering', 'signal1', 'pushing', 'signal2'].includes(run.state);
    const fresh = run && !active && run.finished_at && Date.now() / 1000 - run.finished_at < 60 && obstacleSeenEnd !== run.id;
    if (!active && !fresh) { if (b) b.remove(); return; }
    if (!b) {
      b = document.createElement('div');
      b.id = 'obstacle-banner';
      b.setAttribute('role', 'status');
      const bar = $('.topbar') || $('header');
      if (bar) bar.insertAdjacentElement('afterend', b); else document.body.prepend(b);
      b.addEventListener('click', async (e) => {
        if (e.target.closest('[data-ob-abort]')) {
          if (!confirm('장애물 처리를 중단할까요? 모든 로봇이 그 자리에 멈추고, 역할(이동 경로·경비)은 멈춘 채로 운영 모드로 돌아갑니다.')) return;
          ros.publish('/fleet/obstacle_cmd', 'std_msgs/msg/String', { data: JSON.stringify({ cmd: 'abort' }) });
          try { await fetch('/api/obstacle/abort', { method: 'POST' }); } catch (err) { /* 비상정지로 */ }
          loadObstacle();
        }
        if (e.target.closest('[data-ob-close]')) { obstacleSeenEnd = obstacleState.run && obstacleState.run.id; renderObstacle(); }
      });
    }
    const team = Object.keys((run.plan && run.plan.robots) || {}).map((id) => (robots.find((r) => r.id === id) || { name: id }).name);
    const cls = active ? 'active' : run.state === 'done' ? 'done' : 'bad';
    b.className = `obstacle-banner ${cls}`;
    const html = `<b>장애물 처리 #${run.id} · ${esc(run.label)}</b>
      ${run.state === 'pushing' ? `<span class="ob-bar" aria-label="진행 ${Math.round(run.progress * 100)}%"><span style="width: ${Math.round(run.progress * 100)}%"></span></span><span class="mono">${Math.round(run.progress * 100)}%</span>` : ''}
      <span class="muted">${esc(run.message || '')}</span>
      ${team.length ? `<span class="muted">· ${esc(team.join(', '))}</span>` : ''}
      ${run.alert_id ? `<a href="patrol.html#alert-${run.alert_id}">경보 #${run.alert_id}</a>` : ''}
      <span class="spacer"></span>
      ${active ? '<button class="btn btn-sm btn-danger-outline" data-ob-abort>중단</button>' : '<button class="btn btn-sm" data-ob-close aria-label="닫기">닫기</button>'}`;
    if (b.dataset.html !== html) { b.innerHTML = html; b.dataset.html = html; }
    // 모여서·도착해서 소리 내는 동안 관제 화면도 1초마다 짧게 (Pinky 처럼 소리를 못 내는 로봇 대신)
    if (active && (run.state === 'signal1' || run.state === 'signal2') && Date.now() - obstacleBeepAt > 950) {
      obstacleBeepAt = Date.now();
      try {
        const ac = new (window.AudioContext || window.webkitAudioContext)();
        const o = ac.createOscillator(); const g = ac.createGain();
        o.frequency.value = run.state === 'signal1' ? 880 : 1175; g.gain.value = 0.08; o.connect(g); g.connect(ac.destination);
        o.start(); o.stop(ac.currentTime + 0.2); setTimeout(() => ac.close(), 500);
      } catch (e) { /* 소리 불가 */ }
    }
  }

  // 지도에 출구와 진행 중인 장애물 처리(장애물, 출구까지 밀 길, 로봇 자리, 추정 위치)를 그림 (플릿·경비 화면)
  function attachObstacle(view) {
    const apply = (d) => {
      view.state.exit = d && d.exit ? { x: d.exit.x, y: d.exit.y, radius: d.exit.radius } : null;
      const run = d && d.run;
      const active = run && ['gathering', 'signal1', 'pushing', 'signal2'].includes(run.state);
      view.state.obstacle = active ? {
        ...run.obstacle, ...((run.plan || {}).obstacle || {}), path: (run.plan || {}).path,   // 로봇들이 마지막으로 잰 장애물 est: (run.plan || {}).est, label: `장애물 · ${run.label}`,
        slots: Object.entries((run.plan || {}).robots || {}).map(([id, sl]) => ({ ...sl, color: (robots.find((r) => r.id === id) || {}).color })),
      } : null;
      view.invalidate();
    };
    document.addEventListener('tb3:obstacle', (e) => apply(e.detail));
    apply(obstacleState);
  }

  async function pollMode() {
    try {
      modeState = await (await fetch('/api/mode', { cache: 'no-store' })).json();
      watchObstacle(modeState.mode === 'obstacle');
      if (fleet.mapFile) {
        const r = await (await fetch(`/api/alerts?map=${encodeURIComponent(fleet.mapFile)}&status=open&limit=1`, { cache: 'no-store' })).json();
        openAlerts = (r.counts && r.counts.open) || 0;
      }
      renderMode();
    } catch (e) { /* serve.py 없이 열었을 때 */ }
    loadSafety();   // 다른 브라우저가 LiDAR 안전 구역을 바꿨을 수 있음
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
    // 경비 로봇이 장애물 앞에서 기다리는 동안: 상단 배지 + 몇 초마다 경고음 (Pinky 처럼 소리를 못 내는 로봇 대신 관제실에서)
    ros.subscribe('/fleet/patrol_status', 'std_msgs/msg/String', (m) => {
      try { guardLive = JSON.parse(m.data); guardLive.at = Date.now(); } catch (e) { /* ignore */ }
    }, { throttle: 1000 });
    setInterval(renderAlarm, 1000);
  }
  let guardLive = { robots: {} };

  // ---------- 로봇이 앞으로 갈 길 (지도에 그림) ----------
  //   plan  = 지금 가는 길: Nav2 계획 경로에서 남은 부분 (Robot.remainingPlan, 멈추면 사라짐)
  //   stops = 앞으로 들를 지점: 워커는 이동 경로의 이번 바퀴 남은 지점, 경비는 순회의 남은 지점
  let workerStops = {};            // robot id -> [[x, y], ...]
  let stopsTimer = null;
  async function loadWorkerStops() {
    if (!fleet.mapFile) { workerStops = {}; return; }
    try {
      const [a, rt] = await Promise.all([
        fetch('/api/assignments', { cache: 'no-store' }).then((r) => r.json()),
        fetch(`/api/routes?map=${encodeURIComponent(fleet.mapFile)}`, { cache: 'no-store' }).then((r) => r.json())]);
      const byRoute = new Map((rt.routes || []).map((r) => [r.id, r]));
      const out = {};
      for (const x of a.assignments || []) {
        const route = byRoute.get(x.route_id);
        if (x.state !== 'running' || !route) continue;
        out[x.robot_id] = route.points.slice(x.waypoint).map((p) => [p.x, p.y]);   // waypoint = 지금 가는 지점 (0부터)
      }
      workerStops = out;
    } catch (e) { /* 다음에 */ }
  }
  // 지도를 그리는 화면에서 한 번 부름: 로봇마다 Nav2 계획 구독 + 워커 경로 진행 읽기(2초)
  function watchPaths() {
    robots.forEach((r) => r.watchPlan());
    if (stopsTimer) return;
    loadWorkerStops();
    stopsTimer = setInterval(loadWorkerStops, 2000);
  }
  function stopsFor(r) {
    if (workerStops[r.id]) return workerStops[r.id];
    const g = guardLive.at && Date.now() - guardLive.at < 4000 ? (guardLive.robots || {})[r.id] : null;
    if (g && g.ctrl === 'running' && g.plan && g.plan.length) return g.plan.slice(Math.min(g.waypoint, g.plan.length - 1));
    return [];
  }
  function pathsFor(r) {
    const plan = r.remainingPlan();
    let stops = stopsFor(r);
    // 진행(지점 번호)은 1~2초 늦게 올 수 있어, 지금 계획이 끝나는 곳(= 지금 가는 지점)에 가장 가까운 지점부터 보여 줌
    if (plan && stops.length > 1) {
      const [ex, ey] = plan[plan.length - 1];
      let best = -1, bd = 0.5 * 0.5;
      stops.forEach(([x, y], i) => { const d = (x - ex) ** 2 + (y - ey) ** 2; if (d < bd) { bd = d; best = i; } });
      if (best > 0) stops = stops.slice(best);
    }
    return { plan, stops };
  }

  let alarmBeepAt = 0;
  const alarmSeen = new Set();
  function renderAlarm() {
    const fresh = guardLive.at && Date.now() - guardLive.at < 4000;
    const waiting = fresh ? Object.entries(guardLive.robots || {}).filter(([, s]) => s.ctrl === 'alarm') : [];
    const chip = $('#alarm-chip');
    if (chip) {
      chip.classList.toggle('hidden', !waiting.length);
      if (waiting.length) {
        const names = waiting.map(([id]) => (robots.find((r) => r.id === id) || { name: id }).name);
        chip.textContent = `장애물 대기 ${waiting.length}대 · ${names.join(', ')}`;
        const aid = waiting[0][1].alarm_alert_id;
        chip.href = aid ? `patrol.html#alert-${aid}` : 'roles.html';
      }
    }
    for (const [id, s] of waiting) {
      const key = `${id}:${s.alarm_alert_id}`;
      if (alarmSeen.has(key)) continue;
      alarmSeen.add(key);
      const name = (robots.find((r) => r.id === id) || { name: id }).name;
      toastAction(`${name}: 지도에 없던 장애물 앞에 멈춰 기다립니다 (경보 #${s.alarm_alert_id}). 처리하면 다시 경비합니다`, '경보 보기',
        () => { location.href = `patrol.html#alert-${s.alarm_alert_id}`; }, 'danger', 10000);
    }
    const interval = 5000;
    if (waiting.length && Date.now() - alarmBeepAt >= interval) {
      alarmBeepAt = Date.now();
      try {   // 두 음 (높음-낮음)
        const ac = new (window.AudioContext || window.webkitAudioContext)();
        [[988, 0], [740, 0.22]].forEach(([f, t]) => {
          const o = ac.createOscillator(); const g = ac.createGain();
          o.frequency.value = f; g.gain.value = 0.09; o.connect(g); g.connect(ac.destination);
          o.start(ac.currentTime + t); o.stop(ac.currentTime + t + 0.18);
        });
        setTimeout(() => ac.close(), 800);
      } catch (e) { /* 소리 불가 (화면을 한 번 누르기 전에는 브라우저가 막을 수 있음) */ }
    }
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
    let roleRows = [];
    try { roleRows = (await (await fetch('/api/roles', { cache: 'no-store' })).json()).roles || []; } catch (e) { /* ignore */ }
    const nWorker = roleRows.filter((r) => r.role === 'worker').length;
    const nGuard = roleRows.filter((r) => r.role === 'guard').length;
    const online = robots.filter((r) => r.online).length;
    const chk = (ok, text, sub) => `<div class="check"><span class="${ok ? 'ok' : 'unk'}">${ok ? '✓' : '!'}</span><span>${esc(text)}${sub ? `<span class="muted" style="display: block; font-size: 11px">${esc(sub)}</span>` : ''}</span><span></span></div>`;
    const cur = modeState.mode;
    if (cur === 'obstacle') {   // 장애물 처리 중에는 모드를 바꿀 수 없음 (배너의 중단으로)
      toast('장애물 처리 중입니다. 모드를 바꾸려면 위 배너에서 먼저 중단하세요', 'warn', 5000);
      return;
    }
    const opt = (m, title, desc) => `<label class="mode-opt${m === cur ? ' cur' : ''}"><input type="radio" name="mode" value="${m}" ${m === cur ? 'checked' : ''}><span><b>${title}</b>${m === cur ? ' <span class="badge">지금</span>' : ''}<br><span class="muted" style="font-size: 12px">${desc}</span></span></label>`;
    dlg.innerHTML = `<form method="dialog" class="col" style="gap: 12px">
      <div class="panel-head" style="justify-content: space-between"><h2 style="font-size: 17px">운영 모드 바꾸기</h2><button value="cancel" class="btn btn-icon" aria-label="닫기">✕</button></div>
      <div class="muted" style="font-size: 12px">지금: <b style="color: var(--text)">${MODE_LABEL[cur]}</b> · 로봇 ${robots.length}대 중 온라인 ${online}대</div>
      ${opt('operate', '운영', '로봇마다 역할대로 움직입니다: 워커는 이동 경로 반복, 경비는 지도 전체를 돌며 새 장애물 감시. 역할 없는 로봇은 수동 조종.')}
      ${opt('mapping', '지도 만들기', '온라인 로봇이 함께 지도를 그립니다. 바꾸면 워커의 이동 경로와 경비가 모두 멈춥니다.')}
      <div class="col" style="gap: 6px; background: var(--bg); border-radius: 10px; padding: 10px" id="mode-checks">
        <div class="muted" style="font-size: 12px">역할 확인 (<a href="roles.html">역할 화면</a>)</div>
        ${chk(!!fleet.mapFile, fleet.mapFile ? `기준 지도: ${fleet.mapFile}` : '기준(활성) 지도 없음', fleet.mapFile ? '' : '지도 만들기로 먼저 만드세요')}
        ${chk(nWorker + nGuard > 0, `워커 ${nWorker}대 · 경비 ${nGuard}대`, nWorker + nGuard ? '' : '역할 화면에서 로봇마다 역할을 정하세요')}
        ${chk(online > 0, `온라인 로봇 ${online}대`, '로봇별 Nav2 와 위치 추정(초기 위치)이 필요합니다')}
        ${chk(maps.length > 0, `저장된 지도 ${maps.length}개`, '')}
      </div>
      <div style="display: flex; gap: 8px"><button value="cancel" class="btn" style="flex: 1 1 0; min-height: 44px">취소</button>
        <button value="ok" class="btn btn-primary" id="mode-ok" style="flex: 2 1 0; min-height: 44px">바꾸기</button></div>
      <div class="muted" style="font-size: 11px; text-align: center">지도 만들기로 바꾸면 이동 경로·경비는 멈춤 상태가 되고, 역할 화면에서 이어서·재개할 수 있습니다</div>
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
      } catch (e) { toast(`모드를 바꾸지 못했습니다: ${e.message}`, 'danger', 6000); }
    };
  }

  const App = {
    ros, store, $, $$, esc, fmt, toast, toastAction, estop, trackGoal, robotUrl, control,
    get mode() { return modeState; }, openModeDialog, pollMode,
    get obstacle() { return obstacleState; }, loadObstacle, attachObstacle,
    get safety() { return safety; }, openSafetyDialog, loadSafety,
    watchPaths, pathsFor,
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
      await loadSafety();
      bindHeader();
      if (!document.body.dataset.noMode) { modeWidget(); pollMode(); setInterval(pollMode, 3000); watchAlerts(); loadObstacle(); }
      else setInterval(loadSafety, 3000);
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
