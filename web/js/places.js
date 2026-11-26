// 지점: 저장된 지도 위에 좌표를 찍고 이름을 붙여 SQLite(data/fleet.db)에 저장
window.App.ready.then(async (App) => {
  'use strict';
  const { $, esc, toast } = App;
  const robots = App.robots;
  robots.forEach((r) => r.start({ tf: true, odomThrottle: 200 }));

  const view = new window.RobotView($('#canvas'), { scale: 60 });
  view.follow = false;
  view.state.robots = [];
  view.emptyText = '';
  let maps = [];
  let current = null;            // 지도 정보
  let places = [];               // [{id, map, name, x, y, yaw, note}]
  let selected = null;           // 선택한 지점 id
  let pick = null;               // null | {kind: 'new'} | {kind: 'move', id}
  let draft = null;              // 새 지점 미리보기 {x, y, yaw}
  let exitPt = null;             // 이 지도의 출구 {x, y} (장애물 처리 때 장애물을 옮겨 둘 곳)

  const deg = (rad) => Math.round((rad * 180) / Math.PI);
  const rad = (d) => (Number(d) * Math.PI) / 180;
  const api = (url, method, body) => App.adminJson(url, { method, body: body ? JSON.stringify(body) : undefined });

  // ---------- 지도 ----------
  async function loadMaps() {
    maps = await (await fetch('/api/maps', { cache: 'no-store' })).json();
    const sel = $('#map-pick');
    if (!maps.length) {
      sel.innerHTML = '<option value="">저장된 지도 없음</option>';
      $('#map-meta').innerHTML = '저장된 지도가 없습니다 → <a href="mapping.html">지도 만들기</a>';
      $('#add-btn').disabled = true;
      render();
      return;
    }
    sel.innerHTML = maps.map((m) => `<option value="${esc(m.name)}">${esc(m.name)}${m.name === App.fleet.mapFile ? ' (활성)' : ''}</option>`).join('');
    const want = new URLSearchParams(location.search).get('map') || App.fleet.mapFile || maps[0].name;
    sel.value = maps.some((m) => m.name === want) ? want : maps[0].name;
    await openMap(sel.value);
  }
  $('#map-pick').addEventListener('change', (e) => openMap(e.target.value));

  async function openMap(name) {
    current = maps.find((m) => m.name === name);
    if (!current) return;
    stopPick();
    closeNew();
    view.state.map = await window.RobotView.mapFromPng(`/api/maps/${encodeURIComponent(name)}.png?t=${current.mtime}`, current);
    view.center.x = current.origin[0] + (current.width * current.resolution) / 2;
    view.center.y = current.origin[1] + (current.height * current.resolution) / 2;
    const fit = Math.min(view.W / (current.width * current.resolution), view.H / (current.height * current.resolution));
    view.scale = Math.max(8, fit * 0.9);
    $('#map-meta').textContent = `${current.width}×${current.height} · ${current.resolution.toFixed(3)} m`;
    const isActive = name === App.fleet.mapFile;
    $('#active-badge').textContent = isActive ? '활성 지도' : '활성 아님';
    $('#active-badge').className = `badge ${isActive ? 'ok' : 'warn'}`;
    view.state.zones = isActive ? (App.zones.zones || []).map((z) => ({ ...z, dim: true })) : [];
    selected = null;
    await loadExit();
    await loadPlaces();
  }

  // ---------- 출구 (지도마다 하나, 장애물 처리 때 로봇들이 장애물을 밀어 옮겨 둘 곳) ----------
  async function loadExit() {
    exitPt = null;
    try { exitPt = (await (await fetch(`/api/maps/${encodeURIComponent(current.name)}/exit`, { cache: 'no-store' })).json()).exit; } catch (e) { /* 없음 */ }
    view.state.exit = exitPt ? { x: exitPt.x, y: exitPt.y, radius: 1.0 } : null;
    $('#exit-clear').classList.toggle('hidden', !exitPt);
    $('#exit-btn').lastChild.textContent = exitPt ? '출구 다시 지정' : '출구 지정';
    const note = $('#exit-note');
    note.innerHTML = exitPt
      ? `<b>출구</b><span class="muted">(${exitPt.x.toFixed(2)}, ${exitPt.y.toFixed(2)}) · 운영 중 경비가 지도에 없던 장애물을 찾으면 워커·경비가 모여 이곳으로 밀어 옮깁니다. 출구 둘레 1 m 는 경비가 장애물로 보지 않습니다.</span>`
      : '<b>출구가 없습니다.</b><span class="muted">출구를 지정해야 장애물 처리 모드가 동작합니다 (없으면 경비가 장애물 앞에서 관제실을 기다림). <b>출구 지정</b>을 누르고 지도의 빈 곳을 클릭하세요.</span>';
    note.classList.remove('hidden');
    view.invalidate();
  }
  async function saveExit(x, y) {
    try {
      await api(`/api/maps/${encodeURIComponent(current.name)}/exit`, 'PUT', { x: round(x), y: round(y) });
      toast(`출구를 (${x.toFixed(2)}, ${y.toFixed(2)}) 에 지정했습니다`, 'ok');
      if (new URLSearchParams(location.search).get('exit')) history.replaceState(null, '', `?map=${encodeURIComponent(current.name)}`);
    } catch (e) { toast(`출구를 저장하지 못했습니다: ${e.message}`, 'danger', 6000); }
    await loadExit();
  }
  $('#exit-btn').addEventListener('click', () => (pick && pick.kind === 'exit' ? stopPick() : startPick({ kind: 'exit' })));
  $('#exit-clear').addEventListener('click', async () => {
    if (!confirm('출구를 지울까요? 출구가 없으면 장애물 처리 모드가 동작하지 않습니다.')) return;
    try { await api(`/api/maps/${encodeURIComponent(current.name)}/exit`, 'DELETE'); toast('출구를 지웠습니다', 'warn'); } catch (e) { toast(`실패: ${e.message}`, 'danger'); }
    await loadExit();
  });

  async function loadPlaces() {
    const r = await fetch(`/api/places?map=${encodeURIComponent(current.name)}`, { cache: 'no-store' });
    const body = await r.json();
    if (!r.ok) { toast(`지점을 읽지 못했습니다: ${body.error || r.status}`, 'danger'); places = []; }
    else places = body.places;
    render();
  }

  // ---------- 지도에서 찍기 ----------
  // 클릭 = 그 자리, 클릭한 채 끌기 = 끈 방향이 도착 방향 (플릿의 목표 지정과 같음)
  function startPick(p) {
    if (!current) { toast('먼저 지도를 고르세요', 'warn'); return; }
    pick = p;
    view.goalMode = true;
    const target = p.kind === 'move' ? places.find((x) => x.id === p.id) : null;
    $('#pick-hint').textContent = p.kind === 'exit' ? '출구를 둘 곳(지도의 빈 곳)을 클릭 · Esc 취소'
      : target ? `"${target.name}" 의 새 위치를 클릭 (끌면 방향) · Esc 취소`
        : '지점을 둘 곳을 클릭 (끌면 도착 방향) · Esc 취소';
    $('#pick-hint').classList.remove('hidden');
    $('#add-btn').setAttribute('aria-pressed', String(p.kind === 'new'));
    $('#exit-btn').setAttribute('aria-pressed', String(p.kind === 'exit'));
  }
  function stopPick() {
    pick = null;
    view.goalMode = false;
    $('#pick-hint').classList.add('hidden');
    $('#add-btn').setAttribute('aria-pressed', 'false');
    $('#exit-btn').setAttribute('aria-pressed', 'false');
  }
  view.onGoal = async (x, y, th) => {
    const p = pick;
    stopPick();
    if (!p) return;
    if (p.kind === 'exit') { await saveExit(x, y); return; }
    if (p.kind === 'new') { openNew(x, y, th); return; }
    const pl = places.find((q) => q.id === p.id);
    if (pl) await savePlace(pl, { x: round(x), y: round(y), yaw: th });
  };
  // 찍기 모드가 아닐 때 클릭: 가까운 지점 선택
  view.onPointClick = (x, y) => {
    const [px, py] = view.toScreen(x, y);
    let best = null, bd = 14;
    for (const p of places) {
      const [sx, sy] = view.toScreen(p.x, p.y);
      const d = Math.hypot(sx - px, sy - py);
      if (d < bd) { bd = d; best = p; }
    }
    selected = best ? best.id : null;
    render();
    if (best) { const el = document.querySelector(`[data-place="${best.id}"]`); if (el) el.scrollIntoView({ block: 'nearest' }); }
  };
  const round = (v) => Math.round(v * 1000) / 1000;

  $('#add-btn').addEventListener('click', () => (pick && pick.kind === 'new' ? stopPick() : startPick({ kind: 'new' })));
  window.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (pick) { e.stopImmediatePropagation(); stopPick(); return; }
    if (draft && !e.target.closest('input')) { e.stopImmediatePropagation(); closeNew(); }
  }, true);
  $('#zoom-in').addEventListener('click', () => view.zoom(1.25));
  $('#zoom-out').addEventListener('click', () => view.zoom(0.8));

  // ---------- 로봇 현재 위치로 ----------
  const fromRobot = $('#from-robot');
  fromRobot.innerHTML += robots.map((r) => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join('');
  fromRobot.addEventListener('change', () => {
    const r = robots.find((x) => x.id === fromRobot.value);
    fromRobot.value = '';
    if (!r || !current) return;
    if (current.name !== App.fleet.mapFile) { toast('로봇 위치는 활성 지도 기준입니다. 활성 지도를 고르세요', 'warn', 5000); return; }
    const p = r.mapPose;
    if (!p) { toast(`${r.name}: 지도 위 위치를 모릅니다 (AMCL·SLAM 의 map→odom TF 없음)`, 'warn', 5000); return; }
    openNew(p.x, p.y, p.th);
  });

  // ---------- 새 지점 ----------
  function openNew(x, y, yaw) {
    draft = { x: round(x), y: round(y), yaw };
    $('#new-x').value = draft.x.toFixed(2);
    $('#new-y').value = draft.y.toFixed(2);
    $('#new-deg').value = deg(yaw);
    $('#new-name').value = '';
    $('#new-note').value = '';
    $('#new-panel').classList.remove('hidden');
    $('#new-name').focus();
    render();
  }
  function closeNew() {
    draft = null;
    $('#new-panel').classList.add('hidden');
    render();
  }
  $('#new-cancel').addEventListener('click', closeNew);
  $('#new-name').addEventListener('input', render);   // 지도 위 미리보기 이름
  ['#new-x', '#new-y', '#new-deg'].forEach((s) => $(s).addEventListener('input', () => {
    if (!draft) return;
    const x = Number($('#new-x').value), y = Number($('#new-y').value), d = Number($('#new-deg').value);
    if (Number.isFinite(x)) draft.x = x;
    if (Number.isFinite(y)) draft.y = y;
    if (Number.isFinite(d)) draft.yaw = rad(d);
    render();
  }));
  $('#new-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = $('#new-name').value.trim();
    if (!name) { $('#new-name').focus(); return; }
    const body = {
      map: current.name, name, note: $('#new-note').value.trim(),
      x: Number($('#new-x').value), y: Number($('#new-y').value), yaw: rad($('#new-deg').value || 0),
    };
    if (!Number.isFinite(body.x) || !Number.isFinite(body.y) || !Number.isFinite(body.yaw)) { toast('좌표를 숫자로 넣으세요', 'warn'); return; }
    try {
      const p = await api('/api/places', 'POST', body);
      places.push(p);
      places.sort((a, b) => a.name.localeCompare(b.name, 'ko'));
      selected = p.id;
      closeNew();
      toast(`"${p.name}" 지점을 저장했습니다`, 'ok');
    } catch (err) { toast(`저장 실패: ${err.message}`, 'danger', 6000); }
  });

  // ---------- 목록 ----------
  $('#filter').addEventListener('input', render);

  async function savePlace(p, patch) {
    try {
      const out = await api(`/api/places/${p.id}`, 'PUT', patch);
      Object.assign(p, out);
      selected = p.id;
      toast(`"${p.name}" 을(를) 고쳤습니다`, 'ok', 2000);
    } catch (err) { toast(`저장 실패: ${err.message}`, 'danger', 6000); }
    render();
  }

  function render() {
    const f = $('#filter').value.trim().toLowerCase();
    view.state.places = [
      ...places.map((p) => ({ ...p, selected: p.id === selected, dim: f && !p.name.toLowerCase().includes(f) })),
      ...(draft ? [{ ...draft, name: $('#new-name').value.trim() || '새 지점', selected: true }] : []),
    ];
    view.invalidate();
    $('#place-count').textContent = places.length;
    const shown = places.filter((p) => !f || p.name.toLowerCase().includes(f));
    if (!current) { $('#place-list').innerHTML = '<div class="empty">저장된 지도가 없습니다.</div>'; return; }
    $('#place-list').innerHTML = shown.length ? shown.map((p) => `
      <div class="item" data-place="${p.id}" style="flex-direction: column; align-items: stretch; gap: 8px; ${p.id === selected ? 'outline: 1px solid var(--warn)' : ''}">
        <div style="display: flex; gap: 8px; align-items: center">
          <input data-k="name" value="${esc(p.name)}" maxlength="40" aria-label="지점 이름" style="flex: 1 1 auto; min-width: 0">
          <button class="btn btn-sm" data-act="show">보기</button>
          <button class="btn btn-sm" data-act="move" title="지도에서 위치 다시 찍기">다시 찍기</button>
          <button class="btn btn-sm btn-danger-outline" data-act="del" aria-label="${esc(p.name)} 삭제">삭제</button>
        </div>
        <div class="place-grid">
          <label>x (m)<input type="number" step="0.01" data-k="x" value="${p.x.toFixed(2)}"></label>
          <label>y (m)<input type="number" step="0.01" data-k="y" value="${p.y.toFixed(2)}"></label>
          <label>방향 (°)<input type="number" step="1" data-k="deg" value="${deg(p.yaw)}"></label>
        </div>
        <input data-k="note" value="${esc(p.note || '')}" maxlength="200" placeholder="메모" aria-label="${esc(p.name)} 메모" style="font-size: 12px">
      </div>`).join('')
      : `<div class="empty">${places.length ? '검색과 맞는 지점이 없습니다.' : '지점이 없습니다. <b>지도에 지점 찍기</b>를 누르고 지도를 클릭하세요.'}</div>`;
  }

  $('#place-list').addEventListener('click', async (e) => {
    const item = e.target.closest('[data-place]'); if (!item) return;
    const p = places.find((x) => x.id === Number(item.dataset.place)); if (!p) return;
    const act = e.target.closest('[data-act]')?.dataset.act;
    if (!act) { if (selected !== p.id) { selected = p.id; render(); } return; }
    if (act === 'show') { selected = p.id; view.center.x = p.x; view.center.y = p.y; render(); }
    if (act === 'move') { selected = p.id; render(); startPick({ kind: 'move', id: p.id }); }
    if (act === 'del') {
      if (!confirm(`"${p.name}" 지점을 지울까요? 되돌릴 수 없습니다.`)) return;
      try {
        await api(`/api/places/${p.id}`, 'DELETE');
        places = places.filter((x) => x !== p);
        if (selected === p.id) selected = null;
        toast(`"${p.name}" 을(를) 지웠습니다`, 'ok', 2000);
        render();
      } catch (err) { toast(`삭제 실패: ${err.message}`, 'danger', 6000); }
    }
  });
  // 값을 바꾸고 포커스를 옮기거나 Enter 를 누르면 바로 저장
  $('#place-list').addEventListener('change', async (e) => {
    const item = e.target.closest('[data-place]'); if (!item) return;
    const p = places.find((x) => x.id === Number(item.dataset.place)); if (!p) return;
    const k = e.target.dataset.k;
    const v = e.target.value.trim();
    if (k === 'name') {
      if (!v) { e.target.value = p.name; return; }
      await savePlace(p, { name: v });
    } else if (k === 'note') await savePlace(p, { note: v });
    else {
      const n = Number(v);
      if (v === '' || !Number.isFinite(n)) { toast('숫자를 넣으세요', 'warn'); render(); return; }
      await savePlace(p, k === 'deg' ? { yaw: rad(n) } : { [k]: n });
    }
  });

  // ---------- 로봇 위치 표시 ----------
  setInterval(() => {
    const active = current && current.name === App.fleet.mapFile;
    view.state.robots = active ? robots.filter((r) => r.online && r.mapPose).map((r) => ({ name: r.name, color: r.color, pose: r.mapPose })) : [];
    view.invalidate();
  }, 300);

  await loadMaps();
  // 지도 만들기를 끝내고 넘어온 경우(?exit=1): 바로 출구 찍기
  if (new URLSearchParams(location.search).get('exit') && current) startPick({ kind: 'exit' });
});
