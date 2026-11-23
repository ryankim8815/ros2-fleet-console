// 플릿 관제: 여러 로봇(TurtleBot3, Pinky Pro)을 네임스페이스로 구분해 한 화면에서 관제
window.App.ready.then((App) => {
  'use strict';
  const { ros, $, $$, esc, fmt, toast } = App;
  const P = window.Pose2;
  const norm = (f) => (f || '').replace(/^\//, '');
  const robots = App.robots;
  let sel = App.robot;
  let typeFilter = 'all';
  const hidden = new Set();          // 지도에서 숨긴 로봇 id
  const extra = new Map();           // id -> {scan, plan, goal, lastMapPose, wasOnline, wasLow, nav}

  // LiDAR 구독 간격: 평소에는 수신 여부만 확인(2초), 선택한 로봇을 LiDAR 레이어로 볼 때만 0.2초
  const SCAN_SLOW = 2000, SCAN_FAST = 200;
  function setScanRate(r, ms) {
    const x = extra.get(r.id);
    if (x.scanRate === ms) return;
    if (x.unsubScan) x.unsubScan();
    x.scanRate = ms;
    x.unsubScan = ros.subscribe(r.topic('scan'), 'sensor_msgs/msg/LaserScan', (m) => {
      r.mark('scan');
      x.scanLocal = window.scanPoints(m, 2, r.sensorOffset(m.header.frame_id));
    }, { serverThrottle: ms });
  }
  function updateScanRates() {
    robots.forEach((r) => setScanRate(r, view.layers.scan && r === sel ? SCAN_FAST : SCAN_SLOW));
  }

  robots.forEach((r) => {
    r.start({ tf: true, odomThrottle: 100 });   // odom 최대 10 Hz
    const x = { scan: null, plan: null, goal: null, lastMapPose: null, wasOnline: null, wasLow: false, nav: null };
    extra.set(r.id, x);
    setScanRate(r, SCAN_SLOW);
    ros.subscribe(r.topic('plan'), 'nav_msgs/msg/Path', (m) => {
      const f = norm(m.header.frame_id);
      let T = P.identity;
      if (f !== 'map' && r.mapToOdom) T = r.mapToOdom.pose;   // odom 프레임 경로 → map
      x.plan = (m.poses || []).map((ps) => P.apply(T, ps.pose.position.x, ps.pose.position.y));
    }, { serverThrottle: 1000 });
  });

  // ================= 지도 =================
  const view = new window.RobotView($('#canvas'), { scale: 50 });
  view.state.robots = [];
  view.layers.scan = false;
  view.emptyText = '지도 위 위치를 아는 로봇이 없습니다 (map → <ns>/odom TF 필요: AMCL 또는 SLAM)';
  const MAP = App.fleet.mapTopic || '/map';
  $('#map-info').textContent = MAP;
  ros.subscribe(MAP, 'nav_msgs/msg/OccupancyGrid', (m) => {
    const o = m.info.origin;
    view.state.map = { x: o.position.x, y: o.position.y, th: P.yaw(o.orientation), res: m.info.resolution, canvas: window.RobotView.mapImage(m) };
    $('#map-info').textContent = `${MAP} · ${m.info.width}×${m.info.height} · ${Number(m.info.resolution).toFixed(3)} m`;
    view.invalidate();
  }, { serverThrottle: 3000, compression: 'png' });

  $$('[data-layer]').forEach((b) => b.addEventListener('click', () => {
    const on = b.getAttribute('aria-pressed') !== 'true';
    b.setAttribute('aria-pressed', String(on));
    view.layers[b.dataset.layer] = on;
    updateScanRates();
    view.invalidate();
  }));
  const followBtn = $('#follow-btn');
  followBtn.addEventListener('click', () => view.setFollow(!view.follow));
  view.onChange = () => followBtn.setAttribute('aria-pressed', String(view.follow));
  $('#zoom-in').addEventListener('click', () => view.zoom(1.25));
  $('#zoom-out').addEventListener('click', () => view.zoom(0.8));

  function updateView() {
    view.state.robots = robots.filter((r) => !hidden.has(r.id)).map((r) => {
      const x = extra.get(r.id);
      let pose = r.mapPose;
      let stale = false;
      if (pose && r.online) x.lastMapPose = pose;
      else if (x.lastMapPose) { pose = x.lastMapPose; stale = true; }
      const scan = r === sel && pose && !stale && x.scanLocal ? x.scanLocal.map(([a, b]) => P.apply(pose, a, b)) : null;
      return { name: r.name, color: r.color, selected: r === sel, pose, stale, plan: x.plan, goal: x.goal, scan };
    });
    const noPose = robots.filter((r) => r.online && !r.mapPose).map((r) => r.name);
    $('#no-map-note').textContent = noPose.length ? `지도 위치 없음: ${noPose.join(', ')}` : '';
    // 로봇 간 최소 거리 (지도 위치를 아는 온라인 로봇끼리)
    const live = robots.filter((r) => r.online && r.mapPose);
    let best = null;
    for (let i = 0; i < live.length; i += 1) {
      for (let j = i + 1; j < live.length; j += 1) {
        const a = live[i].mapPose, b = live[j].mapPose;
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        if (!best || d < best.d) best = { d, a: live[i], b: live[j] };
      }
    }
    const md = $('#min-dist');
    md.classList.toggle('hidden', !best);
    if (best) {
      md.textContent = `로봇 간 최소 거리 ${best.d.toFixed(2)} m (${best.a.name} ↔ ${best.b.name})`;
      md.dataset.level = best.d < MIN_DIST_WARN ? 'bad' : best.d < MIN_DIST_WARN * 2 ? 'warn' : 'ok';
      const close = best.d < MIN_DIST_WARN;
      if (close && !minDistWarned) event(null, 'WARN', `${best.a.name} 와 ${best.b.name} 가 ${best.d.toFixed(2)} m 까지 가까워졌습니다`);
      minDistWarned = close;
    }
    view.invalidate();
  }
  // ---- 이동 구역 표시 (활성 지도의 구역, 선택한 로봇에 해당하지 않는 구역은 흐리게) ----
  function updateZones() {
    view.state.zones = (App.zones.zones || []).map((z) => ({ ...z, label: z.name, dim: !(z.robots.includes('*') || z.robots.includes(sel.id)) }));
  }
  setInterval(updateZones, 1000);

  // ---- 지도가 없을 때 (처음 시작) 안내 ----
  (async () => {
    let maps = [];
    try { maps = await (await fetch('/api/maps', { cache: 'no-store' })).json(); } catch (e) { return; }
    const b = $('#map-banner');
    if (!maps.length) {
      b.innerHTML = '<b>아직 지도가 없습니다.</b><span class="muted">로봇 한 대로 먼저 지도를 그리면 이동 구역과 여러 대의 Nav2 에 같은 지도를 씁니다.</span><a class="btn btn-primary" href="mapping.html" style="text-decoration: none">지도 만들기 시작</a>';
      b.classList.remove('hidden');
    } else if (!App.fleet.mapFile) {
      b.innerHTML = `<b>활성 지도가 정해지지 않았습니다.</b><span class="muted">저장된 지도 ${maps.length}개 중 하나를 활성 지도로 지정하세요.</span><a class="btn" href="zones.html" style="text-decoration: none">지도 고르기</a>`;
      b.classList.remove('hidden');
    }
  })();

  const MIN_DIST_WARN = 0.5;   // m
  let minDistWarned = false;
  setInterval(updateView, 100);

  function renderMapChips() {
    $('#map-chips').innerHTML = robots.map((r) => `<button class="chip" data-chip="${esc(r.id)}" aria-pressed="${!hidden.has(r.id)}"><span class="swatch" style="background: ${esc(r.color)}"></span>${esc(r.name)}</button>`).join('');
  }
  $('#map-chips').addEventListener('click', (e) => {
    const c = e.target.closest('[data-chip]'); if (!c) return;
    if (hidden.has(c.dataset.chip)) hidden.delete(c.dataset.chip); else hidden.add(c.dataset.chip);
    renderMapChips();
  });
  renderMapChips();

  // ---- 목표 지정 / 초기 위치 (선택한 로봇) ----
  const goalBtn = $('#goal-btn'), initBtn = $('#init-btn');
  let clickMode = null;   // 'goal' | 'init' | null
  function setGoalMode(mode) {
    clickMode = mode;
    view.goalMode = !!mode;
    goalBtn.setAttribute('aria-pressed', String(mode === 'goal'));
    goalBtn.classList.toggle('btn-primary', mode === 'goal');
    initBtn.setAttribute('aria-pressed', String(mode === 'init'));
    initBtn.classList.toggle('btn-primary', mode === 'init');
    $('#goal-hint').textContent = mode === 'init'
      ? `${sel.name} 의 실제 위치를 클릭 후 끌어서 방향 지정 · Esc 취소`
      : '지도에서 클릭 후 끌어서 방향 지정 · Esc 취소';
    $('#goal-hint').classList.toggle('hidden', !mode);
  }
  goalBtn.addEventListener('click', () => setGoalMode(clickMode === 'goal' ? null : 'goal'));
  initBtn.addEventListener('click', () => setGoalMode(clickMode === 'init' ? null : 'init'));
  window.addEventListener('keydown', (e) => { if (e.key === 'Escape') setGoalMode(null); });

  async function sendGoal(r, x, y, th) {
    const action = r.topic('navigate_to_pose');
    try {
      const s = await ros.callService('/rosapi/action_servers', {});
      if (!(s.action_servers || []).includes(action)) { toast(`${r.name}: ${action} 액션 서버가 없습니다 (Nav2 미실행)`, 'warn', 5000); return; }
    } catch (e) { toast(`액션 서버 확인 실패: ${e.message}`, 'warn'); return; }
    const xr = extra.get(r.id);
    if (xr.nav && xr.nav.g) xr.nav.g.cancel();
    xr.goal = { x, y, th };
    const nav = { state: '전송 중', remain: NaN, g: null };
    xr.nav = nav;
    event(r, 'INFO', `goal 전송 (${x.toFixed(2)}, ${y.toFixed(2)})`);
    nav.g = App.trackGoal(r.navGoal(x, y, th, 'map', (f) => { nav.state = '실행 중'; nav.remain = f.distance_remaining; }));
    try {
      const res = await nav.g.result;
      nav.state = res.status === 4 ? '도착' : res.status === 5 ? '취소됨' : `실패 (${window.Ros.GOAL_STATUS[res.status] || res.status})`;
      event(r, res.status === 4 ? 'INFO' : 'WARN', `goal ${nav.state}`);
    } catch (e) { nav.state = '실패'; event(r, 'ERROR', `goal 실패: ${e.message}`); }
    nav.g = null;
    if (xr.nav === nav) xr.goal = null;
  }
  view.onGoal = (x, y, th) => {
    const mode = clickMode;
    setGoalMode(null);
    if (mode === 'init') {
      sel.setInitialPose(x, y, th);
      event(sel, 'INFO', `초기 위치 (${x.toFixed(2)}, ${y.toFixed(2)}, ${(th * 180 / Math.PI).toFixed(0)}°) → ${sel.topic('initialpose')}`);
      toast(`${sel.name}: 초기 위치를 보냈습니다`, 'ok', 2000);
    } else sendGoal(sel, x, y, th);
  };

  // ================= 로봇 카드 =================
  function stateOf(r) {
    const x = extra.get(r.id);
    if (!r.online) return ['오프라인', ''];
    if (r.link === 'unstable') return ['연결 불안정', 'warn'];
    if (x.nav && x.nav.g) return ['Nav2 주행', 'run'];
    if (Math.abs(r.v) > 0.005 || Math.abs(r.w) > 0.02) return ['이동 중', 'run'];
    if (r.batteryLow()) return ['배터리 낮음', 'warn'];
    return ['대기', 'ok'];
  }
  let holders = {};
  setInterval(async () => { holders = await App.control.holders(); }, 1000);
  function controllerOf(r) {
    const h = holders[r.id];
    if (!h) return '';
    return h.client === App.control.clientId ? '조종: 이 브라우저' : `조종: ${h.label}`;
  }
  function taskOf(r) {
    const c = controllerOf(r);
    return c ? `${taskText(r)} · ${c}` : taskText(r);
  }
  function taskText(r) {
    const x = extra.get(r.id);
    if (!r.online) return r.age('odom') === Infinity ? `${r.topic('odom')} 수신 기록 없음` : `${Math.round(r.age('odom') / 60000)}분 전부터 ${r.topic('odom')} 수신 없음`;
    if (x.nav && x.nav.g && x.goal) return `목표 (${x.goal.x.toFixed(2)}, ${x.goal.y.toFixed(2)}) · 남은 ${fmt(x.nav.remain, 2)} m`;
    if (!r.mapPose) return '지도 위치 없음 (map TF 없음)';
    const p = r.mapPose;
    return `map (${p.x.toFixed(2)}, ${p.y.toFixed(2)})`;
  }
  function renderCards() {
    const list = robots.filter((r) => typeFilter === 'all' || r.type === typeFilter);
    $('#robot-count').textContent = robots.length;
    $('#robot-cards').innerHTML = list.map((r) => {
      const [st, cls] = stateOf(r);
      const p = r.batteryPercent();
      return `<button class="rcard ${r.online ? '' : 'off'}" data-robot="${esc(r.id)}" aria-pressed="${r === sel}" style="--rc: ${esc(r.color)}">
        <div class="top"><span class="swatch" style="width: 12px; height: 12px; background: ${esc(r.color)}"></span>
          <div style="flex: 1 1 auto; min-width: 0"><div class="nm">${esc(r.name)}</div><div class="sub">${esc(r.profile.model)} · <span class="mono">${esc(r.ns || '(ns 없음)')}</span></div></div>
          <span class="badge ${cls}">${st}</span></div>
        <div class="bat"><div class="bar"><div style="width: ${Number.isFinite(p) ? Math.round(p) : 0}%; background: ${r.batteryLow() ? 'var(--warn)' : 'var(--ok)'}"></div></div><span class="mono">${esc(r.batteryText())}</span></div>
        <div class="task">${esc(taskOf(r))}</div></button>`;
    }).join('') || '<div class="empty">해당 종류의 로봇이 없습니다</div>';
    const on = robots.filter((r) => r.online).length;
    const warn = robots.filter((r) => r.online && r.batteryLow()).length;
    $('#sum-on').innerHTML = `<span class="dot"></span>온라인 ${on}`;
    $('#sum-off').textContent = `오프라인 ${robots.length - on}`;
    $('#sum-warn').textContent = `배터리 경고 ${warn}`;
    $('#sum-warn').classList.toggle('hidden', !warn);
  }
  $('#robot-cards').addEventListener('click', (e) => { const b = e.target.closest('[data-robot]'); if (b) select(robots.find((r) => r.id === b.dataset.robot)); });
  $$('[data-filter]').forEach((b) => b.addEventListener('click', () => {
    typeFilter = b.dataset.filter;
    $$('[data-filter]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    renderCards();
  }));

  // ================= 선택한 로봇 패널 =================
  function select(r) {
    if (!r) return;
    sel = r;
    App.store.set('tb3.robot', r.id);
    $('#goal-target').textContent = r.name;
    $('#link-dash').href = App.robotUrl(r.id, 'index.html');
    view.setFollow(true);
    updateScanRates();
    renderDetail();
    renderCards();
  }

  function deviceHtml(r) {
    const missing = (s) => (r.has(s) ? '' : ' disabled title="서비스가 없습니다"');
    if (r.type === 'pinky') {
      const LEDS = [['#FF4D4D', 255, 0, 0, '빨강'], ['#FB923C', 255, 100, 0, '주황'], ['#FACC15', 255, 200, 0, '노랑'], ['#4ADE80', 0, 255, 0, '초록'], ['#60A5FA', 0, 80, 255, '파랑'], ['#F9A8D4', 255, 60, 160, '분홍']];
      const any = r.has('set_led') || r.has('set_emotion');
      return `<section class="panel" style="--rc: ${esc(r.color)}">
        <div class="panel-head" style="justify-content: space-between"><h2>Pinky Pro 장치</h2><span class="muted" style="font-size: 11px">종류별 패널</span></div>
        ${any ? '' : `<div class="empty" style="padding: 0">LED·표정 서비스가 없습니다. 로봇에서 led_server / emotion_server 를 ${esc(r.ns || '/')} 네임스페이스로 실행하세요.</div>`}
        <div class="muted" style="font-size: 12px">LED 스트립 · <span class="mono">${esc(r.topic('set_led'))}</span></div>
        <div style="display: flex; gap: 8px; flex-wrap: wrap">${LEDS.map(([c, R, G, B, n]) => `<button class="led" style="background: ${c}" data-led="${R},${G},${B}" aria-label="LED ${n}"${missing('set_led')}></button>`).join('')}
          <button class="btn btn-sm" data-led="off"${missing('set_led')}>끄기</button></div>
        <label class="field"><span class="line">밝기 <b class="mono" style="font-size: 11px">${esc(r.topic('set_brightness'))}</b></span>
          <input type="range" min="0" max="100" value="60" id="led-bright" style="accent-color: ${esc(r.color)}"${missing('set_brightness')}></label>
        <div class="muted" style="font-size: 12px">LCD 표정 · <span class="mono">${esc(r.topic('set_emotion'))}</span></div>
        <div class="emo">${r.profile.emotions.map((e) => `<button data-emo="${e}"${missing('set_emotion')}>${e}</button>`).join('')}</div>
      </section>`;
    }
    return `<section class="panel">
      <div class="panel-head" style="justify-content: space-between"><h2>TurtleBot3 장치</h2><span class="muted" style="font-size: 11px">종류별 패널</span></div>
      <div class="muted" style="font-size: 12px">모터 전원 · <span class="mono">${esc(r.topic('motor_power'))}</span></div>
      <div class="grid2"><button class="btn" data-motor="1"${missing('motor_power')}>모터 켜기</button><button class="btn btn-danger-outline" data-motor="0"${missing('motor_power')}>모터 끄기</button></div>
      <div class="muted" style="font-size: 12px">사운드 · <span class="mono">${esc(r.topic('sound'))}</span></div>
      <div style="display: flex; gap: 8px"><select id="snd" style="flex: 1 1 auto"${missing('sound')}>${r.profile.sounds.map(([v, n]) => `<option value="${v}">${v} · ${n}</option>`).join('')}</select><button class="btn" data-sound${missing('sound')}>재생</button></div>
      <button class="btn" data-reset${missing('reset_odometry')}>odom 초기화</button>
    </section>`;
  }

  function renderDetail() {
    const r = sel;
    $('#detail').innerHTML = `
      <section class="panel" style="border-color: ${esc(r.color)}">
        <div class="panel-head">
          <span class="swatch" style="width: 12px; height: 12px; background: ${esc(r.color)}"></span>
          <div style="flex: 1 1 auto; min-width: 0"><h2 style="font-size: 17px">${esc(r.name)}</h2><div class="muted" style="font-size: 12px">${esc(r.profile.model)} · <span class="mono">${esc(r.ns || '(ns 없음)')}</span> · TF <span class="mono">${esc(r.tfTopic)}</span></div></div>
          <button class="btn btn-danger-outline" id="stop-one">정지</button>
        </div>
        <div class="grid3">
          <div class="tile"><div class="k">v</div><div class="v" id="d-v">—</div></div>
          <div class="tile"><div class="k">ω</div><div class="v" id="d-w">—</div></div>
          <div class="tile"><div class="k">남은 거리</div><div class="v" id="d-remain">—</div></div>
        </div>
        <div class="muted" style="font-size: 12px" id="d-pose"></div>
        <div class="grid2">
          <a class="btn" href="${esc(App.robotUrl(r.id, 'index.html'))}" style="text-decoration: none">로봇 대시보드 ↗</a>
          <a class="btn" href="${esc(App.robotUrl(r.id, 'mobile.html'))}" style="text-decoration: none">모바일 조종 ↗</a>
        </div>
        <button class="btn btn-sm hidden" id="goal-cancel">이 로봇 goal 취소</button>
      </section>
      ${deviceHtml(r)}`;
    updateDetail();
  }

  function updateDetail() {
    const r = sel, x = extra.get(r.id);
    const set = (id, t) => { const el = $(id); if (el) el.textContent = t; };
    set('#d-v', fmt(r.v, 2)); set('#d-w', fmt(r.w, 2));
    set('#d-remain', x.nav && x.nav.g ? `${fmt(x.nav.remain, 2)} m` : (x.nav ? x.nav.state : '—'));
    const mp = r.mapPose;
    set('#d-pose', r.online ? (mp ? `map (${mp.x.toFixed(2)}, ${mp.y.toFixed(2)}, ${(mp.th * 180 / Math.PI).toFixed(0)}°)` : `odom (${fmt(r.pose && r.pose.x)}, ${fmt(r.pose && r.pose.y)}) · map TF 없음`) : '오프라인');
    const gc = $('#goal-cancel'); if (gc) gc.classList.toggle('hidden', !(x.nav && x.nav.g));
  }

  $('#detail').addEventListener('click', async (e) => {
    const r = sel;
    const t = e.target.closest('button');
    if (!t || t.disabled) return;
    try {
      if (t.id === 'stop-one') { r.halt(); const x = extra.get(r.id); if (x.nav && x.nav.g) x.nav.g.cancel(); toast(`${r.name} 정지`, 'warn', 1500); return; }
      if (t.id === 'goal-cancel') { const x = extra.get(r.id); if (x.nav && x.nav.g) x.nav.g.cancel(); return; }
      if (t.dataset.led) {
        const args = t.dataset.led === 'off' ? { command: 'clear', pixels: [], r: 0, g: 0, b: 0 }
          : (([R, G, B]) => ({ command: 'fill', pixels: [], r: R, g: G, b: B }))(t.dataset.led.split(',').map(Number));
        const res = await r.call('set_led', args);
        $$('#detail [data-led]').forEach((b) => b.setAttribute('aria-pressed', String(b === t)));
        if (res.success === false) toast(`LED: ${res.message}`, 'warn');
        return;
      }
      if (t.dataset.emo) {
        const res = await r.call('set_emotion', { emotion: t.dataset.emo });
        $$('#detail [data-emo]').forEach((b) => b.setAttribute('aria-pressed', String(b === t)));
        event(r, 'INFO', `표정 → ${t.dataset.emo}${res.response ? ` (${res.response})` : ''}`);
        return;
      }
      if (t.dataset.motor) {
        const on = t.dataset.motor === '1';
        if (!on && !confirm(`${r.name} 의 모터 전원을 끌까요? 바퀴가 풀립니다.`)) return;
        const res = await r.call('motor_power', { data: on });
        toast(`${r.name} 모터 ${on ? '켜기' : '끄기'}: ${res.message || (res.success ? '성공' : '실패')}`, res.success ? 'ok' : 'warn');
        return;
      }
      if (t.hasAttribute('data-sound')) { await r.call('sound', { value: Number($('#snd').value) }); return; }
      if (t.hasAttribute('data-reset')) {
        if (!confirm(`${r.name} 의 odom 을 초기화할까요?`)) return;
        const res = await r.call('reset_odometry');
        toast(res.success === false ? `실패: ${res.message}` : `${r.name} odom 초기화`, res.success === false ? 'warn' : 'ok');
      }
    } catch (err) { toast(`${r.name}: ${err.message}`, 'danger'); }
  });
  $('#detail').addEventListener('change', async (e) => {
    if (e.target.id !== 'led-bright') return;
    try { await sel.call('set_brightness', { brightness: Number(e.target.value) }); } catch (err) { toast(`밝기: ${err.message}`, 'warn'); }
  });

  // ================= 비교 표 =================
  function renderTable() {
    $('#cmp-rows').innerHTML = robots.map((r) => {
      const x = extra.get(r.id);
      const [st, cls] = stateOf(r);
      const hz = r.online ? r.hz('odom').toFixed(1) : '—';
      const scanOk = r.age('scan') < SCAN_SLOW + 3000;
      return `<tr class="clickable${r === sel ? ' selected' : ''}" data-robot="${esc(r.id)}">
        <td><span style="display: inline-flex; align-items: center; gap: 8px"><span class="swatch" style="background: ${esc(r.color)}"></span>${esc(r.name)}</span></td>
        <td class="muted">${esc(r.label)}</td><td><span class="badge ${cls}">${st}</span></td>
        <td class="num">${fmt(r.v, 2)}</td><td class="num">${hz}</td>
        <td>${r.online ? (scanOk ? '<span class="badge ok">수신</span>' : '<span class="badge bad">없음</span>') : '—'}</td>
        <td class="mono" style="font-size: 12px">${esc(r.batteryText())}</td>
        <td class="muted">${esc(x.nav ? x.nav.state : '—')}</td></tr>`;
    }).join('');
  }
  $('#cmp-rows').addEventListener('click', (e) => { const tr = e.target.closest('[data-robot]'); if (tr) select(robots.find((r) => r.id === tr.dataset.robot)); });

  // ================= 이벤트 =================
  const events = [];
  const LVC = { INFO: 'lv-INFO', WARN: 'lv-WARN', ERROR: 'lv-ERROR', FATAL: 'lv-FATAL' };
  function event(r, lv, msg, ts) {
    events.push({ t: ts || new Date().toTimeString().slice(0, 8), r, lv, msg });
    if (events.length > 300) events.shift();
    evDirty = true;
  }
  let evDirty = false;
  function renderEvents() {
    $('#events').innerHTML = events.length ? events.slice(-120).reverse().map((e) => `<div class="ln"><span class="ts">${e.t}</span> <span style="color: ${e.r ? esc(e.r.color) : 'var(--muted)'}">${esc(e.r ? e.r.name : '—')}</span> <span class="${LVC[e.lv] || ''}">${e.lv}</span> ${esc(e.msg)}</div>`).join('') : '<div class="empty">이벤트 대기 중…</div>';
  }
  $('#ev-clear').addEventListener('click', () => { events.length = 0; renderEvents(); });
  const LV = { 30: 'WARN', 40: 'ERROR', 50: 'FATAL' };
  ros.subscribe('/rosout', 'rcl_interfaces/msg/Log', (l) => {
    if (l.level < 30) return;
    // 로거 이름(예: tb3_01.turtlebot3_node)으로 로봇을 찾음
    const r = robots.filter((x) => x.logPrefix).find((x) => l.name === x.logPrefix || l.name.startsWith(`${x.logPrefix}.`))
      || robots.find((x) => !x.logPrefix && robots.length === 1);
    const d = new Date(l.stamp.sec * 1000 + Math.floor(l.stamp.nanosec / 1e6));
    event(r || null, LV[l.level] || 'WARN', `[${l.name}] ${l.msg}`, d.toTimeString().slice(0, 8));
  }, { queue: 50 });

  function watchStates() {
    robots.forEach((r) => {
      const x = extra.get(r.id);
      const on = r.online;
      if (x.wasOnline !== null && on !== x.wasOnline) event(r, on ? 'INFO' : 'ERROR', on ? '온라인 (odom 수신)' : `${r.topic('odom')} 수신 끊김`);
      x.wasOnline = on;
      const low = on && r.batteryLow();
      if (low && !x.wasLow) event(r, 'WARN', `배터리 낮음 (${r.batteryText()})`);
      x.wasLow = low;
    });
  }
  setTimeout(() => { robots.forEach((r) => { extra.get(r.id).wasOnline = r.online; }); setInterval(watchStates, 1000); }, 4000);

  // ================= 주기 갱신 =================
  setInterval(() => { renderCards(); renderTable(); updateDetail(); if (evDirty) { evDirty = false; renderEvents(); } }, 1000);
  setInterval(() => { if (ros.connected) robots.forEach((r) => r.refreshServices()); }, 15000);
  let lastSvc = '';
  setInterval(() => {   // 서비스 목록이 바뀌면 장치 패널 다시 그림
    const key = [...sel.services].sort().join();
    if (key !== lastSvc) { lastSvc = key; renderDetail(); }
  }, 1000);
  select(sel);
});
