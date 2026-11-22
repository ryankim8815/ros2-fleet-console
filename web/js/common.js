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
    document.dispatchEvent(new CustomEvent('tb3:estop'));
    toast(`비상정지: 로봇 ${robots.length}대에 정지 명령을 보내고 진행 중인 goal 을 취소했습니다`, 'danger');
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

  const App = {
    ros, store, $, $$, esc, fmt, toast, toastAction, estop, trackGoal, robotUrl, control,
    get fleet() { return fleet; },
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
      bindHeader();
      // 현재 로봇의 기본 상태 구독 (배터리 표시용). 플릿 페이지는 직접 모든 로봇을 시작함
      if (!document.body.dataset.fleet) robot.start({ tf: false });
      robot.onChange(renderBattery);
      setInterval(renderBattery, 5000);
      ros.connect();
      pingLoop();
      resolve(App);
    });
  });
  window.App = App;
})();
