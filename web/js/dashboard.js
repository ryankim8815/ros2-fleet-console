// 로봇별 대시보드: 실시간 뷰, 원격조종, 위치, 내비게이션 goal, 토픽 상태, 로그
// 다루는 로봇은 ?robot=<id> (없으면 마지막으로 본 로봇) — 토픽은 모두 그 로봇의 네임스페이스 아래
window.App.ready.then((App) => {
  'use strict';
  const { ros, $, $$, esc, fmt, toast } = App;
  const R = App.robot;
  const P = window.Pose2;
  const norm = (f) => (f || '').replace(/^\//, '');
  const ODOM = norm(R.odomFrame);

  document.title = `${R.name} · 로봇 관제센터`;
  $('#robot-ns').textContent = R.ns || '(없음)';
  $('#robot-type').textContent = R.profile.model;

  // ================= 실시간 뷰 =================
  const view = new window.RobotView($('#canvas'), { scale: 70 });
  const st = view.state;
  let odomPose = null;
  let mapToOdom = null;
  const MAP_TF_TIMEOUT = 10000;

  function displayFrame() { return mapToOdom && performance.now() - mapToOdom.t < MAP_TF_TIMEOUT ? 'map' : 'odom'; }
  function frameToDisplay(frame) {
    const disp = displayFrame();
    let f = norm(frame);
    if (f === ODOM || f === 'odom') f = 'odom';
    else if (f !== 'map') f = disp;
    if (f === disp || !mapToOdom) return P.identity;
    return f === 'odom' ? mapToOdom.pose : P.inverse(mapToOdom.pose);
  }
  const sendFrame = () => (displayFrame() === 'map' ? 'map' : R.odomFrame);

  let lastFrame = 'odom';
  function updateFrameLabel() {
    const f = displayFrame();
    if (f !== lastFrame) { st.trail = []; lastFrame = f; }
    $('#frame-label').textContent = `frame: ${f === 'map' ? 'map' : R.odomFrame}`;
  }

  $$('[data-layer]').forEach((b) => b.addEventListener('click', () => {
    const on = b.getAttribute('aria-pressed') !== 'true';
    b.setAttribute('aria-pressed', String(on));
    view.layers[b.dataset.layer] = on;
    view.invalidate();
  }));
  const followBtn = $('#follow-btn');
  followBtn.addEventListener('click', () => view.setFollow(!view.follow));
  view.onChange = () => followBtn.setAttribute('aria-pressed', String(view.follow));
  $('#zoom-in').addEventListener('click', () => view.zoom(1.25));
  $('#zoom-out').addEventListener('click', () => view.zoom(0.8));
  $('#trail-clear').addEventListener('click', () => { st.trail = []; view.invalidate(); });
  setInterval(() => { $('#scale-label').textContent = `격자 0.5 m · ${Math.round(view.scale)} px/m`; }, 500);

  // ---- 토픽 수신 기록 ----
  const times = {};
  const mon = (topic) => { (times[topic] ||= []).push(performance.now()); };

  // ---- TF: map -> <prefix>odom ----
  ros.subscribe(R.tfTopic, 'tf2_msgs/msg/TFMessage', (m) => {
    mon(R.tfTopic);
    for (const t of m.transforms || []) {
      if (norm(t.header.frame_id) === 'map' && norm(t.child_frame_id) === ODOM) {
        const tr = t.transform.translation;
        mapToOdom = { pose: { x: tr.x, y: tr.y, th: P.yaw(t.transform.rotation) }, t: performance.now() };
      }
    }
  });

  // ---- odom ----
  let dist = 0, lastTrail = null;
  ros.subscribe(R.topic('odom'), 'nav_msgs/msg/Odometry', (m) => {
    mon(R.topic('odom'));
    const p = m.pose.pose;
    const prev = odomPose;
    odomPose = { x: p.position.x, y: p.position.y, th: P.yaw(p.orientation) };
    if (prev) { const d = Math.hypot(odomPose.x - prev.x, odomPose.y - prev.y); if (d < 0.5) dist += d; }
    updateFrameLabel();
    const pose = P.compose(frameToDisplay('odom'), odomPose);
    st.pose = pose;
    if (!lastTrail || Math.hypot(pose.x - lastTrail.x, pose.y - lastTrail.y) > 0.03) {
      st.trail.push([pose.x, pose.y]);
      if (st.trail.length > 3000) st.trail.shift();
      lastTrail = pose;
    }
    view.invalidate();
  });

  // ---- scan ----
  ros.subscribe(R.topic('scan'), 'sensor_msgs/msg/LaserScan', (m) => {
    mon(R.topic('scan'));
    if (!st.pose) return;
    st.scan = window.scanPoints(m, 1, R.sensorOffset(m.header.frame_id)).map(([x, y]) => P.apply(st.pose, x, y));
    view.invalidate();
  });

  // ---- map (공유 지도) ----
  const MAP = (App.fleet.mapTopic || '/map');
  ros.subscribe(MAP, 'nav_msgs/msg/OccupancyGrid', (m) => {
    mon(MAP);
    const o = m.info.origin;
    st.mapMsg = { frame: m.header.frame_id, x: o.position.x, y: o.position.y, th: P.yaw(o.orientation), res: m.info.resolution, canvas: window.RobotView.mapImage(m) };
    placeMap();
  }, { serverThrottle: 3000, compression: 'png' });
  function placeMap() {
    if (!st.mapMsg) return;
    const o = P.compose(frameToDisplay(st.mapMsg.frame || 'map'), st.mapMsg);
    st.map = { ...st.mapMsg, x: o.x, y: o.y, th: o.th };
    view.invalidate();
  }
  setInterval(placeMap, 1000);

  // ---- 이 로봇에 적용되는 이동 구역 (map 프레임 → 표시 프레임) ----
  setInterval(() => {
    const T = frameToDisplay('map');
    st.zones = (R.zones || []).map((z) => ({ ...z, label: z.name, points: z.points.map(([x, y]) => P.apply(T, x, y)) }));
  }, 1000);

  // ---- plan ----
  ros.subscribe(R.topic('plan'), 'nav_msgs/msg/Path', (m) => {
    mon(R.topic('plan'));
    const T = frameToDisplay(m.header.frame_id);
    st.plan = (m.poses || []).map((ps) => P.apply(T, ps.pose.position.x, ps.pose.position.y));
    view.invalidate();
  }, { serverThrottle: 500 });

  // ================= 내비게이션 goal =================
  const goalBtn = $('#goal-btn');
  let goal = null, goalStartDist = null;
  function setGoalMode(on) {
    view.goalMode = on;
    goalBtn.setAttribute('aria-pressed', String(on));
    goalBtn.classList.toggle('btn-primary', on);
    $('#goal-hint').classList.toggle('hidden', !on);
  }
  goalBtn.addEventListener('click', () => setGoalMode(!view.goalMode));
  window.addEventListener('keydown', (e) => { if (e.key === 'Escape') setGoalMode(false); });

  function navUi(state, cls) { const b = $('#nav-state'); b.textContent = state; b.className = `badge ${cls || ''}`; }

  view.onGoal = async (x, y, th) => {
    setGoalMode(false);
    const action = R.topic('navigate_to_pose');
    try {
      const r = await ros.callService('/rosapi/action_servers', {});
      if (!(r.action_servers || []).includes(action)) {
        toast(`${action} 액션 서버가 없습니다. 이 로봇의 Nav2 를 먼저 실행하세요.`, 'warn', 5000);
        return;
      }
    } catch (e) { toast(`액션 서버 확인 실패: ${e.message}`, 'warn'); return; }
    if (goal) goal.cancel();
    st.goal = { x, y, th };
    view.invalidate();
    goalStartDist = null;
    $('#nav-progress').style.width = '0%';
    navUi('전송 중', 'run');
    $('#nav-cancel').disabled = false;
    const g = App.trackGoal(R.navGoal(x, y, th, sendFrame(), (f) => {
      navUi('실행 중', 'run');
      const rem = f.distance_remaining;
      if (goalStartDist === null && rem > 0) goalStartDist = rem;
      $('#nav-remain').textContent = `${fmt(rem, 2)} m`;
      $('#nav-recover').textContent = `${f.number_of_recoveries ?? 0} 회`;
      if (goalStartDist) $('#nav-progress').style.width = `${Math.max(0, Math.min(100, (1 - rem / goalStartDist) * 100))}%`;
    }));
    goal = g;
    try {
      const res = await g.result;
      const name = window.Ros.GOAL_STATUS[res.status] || res.status;
      if (res.status === 4) { navUi('도착', 'ok'); $('#nav-progress').style.width = '100%'; toast(`${R.name}: 목표 지점에 도착했습니다`, 'ok'); }
      else if (res.status === 5) navUi('취소됨', 'warn');
      else navUi(`실패 (${name})`, 'bad');
    } catch (e) { navUi('실패', 'bad'); toast(`goal 실패: ${e.message}`, 'danger'); }
    if (goal === g) { goal = null; $('#nav-cancel').disabled = true; st.goal = null; view.invalidate(); }
  };
  $('#nav-cancel').addEventListener('click', () => { if (goal) { goal.cancel(); navUi('취소 요청', 'warn'); } });

  // ================= 원격조종 =================
  const linMax = $('#lin-max'), angMax = $('#ang-max');
  linMax.max = String(R.limits.lin); angMax.max = String(R.limits.ang);
  linMax.value = String(Math.min(Number(linMax.value), R.limits.lin * 0.7).toFixed(2));
  angMax.value = String(Math.min(Number(angMax.value), R.limits.ang * 0.55).toFixed(2));
  const showMax = () => { $('#lin-val').textContent = `${Number(linMax.value).toFixed(2)} m/s`; $('#ang-val').textContent = `${Number(angMax.value).toFixed(2)} rad/s`; };
  linMax.addEventListener('input', showMax); angMax.addEventListener('input', showMax); showMax();

  const held = { f: false, b: false, l: false, r: false };
  let wasActive = false;
  const anyHeld = () => held.f || held.b || held.l || held.r;
  const markPad = () => $$('.pad [data-dir]').forEach((b) => b.classList.toggle('active', !!held[b.dataset.dir]));
  function stopNow() { Object.keys(held).forEach((k) => { held[k] = false; }); markPad(); R.cmdVel(0, 0); wasActive = false; }

  setInterval(() => {
    if (anyHeld()) {
      if (!App.control.drive(R)) return;   // 다른 브라우저가 조종 중이면 보내지 않음
      R.cmdVel(((held.f ? 1 : 0) - (held.b ? 1 : 0)) * Number(linMax.value), ((held.l ? 1 : 0) - (held.r ? 1 : 0)) * Number(angMax.value));
      wasActive = true;
    } else if (wasActive) {
      R.cmdVel(0, 0); setTimeout(() => { R.cmdVel(0, 0); App.control.release(R); }, 80);
      wasActive = false;
    }
  }, 100);
  document.addEventListener('tb3:control-lost', () => { Object.keys(held).forEach((k) => { held[k] = false; }); markPad(); wasActive = false; });

  $$('.pad [data-dir]').forEach((b) => {
    const d = b.dataset.dir;
    if (d === 'stop') { b.addEventListener('click', stopNow); return; }
    b.addEventListener('pointerdown', (e) => { e.preventDefault(); try { b.setPointerCapture(e.pointerId); } catch (err) { /* 무시 */ } held[d] = true; markPad(); });
    const up = () => { held[d] = false; markPad(); };
    b.addEventListener('pointerup', up);
    b.addEventListener('pointercancel', up);
    b.addEventListener('lostpointercapture', up);
  });

  const KEYMAP = { w: 'f', s: 'b', a: 'l', d: 'r', arrowup: 'f', arrowdown: 'b', arrowleft: 'l', arrowright: 'r' };
  const typing = (e) => /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) && e.target.type !== 'range';
  window.addEventListener('keydown', (e) => {
    if (typing(e) || e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === ' ') { e.preventDefault(); stopNow(); return; }
    if (KEYMAP[k]) { e.preventDefault(); held[KEYMAP[k]] = true; markPad(); }
  });
  window.addEventListener('keyup', (e) => { const k = KEYMAP[e.key.toLowerCase()]; if (k) { held[k] = false; markPad(); } });
  window.addEventListener('blur', () => { if (anyHeld()) stopNow(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden && anyHeld()) stopNow(); });
  document.addEventListener('tb3:estop', () => { Object.keys(held).forEach((k) => { held[k] = false; }); markPad(); wasActive = false; });

  // ================= 위치 · 자세 =================
  setInterval(() => {
    $('#v-now').textContent = fmt(R.v, 2);
    $('#w-now').textContent = fmt(R.w, 2);
    if (odomPose) {
      $('#pose-x').textContent = `${odomPose.x.toFixed(3)} m`;
      $('#pose-y').textContent = `${odomPose.y.toFixed(3)} m`;
      $('#pose-t').textContent = `${(odomPose.th * 180 / Math.PI).toFixed(1)}°`;
    }
    $('#dist').textContent = `${dist.toFixed(2)} m`;
    $('#cmdvel-type').textContent = R.cmdVelType.replace('geometry_msgs/msg/', '');
  }, 200);

  const resetBtn = $('#odom-reset');
  if (!R.profile.services.reset_odometry) resetBtn.classList.add('hidden');
  resetBtn.addEventListener('click', async () => {
    if (!confirm(`${R.name} 의 odom 을 (0, 0, 0) 으로 초기화할까요?\n(${R.topic('reset_odometry')} 서비스 호출)`)) return;
    try {
      const r = await R.call('reset_odometry');
      st.trail = []; dist = 0;
      toast(r.success === false ? `실패: ${r.message}` : 'odom 을 초기화했습니다', r.success === false ? 'warn' : 'ok');
    } catch (e) { toast(`odom 초기화 실패: ${e.message}`, 'danger'); }
  });

  // ================= 토픽 상태 =================
  const MON = [
    { topic: R.topic('scan'), type: 'LaserScan', expect: R.profile.expect.scan, required: true },
    { topic: R.topic('odom'), type: 'Odometry', expect: R.profile.expect.odom, required: true },
    ...R.profile.battery.map((b) => ({ topic: R.topic(b.topic), type: b.type.split('/').pop(), required: true, note: R.type === 'pinky' ? '5초 간격' : '' })),
    { topic: R.tfTopic, type: 'TFMessage' },
    { topic: MAP, type: 'OccupancyGrid', note: '3초 간격 수신' },
    { topic: R.topic('plan'), type: 'Path', note: '0.5초 간격 수신' },
    { topic: '/rosout', type: 'Log' },
  ];
  R.profile.battery.forEach((b) => ros.subscribe(R.topic(b.topic), b.type, () => mon(R.topic(b.topic))));

  function renderTopics() {
    const now = performance.now();
    $('#topic-rows').innerHTML = MON.map((m) => {
      const arr = (times[m.topic] ||= []);
      while (arr.length && now - arr[0] > 6000) arr.shift();
      const last = arr.length ? now - arr[arr.length - 1] : Infinity;
      let hz = '—';
      if (arr.length >= 2) hz = ((arr.length - 1) / ((arr[arr.length - 1] - arr[0]) / 1000)).toFixed(1);
      else if (arr.length === 1) hz = '<1';
      const limit = m.note === '5초 간격' ? 12000 : 5000;
      let badge;
      if (!ros.connected) badge = '<span class="badge">—</span>';
      else if (last > limit) badge = m.required ? '<span class="badge bad"><span class="dot"></span>수신 없음</span>' : '<span class="badge">대기</span>';
      else if (m.expect && Number(hz) < m.expect * 0.6) badge = '<span class="badge warn"><span class="dot"></span>느림</span>';
      else badge = '<span class="badge ok"><span class="dot"></span>정상</span>';
      return `<tr><td class="mono">${esc(m.topic)}${m.note ? `<div class="sub-type">${esc(m.note)}</div>` : ''}</td><td class="muted mono" style="font-size: 12px">${esc(m.type)}</td><td class="num">${hz}</td><td>${badge}</td></tr>`;
    }).join('');
  }
  setInterval(renderTopics, 1000);
  renderTopics();

  // ================= 로그 =================
  const LV = { 10: 'DEBUG', 20: 'INFO', 30: 'WARN', 40: 'ERROR', 50: 'FATAL' };
  const logs = [];
  const logBox = $('#log');
  const levelSel = $('#log-level');
  const onlyMine = $('#log-mine');
  if (!R.logPrefix) { onlyMine.checked = false; onlyMine.parentElement.classList.add('hidden'); }
  const mine = (name) => name === R.logPrefix || name.startsWith(`${R.logPrefix}.`);
  function renderLogs() {
    const min = Number(levelSel.value);
    const shown = logs.filter((l) => l.level >= min && (!onlyMine.checked || mine(l.name))).slice(-200).reverse();
    logBox.innerHTML = shown.length ? shown.map((l) => `<div class="ln"><span class="ts">${l.ts}</span> <span class="lv-${l.lv}">${l.lv}</span> [${esc(l.name)}] ${esc(l.msg)}</div>`).join('') : '<div class="empty">표시할 로그가 없습니다</div>';
  }
  let logDirty = false;
  ros.subscribe('/rosout', 'rcl_interfaces/msg/Log', (l) => {
    mon('/rosout');
    const d = new Date((l.stamp.sec * 1000) + Math.floor(l.stamp.nanosec / 1e6));
    logs.push({ level: l.level, lv: LV[l.level] || String(l.level), name: l.name, msg: l.msg, ts: d.toTimeString().slice(0, 8) });
    if (logs.length > 1000) logs.splice(0, logs.length - 1000);
    logDirty = true;
  }, { queue: 50 });
  setInterval(() => { if (logDirty) { logDirty = false; renderLogs(); } }, 500);
  levelSel.addEventListener('change', renderLogs);
  onlyMine.addEventListener('change', renderLogs);
  $('#log-clear').addEventListener('click', () => { logs.length = 0; renderLogs(); });
});
