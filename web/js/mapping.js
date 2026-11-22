// 지도 작성: 로봇 한 대를 골라 PC 에서 slam_toolbox 실행 → 조종하며 지도 확장 → 저장 → 활성 지도로 지정
window.App.ready.then(async (App) => {
  'use strict';
  const { ros, $, $$, esc, fmt, toast } = App;
  const robots = App.robots;
  robots.forEach((r) => r.start({ tf: true, odomThrottle: 100 }));

  const view = new window.RobotView($('#canvas'), { scale: 50 });
  view.emptyText = '';
  view.state.robots = [];
  let pickId = new URLSearchParams(location.search).get('robot') || '';
  let status = { running: false };
  let R = null;            // 지도 작성 중인 로봇
  let unsubMap = null;

  // ---------- 1. 로봇 고르기 ----------
  function renderPick() {
    $('#robot-pick').innerHTML = robots.map((r) => {
      const busy = status.running && status.robot && status.robot.id !== r.id;
      return `<label class="${r.online && !busy ? '' : 'off'}"><input type="radio" name="robot" value="${esc(r.id)}" ${pickId === r.id ? 'checked' : ''} ${r.online && !status.running ? '' : 'disabled'}>
        <span class="swatch" style="background: ${esc(r.color)}"></span><span style="flex: 1 1 auto">${esc(r.name)} <span class="muted" style="font-size: 12px">${esc(r.label)} · ${esc(r.ns || '(ns 없음)')}</span></span>
        <span class="badge ${r.online ? 'ok' : ''}">${r.online ? '온라인' : '오프라인'}</span></label>`;
    }).join('');
  }
  $('#robot-pick').addEventListener('change', (e) => { pickId = e.target.value; steps(); });

  // ---------- 2. 시작 / 끝내기 ----------
  $('#start').addEventListener('click', async () => {
    if (!pickId) { toast('로봇을 고르세요', 'warn'); return; }
    try {
      $('#start').disabled = true;
      await App.adminJson('/api/mapping/start', { method: 'POST', body: JSON.stringify({ robot: pickId }) });
      toast('slam_toolbox 를 시작했습니다. 지도가 나타날 때까지 몇 초 걸립니다.', 'ok');
      await poll();
    } catch (e) { toast(`시작 실패: ${e.message}`, 'danger', 6000); }
    $('#start').disabled = false;
  });
  $('#stop').addEventListener('click', async () => {
    if (!confirm('지도 작성을 끝낼까요? 저장하지 않은 지도는 사라집니다.')) return;
    try { await App.adminJson('/api/mapping/stop', { method: 'POST', body: '{}' }); await poll(); } catch (e) { toast(`실패: ${e.message}`, 'danger'); }
  });

  async function poll() {
    try { status = await (await fetch('/api/mapping', { cache: 'no-store' })).json(); } catch (e) { return; }
    const id = status.running && status.robot ? status.robot.id : null;
    if (id && (!R || R.id !== id)) attach(robots.find((r) => r.id === id));
    if (!id && R) detach();
    if (id) pickId = id;
    $('#run-state').textContent = status.running ? `실행 중 · ${status.robot.name} · ${status.seconds}초` : (status.exited !== undefined ? `종료됨 (코드 ${status.exited})` : '대기');
    if (status.log && status.log.length && status.exited) $('#run-state').title = status.log.join('');
    steps();
  }
  setInterval(poll, 2000);

  // ---------- 지도 표시 ----------
  function attach(r) {
    R = r;
    unsubMap = ros.subscribe(r.topic('map'), 'nav_msgs/msg/OccupancyGrid', (m) => {
      const o = m.info.origin;
      view.state.map = { x: o.position.x, y: o.position.y, th: window.Pose2.yaw(o.orientation), res: m.info.resolution, canvas: window.RobotView.mapImage(m) };
      const known = m.data.reduce((a, v) => a + (v >= 0 ? 1 : 0), 0);
      $('#map-stat').textContent = `${r.topic('map')} · ${m.info.width}×${m.info.height} · 탐색 ${(known * m.info.resolution * m.info.resolution).toFixed(1)} m²`;
      view.invalidate();
    }, { serverThrottle: 2000, compression: 'png' });
  }
  function detach() {
    if (unsubMap) unsubMap();
    unsubMap = null; R = null;
  }
  setInterval(() => {
    view.state.robots = R && R.mapPose ? [{ name: R.name, color: R.color, pose: R.mapPose, selected: true }] : [];
    view.invalidate();
  }, 100);
  $('#zoom-in').addEventListener('click', () => view.zoom(1.25));
  $('#zoom-out').addEventListener('click', () => view.zoom(0.8));
  $('#follow-btn').addEventListener('click', () => view.setFollow(!view.follow));
  view.onChange = () => $('#follow-btn').setAttribute('aria-pressed', String(view.follow));

  // ---------- 3. 조종 ----------
  const spd = $('#spd');
  const showSpd = () => { const r = R || robots.find((x) => x.id === pickId); $('#spd-val').textContent = r ? `${(r.limits.lin * spd.value).toFixed(2)} m/s` : ''; };
  spd.addEventListener('input', showSpd);
  const held = { f: false, b: false, l: false, r: false };
  let wasActive = false;
  const anyHeld = () => held.f || held.b || held.l || held.r;
  const markPad = () => $$('.pad [data-dir]').forEach((b) => b.classList.toggle('active', !!held[b.dataset.dir]));
  setInterval(() => {
    if (!R) return;
    if (anyHeld()) {
      if (!App.control.drive(R)) return;
      const k = Number(spd.value);
      R.cmdVel(((held.f ? 1 : 0) - (held.b ? 1 : 0)) * R.limits.lin * k, ((held.l ? 1 : 0) - (held.r ? 1 : 0)) * R.limits.ang * k * 0.5);
      wasActive = true;
    } else if (wasActive) {
      R.cmdVel(0, 0); setTimeout(() => { R.cmdVel(0, 0); App.control.release(R); }, 80);
      wasActive = false;
    }
  }, 100);
  $$('.pad [data-dir]').forEach((b) => {
    const d = b.dataset.dir;
    if (d === 'stop') { b.addEventListener('click', () => { Object.keys(held).forEach((k) => { held[k] = false; }); markPad(); if (R) R.halt(); }); return; }
    b.addEventListener('pointerdown', (e) => { e.preventDefault(); try { b.setPointerCapture(e.pointerId); } catch (err) { /* 무시 */ } held[d] = true; markPad(); });
    const up = () => { held[d] = false; markPad(); };
    b.addEventListener('pointerup', up); b.addEventListener('pointercancel', up); b.addEventListener('lostpointercapture', up);
  });
  const KEYMAP = { w: 'f', s: 'b', a: 'l', d: 'r', arrowup: 'f', arrowdown: 'b', arrowleft: 'l', arrowright: 'r' };
  const typing = (e) => /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) && e.target.type !== 'range';
  window.addEventListener('keydown', (e) => {
    if (typing(e) || e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === ' ') { e.preventDefault(); Object.keys(held).forEach((x) => { held[x] = false; }); markPad(); if (R) R.halt(); return; }
    if (KEYMAP[k]) { e.preventDefault(); held[KEYMAP[k]] = true; markPad(); }
  });
  window.addEventListener('keyup', (e) => { const k = KEYMAP[e.key.toLowerCase()]; if (k) { held[k] = false; markPad(); } });
  window.addEventListener('blur', () => { Object.keys(held).forEach((k) => { held[k] = false; }); markPad(); });
  document.addEventListener('tb3:estop', () => { Object.keys(held).forEach((k) => { held[k] = false; }); markPad(); wasActive = false; });
  document.addEventListener('tb3:control-lost', () => { Object.keys(held).forEach((k) => { held[k] = false; }); markPad(); wasActive = false; });

  // ---------- 4. 저장 ----------
  $('#make-active').checked = !App.fleet.mapFile;
  $('#save').addEventListener('click', async () => {
    const name = $('#map-name').value.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$/.test(name)) { toast('지도 이름은 영문·숫자·_- 로 1~41자', 'warn'); return; }
    try {
      $('#save').disabled = true;
      const info = await App.adminJson('/api/mapping/save', { method: 'POST', body: JSON.stringify({ name }) });
      if ($('#make-active').checked) await App.setActiveMap(name);
      $('#saved').innerHTML = `<div class="check"><span class="ok">✓</span><span class="mono">config/maps/${esc(info.name)}.yaml</span><span class="muted">${info.width}×${info.height}</span></div>
        <div class="muted" style="font-size: 12px; line-height: 1.6">${$('#make-active').checked ? '활성 지도로 지정했습니다. ' : ''}다음: <a href="zones.html?map=${encodeURIComponent(info.name)}">이동 구역 그리기</a> · 로봇별 Nav2 실행<br>
        <span class="mono">pixi run ros2 launch ros2-fleet-console/robot/nav2_ns.launch.py namespace:=&lt;ns&gt; map:=${esc(info.name)}</span></div>`;
      toast(`지도 ${name} 를 저장했습니다`, 'ok');
    } catch (e) { toast(`저장 실패: ${e.message}`, 'danger', 6000); }
    $('#save').disabled = !status.running;
  });

  // ---------- 단계 표시 ----------
  function steps() {
    renderPick();
    const running = status.running;
    $('#start').disabled = running || !pickId;
    $('#stop').disabled = !running;
    $('#save').disabled = !running;
    $$('.pad button').forEach((b) => { b.disabled = !running; });
    const st = (id, s) => { $(id).dataset.state = s; };
    st('#s1', pickId ? 'done' : 'now');
    st('#s2', running ? 'done' : pickId ? 'now' : '');
    st('#s3', running ? 'now' : '');
    st('#s4', running ? 'now' : $('#saved').innerHTML ? 'done' : '');
    showSpd();
  }
  setInterval(renderPick, 3000);
  await poll();
});
