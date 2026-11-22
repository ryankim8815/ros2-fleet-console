// 로봇 등록: fleet.json 편집 (추가·수정·삭제·순서), 네임스페이스 자동 검색, 연결 테스트
window.App.ready.then((App) => {
  'use strict';
  const { ros, $, $$, esc, toast } = App;
  const COLORS = ['#2DD4BF', '#FB923C', '#A78BFA', '#F472B6', '#FACC15', '#60A5FA', '#4ADE80', '#F87171'];
  let list = JSON.parse(JSON.stringify(App.fleet.robots));
  let mapTopic = App.fleet.mapTopic || '/map';
  let dirty = false;
  let editIdx = -1;
  let type = 'turtlebot3';
  let color = COLORS[list.length % COLORS.length];
  const form = $('#form');
  const F = (n) => form.elements[n];

  // ---------- 목록 ----------
  const live = new Map(App.robots.map((r) => [r.id, r]));
  App.robots.forEach((r) => r.start({ tf: false }));

  function setDirty(d) {
    dirty = d;
    $('#save').disabled = !d;
    $('#dirty-note').textContent = d ? '저장하지 않은 변경이 있습니다. 저장하면 다른 페이지는 새로고침 후 반영됩니다.' : '';
  }
  window.addEventListener('beforeunload', (e) => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });

  function renderRows() {
    $('#rows').innerHTML = list.map((r, i) => {
      const L = live.get(r.id);
      const st = !L ? '<span class="badge">저장 후 확인</span>' : L.online ? '<span class="badge ok"><span class="dot"></span>온라인</span>' : '<span class="badge">오프라인</span>';
      return `<tr${i === editIdx ? ' class="selected"' : ''}>
        <td><span style="display: inline-flex; align-items: center; gap: 8px; font-weight: 600"><span class="swatch" style="background: ${esc(r.color)}"></span>${esc(r.name)}</span><div class="sub-type">${esc(r.id)}</div></td>
        <td class="muted">${esc((window.Profiles[r.type] || {}).model || r.type)}</td>
        <td class="mono">${esc(r.ns || '(없음)')}</td><td class="mono">${esc(r.tfTopic)}</td><td class="mono">${esc(r.tfPrefix || '—')}</td>
        <td>${st}</td>
        <td style="text-align: right; white-space: nowrap">
          <button class="btn btn-sm btn-icon" data-up="${i}" aria-label="${esc(r.name)} 위로"${i ? '' : ' disabled'}>↑</button>
          <button class="btn btn-sm" data-edit="${i}">편집</button>
          <button class="btn btn-sm btn-danger-outline" data-del="${i}">삭제</button></td></tr>`;
    }).join('') || '<tr><td colspan="7" class="empty">등록된 로봇이 없습니다</td></tr>';
  }
  setInterval(renderRows, 2000);

  $('#rows').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.edit) startEdit(Number(b.dataset.edit));
    if (b.dataset.del) {
      const i = Number(b.dataset.del);
      if (!confirm(`${list[i].name} 을(를) 목록에서 뺄까요? (저장해야 반영)`)) return;
      list.splice(i, 1); if (editIdx === i) resetForm(); setDirty(true); renderRows();
    }
    if (b.dataset.up) { const i = Number(b.dataset.up); [list[i - 1], list[i]] = [list[i], list[i - 1]]; setDirty(true); renderRows(); }
  });

  // 저장은 관리자 토큰 필요 (PC 의 ros2-fleet-console/config/admin_token). 한 번 입력하면 이 브라우저에 기억
  async function putFleet(retry) {
    const token = App.store.get('tb3.adminToken', '');
    const r = await fetch('/api/fleet', { method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-Admin-Token': token }, body: JSON.stringify({ mapTopic, robots: list, ...(App.fleet.mapFile ? { mapFile: App.fleet.mapFile } : {}) }) });
    if (r.status === 401 && retry) {
      const t = prompt('관리자 토큰을 입력하세요.\nPC 에서: cat ~/turtlebot3/ros2-fleet-console/config/admin_token', '');
      if (!t) throw new Error('토큰이 없어 저장하지 않았습니다');
      App.store.set('tb3.adminToken', t.trim());
      return putFleet(false);
    }
    return r;
  }
  $('#save').addEventListener('click', async () => {
    try {
      const r = await putFleet(true);
      const body = await r.json();
      if (r.status === 401) App.store.set('tb3.adminToken', '');
      if (!r.ok) throw new Error(body.error || r.status);
      list = body.robots;
      setDirty(false);
      toast(`로봇 ${list.length}대를 저장했습니다`, 'ok');
      setTimeout(() => location.reload(), 600);
    } catch (e) { toast(`저장 실패: ${e.message}`, 'danger', 6000); }
  });

  const mapIn = $('#map-topic');
  mapIn.value = mapTopic;
  mapIn.addEventListener('input', () => {
    if (mapIn.checkValidity() && mapIn.value.trim()) { mapTopic = mapIn.value.trim(); setDirty(true); }
  });

  // ---------- 폼 ----------
  function renderColors() {
    $('#colors').innerHTML = COLORS.map((c) => `<button type="button" role="radio" class="led" style="background: ${c}; width: 32px; height: 32px" aria-checked="${c === color}" aria-pressed="${c === color}" aria-label="색 ${c}" data-color="${c}"></button>`).join('');
  }
  $('#colors').addEventListener('click', (e) => { const b = e.target.closest('[data-color]'); if (b) { color = b.dataset.color; renderColors(); } });

  function setType(t) {
    type = t;
    $$('#type-pick [data-type]').forEach((b) => {
      const on = b.dataset.type === t;
      b.setAttribute('aria-checked', String(on));
      b.style.borderColor = on ? 'var(--accent)' : '';
      b.style.boxShadow = on ? '0 0 0 1px var(--accent)' : '';
    });
    deriveTf();
    $('#tf-hint').textContent = t === 'pinky'
      ? 'Pinky: 프레임 이름이 고정이라 TF 를 <ns>/tf 로 분리하고 접두사는 비웁니다 (pinky_ns.launch.py 사용).'
      : 'TurtleBot3: 프레임 접두사는 X/. TF 토픽은 robot.launch.py namespace:=X 면 /tf, tb3_ns.launch.py(Nav2 용) 면 /X/tf 입니다. 자동 검색이 실제 값을 채웁니다.';
  }
  $$('#type-pick [data-type]').forEach((b) => b.addEventListener('click', () => setType(b.dataset.type)));

  // 네임스페이스로부터 TF 설정 기본값
  let tfTouched = false;
  function deriveTf() {
    if (tfTouched) return;
    const ns = F('ns').value.trim().replace(/\/$/, '');
    const leaf = ns.replace(/^\//, '');
    if (type === 'pinky') { F('tfTopic').value = ns ? `${ns}/tf` : '/tf'; F('tfPrefix').value = ''; }
    else { F('tfTopic').value = '/tf'; F('tfPrefix').value = leaf ? `${leaf.split('/').pop()}/` : ''; }
  }
  F('ns').addEventListener('input', () => {
    deriveTf();
    const leaf = F('ns').value.trim().replace(/^\/|\/$/g, '').split('/').pop();
    if (editIdx < 0 && leaf) { if (!F('id').dataset.touched) F('id').value = leaf; if (!F('name').dataset.touched) F('name').value = leaf.replace(/_/g, '-'); }
  });
  ['tfTopic', 'tfPrefix'].forEach((n) => F(n).addEventListener('input', () => { tfTouched = true; }));
  ['id', 'name'].forEach((n) => F(n).addEventListener('input', () => { F(n).dataset.touched = '1'; }));

  function resetForm() {
    editIdx = -1; tfTouched = false;
    form.reset();
    ['id', 'name'].forEach((n) => delete F(n).dataset.touched);
    color = COLORS[list.length % COLORS.length];
    $('#form-title').textContent = '로봇 추가';
    $('#submit').textContent = '목록에 추가';
    $('#checks').innerHTML = '';
    setType('turtlebot3'); renderColors(); renderRows();
  }
  function startEdit(i) {
    const r = list[i];
    editIdx = i; tfTouched = true;
    F('name').value = r.name; F('id').value = r.id; F('ns').value = r.ns; F('tfTopic').value = r.tfTopic; F('tfPrefix').value = r.tfPrefix;
    color = r.color;
    $('#form-title').textContent = `편집: ${r.name}`;
    $('#submit').textContent = '변경 적용';
    $('#checks').innerHTML = '';
    setType(r.type); renderColors(); renderRows();
  }
  $('#cancel-edit').addEventListener('click', resetForm);

  function formValue() {
    return {
      id: F('id').value.trim(), name: F('name').value.trim(), type,
      ns: F('ns').value.trim().replace(/\/$/, ''), tfTopic: F('tfTopic').value.trim(), tfPrefix: F('tfPrefix').value.trim(), color,
    };
  }
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const v = formValue();
    const others = list.filter((_, i) => i !== editIdx);
    if (others.some((r) => r.id === v.id)) { toast(`ID 중복: ${v.id}`, 'warn'); return; }
    if (others.some((r) => r.ns === v.ns)) { toast(`네임스페이스 중복: ${v.ns || '(없음)'}`, 'warn'); return; }
    if (editIdx >= 0) list[editIdx] = v; else list.push(v);
    setDirty(true);
    toast(`${v.name} ${editIdx >= 0 ? '수정' : '추가'} — 저장을 눌러야 반영됩니다`, 'info');
    resetForm();
  });

  // ---------- 연결 테스트 ----------
  async function graph() {
    const [t, s] = await Promise.all([ros.callService('/rosapi/topics_and_raw_types', {}), ros.callService('/rosapi/services', {})]);
    return { topics: new Map((t.topics || []).map((n, i) => [n, t.types[i]])), services: new Set(s.services || []) };
  }
  $('#test').addEventListener('click', async () => {
    const v = formValue();
    const prof = window.Profiles[v.type];
    const box = $('#checks');
    box.innerHTML = '<div class="muted" style="font-size: 12px">확인 중…</div>';
    let g;
    try { g = await graph(); } catch (e) { box.innerHTML = `<div class="muted">rosapi 조회 실패: ${esc(e.message)}</div>`; return; }
    const t = (n) => `${v.ns}/${n}`;
    const items = [
      ['topic', t('odom'), 'nav_msgs/msg/Odometry', true],
      ['topic', t('scan'), 'sensor_msgs/msg/LaserScan', true],
      ['topic', t('cmd_vel'), null, true],
      ...prof.battery.map((b) => ['topic', t(b.topic), b.type, true]),
      ...Object.keys(prof.services).map((s) => ['service', t(s), prof.services[s], false]),
      ['topic', v.tfTopic, 'tf2_msgs/msg/TFMessage', true],
    ];
    const rows = items.map(([kind, name, typ, req]) => {
      let ok;
      let note = typ ? typ.split('/').pop() : '';
      if (kind === 'topic') {
        ok = g.topics.has(name);
        if (ok && typ && g.topics.get(name) !== typ) { note = `타입 다름: ${g.topics.get(name)}`; ok = false; }
        if (ok && !typ) note = g.topics.get(name).split('/').pop();
      } else ok = g.services.has(name);
      const cls = ok ? 'ok' : req ? 'no' : 'unk';
      return `<div class="check"><span class="${cls}">${ok ? '✓' : req ? '✕' : '–'}</span><span class="mono" style="overflow-wrap: anywhere">${esc(name)}</span><span class="muted">${esc(ok || !req ? note : '없음')}</span></div>`;
    });
    // TF 프레임 확인: 실제 /tf 에서 <prefix>odom 이 보이는지 2초간 수신
    rows.push('<div class="check"><span class="unk" id="tf-mark">…</span><span class="mono" id="tf-name"></span><span class="muted" id="tf-note">TF 확인 중</span></div>');
    box.innerHTML = rows.join('');
    $('#tf-name').textContent = `${v.tfPrefix}odom → ${v.tfPrefix}base_footprint`;
    const frames = new Set();
    const un = ros.subscribe(v.tfTopic, 'tf2_msgs/msg/TFMessage', (m) => (m.transforms || []).forEach((x) => frames.add(`${x.header.frame_id.replace(/^\//, '')}>${x.child_frame_id.replace(/^\//, '')}`)));
    setTimeout(() => {
      un();
      const want = `${v.tfPrefix}odom>${v.tfPrefix}base_footprint`;
      const ok = frames.has(want);
      $('#tf-mark').textContent = ok ? '✓' : '✕';
      $('#tf-mark').className = ok ? 'ok' : 'no';
      $('#tf-note').textContent = ok ? '수신' : (frames.size ? `없음 (본 것: ${[...frames].slice(0, 3).join(', ')})` : '수신 없음');
    }, 2500);
  });

  // ---------- 자동 검색 ----------
  $('#discover').addEventListener('click', async () => {
    let g;
    try { g = await graph(); } catch (e) { toast(`rosapi 조회 실패: ${e.message}`, 'danger'); return; }
    const found = [];
    for (const [name, typ] of g.topics) {
      if (typ !== 'nav_msgs/msg/Odometry' || !name.endsWith('/odom')) continue;
      const ns = name.slice(0, -'/odom'.length);
      const has = (s) => g.services.has(`${ns}/${s}`) || g.topics.has(`${ns}/${s}`);
      let guess = null;
      if (has('set_emotion') || has('set_led') || has('battery/percent')) guess = 'pinky';
      else if (has('motor_power') || has('sensor_state') || has('battery_state')) guess = 'turtlebot3';
      // TF 를 <ns>/tf 로 보내는지 (Pinky 래퍼, tb3_ns.launch.py) 아니면 전역 /tf 인지
      const tf = g.topics.has(`${ns}/tf`) ? `${ns}/tf` : '/tf';
      found.push({ ns, guess, tf, known: list.some((r) => r.ns === ns) });
    }
    $('#found-panel').classList.remove('hidden');
    $('#found').innerHTML = found.length ? found.map((f) => `<div class="item"><div class="main"><div class="n">${esc(f.ns || '(네임스페이스 없음)')}</div><div class="ty">${f.guess ? `추정: ${esc(window.Profiles[f.guess].model)}` : '종류를 추정하지 못함'}</div></div>
      ${f.known ? '<span class="badge ok">등록됨</span>' : `<button class="btn btn-sm" data-use="${esc(f.ns)}" data-guess="${esc(f.guess || 'turtlebot3')}" data-tf="${esc(f.tf)}">폼에 채우기</button>`}</div>`).join('')
      : '<div class="empty">&lt;ns&gt;/odom 토픽을 찾지 못했습니다. 로봇 bringup 이 실행 중인지 확인하세요.</div>';
  });
  $('#found').addEventListener('click', (e) => {
    const b = e.target.closest('[data-use]'); if (!b) return;
    resetForm();
    F('ns').value = b.dataset.use;
    setType(b.dataset.guess);
    F('ns').dispatchEvent(new Event('input'));
    if (b.dataset.tf) { F('tfTopic').value = b.dataset.tf; tfTouched = true; }   // 실제로 쓰는 TF 토픽
    F('name').focus();
  });

  resetForm();
});
