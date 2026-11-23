// 경비 모드: 경보 현황(목록·지도·상세 비교·처리), 순찰 상태, 순찰 경로·감지 설정 편집
window.App.ready.then(async (App) => {
  'use strict';
  const { ros, $, $$, esc, toast } = App;
  const robots = App.robots;
  const byId = (id) => robots.find((r) => r.id === id);
  robots.forEach((r) => r.start({ tf: true, odomThrottle: 200 }));
  const KIND = { new: '새로 생긴 것', missing: '사라진 것' };
  const SEV = { high: '높음', medium: '보통', low: '낮음' };
  const STATUS = { open: '미확인', ok: '이상 없음', false: '오탐', baseline: '기준 지도 반영' };
  const STATE = { patrolling: '순찰 중', inspecting: '확인하러 이동', idle: '대기', no_route: '경로 없음', nav_unavailable: 'Nav2 없음', paused: '멈춤', rejected: 'goal 거부' };

  const mapName = App.fleet.mapFile;
  const view = new window.RobotView($('#canvas'), { scale: 50 });
  view.follow = false;
  view.emptyText = '';
  view.state.robots = [];
  let mapInfo = null, mapImg = null;
  let alerts = [], filter = 'open', selected = null, detail = null;
  let patrol = { routes: {}, settings: {} }, dirty = false;
  let tab = 'watch';

  if (!mapName) {
    $('#no-map').innerHTML = '<b>활성(기준) 지도가 없습니다.</b><span class="muted">경비는 기준 지도와 지금 스캔을 비교합니다.</span><a class="btn btn-primary" href="mapping.html" style="text-decoration: none">지도 만들기</a>';
    $('#no-map').classList.remove('hidden');
    return;
  }

  // ---------- 지도 ----------
  async function loadMap() {
    const maps = await (await fetch('/api/maps', { cache: 'no-store' })).json();
    mapInfo = maps.find((m) => m.name === mapName);
    if (!mapInfo) return;
    const url = `/api/maps/${encodeURIComponent(mapName)}.png?t=${mapInfo.mtime}`;
    view.state.map = await window.RobotView.mapFromPng(url, mapInfo);
    mapImg = new Image(); mapImg.src = url; await mapImg.decode();
    $('#base-info').textContent = `기준 지도 ${mapName} · ${new Date(mapInfo.mtime * 1000).toLocaleString('ko-KR')}`;
  }
  function fit() {
    const w = mapInfo.width * mapInfo.resolution, h = mapInfo.height * mapInfo.resolution;
    view.center.x = mapInfo.origin[0] + w / 2; view.center.y = mapInfo.origin[1] + h / 2;
    view.scale = Math.max(8, Math.min(view.W / w, view.H / h) * 0.9);
  }
  await loadMap();
  if (mapInfo) fit();
  $$('[data-layer]').forEach((b) => b.addEventListener('click', () => {
    const on = b.getAttribute('aria-pressed') !== 'true';
    b.setAttribute('aria-pressed', String(on)); view.layers[b.dataset.layer] = on; view.invalidate();
  }));
  $('#zoom-in').addEventListener('click', () => view.zoom(1.25));
  $('#zoom-out').addEventListener('click', () => view.zoom(0.8));

  // ---------- 경비 시작/중지 (모드) ----------
  document.addEventListener('tb3:mode', (e) => {
    const on = e.detail.mode === 'patrol';
    const b = $('#patrol-toggle');
    b.textContent = on ? '경비 끝내기' : '경비 시작';
    b.className = on ? 'btn btn-danger-outline' : 'btn btn-primary';
    const p = e.detail.patrol || {};
    $('#patrol-proc').textContent = on ? (p.running ? `감지 노드 실행 중 · ${Math.floor((p.seconds || 0) / 60)}분째` : `감지 노드 정지됨${p.exited !== undefined ? ` (종료 ${p.exited})` : ''}`) : '경비 모드가 아닙니다';
  });
  $('#patrol-toggle').addEventListener('click', async () => {
    const on = App.mode.mode === 'patrol';
    if (on && !confirm('경비를 끝내고 운영 모드로 바꿀까요? 순찰과 감지가 멈춥니다.')) return;
    try {
      await App.adminJson('/api/mode', { method: 'POST', body: JSON.stringify({ mode: on ? 'operate' : 'patrol' }) });
      await App.pollMode();
      toast(on ? '경비를 끝냈습니다' : '경비를 시작했습니다', 'ok');
    } catch (e) { toast(`실패: ${e.message}`, 'danger', 6000); }
  });

  // 주기적으로 다시 그리는 곳은 내용이 바뀔 때만 바꿈 (누르려던 버튼이 중간에 사라지지 않게)
  const lastHtml = new Map();
  const setHtml = (sel, html) => { if (lastHtml.get(sel) !== html) { $(sel).innerHTML = html; lastHtml.set(sel, html); } };

  // ---------- 경보 목록 ----------
  async function loadAlerts() {
    try {
      const r = await (await fetch(`/api/alerts?map=${encodeURIComponent(mapName)}&limit=200`, { cache: 'no-store' })).json();
      alerts = r.alerts;
      $('#cnt-open').textContent = r.counts.open;
      $('#cnt-all').textContent = r.counts.total;
    } catch (e) { /* ignore */ }
    renderAlerts();
    drawAlerts();
  }
  const ago = (t) => { const s = Math.round(Date.now() / 1000 - t); return s < 60 ? `${s}초 전` : s < 3600 ? `${Math.floor(s / 60)}분 전` : `${Math.floor(s / 3600)}시간 전`; };
  function renderAlerts() {
    const list = alerts.filter((a) => filter === 'all' || a.status === 'open');
    setHtml('#alert-list', list.length ? list.map((a) => {
      const r0 = byId(a.robots[0]);
      return `<button class="acard ${a.status !== 'open' ? 'done' : ''}" data-alert="${a.id}" aria-pressed="${a.id === selected}">
        <div style="display: flex; gap: 8px; align-items: center"><b class="mono sev-${a.severity}">#${a.id}</b><span class="badge sev-${a.severity}">${SEV[a.severity]}</span><span class="spacer"></span><span class="badge">${STATUS[a.status]}</span></div>
        <div style="font-weight: 600">${KIND[a.kind]} · ${a.area.toFixed(2)} m²</div>
        <div class="muted" style="font-size: 12px; display: flex; gap: 6px; align-items: center"><span class="swatch" style="background: ${r0 ? esc(r0.color) : '#8B949E'}"></span>${esc(a.robots.map((id) => (byId(id) || { name: id }).name).join(', '))} · (${a.x.toFixed(1)}, ${a.y.toFixed(1)}) · ${ago(a.last_seen)}${a.seen > 1 ? ` · ${a.seen}회` : ''}</div>
      </button>`;
    }).join('') : `<div class="empty">${filter === 'open' ? '확인 안 한 경보가 없습니다' : '경보가 없습니다'}</div>`);
  }
  $('#alert-list').addEventListener('click', (e) => { const b = e.target.closest('[data-alert]'); if (b) select(Number(b.dataset.alert)); });
  $$('[data-filter]').forEach((b) => b.addEventListener('click', () => {
    filter = b.dataset.filter; $$('[data-filter]').forEach((x) => x.setAttribute('aria-pressed', String(x === b))); renderAlerts();
  }));
  function drawAlerts() {
    view.state.zones = alerts.filter((a) => a.status === 'open' || a.id === selected).map((a) => {
      const hw = Math.max(a.w, 0.1) / 2 + 0.05, hh = Math.max(a.h, 0.1) / 2 + 0.05;
      const isNew = a.kind === 'new';
      return {
        points: [[a.x - hw, a.y - hh], [a.x + hw, a.y - hh], [a.x + hw, a.y + hh], [a.x - hw, a.y + hh]],
        type: 'alert', label: `#${a.id}`, selected: a.id === selected,
        fill: isNew ? 'rgba(248,113,113,0.3)' : 'rgba(251,191,36,0.12)', stroke: isNew ? '#F87171' : '#FBBF24',
        dash: isNew ? [] : [6, 4], labelColor: isNew ? '#FCA5A5' : '#FDE68A', dim: a.status !== 'open',
      };
    });
    view.invalidate();
  }

  // ---------- 경보 상세 ----------
  async function select(id) {
    selected = id;
    history.replaceState(null, '', `#alert-${id}`);
    renderAlerts(); drawAlerts();
    try { detail = await (await fetch(`/api/alerts/${id}`, { cache: 'no-store' })).json(); } catch (e) { return; }
    view.center.x = detail.x; view.center.y = detail.y; view.invalidate();
    renderDetail();
  }
  function renderDetail() {
    const a = detail;
    if (!a) return;
    const open = a.status === 'open';
    $('#detail').style.borderColor = open ? 'var(--danger)' : '';
    $('#detail').innerHTML = `
      <div class="panel-head"><b class="mono sev-${a.severity}">#${a.id}</b><span class="badge sev-${a.severity}">${SEV[a.severity]}</span><span class="badge">${STATUS[a.status]}</span><div class="spacer"></div><span class="muted" style="font-size: 12px">${new Date(a.first_seen * 1000).toLocaleTimeString('ko-KR')}</span></div>
      <h2 style="font-size: 17px">${KIND[a.kind]}</h2>
      <div class="muted" style="font-size: 12px">${esc(a.robots.map((id) => (byId(id) || { name: id }).name).join(', '))} · map (${a.x.toFixed(2)}, ${a.y.toFixed(2)}) · 약 ${a.w.toFixed(2)} × ${a.h.toFixed(2)} m</div>
      <div class="cmp">
        <figure><canvas id="cmp-base" width="240" height="180" aria-label="기준 지도의 같은 곳"></canvas><figcaption>기준 지도</figcaption></figure>
        <figure><canvas id="cmp-scan" width="240" height="180" aria-label="발견 당시 LiDAR 스캔"></canvas><figcaption>발견 당시 스캔 (점) · 상자 = 차이</figcaption></figure>
      </div>
      <div class="grid3">
        <div class="tile"><div class="k">크기</div><div class="v" style="font-size: 15px" data-v="area">${a.area.toFixed(2)} m²</div></div>
        <div class="tile"><div class="k">지속</div><div class="v" style="font-size: 15px" data-v="dur">${Math.max(0, Math.round((a.last_seen - a.first_seen) / 60))}분</div></div>
        <div class="tile"><div class="k">본 로봇</div><div class="v" style="font-size: 15px" data-v="seen">${a.robots.length}대 · ${a.seen}회</div></div>
      </div>
      ${a.note ? `<div class="muted" style="font-size: 12px">메모: ${esc(a.note)}</div>` : ''}
      <div class="grid2">
        <button class="btn" data-act="ok" style="min-height: 44px; ${open ? 'background: var(--ok); color: #0E1116; border: 0; font-weight: 700' : ''}" ${open ? '' : 'disabled'}>확인 · 이상 없음</button>
        <button class="btn" data-act="false" style="min-height: 44px" ${open ? '' : 'disabled'}>오탐으로 처리</button>
        <button class="btn" data-act="inspect" style="min-height: 44px">가까운 로봇 보내 다시 보기</button>
        <button class="btn" data-act="link" style="min-height: 44px">경보 링크 복사</button>
      </div>
      ${open ? '<button class="btn" data-act="baseline" style="border-style: dashed">기준 지도에 반영 (앞으로 이 상태를 정상으로)</button>' : '<button class="btn btn-sm" data-act="open">다시 열기</button>'}`;
    drawCompare();
  }
  function drawCompare() {
    const a = detail;
    if (!a || !mapImg) return;
    const R = 1.5;   // 경보 중심 ±1.5 m
    const res = mapInfo.resolution, H = mapInfo.height;
    const toPx = (x, y) => [(x - mapInfo.origin[0]) / res, H - (y - mapInfo.origin[1]) / res];
    for (const id of ['cmp-base', 'cmp-scan']) {
      const cv = $(`#${id}`);
      const g = cv.getContext('2d');
      const [cx, cy] = toPx(a.x, a.y);
      const span = (2 * R) / res;
      const sx = cv.width / span, sy = cv.height / (span * 0.75);
      g.fillStyle = '#161B22'; g.fillRect(0, 0, cv.width, cv.height);
      g.imageSmoothingEnabled = false;
      g.drawImage(mapImg, cx - span / 2, cy - span * 0.375, span, span * 0.75, 0, 0, cv.width, cv.height);
      const w2s = (x, y) => { const [px, py] = toPx(x, y); return [(px - (cx - span / 2)) * sx, (py - (cy - span * 0.375)) * sy]; };
      if (id === 'cmp-scan') {
        g.fillStyle = '#E6EDF3';
        for (const [x, y] of a.scan || []) { const [u, v] = w2s(x, y); g.fillRect(u - 1.2, v - 1.2, 2.4, 2.4); }
        const [u0, v0] = w2s(a.x - a.w / 2, a.y + a.h / 2);
        g.strokeStyle = a.kind === 'new' ? '#F87171' : '#FBBF24'; g.lineWidth = 2;
        g.setLineDash(a.kind === 'new' ? [] : [5, 4]);
        g.strokeRect(u0 - 3, v0 - 3, a.w / res * sx + 6, a.h / res * sy + 6);
        g.setLineDash([]);
      }
    }
  }
  $('#detail').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-act]'); if (!b || !detail) return;
    const a = detail;
    try {
      if (b.dataset.act === 'ok' || b.dataset.act === 'false' || b.dataset.act === 'open') {
        const r = await fetch(`/api/alerts/${a.id}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: b.dataset.act }) });
        if (!r.ok) throw new Error((await r.json()).error);
        detail = await r.json();
        toast(`#${a.id}: ${STATUS[detail.status]}`, 'ok');
      } else if (b.dataset.act === 'baseline') {
        if (!confirm(`#${a.id} 를 기준 지도에 반영할까요? (${a.kind === 'new' ? '그 자리를 벽으로' : '그 자리를 빈 곳으로'} 바꿈, 원본은 .bak 로 보관)`)) return;
        const r = await App.adminJson(`/api/alerts/${a.id}/baseline`, { method: 'POST', body: '{}' });
        detail = r.alert;
        await loadMap();
        toast(`기준 지도에 반영 (${r.cells}칸)`, 'ok');
      } else if (b.dataset.act === 'inspect') {
        const cand = robots.filter((r) => r.online && r.mapPose).sort((p, q) => Math.hypot(p.mapPose.x - a.x, p.mapPose.y - a.y) - Math.hypot(q.mapPose.x - a.x, q.mapPose.y - a.y));
        if (!cand.length) { toast('지도 위치를 아는 온라인 로봇이 없습니다', 'warn'); return; }
        ros.publish('/fleet/patrol_cmd', 'std_msgs/msg/String', { data: JSON.stringify({ cmd: 'inspect', robot: cand[0].id, x: a.x, y: a.y }) });
        toast(`${cand[0].name} 를 #${a.id} 근처로 보냈습니다 (확인 뒤 순찰 재개)`, 'ok');
        return;
      } else if (b.dataset.act === 'link') {
        const url = `${location.origin}${location.pathname}#alert-${a.id}`;
        try { await navigator.clipboard.writeText(url); toast('링크를 복사했습니다', 'ok', 1500); } catch (err) { prompt('경보 링크', url); }
        return;
      }
      renderDetail(); await loadAlerts();
    } catch (err) { toast(`실패: ${err.message}`, 'danger'); }
  });

  // ---------- 실시간 ----------
  document.addEventListener('tb3:alert', async (e) => {
    await loadAlerts();
    if (!detail || e.detail.alert.id !== detail.id) return;
    // 갱신(update)마다 통째로 다시 그리면 누르려던 버튼이 사라지므로, 모양이 바뀔 때만 다시 그림
    const prev = detail;
    try { detail = await (await fetch(`/api/alerts/${prev.id}`, { cache: 'no-store' })).json(); } catch (err) { return; }
    if (detail.status !== prev.status || detail.severity !== prev.severity || detail.robots.length !== prev.robots.length || detail.note !== prev.note) { renderDetail(); return; }
    const set = (k, v) => { const el = $(`#detail [data-v="${k}"]`); if (el) el.textContent = v; };
    set('area', `${detail.area.toFixed(2)} m²`);
    set('dur', `${Math.max(0, Math.round((detail.last_seen - detail.first_seen) / 60))}분`);
    set('seen', `${detail.robots.length}대 · ${detail.seen}회`);
  });
  setInterval(loadAlerts, 5000);
  $('#notif').checked = 'Notification' in window && Notification.permission === 'granted';
  $('#notif').addEventListener('change', async (e) => {
    if (!('Notification' in window)) { toast('이 브라우저는 알림을 지원하지 않습니다', 'warn'); e.target.checked = false; return; }
    const p = await Notification.requestPermission();
    e.target.checked = p === 'granted';
    if (p !== 'granted') toast('알림이 허용되지 않았습니다 (https 또는 localhost 에서만 가능할 수 있음)', 'warn');
  });

  // ---------- 순찰 상태 ----------
  let pstatus = { robots: {} };
  ros.subscribe('/fleet/patrol_status', 'std_msgs/msg/String', (m) => { try { pstatus = JSON.parse(m.data); pstatus.at = Date.now(); } catch (e) { /* ignore */ } }, { throttle: 1000 });
  function renderStatus() {
    const fresh = pstatus.at && Date.now() - pstatus.at < 4000;
    setHtml('#status-rows', robots.map((r) => {
      const s = fresh ? pstatus.robots[r.id] : null;
      const st = s ? (STATE[s.state] || s.state) : (r.online ? '—' : '오프라인');
      return `<tr><td><span style="display: inline-flex; gap: 8px; align-items: center"><span class="swatch" style="background: ${esc(r.color)}"></span>${esc(r.name)}</span></td>
        <td>${esc(st)}</td><td class="mono">${s && s.points ? `${s.waypoint + 1} / ${s.points}` : '—'}</td><td class="num">${s ? s.laps : '—'}</td>
        <td>${s ? (s.localized ? '<span class="badge ok">됨</span>' : '<span class="badge warn">안 됨</span>') : '—'}</td>
        <td class="mono" style="font-size: 12px">${esc(r.batteryText())}</td>
        <td>${s ? `<button class="btn btn-sm" data-pr="${esc(r.id)}" data-cmd="${s.paused ? 'resume' : 'pause'}">${s.paused ? '재개' : '멈춤'}</button>` : ''}</td></tr>`;
    }).join(''));
    view.state.robots = robots.filter((r) => r.online && r.mapPose).map((r) => ({ name: r.name, color: r.color, pose: r.mapPose, selected: false }));
    view.invalidate();
  }
  setInterval(renderStatus, 1000);
  $('#status-rows').addEventListener('click', (e) => {
    const b = e.target.closest('[data-pr]'); if (!b) return;
    ros.publish('/fleet/patrol_cmd', 'std_msgs/msg/String', { data: JSON.stringify({ cmd: b.dataset.cmd, robot: b.dataset.pr }) });
  });

  // ---------- 순찰 경로 · 감지 설정 ----------
  patrol = await (await fetch(`/api/patrol/${encodeURIComponent(mapName)}`, { cache: 'no-store' })).json();
  const routeSel = $('#route-robot');
  routeSel.innerHTML = robots.map((r) => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join('');
  const curRoute = () => (patrol.routes[routeSel.value] ||= { points: [], enabled: true });
  function setDirty(d) { dirty = d; $('#routes-save').disabled = !d; }
  function drawRoutes() {
    view.state.routes = robots.map((r) => ({ color: r.color, loop: true, highlight: tab === 'routes' && r.id === routeSel.value, points: (patrol.routes[r.id] || {}).points || [] }))
      .filter((x) => tab === 'watch' || x.highlight || x.points.length);
    view.invalidate();
  }
  function renderRoute() {
    const rt = curRoute();
    $('#route-enabled').checked = rt.enabled;
    $('#wp-list').innerHTML = rt.points.length ? rt.points.map((p, i) => `<div class="wp"><span class="badge">${i + 1}</span><span class="mono">(${p[0].toFixed(2)}, ${p[1].toFixed(2)})</span><button class="btn btn-sm" data-del="${i}" aria-label="${i + 1}번 지점 빼기">빼기</button></div>`).join('') : '<div class="empty">지점이 없습니다</div>';
    drawRoutes();
  }
  function renderSettings() {
    const s = patrol.settings;
    $$('[data-s]').forEach((i) => { if (i.type === 'checkbox') i.checked = !!s[i.dataset.s]; else i.value = s[i.dataset.s]; });
    $('#s-min_area').textContent = `${Number(s.min_area).toFixed(2)} m²`;
    $('#s-persist').textContent = `${s.persist}회`;
    $('#s-max_range').textContent = `${Number(s.max_range).toFixed(1)} m`;
    $('#s-tol').textContent = `${Number(s.tol).toFixed(2)} m`;
    $('#s-decay').textContent = `${s.decay}초`;
  }
  $$('[data-s]').forEach((i) => i.addEventListener('input', () => {
    patrol.settings[i.dataset.s] = i.type === 'checkbox' ? i.checked : Number(i.value);
    renderSettings(); setDirty(true);
  }));
  routeSel.addEventListener('change', renderRoute);
  $('#route-enabled').addEventListener('change', (e) => { curRoute().enabled = e.target.checked; setDirty(true); });
  $('#wp-list').addEventListener('click', (e) => { const b = e.target.closest('[data-del]'); if (b) { curRoute().points.splice(Number(b.dataset.del), 1); setDirty(true); renderRoute(); } });
  $('#wp-undo').addEventListener('click', () => { curRoute().points.pop(); setDirty(true); renderRoute(); });
  $('#wp-clear').addEventListener('click', () => { if (confirm('이 로봇의 순찰 지점을 모두 지울까요?')) { curRoute().points = []; setDirty(true); renderRoute(); } });
  $('#routes-save').addEventListener('click', async () => {
    try {
      patrol = { ...patrol, ...(await App.adminJson(`/api/patrol/${encodeURIComponent(mapName)}`, { method: 'PUT', body: JSON.stringify({ routes: patrol.routes, settings: patrol.settings }) })) };
      setDirty(false); renderRoute(); renderSettings();
      toast('순찰 경로·감지 설정을 저장했습니다', 'ok');
    } catch (e) { toast(`저장 실패: ${e.message}`, 'danger', 6000); }
  });
  window.addEventListener('beforeunload', (e) => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });

  function setTab(t) {
    tab = t;
    $$('[data-tab]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === t)));
    $('#left-watch').classList.toggle('hidden', t !== 'watch');
    $('#right-watch').classList.toggle('hidden', t !== 'watch');
    $('#bottom-watch').classList.toggle('hidden', t !== 'watch');
    $('#left-routes').classList.toggle('hidden', t !== 'routes');
    $('#route-hint').classList.toggle('hidden', t !== 'routes');
    view.onPointClick = t === 'routes' ? (x, y) => { curRoute().points.push([Number(x.toFixed(2)), Number(y.toFixed(2))]); setDirty(true); renderRoute(); } : null;
    view.state.zones = t === 'watch' ? view.state.zones : [];
    if (t === 'watch') drawAlerts();
    drawRoutes();
    view.resize(); view.invalidate();
  }
  $$('[data-tab]').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));

  renderRoute(); renderSettings(); setTab('watch');
  await loadAlerts();
  const m = location.hash.match(/^#alert-(\d+)$/);
  // 같은 페이지에서 경보 링크(#alert-N)를 열면 다시 불러오지 않고 hash 만 바뀜
  window.addEventListener('hashchange', () => { const h = location.hash.match(/^#alert-(\d+)$/); if (h && Number(h[1]) !== selected) select(Number(h[1])); });
  if (m) select(Number(m[1]));
  else { const first = alerts.find((a) => a.status === 'open'); if (first) select(first.id); }
});
