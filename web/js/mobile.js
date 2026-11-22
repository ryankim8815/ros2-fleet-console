// 모바일 원격조종: 로봇 전환, 가상 조이스틱, 속도 단계, LiDAR 미니 뷰, Pinky 표정·LED
window.App.ready.then((App) => {
  'use strict';
  const { ros, $, $$, esc, fmt, toast } = App;
  const R = App.robot;
  document.documentElement.style.setProperty('--accent', R.color);
  document.title = `${R.name} · 모바일 조종`;

  // ---- 로봇 전환 칩 (다른 로봇은 상태만 가볍게 구독) ----
  App.robots.forEach((r) => { if (r !== R) r.start({ tf: false, odomThrottle: 500 }); });   // 다른 로봇은 상태만
  function renderChips() {
    $('#robot-chips').innerHTML = App.robots.map((r) => {
      const p = r.batteryPercent();
      const sub = r.online ? `${r.label} · ${Number.isFinite(p) ? Math.round(p) : '—'}%` : '오프라인';
      return `<a href="${esc(App.robotUrl(r.id))}" aria-current="${r === R}" class="${r.online ? '' : 'off'}" style="--rc: ${esc(r.color)}"><b><span class="swatch" style="background: ${esc(r.color)}"></span>${esc(r.name)}</b><small>${esc(sub)}</small></a>`;
    }).join('');
  }
  renderChips();
  setInterval(renderChips, 2000);

  // ---- 미니 뷰 (로봇 기준, 전방이 위쪽) ----
  const view = new window.RobotView($('#mini'), { scale: 45, robotUp: true, interactive: false });
  view.state.pose = { x: 0, y: 0, th: 0 };
  view.layers.path = false;
  ros.subscribe(R.topic('scan'), 'sensor_msgs/msg/LaserScan', (m) => {
    view.state.scan = window.scanPoints(m, 1, R.sensorOffset(m.header.frame_id));
    let near = Infinity;
    m.ranges.forEach((r, i) => {
      if (r === null || !Number.isFinite(r) || r < m.range_min || r > m.range_max) return;
      let a = m.angle_min + i * m.angle_increment;
      a = Math.atan2(Math.sin(a), Math.cos(a));
      if (Math.abs(a) < 0.35 && r < near) near = r;
    });
    const el = $('#near');
    el.textContent = Number.isFinite(near) ? `${near.toFixed(2)} m` : '—';
    el.style.color = near < 0.3 ? 'var(--danger-2)' : near < 0.6 ? 'var(--warn)' : 'var(--text)';
    view.invalidate();
  }, { throttle: 150 });
  setInterval(() => { $('#v-now').textContent = fmt(R.v, 2); $('#w-now').textContent = fmt(R.w, 2); }, 200);

  // ---- Pinky 표정 · LED ----
  if (R.type === 'pinky') {
    $('#pinky-quick').classList.remove('hidden');
    const LEDS = [['#FF4D4D', 255, 0, 0], ['#FB923C', 255, 100, 0], ['#4ADE80', 0, 255, 0], ['#60A5FA', 0, 80, 255], ['#F9A8D4', 255, 60, 160]];
    $('#pinky-row').innerHTML = R.profile.emotions.slice(0, 5).map((e) => `<button class="qbtn" data-emo="${e}">${e}</button>`).join('')
      + LEDS.map(([c, r, g, b]) => `<button class="led" style="background: ${c}; flex: none" data-led="${r},${g},${b}" aria-label="LED ${c}"></button>`).join('')
      + '<button class="qbtn" data-led="off">LED 끄기</button>';
    const note = () => { $('#pinky-note').textContent = R.has('set_emotion') || R.has('set_led') ? '' : '(LED·표정 서버가 실행 중이 아님)'; };
    R.onChange(note); setTimeout(note, 2000);
    $('#pinky-row').addEventListener('click', async (e) => {
      const em = e.target.closest('[data-emo]');
      const ld = e.target.closest('[data-led]');
      try {
        if (em) { await R.call('set_emotion', { emotion: em.dataset.emo }); toast(`표정 → ${em.dataset.emo}`, 'ok', 1500); }
        if (ld) {
          const args = ld.dataset.led === 'off' ? { command: 'clear', pixels: [], r: 0, g: 0, b: 0 }
            : (([r, g, b]) => ({ command: 'fill', pixels: [], r, g, b }))(ld.dataset.led.split(',').map(Number));
          await R.call('set_led', args);
        }
      } catch (err) { toast(`실패: ${err.message}`, 'warn'); }
    });
  }

  // ---- 속도 단계 ----
  let factor = 0.65;
  $$('[data-speed]').forEach((b) => b.addEventListener('click', () => {
    factor = Number(b.dataset.speed);
    $$('[data-speed]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
  }));

  // ---- 조이스틱 ----
  const joy = $('#joy'), knob = $('#knob');
  let input = null, pointer = null;
  const setKnob = (dx, dy) => { knob.style.transform = `translate(${dx}px, ${dy}px)`; };
  function fromEvent(e) {
    const r = joy.getBoundingClientRect();
    const Rr = r.width / 2 - 32;
    let dx = e.clientX - (r.left + r.width / 2), dy = e.clientY - (r.top + r.height / 2);
    const d = Math.hypot(dx, dy);
    if (d > Rr) { dx = dx / d * Rr; dy = dy / d * Rr; }
    setKnob(dx, dy);
    const dead = 0.12, nx = dx / Rr, ny = -dy / Rr;
    input = { x: Math.abs(nx) < dead ? 0 : nx, y: Math.abs(ny) < dead ? 0 : ny };
  }
  function release() {
    const was = !!input;
    pointer = null; input = null;
    joy.classList.remove('active');
    setKnob(0, 0);
    if (was && App.control.mine(R)) { R.cmdVel(0, 0); setTimeout(() => { R.cmdVel(0, 0); App.control.release(R); }, 80); }
  }
  document.addEventListener('tb3:control-lost', () => { pointer = null; input = null; joy.classList.remove('active'); setKnob(0, 0); });
  joy.addEventListener('pointerdown', (e) => { e.preventDefault(); pointer = e.pointerId; try { joy.setPointerCapture(e.pointerId); } catch (err) { /* 무시 */ } joy.classList.add('active'); fromEvent(e); });
  joy.addEventListener('pointermove', (e) => { if (e.pointerId === pointer) fromEvent(e); });
  joy.addEventListener('pointerup', (e) => { if (e.pointerId === pointer) release(); });
  joy.addEventListener('pointercancel', release);
  joy.addEventListener('lostpointercapture', () => { if (pointer !== null) release(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden && input) release(); });
  document.addEventListener('tb3:estop', () => { pointer = null; input = null; joy.classList.remove('active'); setKnob(0, 0); });
  $('#stop-one').addEventListener('click', () => { input = null; setKnob(0, 0); R.halt(); toast(`${R.name} 정지`, 'warn', 1500); });

  setInterval(() => {
    if (!input) return;
    if (!App.control.drive(R)) return;   // 다른 브라우저가 조종 중
    R.cmdVel(input.y * R.limits.lin * factor, -input.x * R.limits.ang * factor * 0.6);
  }, 100);
});
