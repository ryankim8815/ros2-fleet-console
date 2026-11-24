// 이동 경로: 지점을 순서대로 이은 경로 CRUD + 로봇별 경로 할당(반복 횟수)·시작/멈춤/이어서/정지 (data/fleet.db)
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
  let places = [];               // 이 지도의 지점
  let routes = [];               // 이 지도의 경로
  let assigns = [];              // 모든 로봇의 할당
  let runner = { running: false };
  let mode = 'operate';
  let selected = null;           // 선택한 경로 id
  let edit = null;               // 편집 중 {id|null, name, note, points: [place_id]}
  let editDirty = false;

  const api = (url, method, body) => App.adminJson(url, { method, body: body ? JSON.stringify(body) : undefined });
  const placeById = (id) => places.find((p) => p.id === id);
  const routeById = (id) => routes.find((r) => r.id === id);
  window.addEventListener('beforeunload', (e) => { if (editDirty) { e.preventDefault(); e.returnValue = ''; } });

  // ---------- 지도 ----------
  async function loadMaps() {
    maps = await (await fetch('/api/maps', { cache: 'no-store' })).json();
    const sel = $('#map-pick');
    if (!maps.length) {
      sel.innerHTML = '<option value="">저장된 지도 없음</option>';
      $('#map-meta').innerHTML = '저장된 지도가 없습니다 → <a href="mapping.html">지도 만들기</a>';
      $('#new-route').disabled = true;
      render();
      return;
    }
    sel.innerHTML = maps.map((m) => `<option value="${esc(m.name)}">${esc(m.name)}${m.name === App.fleet.mapFile ? ' (활성)' : ''}</option>`).join('');
    const want = new URLSearchParams(location.search).get('map') || App.fleet.mapFile || maps[0].name;
    sel.value = maps.some((m) => m.name === want) ? want : maps[0].name;
    await openMap(sel.value);
  }
  $('#map-pick').addEventListener('change', async (e) => {
    if (editDirty && !confirm('저장하지 않은 경로 변경이 있습니다. 다른 지도로 바꿀까요?')) { e.target.value = current.name; return; }
    await openMap(e.target.value);
  });

  async function openMap(name) {
    current = maps.find((m) => m.name === name);
    if (!current) return;
    closeEditor(true);
    view.state.map = await window.RobotView.mapFromPng(`/api/maps/${encodeURIComponent(name)}.png?t=${current.mtime}`, current);
    view.center.x = current.origin[0] + (current.width * current.resolution) / 2;
    view.center.y = current.origin[1] + (current.height * current.resolution) / 2;
    const fit = Math.min(view.W / (current.width * current.resolution), view.H / (current.height * current.resolution));
    view.scale = Math.max(8, fit * 0.9);
    $('#map-meta').textContent = `${current.width}×${current.height} · ${current.resolution.toFixed(3)} m`;
    const isActive = name === App.fleet.mapFile;
    $('#active-badge').textContent = isActive ? '활성 지도' : '활성 아님 (시작 불가)';
    $('#active-badge').className = `badge ${isActive ? 'ok' : 'warn'}`;
    selected = null;
    await loadData();
  }

  async function loadData() {
    const q = `map=${encodeURIComponent(current.name)}`;
    const [p, r] = await Promise.all([
      fetch(`/api/places?${q}`, { cache: 'no-store' }).then((x) => x.json()),
      fetch(`/api/routes?${q}`, { cache: 'no-store' }).then((x) => x.json()),
    ]);
    places = p.places || [];
    routes = r.routes || [];
    if (selected && !routeById(selected)) selected = null;
    await loadAssigns();
    render();
  }

  async function loadAssigns() {
    try {
      const a = await (await fetch('/api/assignments', { cache: 'no-store' })).json();
      assigns = a.assignments || [];
      runner = a.runner || { running: false };
      mode = a.mode;
    } catch (e) { /* 다음에 다시 */ }
  }

  // ---------- 지도 그리기 ----------
  function drawMap() {
    const pts = edit ? edit.points : (routeById(selected)?.points || []).map((p) => p.place_id);
    const inRoute = new Set(pts);
    view.state.places = places.map((p) => ({ x: p.x, y: p.y, yaw: p.yaw, name: p.name, dim: !!(edit || selected) && !inRoute.has(p.id) }));
    const line = pts.map(placeById).filter(Boolean).map((p) => [p.x, p.y]);
    view.state.routes = line.length ? [{ points: line, color: '#FBBF24', loop: true, highlight: true }] : [];
    view.invalidate();
  }

  // 지점 클릭: 편집 중이면 경로 끝에 추가, 아니면 그 지점을 쓰는 경로 선택
  view.onPointClick = (x, y) => {
    const [px, py] = view.toScreen(x, y);
    let best = null, bd = 14;
    for (const p of places) {
      const [sx, sy] = view.toScreen(p.x, p.y);
      const d = Math.hypot(sx - px, sy - py);
      if (d < bd) { bd = d; best = p; }
    }
    if (!best) return;
    if (edit) { addPoint(best.id); return; }
    const r = routes.find((rt) => rt.points.some((q) => q.place_id === best.id));
    if (r) { selected = r.id; render(); }
  };
  $('#zoom-in').addEventListener('click', () => view.zoom(1.25));
  $('#zoom-out').addEventListener('click', () => view.zoom(0.8));

  // ---------- 경로 목록 ----------
  function renderRoutes() {
    $('#route-count').textContent = routes.length;
    if (!current) { $('#route-list').innerHTML = '<div class="empty">저장된 지도가 없습니다.</div>'; return; }
    if (!places.length) {
      $('#route-list').innerHTML = `<div class="empty">이 지도에 지점이 없습니다. 먼저 <a href="places.html?map=${encodeURIComponent(current.name)}">지점</a>을 만드세요.</div>`;
      $('#new-route').disabled = true;
      return;
    }
    $('#new-route').disabled = false;
    $('#route-list').innerHTML = routes.length ? routes.map((r) => {
      const who = r.robots.map((id) => robots.find((x) => x.id === id)).filter(Boolean);
      return `<button class="item" data-route="${r.id}" style="text-align: left; border: 0; cursor: pointer; ${r.id === (edit ? edit.id : selected) ? 'outline: 1px solid var(--warn)' : ''}">
        <div class="main">
          <div style="font-weight: 600">${esc(r.name)}</div>
          <div class="ty">${r.points.map((p) => esc(p.name)).join(' → ')} → ↺</div>
        </div>
        ${who.map((x) => `<span class="swatch" title="${esc(x.name)}" style="background: ${esc(x.color)}"></span>`).join('')}
      </button>`;
    }).join('') : '<div class="empty">경로가 없습니다. <b>+ 새 경로</b>를 누르고 지도에서 지점을 차례로 클릭하세요.</div>';
  }
  $('#route-list').addEventListener('click', (e) => {
    const b = e.target.closest('[data-route]'); if (!b) return;
    if (editDirty && !confirm('저장하지 않은 경로 변경이 있습니다. 버릴까요?')) return;
    const r = routeById(Number(b.dataset.route));
    selected = r.id;
    openEditor(r);
  });
  $('#new-route').addEventListener('click', () => {
    if (editDirty && !confirm('저장하지 않은 경로 변경이 있습니다. 버릴까요?')) return;
    selected = null;
    let n = routes.length + 1;
    while (routes.some((r) => r.name === `경로 ${n}`)) n += 1;
    openEditor({ id: null, name: `경로 ${n}`, note: '', points: [] });
    $('#r-name').select();
  });

  // ---------- 경로 편집 ----------
  function openEditor(r) {
    edit = { id: r.id, name: r.name, note: r.note || '', points: r.points.map((p) => (typeof p === 'number' ? p : p.place_id)) };
    editDirty = false;
    $('#editor').classList.remove('hidden');
    $('#edit-hint').classList.remove('hidden');
    $('#editor-title').textContent = r.id ? '경로 편집' : '새 경로';
    $('#r-name').value = edit.name;
    $('#r-note').value = edit.note;
    $('#r-delete').classList.toggle('hidden', !r.id);
    render();
  }
  function closeEditor(force) {
    if (!force && editDirty && !confirm('저장하지 않은 변경을 버릴까요?')) return;
    edit = null;
    editDirty = false;
    $('#editor').classList.add('hidden');
    $('#edit-hint').classList.add('hidden');
    render();
  }
  function changed() { editDirty = true; render(); }
  function addPoint(id) {
    if (edit.points.length && edit.points[edit.points.length - 1] === id) { toast('같은 지점을 연달아 넣을 수 없습니다', 'warn', 2000); return; }
    if (edit.points.length >= 100) { toast('경로에는 지점을 100개까지 넣을 수 있습니다', 'warn'); return; }
    edit.points.push(id);
    changed();
  }
  $('#r-name').addEventListener('input', (e) => { edit.name = e.target.value; editDirty = true; });
  $('#r-note').addEventListener('input', (e) => { edit.note = e.target.value; editDirty = true; });
  $('#r-add').addEventListener('change', (e) => { const id = Number(e.target.value); e.target.value = ''; if (id) addPoint(id); });
  $('#r-points').addEventListener('click', (e) => {
    const b = e.target.closest('[data-op]'); if (!b) return;
    const i = Number(b.closest('[data-i]').dataset.i);
    const pts = edit.points;
    if (b.dataset.op === 'up' && i > 0) [pts[i - 1], pts[i]] = [pts[i], pts[i - 1]];
    if (b.dataset.op === 'down' && i < pts.length - 1) [pts[i + 1], pts[i]] = [pts[i], pts[i + 1]];
    if (b.dataset.op === 'del') pts.splice(i, 1);
    changed();
  });
  $('#r-cancel').addEventListener('click', () => closeEditor(false));

  function renderEditor() {
    if (!edit) return;
    $('#r-points').innerHTML = edit.points.length ? edit.points.map((id, i) => {
      const p = placeById(id);
      const dup = i > 0 && edit.points[i - 1] === id;
      return `<li class="item" data-i="${i}" style="padding: 6px 8px; ${dup ? 'outline: 1px solid var(--danger-2)' : ''}">
        <span class="mono muted" style="width: 22px">${i + 1}</span>
        <div class="main"><div>${p ? esc(p.name) : '(지운 지점)'}</div>${p ? `<div class="ty">(${p.x.toFixed(2)}, ${p.y.toFixed(2)})</div>` : ''}</div>
        <button class="btn btn-sm" data-op="up" aria-label="위로" ${i === 0 ? 'disabled' : ''}>↑</button>
        <button class="btn btn-sm" data-op="down" aria-label="아래로" ${i === edit.points.length - 1 ? 'disabled' : ''}>↓</button>
        <button class="btn btn-sm btn-danger-outline" data-op="del" aria-label="경로에서 빼기">✕</button>
      </li>`;
    }).join('') : '<li class="empty">지도에서 지점을 클릭하거나 아래에서 고르세요.</li>';
    $('#r-add').innerHTML = '<option value="">지점 고르기…</option>' + places.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
  }

  $('#r-save').addEventListener('click', async () => {
    const body = { name: edit.name.trim(), note: edit.note.trim(), points: edit.points };
    if (!body.name) { toast('경로 이름을 넣으세요', 'warn'); $('#r-name').focus(); return; }
    if (!body.points.length) { toast('지점을 하나 이상 넣으세요', 'warn'); return; }
    try {
      const r = edit.id ? await api(`/api/routes/${edit.id}`, 'PUT', body) : await api('/api/routes', 'POST', { map: current.name, ...body });
      editDirty = false;
      selected = r.id;
      toast(`"${r.name}" 경로를 저장했습니다${r.robots.length ? ' (실행 중인 로봇은 바뀐 경로로 이어서 감)' : ''}`, 'ok');
      await loadData();
      openEditor(routeById(r.id));
    } catch (err) { toast(`저장 실패: ${err.message}`, 'danger', 6000); }
  });
  $('#r-delete').addEventListener('click', async () => {
    const r = routeById(edit.id);
    if (!r || !confirm(`"${r.name}" 경로를 지울까요? 되돌릴 수 없습니다.`)) return;
    try {
      await api(`/api/routes/${r.id}`, 'DELETE');
      toast(`"${r.name}" 경로를 지웠습니다`, 'ok', 2000);
      selected = null;
      closeEditor(true);
      await loadData();
    } catch (err) { toast(`삭제 실패: ${err.message}`, 'danger', 6000); }
  });

  // ---------- 로봇별 할당 ----------
  const STATE = {
    idle: ['시작 전', ''], stopped: ['정지', ''], running: ['실행 중', 'run'], paused: ['멈춤', 'warn'], done: ['완료', 'ok'], failed: ['실패', 'bad'],
  };
  const RUN = { moving: '이동 중', nav_unavailable: 'Nav2 기다림' };
  const EVENT = { start: '시작', resume: '이어서', pause: '멈춤', stop: '정지', loop: '바퀴 완료', done: '완료', fail: '실패', edit: '경로 수정' };
  const draftAssign = {};   // 로봇 id -> {route_id, repeat_count} (저장 전 입력)
  const history = {};       // 로봇 id -> 최근 실행 목록 (이력 펼친 로봇만)
  const hhmm = (t) => (t ? new Date(t * 1000).toLocaleString('ko-KR', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—');

  async function loadHistory() {
    await Promise.all(Object.keys(history).map(async (id) => {
      try { history[id] = (await (await fetch(`/api/assignments/${encodeURIComponent(id)}/runs?limit=5`, { cache: 'no-store' })).json()).runs; } catch (e) { /* 다음에 */ }
    }));
  }
  function historyHtml(id) {
    const runs = history[id];
    if (!runs) return '<div class="muted" style="font-size: 12px">불러오는 중…</div>';
    if (!runs.length) return '<div class="muted" style="font-size: 12px">실행 이력이 없습니다.</div>';
    return runs.map((r) => {
      const [st, cls] = STATE[r.state] || [r.state, ''];
      const loops = r.repeat_count ? `${r.loops_done}/${r.repeat_count}바퀴` : `${r.loops_done}바퀴 (계속)`;
      const ev = r.events.slice(0, r === runs[0] ? 8 : 3).map((e) => `<div>${esc(hhmm(e.t))} · ${esc(EVENT[e.kind] || e.kind)}${e.loop != null ? ` · ${e.loop}바퀴` : ''}${e.message ? ` · ${esc(e.message)}` : ''}</div>`).join('');
      return `<div style="font-size: 12px; margin-bottom: 6px">
        <div class="mono">#${r.id} · ${esc(r.route_name)}${r.route_id ? '' : ' (지운 경로)'} · ${esc(hhmm(r.started_at))} · <span class="badge ${cls}">${st}</span> · ${loops}</div>
        <div class="muted mono" style="padding: 2px 0 0 14px; line-height: 1.6">${ev || '이력 없음'}</div>
      </div>`;
    }).join('');
  }

  function progressText(a) {
    const loops = a.repeat_count ? `${a.loops_done}/${a.repeat_count}바퀴` : `${a.loops_done}바퀴 (계속)`;
    const wp = a.state === 'running' || a.state === 'paused' || a.state === 'failed' ? ` · 다음 지점 ${Math.min(a.waypoint + 1, a.points)}/${a.points}` : '';
    return loops + wp;
  }

  function renderAssigns() {
    const active = current && current.name === App.fleet.mapFile;
    $('#runner-state').textContent = runner.running ? '경로 실행기 동작 중' : (runner.exited !== undefined ? `경로 실행기 멈춤 (exit ${runner.exited})` : '');
    if (!robots.length) { $('#assign-list').innerHTML = '<div class="empty">등록된 로봇이 없습니다 → <a href="setup.html">로봇 등록</a></div>'; return; }
    // 입력 중인 칸은 다시 그리지 않음 (1초마다 상태 갱신)
    const focused = document.activeElement && document.activeElement.closest('#assign-list [data-a]');
    const gone = assigns.filter((a) => !a.robot_active || !robots.some((r) => r.id === a.robot_id))
      .map((a) => ({ id: a.robot_id, name: a.robot_id, color: '#8B949E', gone: true }));
    const html = [...robots, ...gone].map((r) => {
      const a = assigns.find((x) => x.robot_id === r.id);
      const d = draftAssign[r.id] || {};
      const rid = d.route_id !== undefined ? d.route_id : (a ? a.route_id : '');
      const rep = d.repeat_count !== undefined ? d.repeat_count : (a ? a.repeat_count : 1);
      const otherMap = a && current && a.map !== current.name;
      const [stText, stCls] = a ? STATE[a.state] || [a.state, ''] : ['할당 없음', ''];
      const unsaved = a ? (rid !== a.route_id || rep !== a.repeat_count) : rid !== '';
      const opts = (otherMap ? `<option value="${a.route_id}">${esc(a.route_name)} (지도 ${esc(a.map)})</option>` : '')
        + routes.map((rt) => `<option value="${rt.id}" ${rt.id === rid ? 'selected' : ''}>${esc(rt.name)}</option>`).join('');
      const running = a && a.state === 'running';
      if (r.gone) {
        return `<div class="item" data-a="${esc(r.id)}" style="flex-wrap: wrap; gap: 8px; opacity: 0.8">
          <span class="swatch" style="background: ${esc(r.color)}"></span><b style="min-width: 90px">${esc(r.name)}</b>
          <span class="badge warn">로봇 목록에서 빠짐</span><span class="muted" style="font-size: 12px">${esc(a.route_name)} · ${stText}</span>
          <div class="spacer"></div>
          <button class="btn btn-sm" data-act="stop" ${['running', 'paused', 'failed'].includes(a.state) ? '' : 'disabled'}>정지</button>
          <button class="btn btn-sm btn-danger-outline" data-act="unassign" ${running ? 'disabled' : ''}>해제</button>
        </div>`;
      }
      return `<div class="item" data-a="${esc(r.id)}" style="flex-wrap: wrap; gap: 8px">
        <span class="swatch" style="background: ${esc(r.color)}"></span>
        <b style="min-width: 90px">${esc(r.name)}</b>
        <select data-k="route" aria-label="${esc(r.name)} 경로" ${running ? 'disabled title="멈추거나 정지한 뒤 바꿀 수 있습니다"' : ''}>
          <option value="" ${rid === '' ? 'selected' : ''}>경로 없음</option>${opts}
        </select>
        <label class="muted" style="font-size: 12px; display: flex; align-items: center; gap: 4px">반복
          <input type="number" data-k="repeat" min="0" max="10000" step="1" value="${rep}" style="width: 80px" aria-label="${esc(r.name)} 반복 횟수 (0 = 계속)"></label>
        <button class="btn btn-sm ${unsaved ? 'btn-primary' : ''}" data-act="save" ${unsaved && rid !== '' ? '' : 'disabled'}>할당 저장</button>
        <span class="badge ${stCls}">${stText}</span>
        <div class="spacer"></div>
        ${a ? `
          <button class="btn btn-sm" data-act="start" ${running || !active || otherMap ? 'disabled' : ''} title="처음(1바퀴째 첫 지점)부터">시작</button>
          <button class="btn btn-sm" data-act="pause" ${running ? '' : 'disabled'}>멈춤</button>
          <button class="btn btn-sm" data-act="resume" ${['paused', 'failed', 'stopped'].includes(a.state) && a.run_id && active && !otherMap ? '' : 'disabled'} title="멈춘 바퀴·지점부터">이어서</button>
          <button class="btn btn-sm" data-act="stop" ${['running', 'paused', 'failed'].includes(a.state) ? '' : 'disabled'}>정지</button>
          <button class="btn btn-sm btn-danger-outline" data-act="unassign" ${running ? 'disabled' : ''}>해제</button>` : ''}
        <button class="btn btn-sm" data-act="history" aria-expanded="${r.id in history}">이력</button>
        ${a ? `<div style="flex-basis: 100%; font-size: 12px" class="muted mono">${esc(a.route_name)} · ${a.state === 'idle' ? '시작 전' : progressText(a)}${a.run_state ? ` · ${esc(RUN[a.run_state] || a.run_state)}` : ''}${a.message ? ` · ${esc(a.message)}` : ''}</div>` : ''}
        ${r.id in history ? `<div style="flex-basis: 100%; border-top: 1px solid var(--line); padding-top: 6px">${historyHtml(r.id)}</div>` : ''}
      </div>`;
    }).join('');
    if (focused) {   // 포커스 칸만 남기고 상태 줄만 바꾸기는 복잡 → 포커스 중에는 건너뜀
      return;
    }
    $('#assign-list').innerHTML = html;
    if (mode !== 'operate') $('#assign-list').insertAdjacentHTML('afterbegin', `<div class="banner" role="status">지금은 운영 모드가 아니라(${esc(mode)}) 경로를 시작할 수 없습니다.</div>`);
  }

  $('#assign-list').addEventListener('change', (e) => {
    const row = e.target.closest('[data-a]'); if (!row) return;
    const id = row.dataset.a;
    const a = assigns.find((x) => x.robot_id === id);
    const d = draftAssign[id] || (draftAssign[id] = {});
    if (e.target.dataset.k === 'route') d.route_id = e.target.value === '' ? '' : Number(e.target.value);
    if (e.target.dataset.k === 'repeat') {
      const n = Number(e.target.value);
      d.repeat_count = Number.isInteger(n) && n >= 0 ? n : (a ? a.repeat_count : 1);
    }
    e.target.blur();
    renderAssigns();
  });

  $('#assign-list').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-act]'); if (!b) return;
    const id = b.closest('[data-a]').dataset.a;
    const r = robots.find((x) => x.id === id) || { id, name: id };
    const a = assigns.find((x) => x.robot_id === id);
    const act = b.dataset.act;
    if (act === 'history') {
      if (id in history) delete history[id]; else { history[id] = null; await loadHistory(); }
      renderAssigns();
      return;
    }
    try {
      if (act === 'save') {
        const d = draftAssign[id] || {};
        const body = { route_id: d.route_id !== undefined ? d.route_id : a.route_id, repeat_count: d.repeat_count !== undefined ? d.repeat_count : (a ? a.repeat_count : 1) };
        if (a && a.state === 'paused' && body.route_id !== a.route_id && !confirm('경로를 바꾸면 진행이 처음으로 돌아갑니다. 계속할까요?')) return;
        await api(`/api/assignments/${encodeURIComponent(id)}`, 'PUT', body);
        delete draftAssign[id];
        toast(`${r.name}: 할당을 저장했습니다`, 'ok', 2000);
      } else if (act === 'unassign') {
        if (!confirm(`${r.name} 의 경로 할당을 해제할까요?`)) return;
        await api(`/api/assignments/${encodeURIComponent(id)}`, 'DELETE');
        delete draftAssign[id];
      } else {
        if (act === 'start') {
          const n = a.repeat_count ? `${a.repeat_count}바퀴` : '멈출 때까지 계속';
          if (!confirm(`${r.name} 이(가) "${a.route_name}" 경로를 ${n} 스스로 돕니다. 시작할까요?`)) return;
        }
        const url = `/api/assignments/${encodeURIComponent(id)}/${act}`;
        // 멈춤·정지는 토큰 없이 (안전 쪽), 시작·이어서는 토큰
        if (act === 'pause' || act === 'stop') {
          const res = await fetch(url, { method: 'POST' });
          const body = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
          // 실행기에 바로 취소 (DB 확인은 0.5초 주기)
          App.ros.publish('/fleet/route_cmd', 'std_msgs/msg/String', { data: JSON.stringify({ cmd: 'pause', robot: id }) });
        } else await api(url, 'POST');
        toast(`${r.name}: ${{ start: '시작', resume: '이어서 감', pause: '멈춤', stop: '정지' }[act]}`, act === 'start' || act === 'resume' ? 'ok' : 'warn', 2000);
      }
    } catch (err) { toast(`${r.name}: ${err.message}`, 'danger', 6000); }
    await loadData();
  });

  // ---------- 그리기 ----------
  function render() {
    drawMap();
    renderRoutes();
    renderEditor();
    renderAssigns();
  }

  // 할당 상태 1초마다, 로봇 위치 0.3초마다
  setInterval(async () => { if (!current) return; await loadAssigns(); renderAssigns(); renderRoutes(); }, 1000);
  setInterval(async () => { if (Object.keys(history).length) { await loadHistory(); renderAssigns(); } }, 3000);
  setInterval(() => {
    const active = current && current.name === App.fleet.mapFile;
    view.state.robots = active ? robots.filter((r) => r.online && r.mapPose).map((r) => ({ name: r.name, color: r.color, pose: r.mapPose })) : [];
    view.invalidate();
  }, 300);

  await loadMaps();
});
