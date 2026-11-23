// 지도 만들기 모드: 여러 로봇이 함께 (로봇마다 slam_toolbox + 지도 합치기 /map_merged)
window.App.ready.then(async (App) => {
  'use strict';
  const { ros, $, $$, esc, toast } = App;
  const P = window.Pose2;
  const robots = App.robots;
  const norm = (f) => (f || '').replace(/^\//, '');
  robots.forEach((r) => r.start({ tf: false, odomThrottle: 100 }));

  const view = new window.RobotView($('#canvas'), { scale: 45 });
  view.emptyText = '';
  view.follow = false;
  view.state.robots = [];
  let status = { running: false };
  const slamTf = new Map();   // robot id -> {pose, t}  ('<ns>/map' → '<prefix>odom', 로봇 SLAM)
  const unsubs = [];

  // ---------- 모드 안내 ----------
  document.addEventListener('tb3:mode', (e) => {
    const b = $('#mode-banner');
    const m = e.detail.mode;
    if (m !== 'mapping' && !status.running) {
      b.innerHTML = `<b>지금 모드: ${m === 'patrol' ? '경비' : '운영'}</b><span class="muted">지도 만들기를 시작하면 모드가 '지도 만들기'로 바뀌고 경비는 멈춥니다.</span>`;
      b.classList.remove('hidden');
    } else b.classList.add('hidden');
  });

  // ---------- 1. 로봇 · 시작 자세 ----------
  const saved = JSON.parse(App.store.get('tb3.mapStart', '{}'));
  function renderRows() {
    const running = status.running;
    const rs = running ? status.robots : null;
    $('#robot-rows').innerHTML = '<div class="rrow" style="background: transparent; border: 0; padding: 0 10px"><span></span><span class="hd" style="text-align: left">로봇</span><span class="hd">x (m)</span><span class="hd">y (m)</span><span class="hd">방향 °</span></div>'
      + robots.map((r) => {
        const s = (rs && rs.find((x) => x.id === r.id)) || saved[r.id] || { x: 0, y: 0, yaw: 0 };
        // 기본은 모두 참여 (페이지를 연 직후엔 아직 온라인 판정 전이라 r.online 으로 정하지 않음, 시작할 때 오프라인이면 물어봄)
        const on = rs ? !!rs.find((x) => x.id === r.id) : (saved[r.id] ? saved[r.id].use !== false : true);
        const deg = rs ? Math.round(s.yaw * 180 / Math.PI) : (s.deg ?? 0);
        return `<div class="rrow ${r.online ? '' : 'off'}" data-id="${esc(r.id)}">
          <input type="checkbox" data-use ${on ? 'checked' : ''} ${running ? 'disabled' : ''} aria-label="${esc(r.name)} 참여">
          <span><span class="swatch" style="background: ${esc(r.color)}"></span> ${esc(r.name)} <span class="muted" style="font-size: 11px">${r.online ? esc(r.label) : '오프라인'}</span></span>
          <input type="number" step="0.1" data-k="x" value="${s.x}" ${running ? 'disabled' : ''} aria-label="${esc(r.name)} 시작 x">
          <input type="number" step="0.1" data-k="y" value="${s.y}" ${running ? 'disabled' : ''} aria-label="${esc(r.name)} 시작 y">
          <input type="number" step="15" data-k="deg" value="${deg}" ${running ? 'disabled' : ''} aria-label="${esc(r.name)} 시작 방향">
        </div>`;
      }).join('');
  }
  function readRows() {
    const out = [], keep = {};
    $$('#robot-rows [data-id]').forEach((row) => {
      const v = (k) => Number(row.querySelector(`[data-k="${k}"]`).value) || 0;
      const use = row.querySelector('[data-use]').checked;
      keep[row.dataset.id] = { x: v('x'), y: v('y'), deg: v('deg'), use };
      if (use) out.push({ id: row.dataset.id, x: v('x'), y: v('y'), yaw: v('deg') * Math.PI / 180 });
    });
    App.store.set('tb3.mapStart', JSON.stringify(keep));
    return out;
  }

  // ---------- 2. 시작 / 끝내기 ----------
  $('#start').addEventListener('click', async () => {
    const sel = readRows();
    if (!sel.length) { toast('로봇을 하나 이상 고르세요', 'warn'); return; }
    const off = sel.filter((s) => !robots.find((r) => r.id === s.id).online);
    if (off.length && !confirm(`오프라인 로봇이 있습니다: ${off.map((s) => s.id).join(', ')}. 그래도 시작할까요?`)) return;
    try {
      $('#start').disabled = true;
      if (App.mode.mode !== 'mapping') await App.adminJson('/api/mode', { method: 'POST', body: JSON.stringify({ mode: 'mapping' }) });
      await App.adminJson('/api/mapping/start', { method: 'POST', body: JSON.stringify({ robots: sel, explore: $('#explore').checked }) });
      toast(`로봇 ${sel.length}대로 지도 만들기를 시작했습니다. 지도가 나타날 때까지 몇 초 걸립니다.`, 'ok');
      App.pollMode();
      await poll();
    } catch (e) { toast(`시작 실패: ${e.message}`, 'danger', 6000); }
    $('#start').disabled = status.running;
  });
  $('#stop').addEventListener('click', async () => {
    if (!confirm('지도 만들기를 끝낼까요? 저장하지 않은 지도는 사라집니다.')) return;
    try {
      await App.adminJson('/api/mapping/stop', { method: 'POST', body: '{}' });
      await App.adminJson('/api/mode', { method: 'POST', body: JSON.stringify({ mode: 'operate' }) });
      App.pollMode();
      await poll();
    } catch (e) { toast(`실패: ${e.message}`, 'danger'); }
  });

  async function poll() {
    let s;
    try { s = await (await fetch('/api/mapping', { cache: 'no-store' })).json(); } catch (e) { return; }
    const was = status.running;
    status = s;
    if (s.running && !was) attach();
    if (!s.running && was) detach();
    $('#run-badge').textContent = s.running ? `진행 중 · ${s.robots.length}대${s.explore ? ' · 자동 탐색' : ''}` : (s.exited !== undefined ? `종료됨 (${s.exited})` : '대기');
    $('#run-badge').className = `badge ${s.running ? 'run' : ''}`;
    $('#run-time').textContent = s.running ? `${Math.floor(s.seconds / 60)}분 ${s.seconds % 60}초` : '';
    if (s.log && s.exited) $('#run-badge').title = s.log.join('');
    $('#stop').disabled = !s.running;
    $('#start').disabled = s.running;
    $('#save').disabled = !s.running;
    if (s.running !== was || !$('#robot-rows').children.length) renderRows();
    renderDrive();
  }
  setInterval(poll, 2000);

  // ---------- 지도 · 로봇 위치 ----------
  function attach() {
    unsubs.push(ros.subscribe('/map_merged', 'nav_msgs/msg/OccupancyGrid', (m) => {
      const o = m.info.origin;
      const first = !view.state.map;
      view.state.map = { x: o.position.x, y: o.position.y, th: P.yaw(o.orientation), res: m.info.resolution, canvas: window.RobotView.mapImage(m) };
      $('#map-stat').textContent = `/map_merged · ${m.info.width}×${m.info.height}`;
      if (first) fit();
      view.invalidate();
    }, { serverThrottle: 2000, compression: 'png' }));
    unsubs.push(ros.subscribe('/map_merged/stats', 'std_msgs/msg/String', (m) => {
      const s = JSON.parse(m.data);
      $('#known').textContent = `${s.known_m2.toFixed(1)} m²`;
      $('#merged').textContent = `${s.merged} / ${status.robots ? status.robots.length : '—'}`;
      const max = Math.max(1, ...Object.values(s.robots));
      $('#contrib').innerHTML = status.robots.map((sr) => {
        const r = robots.find((x) => x.id === sr.id);
        const v = s.robots[r.ns] || 0;
        return `<div class="contrib"><span class="swatch" style="background: ${esc(r.color)}"></span><span style="width: 90px">${esc(r.name)}</span><div class="bar"><div style="width: ${(v / max) * 100}%; background: ${esc(r.color)}"></div></div><span class="mono">${v.toFixed(1)} m²</span></div>`;
      }).join('');
    }, { throttle: 1000 }));
    // 로봇마다 SLAM TF ('<ns>/map' → '<prefix>odom') 를 로봇의 TF 토픽에서
    status.robots.forEach((sr) => {
      const r = robots.find((x) => x.id === sr.id);
      const parent = r.ns ? `${norm(r.ns)}/map` : 'map';
      unsubs.push(ros.subscribe(r.tfTopic, 'tf2_msgs/msg/TFMessage', (m) => {
        for (const t of m.transforms || []) {
          if (norm(t.header.frame_id) === parent && norm(t.child_frame_id) === norm(r.odomFrame)) {
            const tr = t.transform.translation;
            slamTf.set(r.id, { x: tr.x, y: tr.y, th: P.yaw(t.transform.rotation) });
          }
        }
      }));
    });
  }
  function detach() { unsubs.splice(0).forEach((u) => u()); slamTf.clear(); }

  // 공통 지도 위 자세 = 시작 자세 ∘ (로봇 SLAM 지도 → odom) ∘ odom 자세
  setInterval(() => {
    if (!status.running) { view.state.robots = []; view.invalidate(); return; }
    view.state.robots = status.robots.map((sr) => {
      const r = robots.find((x) => x.id === sr.id);
      const T = slamTf.get(r.id);
      if (!T || !r.pose) return { name: r.name, color: r.color, pose: null };
      return { name: r.name, color: r.color, selected: r.id === $('#drive-robot').value, pose: P.compose(P.compose({ x: sr.x, y: sr.y, th: sr.yaw }, T), r.pose) };
    });
    view.invalidate();
  }, 150);
  function fit() {
    const m = view.state.map;
    if (!m) return;
    const w = m.canvas.width * m.res, h = m.canvas.height * m.res;
    view.center.x = m.x + w / 2; view.center.y = m.y + h / 2;
    view.scale = Math.max(8, Math.min(view.W / w, view.H / h) * 0.9);
    view.invalidate();
  }
  $('#fit').addEventListener('click', fit);
  $('#zoom-in').addEventListener('click', () => view.zoom(1.25));
  $('#zoom-out').addEventListener('click', () => view.zoom(0.8));

  // ---------- 3. 조종 (아무 로봇이나 골라서) ----------
  function renderDrive() {
    const sel = $('#drive-robot');
    const ids = status.running ? status.robots.map((s) => s.id) : robots.map((r) => r.id);
    const cur = sel.value;
    const html = ids.map((id) => { const r = robots.find((x) => x.id === id); return `<option value="${esc(id)}">${esc(r.name)}</option>`; }).join('');
    if (sel.dataset.html !== html) { sel.innerHTML = html; sel.dataset.html = html; if (ids.includes(cur)) sel.value = cur; }
    $$('.pad button').forEach((b) => { b.disabled = !status.running || status.explore; });
    showSpd();
  }
  const driveRobot = () => robots.find((r) => r.id === $('#drive-robot').value);
  const spd = $('#spd');
  const showSpd = () => { const r = driveRobot(); $('#spd-val').textContent = r ? `${(r.limits.lin * spd.value).toFixed(2)} m/s` : ''; };
  spd.addEventListener('input', showSpd);
  const held = { f: false, b: false, l: false, r: false };
  let wasActive = null;
  const anyHeld = () => held.f || held.b || held.l || held.r;
  const markPad = () => $$('.pad [data-dir]').forEach((b) => b.classList.toggle('active', !!held[b.dataset.dir]));
  const clearHeld = () => { Object.keys(held).forEach((k) => { held[k] = false; }); markPad(); };
  setInterval(() => {
    const R = driveRobot();
    if (!R || !status.running || status.explore) return;
    if (anyHeld()) {
      if (!App.control.drive(R)) return;
      const k = Number(spd.value);
      R.cmdVel(((held.f ? 1 : 0) - (held.b ? 1 : 0)) * R.limits.lin * k, ((held.l ? 1 : 0) - (held.r ? 1 : 0)) * R.limits.ang * k * 0.5);
      wasActive = R;
    } else if (wasActive) {
      const W = wasActive;
      W.cmdVel(0, 0); setTimeout(() => { W.cmdVel(0, 0); App.control.release(W); }, 80);
      wasActive = null;
    }
  }, 100);
  $('#drive-robot').addEventListener('change', () => { if (wasActive) { wasActive.halt(); App.control.release(wasActive); wasActive = null; } clearHeld(); showSpd(); });
  $$('.pad [data-dir]').forEach((b) => {
    const d = b.dataset.dir;
    if (d === 'stop') { b.addEventListener('click', () => { clearHeld(); const R = driveRobot(); if (R) R.halt(); }); return; }
    b.addEventListener('pointerdown', (e) => { e.preventDefault(); try { b.setPointerCapture(e.pointerId); } catch (err) { /* 무시 */ } held[d] = true; markPad(); });
    const up = () => { held[d] = false; markPad(); };
    b.addEventListener('pointerup', up); b.addEventListener('pointercancel', up); b.addEventListener('lostpointercapture', up);
  });
  const KEYMAP = { w: 'f', s: 'b', a: 'l', d: 'r', arrowup: 'f', arrowdown: 'b', arrowleft: 'l', arrowright: 'r' };
  const typing = (e) => /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) && e.target.type !== 'range' && e.target.type !== 'checkbox';
  window.addEventListener('keydown', (e) => {
    if (typing(e) || e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === ' ') { e.preventDefault(); clearHeld(); const R = driveRobot(); if (R) R.halt(); return; }
    if (KEYMAP[k]) { e.preventDefault(); held[KEYMAP[k]] = true; markPad(); }
  });
  window.addEventListener('keyup', (e) => { const k = KEYMAP[e.key.toLowerCase()]; if (k) { held[k] = false; markPad(); } });
  window.addEventListener('blur', clearHeld);
  document.addEventListener('tb3:estop', () => { clearHeld(); wasActive = null; });
  document.addEventListener('tb3:control-lost', () => { clearHeld(); wasActive = null; });

  // ---------- 4. 저장 ----------
  $('#make-active').checked = true;
  $('#save').addEventListener('click', async () => {
    const name = $('#map-name').value.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$/.test(name)) { toast('지도 이름은 영문·숫자·_- 로 1~41자', 'warn'); return; }
    try {
      $('#save').disabled = true;
      const info = await App.adminJson('/api/mapping/save', { method: 'POST', body: JSON.stringify({ name }) });
      if ($('#make-active').checked) await App.setActiveMap(name);
      $('#saved').innerHTML = `<div class="check"><span class="ok">✓</span><span class="mono">config/maps/${esc(info.name)}.yaml</span><span class="muted">${info.width}×${info.height}</span></div>
        <div class="muted" style="font-size: 12px; line-height: 1.6">${$('#make-active').checked ? '활성(기준) 지도로 지정했습니다. ' : ''}다음: 끝내기 → 로봇별 Nav2 (<span class="mono">nav2_ns.launch.py map:=${esc(info.name)}</span>) → <a href="patrol.html">경비 · 순찰 경로</a> / <a href="zones.html?map=${encodeURIComponent(info.name)}">이동 구역</a><br>
        로봇마다 AMCL 초기 위치는 이번 시작 자세 + 지금까지 움직인 만큼입니다 (플릿 화면에서 초기 위치를 다시 찍어도 됨).</div>`;
      toast(`지도 ${name} 를 저장했습니다`, 'ok');
    } catch (e) { toast(`저장 실패: ${e.message}`, 'danger', 6000); }
    $('#save').disabled = !status.running;
  });

  await poll();
  renderRows();
});
