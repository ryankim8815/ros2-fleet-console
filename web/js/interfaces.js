// ROS 2 인터페이스 관제: 노드 / 토픽 / 서비스 / 액션 / 파라미터
// 목록은 rosapi, 파라미터는 각 노드의 rcl_interfaces 파라미터 서비스를 직접 사용합니다.
(function () {
  'use strict';
  const { ros, $, $$, esc, toast, store } = window.App;

  const S = {
    tab: 'node',
    nodes: [], topics: new Map(), services: [], actions: [],
    node: null, detail: null, params: null, paramEdits: new Map(),
    hideBuiltin: true,
  };

  const BUILTIN_SRV = /\/(describe_parameters|get_parameters|get_parameter_types|list_parameters|set_parameters|set_parameters_atomically|get_type_description)$|\/_action\//;
  const HEAVY_TYPES = /OccupancyGrid|PointCloud2|Image|CompressedImage|Costmap/;
  const shortType = (t) => (t || '').replace('/msg/', '/').replace('/srv/', '/').replace('/action/', '/');

  // ================= 쓰기 잠금 =================
  const lock = $('#write-lock');
  lock.checked = store.get('tb3.writeLock', 'true') !== 'false';
  function applyLock() {
    store.set('tb3.writeLock', String(lock.checked));
    $$('[data-write]').forEach((b) => { b.disabled = lock.checked; b.title = lock.checked ? '쓰기 잠금을 해제해야 사용할 수 있습니다' : ''; });
  }
  lock.addEventListener('change', applyLock);
  const writable = () => { if (lock.checked) { toast('쓰기 잠금 상태입니다. 상단의 잠금을 해제하세요.', 'warn'); return false; } return true; };

  // ================= 데이터 로드 =================
  async function loadAll() {
    if (!ros.connected) return;
    try {
      const [n, t, s, a] = await Promise.all([
        ros.callService('/rosapi/nodes', {}),
        ros.callService('/rosapi/topics_and_raw_types', {}),
        ros.callService('/rosapi/services', {}),
        ros.callService('/rosapi/action_servers', {}).catch(() => ({ action_servers: [] })),
      ]);
      S.nodes = (n.nodes || []).slice().sort();
      S.topics = new Map((t.topics || []).map((name, i) => [name, t.types[i]]));
      S.services = (s.services || []).slice().sort();
      S.actions = (a.action_servers || []).slice().sort();
      $('#c-node').textContent = S.nodes.length;
      $('#c-topic').textContent = S.topics.size;
      $('#c-service').textContent = S.services.filter((x) => !BUILTIN_SRV.test(x)).length;
      $('#c-action').textContent = S.actions.length;
      renderNodeList();
      if (!S.node || !S.nodes.includes(S.node)) {
        S.node = S.nodes.find((x) => x.includes('turtlebot3_node')) || S.nodes.find((x) => !x.startsWith('/rosapi') && !x.startsWith('/rosbridge')) || S.nodes[0] || null;
      }
      await selectNode(S.node, { keepTab: true });
    } catch (e) {
      toast(`목록을 불러오지 못했습니다: ${e.message}`, 'danger');
    }
  }

  // ================= 노드 목록 =================
  function groupOf(n) {
    if (/turtlebot3|ld08|hlds|robot_state|diff_drive|joint_state/.test(n)) return '로봇';
    if (/navigator|controller|planner|amcl|map_server|behavior|smoother|velocity|waypoint|lifecycle|costmap|docking|collision|route/.test(n)) return '내비게이션 (Nav2)';
    if (/cartographer|slam|occupancy/.test(n)) return 'SLAM';
    if (/rosbridge|rosapi/.test(n)) return '웹 브리지';
    if (/_ros2cli_|transform_listener_impl|launch_ros/.test(n)) return '도구 · 임시';
    return '기타';
  }
  function renderNodeList() {
    const q = $('#node-q').value.trim().toLowerCase();
    const groups = new Map();
    S.nodes.filter((n) => !q || n.toLowerCase().includes(q)).forEach((n) => {
      const g = groupOf(n);
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(n);
    });
    const order = ['로봇', '내비게이션 (Nav2)', 'SLAM', '웹 브리지', '기타', '도구 · 임시'];
    let html = '';
    order.filter((g) => groups.has(g)).forEach((g) => {
      html += `<div class="grp">${esc(g)}</div>`;
      groups.get(g).forEach((n) => { html += `<button data-node="${esc(n)}" aria-current="${n === S.node}"><span class="name">${esc(n)}</span></button>`; });
    });
    $('#node-list').innerHTML = html || '<div class="empty">노드가 없습니다</div>';
  }
  $('#node-list').addEventListener('click', (e) => { const b = e.target.closest('[data-node]'); if (b) selectNode(b.dataset.node); });
  $('#node-q').addEventListener('input', renderNodeList);

  async function selectNode(node, { keepTab } = {}) {
    S.node = node;
    S.detail = null; S.params = null; S.paramEdits.clear();
    renderNodeList();
    if (!keepTab && S.tab !== 'node' && S.tab !== 'param') setTab('node');
    render();
    if (!node) return;
    try {
      const d = await ros.callService('/rosapi/node_details', { node });
      if (S.node !== node) return;
      const svcs = (d.services || []).slice().sort();
      S.detail = {
        pub: (d.publishing || []).slice().sort(),
        sub: (d.subscribing || []).slice().sort(),
        services: svcs.filter((x) => !BUILTIN_SRV.test(x)),
        allServices: svcs,
        actions: svcs.filter((x) => x.endsWith('/_action/send_goal')).map((x) => x.replace('/_action/send_goal', '')),
      };
    } catch (e) { S.detail = { error: e.message }; }
    render();
    loadParams(node);
  }

  // ================= 파라미터 (rcl_interfaces) =================
  const PT = { 1: 'bool', 2: 'integer', 3: 'double', 4: 'string', 5: 'byte[]', 6: 'bool[]', 7: 'integer[]', 8: 'double[]', 9: 'string[]' };
  const PV_FIELD = { 1: 'bool_value', 2: 'integer_value', 3: 'double_value', 4: 'string_value', 5: 'byte_array_value', 6: 'bool_array_value', 7: 'integer_array_value', 8: 'double_array_value', 9: 'string_array_value' };
  function pvToText(v) {
    const f = PV_FIELD[v.type];
    if (!f) return '';
    const x = v[f];
    return Array.isArray(x) ? JSON.stringify(x) : String(x);
  }
  function textToPv(type, text) {
    const v = { type };
    const f = PV_FIELD[type];
    if (type === 1) { if (!/^(true|false)$/i.test(text.trim())) throw new Error('true 또는 false'); v[f] = /^true$/i.test(text.trim()); }
    else if (type === 2) { if (!/^-?\d+$/.test(text.trim())) throw new Error('정수가 아닙니다'); v[f] = parseInt(text, 10); }
    else if (type === 3) { const n = Number(text); if (text.trim() === '' || !Number.isFinite(n)) throw new Error('실수가 아닙니다'); v[f] = n; }
    else if (type === 4) v[f] = text;
    else { const a = JSON.parse(text); if (!Array.isArray(a)) throw new Error('JSON 배열이 아닙니다'); v[f] = a; }
    return v;
  }

  async function loadParams(node) {
    const has = S.detail && S.detail.allServices && S.detail.allServices.includes(`${node}/list_parameters`);
    if (!has) { S.params = { none: true }; render(); return; }
    try {
      const l = await ros.callService(`${node}/list_parameters`, { prefixes: [], depth: 0 }, { type: 'rcl_interfaces/srv/ListParameters' });
      const names = ((l.result && l.result.names) || []).slice().sort();
      const [g, d] = await Promise.all([
        ros.callService(`${node}/get_parameters`, { names }, { type: 'rcl_interfaces/srv/GetParameters' }),
        ros.callService(`${node}/describe_parameters`, { names }, { type: 'rcl_interfaces/srv/DescribeParameters' }).catch(() => ({ descriptors: [] })),
      ]);
      if (S.node !== node) return;
      S.params = names.map((name, i) => {
        const v = g.values[i] || { type: 0 };
        const desc = (d.descriptors || [])[i] || {};
        return { name, type: v.type, text: pvToText(v), desc: desc.description || '', readOnly: !!desc.read_only };
      });
    } catch (e) { S.params = { error: e.message }; }
    render();
  }

  async function applyParams() {
    if (!writable() || !S.paramEdits.size) return;
    const node = S.node;
    const list = [];
    for (const [name, text] of S.paramEdits) {
      const p = S.params.find((x) => x.name === name);
      try { list.push({ name, value: textToPv(p.type, text) }); } catch (e) { toast(`${name}: ${e.message}`, 'warn'); return; }
    }
    if (!confirm(`${node} 의 파라미터 ${list.length}개를 변경할까요?\n\n${list.map((p) => `${p.name} = ${pvToText(p.value)}`).join('\n')}`)) return;
    try {
      const r = await ros.callService(`${node}/set_parameters`, { parameters: list }, { type: 'rcl_interfaces/srv/SetParameters' });
      const fails = (r.results || []).map((x, i) => (x.successful ? null : `${list[i].name}: ${x.reason || '거부됨'}`)).filter(Boolean);
      if (fails.length) toast(`일부 실패 — ${fails.join(', ')}`, 'warn', 6000); else toast(`파라미터 ${list.length}개를 적용했습니다`, 'ok');
      S.paramEdits.clear();
      loadParams(node);
    } catch (e) { toast(`set_parameters 실패: ${e.message}`, 'danger'); }
  }

  function exportYaml() {
    if (!Array.isArray(S.params)) return;
    const node = S.node.replace(/^\//, '');
    const lines = [`${node}:`, '  ros__parameters:'];
    // 점(.)으로 구분된 이름을 중첩 YAML 로
    const tree = {};
    S.params.forEach((p) => {
      const parts = p.name.split('.');
      let o = tree;
      parts.slice(0, -1).forEach((k) => { o = (o[k] ||= {}); });
      o[parts[parts.length - 1]] = p.type === 4 ? JSON.stringify(p.text) : p.text;
    });
    const walk = (o, ind) => Object.keys(o).forEach((k) => {
      if (typeof o[k] === 'object') { lines.push(`${ind}${k}:`); walk(o[k], `${ind}  `); } else lines.push(`${ind}${k}: ${o[k]}`);
    });
    walk(tree, '    ');
    const blob = new Blob([`${lines.join('\n')}\n`], { type: 'text/yaml' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${node.replace(/\//g, '_')}_params.yaml`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  // ================= 렌더링 =================
  function setTab(tab) {
    S.tab = tab;
    $$('[data-tab]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
    render();
  }
  $$('[data-tab]').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));

  const topicRow = (dir, name) => {
    const type = S.topics.get(name) || '';
    return `<tr class="clickable${insp.kind === 'topic' && insp.name === name ? ' selected' : ''}" data-topic="${esc(name)}">${dir ? `<td><span class="tag ${dir}">${dir.toUpperCase()}</span></td>` : ''}<td class="mono">${esc(name)}<div class="sub-type">${esc(shortType(type))}</div></td></tr>`;
  };
  const srvItem = (name) => `<div class="item"><div class="main"><div class="n">${esc(name)}</div></div><button class="btn btn-sm" data-service="${esc(name)}">열기</button></div>`;
  const actItem = (name) => `<div class="item"><div class="main"><div class="n">${esc(name)}</div></div><button class="btn btn-sm" data-action="${esc(name)}">열기</button></div>`;

  function paramsPanel() {
    let body;
    const p = S.params;
    if (!S.node) body = '<div class="empty">노드를 선택하세요</div>';
    else if (p === null) body = '<div class="empty">불러오는 중…</div>';
    else if (p.none) body = '<div class="empty">이 노드는 파라미터 서비스를 제공하지 않습니다</div>';
    else if (p.error) body = `<div class="empty">불러오기 실패: ${esc(p.error)}</div>`;
    else if (!p.length) body = '<div class="empty">파라미터가 없습니다</div>';
    else {
      body = `<div class="table-wrap"><table class="t"><thead><tr><th>이름</th><th>타입</th><th>값</th><th>설명</th></tr></thead><tbody>${p.map((x) => {
        const edited = S.paramEdits.has(x.name);
        const val = edited ? S.paramEdits.get(x.name) : x.text;
        return `<tr><td class="mono" style="font-size: 12.5px">${esc(x.name)}</td><td class="muted" style="font-size: 12px">${PT[x.type] || '—'}</td>
          <td><input data-param="${esc(x.name)}" aria-label="${esc(x.name)} 값" value="${esc(val)}" ${x.readOnly ? 'readonly title="읽기 전용 파라미터"' : ''} style="width: 100%; min-width: 120px; font-family: var(--mono); font-size: 12.5px; padding: 6px 8px; ${edited ? 'border-color: var(--warn)' : ''}"></td>
          <td class="muted" style="font-size: 12px">${x.readOnly ? '<span class="badge">읽기 전용</span> ' : ''}${esc(x.desc)}</td></tr>`;
      }).join('')}</tbody></table></div>`;
    }
    const n = S.paramEdits.size;
    return `<section class="panel"><div class="panel-head"><h2>파라미터</h2><span class="muted" style="font-size: 12px">${esc(S.node || '')} · 변경 시 set_parameters 호출</span><div class="spacer"></div>
      <button class="btn" id="p-reload">다시 읽기</button><button class="btn" id="p-export" ${Array.isArray(p) && p.length ? '' : 'disabled'}>YAML 내보내기</button>
      <button class="btn btn-primary" data-write id="p-apply" ${n ? '' : 'disabled'}>변경 ${n}건 적용</button></div>${body}</section>`;
  }

  function render() {
    const c = $('#center');
    const d = S.detail;
    let html = '';
    if (S.tab === 'node') {
      if (!S.node) html = '<section class="panel"><div class="empty">노드가 없습니다. rosbridge 연결과 로봇 bringup 을 확인하세요.</div></section>';
      else {
        const sum = d && !d.error ? [['Pub', d.pub.length], ['Sub', d.sub.length], ['서비스', d.services.length], ['파라미터', Array.isArray(S.params) ? S.params.length : '—']] : [];
        html += `<section class="panel" style="flex-direction: row; flex-wrap: wrap; align-items: center; gap: 16px">
          <div style="flex: 1 1 260px; min-width: 0"><div class="muted" style="font-size: 12px">선택된 노드</div><h1 class="mono" style="font-size: 22px; font-weight: 500; margin-top: 2px; overflow-wrap: anywhere">${esc(S.node)}</h1></div>
          <div style="display: flex; gap: 8px; flex-wrap: wrap">${sum.map(([k, v]) => `<div class="tile" style="min-width: 64px"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('')}</div></section>`;
        if (!d) html += '<section class="panel"><div class="empty">불러오는 중…</div></section>';
        else if (d.error) html += `<section class="panel"><div class="empty">노드 정보를 불러오지 못했습니다: ${esc(d.error)}</div></section>`;
        else {
          html += `<div class="row">
            <section class="panel" style="flex: 1 1 360px"><h2>토픽 <span class="muted" style="font-weight: 400; font-size: 12px">클릭하면 Echo</span></h2>
              ${d.pub.length + d.sub.length ? `<div class="table-wrap"><table class="t"><thead><tr><th>방향</th><th>토픽 / 타입</th></tr></thead><tbody>${d.pub.map((t) => topicRow('pub', t)).join('')}${d.sub.map((t) => topicRow('sub', t)).join('')}</tbody></table></div>` : '<div class="empty">없음</div>'}</section>
            <div class="col" style="flex: 1 1 300px">
              <section class="panel"><h2>서비스</h2>${d.services.length ? d.services.map(srvItem).join('') : '<div class="empty">없음 (파라미터 기본 서비스 제외)</div>'}</section>
              <section class="panel"><h2>액션 서버</h2>${d.actions.length ? d.actions.map(actItem).join('') : `<div class="empty">rosapi 는 노드별 액션 서버를 구분하지 못합니다. 실행 중인 액션 서버 ${S.actions.length}개는 <button class="btn btn-sm" data-goto="action">액션 탭</button> 에서 볼 수 있습니다.</div>`}</section>
            </div></div>`;
          html += paramsPanel();
        }
      }
    } else if (S.tab === 'topic') {
      const rows = [...S.topics.keys()].sort().map((t) => topicRow(null, t)).join('');
      html = `<section class="panel"><h2>전체 토픽 <span class="muted" style="font-weight: 400; font-size: 12px">${S.topics.size}개 · 클릭하면 Echo</span></h2><div class="table-wrap"><table class="t"><thead><tr><th>토픽 / 타입</th></tr></thead><tbody>${rows}</tbody></table></div></section>`;
    } else if (S.tab === 'service') {
      const list = S.services.filter((x) => !S.hideBuiltin || !BUILTIN_SRV.test(x));
      html = `<section class="panel"><div class="panel-head"><h2>전체 서비스</h2><span class="muted" style="font-size: 12px">${list.length}개</span><div class="spacer"></div>
        <label class="lockbar" style="font-size: 12px"><input type="checkbox" id="hide-builtin" ${S.hideBuiltin ? 'checked' : ''} style="accent-color: var(--accent)"> 파라미터·액션 기본 서비스 숨기기</label></div>
        ${list.map(srvItem).join('') || '<div class="empty">없음</div>'}</section>`;
    } else if (S.tab === 'action') {
      html = `<section class="panel"><h2>전체 액션 서버 <span class="muted" style="font-weight: 400; font-size: 12px">${S.actions.length}개</span></h2>${S.actions.map(actItem).join('') || '<div class="empty">실행 중인 액션 서버가 없습니다 (Nav2 를 실행하면 나타납니다)</div>'}</section>`;
    } else if (S.tab === 'param') {
      html = paramsPanel();
    }
    c.innerHTML = html;
    const hb = $('#hide-builtin');
    if (hb) hb.addEventListener('change', () => { S.hideBuiltin = hb.checked; render(); });
    const pr = $('#p-reload'); if (pr) pr.addEventListener('click', () => { S.paramEdits.clear(); loadParams(S.node); });
    const pe = $('#p-export'); if (pe) pe.addEventListener('click', exportYaml);
    const pa = $('#p-apply'); if (pa) pa.addEventListener('click', applyParams);
    applyLock();
  }

  $('#center').addEventListener('click', (e) => {
    const t = e.target.closest('[data-topic]'); if (t) { openTopic(t.dataset.topic); return; }
    const s = e.target.closest('[data-service]'); if (s) { openService(s.dataset.service); return; }
    const a = e.target.closest('[data-action]'); if (a) { openAction(a.dataset.action); return; }
    const g = e.target.closest('[data-goto]'); if (g) setTab(g.dataset.goto);
  });
  $('#center').addEventListener('input', (e) => {
    const i = e.target.closest('[data-param]');
    if (!i || !Array.isArray(S.params)) return;
    const p = S.params.find((x) => x.name === i.dataset.param);
    if (i.value === p.text) S.paramEdits.delete(p.name); else S.paramEdits.set(p.name, i.value);
    i.style.borderColor = S.paramEdits.has(p.name) ? 'var(--warn)' : '';
    const btn = $('#p-apply');
    btn.textContent = `변경 ${S.paramEdits.size}건 적용`;
    btn.disabled = !S.paramEdits.size || lock.checked;
  });

  // ================= 인스펙터 =================
  const insp = { kind: null, name: null, unsub: null, goal: null };
  function showInsp(kind) {
    ['empty', 'topic', 'service', 'action'].forEach((k) => $(`#insp-${k}`).classList.toggle('hidden', k !== (kind || 'empty')));
  }
  function closeInsp() {
    if (insp.unsub) { insp.unsub(); insp.unsub = null; }
    insp.kind = null; insp.name = null;
    showInsp(null);
    render();
  }
  ['echo', 'srv', 'act'].forEach((p) => $(`#${p}-close`).addEventListener('click', closeInsp));

  // ---- 메시지 기본값 생성 (rosapi typedef) ----
  function buildDefault(typedefs) {
    const map = new Map(typedefs.map((t) => [t.type, t]));
    const prim = (ft) => (ft === 'bool' ? false : /string/.test(ft) ? '' : 0);
    const build = (td, depth) => {
      const o = {};
      td.fieldnames.forEach((f, i) => {
        const ft = td.fieldtypes[i], len = td.fieldarraylen[i];
        const sub = map.get(ft);
        const one = () => (sub && depth < 8 ? build(sub, depth + 1) : prim(ft));
        if (len === 0) o[f] = [];
        else if (len > 0) o[f] = Array.from({ length: len }, one);
        else o[f] = one();
      });
      return o;
    };
    return typedefs.length ? build(typedefs[0], 0) : {};
  }

  // 큰 배열 축약 표시
  function preview(msg) {
    return JSON.stringify(msg, (k, v) => {
      if (Array.isArray(v) && v.length > 24) return [...v.slice(0, 24), `… 총 ${v.length}개`];
      if (typeof v === 'number' && !Number.isInteger(v)) return Number(v.toPrecision(6));
      return v;
    }, 2);
  }

  // ---- 토픽 Echo ----
  let echoPaused = false;
  $('#echo-pause').addEventListener('click', (e) => { echoPaused = !echoPaused; e.currentTarget.setAttribute('aria-pressed', String(echoPaused)); });

  async function openTopic(name) {
    if (insp.unsub) { insp.unsub(); insp.unsub = null; }
    Object.assign(insp, { kind: 'topic', name });
    showInsp('topic');
    render();
    const type = S.topics.get(name) || '';
    $('#echo-name').textContent = name;
    $('#echo-type').textContent = type;
    $('#echo-msg').textContent = '메시지 대기 중…';
    $('#echo-hz').textContent = '—'; $('#echo-bw').textContent = '—'; $('#echo-ps').textContent = '—';
    $('#pub-json').value = '';
    const times = [], sizes = [];
    let latest = null, dirty = false;
    const heavy = HEAVY_TYPES.test(type);
    const unsub = ros.subscribe(name, type || undefined, (m, bytes) => {
      const now = performance.now();
      times.push(now); sizes.push(bytes || 0);
      latest = m; dirty = true;
    }, { serverThrottle: heavy ? 1000 : 0 });
    const timer = setInterval(() => {
      const now = performance.now();
      while (times.length && now - times[0] > 5000) { times.shift(); sizes.shift(); }
      if (times.length >= 2) {
        const span = (times[times.length - 1] - times[0]) / 1000;
        $('#echo-hz').textContent = `${((times.length - 1) / span).toFixed(1)} Hz${heavy ? '*' : ''}`;
        const bw = sizes.reduce((a, b) => a + b, 0) / Math.max(span, 1);
        $('#echo-bw').textContent = bw > 1024 * 1024 ? `${(bw / 1048576).toFixed(1)} MB/s` : `${(bw / 1024).toFixed(1)} KB/s`;
      }
      if (dirty && !echoPaused) { dirty = false; $('#echo-msg').textContent = preview(latest); }
    }, 250);
    insp.unsub = () => { unsub(); clearInterval(timer); };
    try {
      const [p, s] = await Promise.all([ros.callService('/rosapi/publishers', { topic: name }), ros.callService('/rosapi/subscribers', { topic: name })]);
      if (insp.name === name) $('#echo-ps').textContent = `${(p.publishers || []).length} / ${(s.subscribers || []).length}`;
    } catch (e) { /* ignore */ }
  }

  $('#pub-template').addEventListener('click', async () => {
    const type = S.topics.get(insp.name);
    try {
      const r = await ros.callService('/rosapi/message_details', { type });
      $('#pub-json').value = JSON.stringify(buildDefault(r.typedefs || []), null, 2);
    } catch (e) { toast(`타입 정보를 가져오지 못했습니다: ${e.message}`, 'warn'); }
  });
  $('#pub-box').addEventListener('toggle', (e) => { if (e.target.open && !$('#pub-json').value) $('#pub-template').click(); });
  $('#pub-send').addEventListener('click', () => {
    if (!writable()) return;
    let msg;
    try { msg = JSON.parse($('#pub-json').value); } catch (e) { toast(`JSON 오류: ${e.message}`, 'warn'); return; }
    const type = S.topics.get(insp.name);
    if (!confirm(`${insp.name} (${type}) 에 메시지를 1회 발행할까요?`)) return;
    ros.publish(insp.name, type, msg);
    toast(`${insp.name} 에 발행했습니다`, 'ok');
  });

  // ---- 서비스 호출 ----
  async function openService(name) {
    if (insp.unsub) { insp.unsub(); insp.unsub = null; }
    Object.assign(insp, { kind: 'service', name });
    showInsp('service');
    $('#srv-name').textContent = name;
    $('#srv-type').textContent = '타입 확인 중…';
    $('#srv-json').value = '';
    $('#srv-res').classList.add('hidden');
    try {
      const t = await ros.callService('/rosapi/service_type', { service: name });
      insp.srvType = t.type;
      $('#srv-type').textContent = t.type || '(타입 알 수 없음)';
      const r = await ros.callService('/rosapi/service_request_details', { type: t.type });
      if (insp.name === name) $('#srv-json').value = JSON.stringify(buildDefault(r.typedefs || []), null, 2);
    } catch (e) { $('#srv-type').textContent = `타입 확인 실패: ${e.message}`; }
  }
  $('#srv-call').addEventListener('click', async () => {
    if (!writable()) return;
    let args;
    try { args = JSON.parse($('#srv-json').value || '{}'); } catch (e) { toast(`JSON 오류: ${e.message}`, 'warn'); return; }
    const name = insp.name;
    if (!confirm(`서비스 ${name} 를 호출할까요?\n\n${JSON.stringify(args)}`)) return;
    const res = $('#srv-res');
    res.classList.remove('hidden');
    res.textContent = '호출 중…';
    const t0 = performance.now();
    try {
      const v = await ros.callService(name, args, { type: insp.srvType, timeout: 15000 });
      res.textContent = `✓ 응답 ${Math.round(performance.now() - t0)} ms\n${preview(v)}`;
    } catch (e) { res.textContent = `✕ 실패: ${e.message}`; }
  });

  // ---- 액션 ----
  const actTimer = { id: null };
  async function openAction(name) {
    if (insp.unsub) { insp.unsub(); insp.unsub = null; }
    Object.assign(insp, { kind: 'action', name });
    showInsp('action');
    $('#act-name').textContent = name;
    $('#act-json').value = '';
    $('#act-type-in').value = '';
    if (!insp.goal) { $('#act-fb').textContent = '—'; $('#act-res').textContent = '—'; $('#act-state').textContent = '대기'; $('#act-time').textContent = ''; }
    // 주의: rosbridge 2.7.1 의 /rosapi/action_type 은 Jazzy 에서 rosapi 노드를 죽이므로 쓰지 않습니다.
    // 대신 설치된 인터페이스 목록에서 액션 이름과 같은 타입을 찾습니다 (navigate_to_pose -> NavigateToPose).
    const type = await guessActionType(name);
    if (insp.name !== name) return;
    $('#act-type-in').value = type || '';
    $('#act-type').textContent = type ? '이름으로 추정 · 틀리면 직접 수정' : '타입을 추정하지 못했습니다 · 직접 입력하세요';
    if (type) fillGoalTemplate();
  }

  let interfacesCache = null;
  async function guessActionType(name) {
    try {
      if (!interfacesCache) interfacesCache = (await ros.callService('/rosapi/interfaces', {}, { timeout: 8000 })).interfaces || [];
    } catch (e) { return ''; }
    const key = name.split('/').pop().replace(/_/g, '').toLowerCase();
    const hits = interfacesCache.filter((t) => t.includes('/action/') && t.split('/').pop().toLowerCase() === key);
    return hits.find((t) => t.startsWith('nav2_msgs/')) || hits[0] || '';
  }

  async function fillGoalTemplate() {
    const type = $('#act-type-in').value.trim();
    if (!type) return;
    try {
      const r = await ros.callService('/rosapi/action_goal_details', { type });
      if (!(r.typedefs || []).length) throw new Error('알 수 없는 타입');
      $('#act-json').value = JSON.stringify(buildDefault(r.typedefs), null, 2);
    } catch (e) { toast(`Goal 형식을 가져오지 못했습니다: ${e.message}`, 'warn'); }
  }
  $('#act-tpl').addEventListener('click', fillGoalTemplate);
  $('#act-send').addEventListener('click', async () => {
    if (!writable()) return;
    let args;
    try { args = JSON.parse($('#act-json').value || '{}'); } catch (e) { toast(`JSON 오류: ${e.message}`, 'warn'); return; }
    const name = insp.name, type = $('#act-type-in').value.trim();
    if (!type) { toast('액션 타입을 알 수 없습니다', 'warn'); return; }
    if (!confirm(`액션 ${name} 에 goal 을 보낼까요? 로봇이 움직일 수 있습니다.`)) return;
    const g = window.App.trackGoal(ros.sendActionGoal(name, type, args, {
      onFeedback: (f) => { $('#act-state').textContent = `실행 중 · ${name}`; $('#act-fb').textContent = preview(f); },
    }));
    insp.goal = g;
    $('#act-cancel').disabled = false;
    $('#act-state').textContent = `전송됨 · ${name}`;
    $('#act-res').textContent = '—';
    const t0 = performance.now();
    clearInterval(actTimer.id);
    actTimer.id = setInterval(() => { $('#act-time').textContent = `${((performance.now() - t0) / 1000).toFixed(0)} s`; }, 500);
    try {
      const r = await g.result;
      $('#act-state').textContent = `${window.Ros.GOAL_STATUS[r.status] || r.status} · ${name}`;
      $('#act-res').textContent = preview(r.values);
    } catch (e) {
      $('#act-state').textContent = `실패 · ${name}`;
      $('#act-res').textContent = e.message;
    }
    clearInterval(actTimer.id);
    if (insp.goal === g) { insp.goal = null; $('#act-cancel').disabled = true; }
  });
  $('#act-cancel').addEventListener('click', () => { if (insp.goal) { insp.goal.cancel(); $('#act-state').textContent = '취소 요청됨'; } });

  // ================= 시작 =================
  $('#refresh').addEventListener('click', loadAll);
  ros.on('connection', loadAll);
  setInterval(() => { if (ros.connected && S.tab !== 'param' && !S.paramEdits.size) refreshLists(); }, 10000);
  async function refreshLists() {
    // 주기적으로 목록만 갱신 (선택 상태 유지)
    try {
      const n = await ros.callService('/rosapi/nodes', {});
      const nodes = (n.nodes || []).slice().sort();
      if (nodes.join() !== S.nodes.join()) { S.nodes = nodes; $('#c-node').textContent = nodes.length; renderNodeList(); }
    } catch (e) { /* ignore */ }
  }
  applyLock();
  render();
})();
