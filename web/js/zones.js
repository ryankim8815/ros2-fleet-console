// 이동 구역 편집: 저장된 지도 위에 금지/허용 다각형을 그리고, 로봇별로 적용한 뒤 Nav2 keepout 마스크로 저장
window.App.ready.then(async (App) => {
  'use strict';
  const { ros, $, $$, esc, toast } = App;
  const robots = App.robots;
  robots.forEach((r) => r.start({ tf: true, odomThrottle: 200 }));

  const view = new window.RobotView($('#canvas'), { scale: 60 });
  view.follow = false;
  view.state.robots = [];
  view.emptyText = '';   // 로봇이 없어도 안내 문구를 띄우지 않음 (편집 화면)
  let maps = [];
  let current = null;              // 지도 정보
  let zones = [];                  // [{id, name, type, robots, points}]
  let selected = null;
  let drawing = null;              // 'keepout' | 'allowed' | null
  let dirty = false;

  const setDirty = (d) => { dirty = d; $('#save').disabled = !d || !current; };
  window.addEventListener('beforeunload', (e) => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });

  // ---------- 지도 ----------
  async function loadMaps() {
    maps = await (await fetch('/api/maps', { cache: 'no-store' })).json();
    const sel = $('#map-pick');
    if (!maps.length) {
      sel.innerHTML = '<option value="">저장된 지도 없음</option>';
      $('#map-meta').innerHTML = '저장된 지도가 없습니다 → <a href="mapping.html">지도 작성</a>';
      return;
    }
    sel.innerHTML = maps.map((m) => `<option value="${esc(m.name)}">${esc(m.name)}${m.name === App.fleet.mapFile ? ' (활성)' : ''}</option>`).join('');
    const want = new URLSearchParams(location.search).get('map') || App.fleet.mapFile || maps[0].name;
    sel.value = maps.some((m) => m.name === want) ? want : maps[0].name;
    await openMap(sel.value);
  }
  $('#map-pick').addEventListener('change', async (e) => {
    if (dirty && !confirm('저장하지 않은 구역 변경이 있습니다. 다른 지도로 바꿀까요?')) { e.target.value = current.name; return; }
    await openMap(e.target.value);
  });

  async function openMap(name) {
    current = maps.find((m) => m.name === name);
    if (!current) return;
    view.state.map = await window.RobotView.mapFromPng(`/api/maps/${encodeURIComponent(name)}.png?t=${current.mtime}`, current);
    view.center.x = current.origin[0] + (current.width * current.resolution) / 2;
    view.center.y = current.origin[1] + (current.height * current.resolution) / 2;
    const fit = Math.min(view.W / (current.width * current.resolution), view.H / (current.height * current.resolution));
    view.scale = Math.max(8, fit * 0.9);
    $('#map-meta').textContent = `${current.width}×${current.height} · ${current.resolution.toFixed(3)} m`;
    const isActive = name === App.fleet.mapFile;
    $('#active-badge').textContent = isActive ? '활성 지도' : '활성 아님';
    $('#active-badge').className = `badge ${isActive ? 'ok' : 'warn'}`;
    $('#make-active').classList.toggle('hidden', isActive);
    const doc = await (await fetch(`/api/zones/${encodeURIComponent(name)}`, { cache: 'no-store' })).json();
    zones = doc.zones || [];
    selected = null;
    setDirty(false);
    $('#apply-result').innerHTML = '';
    render();
  }

  $('#make-active').addEventListener('click', async () => {
    try {
      await App.setActiveMap(current.name);
      toast(`활성 지도: ${current.name}`, 'ok');
      await loadMaps();
    } catch (e) { toast(`실패: ${e.message}`, 'danger'); }
  });

  // ---------- 그리기 ----------
  function startDraw(type) {
    if (!current) { toast('먼저 지도를 고르세요 (없으면 지도 작성)', 'warn'); return; }
    drawing = type;
    view.state.draftPoly = [];
    view.onPointClick = addPoint;
    $('#draw-hint').textContent = `${type === 'keepout' ? '금지' : '허용'} 구역: 꼭짓점을 클릭 · 첫 점을 다시 누르거나 Enter 로 완료 · Backspace 되돌리기 · Esc 취소`;
    $('#draw-hint').classList.remove('hidden');
  }
  function stopDraw() {
    drawing = null;
    view.state.draftPoly = null;
    view.state.cursor = null;
    view.onPointClick = null;
    $('#draw-hint').classList.add('hidden');
    view.invalidate();
  }
  function addPoint(x, y) {
    const d = view.state.draftPoly;
    if (d.length >= 3) {
      const [sx, sy] = view.toScreen(d[0][0], d[0][1]);
      const [px, py] = view.toScreen(x, y);
      if (Math.hypot(sx - px, sy - py) < 12) { finishDraw(); return; }
    }
    d.push([Number(x.toFixed(3)), Number(y.toFixed(3))]);
    view.invalidate();
  }
  function finishDraw() {
    const pts = view.state.draftPoly;
    if (!pts || pts.length < 3) { toast('꼭짓점이 3개 이상 필요합니다', 'warn'); return; }
    let n = 1;
    while (zones.some((z) => z.id === `zone_${n}`)) n += 1;
    const z = { id: `zone_${n}`, name: `${drawing === 'keepout' ? '금지' : '허용'} ${n}`, type: drawing, robots: ['*'], points: pts };
    zones.push(z);
    selected = z.id;
    stopDraw();
    setDirty(true);
    render();
  }
  $('#new-keepout').addEventListener('click', () => startDraw('keepout'));
  $('#new-allowed').addEventListener('click', () => startDraw('allowed'));
  window.addEventListener('keydown', (e) => {
    if (!drawing) return;
    if (e.key === 'Enter') { e.preventDefault(); finishDraw(); }
    if (e.key === 'Backspace') { e.preventDefault(); view.state.draftPoly.pop(); view.invalidate(); }
    if (e.key === 'Escape') { e.stopImmediatePropagation(); stopDraw(); }
  }, true);
  $('#zoom-in').addEventListener('click', () => view.zoom(1.25));
  $('#zoom-out').addEventListener('click', () => view.zoom(0.8));

  // ---------- 목록 ----------
  const previewSel = $('#preview-robot');
  previewSel.innerHTML += robots.map((r) => `<option value="${esc(r.id)}">${esc(r.name)} 기준</option>`).join('');
  previewSel.addEventListener('change', render);

  function applies(z, rid) { return z.robots.includes('*') || z.robots.includes(rid); }

  function render() {
    const pr = previewSel.value;
    view.state.zones = zones.map((z) => ({ ...z, label: z.name, selected: z.id === selected, dim: pr && !applies(z, pr) }));
    view.invalidate();
    $('#zone-count').textContent = zones.length;
    $('#zone-list').innerHTML = zones.length ? zones.map((z) => {
      const all = z.robots.includes('*');
      return `<div class="item" data-zone="${esc(z.id)}" style="flex-direction: column; align-items: stretch; gap: 8px; ${z.id === selected ? 'outline: 1px solid var(--accent)' : ''}">
        <div style="display: flex; gap: 8px; align-items: center">
          <span class="badge ${z.type === 'keepout' ? 'bad' : 'ok'}">${z.type === 'keepout' ? '금지' : '허용'}</span>
          <input data-name value="${esc(z.name)}" aria-label="구역 이름" maxlength="40" style="flex: 1 1 auto; min-width: 0">
          <button class="btn btn-sm" data-sel>보기</button>
          <button class="btn btn-sm btn-danger-outline" data-del aria-label="${esc(z.name)} 삭제">삭제</button>
        </div>
        <div style="display: flex; gap: 10px; flex-wrap: wrap; font-size: 12px">
          <label style="display: flex; gap: 4px; align-items: center"><input type="checkbox" data-robot="*" ${all ? 'checked' : ''}> 모든 로봇</label>
          ${robots.map((r) => `<label style="display: flex; gap: 4px; align-items: center; ${all ? 'opacity: 0.45' : ''}"><input type="checkbox" data-robot="${esc(r.id)}" ${all || z.robots.includes(r.id) ? 'checked' : ''} ${all ? 'disabled' : ''}><span class="swatch" style="background: ${esc(r.color)}"></span>${esc(r.name)}</label>`).join('')}
        </div>
        <div class="muted mono" style="font-size: 11px">${z.points.length}점 · 면적 ${area(z.points).toFixed(2)} m²</div>
      </div>`;
    }).join('') : '<div class="empty">구역이 없습니다. 지도 위에 금지 또는 허용 구역을 그리세요.</div>';
  }
  function area(pts) {
    let a = 0;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i, i += 1) a += (pts[j][0] + pts[i][0]) * (pts[j][1] - pts[i][1]);
    return Math.abs(a / 2);
  }

  $('#zone-list').addEventListener('click', (e) => {
    const item = e.target.closest('[data-zone]'); if (!item) return;
    const z = zones.find((x) => x.id === item.dataset.zone);
    if (e.target.closest('[data-del]')) {
      if (!confirm(`${z.name} 구역을 지울까요? (저장해야 반영)`)) return;
      zones = zones.filter((x) => x !== z); setDirty(true); render(); return;
    }
    if (e.target.closest('[data-sel]')) {
      selected = z.id;
      const c = z.points.reduce((a, p) => [a[0] + p[0] / z.points.length, a[1] + p[1] / z.points.length], [0, 0]);
      view.center.x = c[0]; view.center.y = c[1];
      render();
    }
  });
  $('#zone-list').addEventListener('change', (e) => {
    const item = e.target.closest('[data-zone]'); if (!item) return;
    const z = zones.find((x) => x.id === item.dataset.zone);
    if (e.target.matches('[data-name]')) z.name = e.target.value.trim() || z.id;
    if (e.target.matches('[data-robot]')) {
      const rid = e.target.dataset.robot;
      if (rid === '*') z.robots = e.target.checked ? ['*'] : robots.map((r) => r.id);
      else {
        const set = new Set(z.robots.filter((x) => x !== '*'));
        if (e.target.checked) set.add(rid); else set.delete(rid);
        z.robots = set.size ? [...set] : ['*'];
        if (set.size === robots.length) z.robots = ['*'];
      }
    }
    setDirty(true);
    render();
  });

  // ---------- 저장 + Nav2 에 바로 반영 ----------
  $('#save').addEventListener('click', async () => {
    try {
      const res = await App.adminJson(`/api/zones/${encodeURIComponent(current.name)}`, { method: 'PUT', body: JSON.stringify({ zones }) });
      zones = res.zones.zones;
      setDirty(false);
      const lines = [];
      const active = current.name === App.fleet.mapFile;
      for (const r of robots) {
        const m = res.masks[r.id];
        if (!m) continue;
        const svc = `${r.ns}/keepout_filter_mask_server/load_map`;
        let state = 'Nav2 마스크 서버 없음 → 다음 Nav2 실행 때 적용';
        if (active) {
          try {
            const out = await ros.callService(svc, { map_url: m.yaml }, { type: 'nav2_msgs/srv/LoadMap', timeout: 5000 });
            state = out.result === 0 ? 'Nav2 에 바로 적용됨' : `적용 실패 (result ${out.result})`;
          } catch (e) { /* 서비스 없음 */ }
        } else state = '활성 지도가 아님 → 활성 지도로 지정한 뒤 Nav2 실행 때 적용';
        lines.push(`<div class="check"><span class="${state.startsWith('Nav2 에 바로') ? 'ok' : 'unk'}">${state.startsWith('Nav2 에 바로') ? '✓' : '•'}</span><span><span class="swatch" style="background: ${esc(r.color)}"></span> ${esc(r.name)} · 구역 ${m.zones}개</span><span class="muted">${esc(state)}</span></div>`);
      }
      $('#apply-result').innerHTML = lines.join('');
      if (active) await App.loadZones();   // 수동 조종 차단에도 반영
      toast('구역을 저장했습니다', 'ok');
      render();
    } catch (e) { toast(`저장 실패: ${e.message}`, 'danger', 6000); }
  });

  // ---------- 로봇 위치 표시 ----------
  setInterval(() => {
    view.state.robots = robots.filter((r) => r.online && r.mapPose).map((r) => ({ name: r.name, color: r.color, pose: r.mapPose, selected: previewSel.value === r.id }));
    view.invalidate();
  }, 300);

  await loadMaps();
});
