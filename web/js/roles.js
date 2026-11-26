// 로봇 역할: 역할 지정(역할 없음·워커·경비), 워커의 이동 경로·경비의 자동 순회 시작·멈춤·재개·정지, 상태
window.App.ready.then((App) => {
  'use strict';
  const { ros, $, esc, toast } = App;
  const robots = App.robots;
  robots.forEach((r) => r.start({ tf: false, odomThrottle: 500 }));
  const ROLE = { '': '역할 없음', worker: '워커', guard: '경비' };
  const GUARD = { stopped: '정지', running: '경비 중', paused: '멈춤', alarm: '장애물 앞 대기' };
  const DETAIL = { patrolling: '순회 중', inspecting: '경보 확인하러 이동', localizing: '지도 위 위치를 기다림 (초기 위치)', no_plan: '갈 수 있는 지점 없음',
    nav_unavailable: 'Nav2 없음', rejected: 'Nav2 가 goal 을 거부', alarm: '장애물 앞 대기', paused: '멈춤', stopped: '정지' };
  const RUN = { idle: '정지', running: '실행 중', paused: '멈춤', done: '완료', failed: '실패', stopped: '정지' };

  let data = { roles: [], guard: {}, mode: 'operate', map: null };
  let assigns = [];
  let live = { robots: {} };      // /fleet/patrol_status (경비 노드의 세부 상태)
  ros.subscribe('/fleet/patrol_status', 'std_msgs/msg/String', (m) => { try { live = JSON.parse(m.data); live.at = Date.now(); } catch (e) { /* ignore */ } }, { throttle: 1000 });

  async function load() {
    try {
      data = await (await fetch('/api/roles', { cache: 'no-store' })).json();
      assigns = (await (await fetch('/api/assignments', { cache: 'no-store' })).json()).assignments || [];
    } catch (e) { /* 다음에 */ }
    render();
  }

  const lastHtml = { v: '' };
  function render() {
    // 모드·경비 노드
    const note = $('#mode-note');
    if (data.mode !== 'operate') {
      note.innerHTML = '<b>지금은 지도 만들기 모드입니다.</b><span class="muted">역할(이동 경로·경비)은 운영 모드에서만 동작합니다. 지도 만들기를 끝내면 운영 모드로 돌아옵니다.</span>';
      note.classList.remove('hidden');
    } else if (!data.map) {
      note.innerHTML = '<b>활성(기준) 지도가 없습니다.</b><span class="muted">워커·경비 모두 활성 지도에서 움직입니다.</span><a class="btn btn-primary" href="mapping.html" style="text-decoration: none">지도 만들기</a>';
      note.classList.remove('hidden');
    } else note.classList.add('hidden');
    const g = data.guard || {};
    $('#guard-proc').textContent = g.running ? `경비 노드 실행 중 · 지도 ${g.map || data.map}` : '경비 노드 꺼짐 (경비를 시작하면 켜짐)';

    const fresh = live.at && Date.now() - live.at < 4000;
    const html = robots.map((r) => {
      const row = data.roles.find((x) => x.robot_id === r.id) || { role: null, guard_state: 'stopped' };
      const role = row.role || '';
      let st = '', acts = '';
      if (role === 'worker') {
        const a = assigns.find((x) => x.robot_id === r.id);
        if (!a) {
          st = '<span>할당된 경로 없음</span><span class="sub"><a href="routes.html">이동 경로</a>에서 경로와 반복 횟수를 할당하세요</span>';
        } else {
          const loops = a.repeat_count ? `${a.loops_done}/${a.repeat_count}바퀴` : `${a.loops_done}바퀴 (계속)`;
          const cls = a.state === 'running' ? 'run' : a.state === 'failed' ? 'bad' : a.state === 'paused' ? 'warn' : '';
          st = `<span><span class="badge ${cls}">${RUN[a.state] || a.state}</span> ${esc(a.route_name)} · ${loops}${a.state === 'running' ? ` · 다음 지점 ${a.waypoint + 1}/${a.points}` : ''}</span>
            ${a.message ? `<span class="sub">${esc(a.message)}</span>` : ''}`;
          const running = a.state === 'running';
          const canResume = ['paused', 'failed', 'stopped'].includes(a.state) && a.run_id;
          acts = `<button class="btn btn-sm btn-primary" data-w="start" ${running ? 'disabled' : ''}>시작</button>
            <button class="btn btn-sm" data-w="pause" ${running ? '' : 'disabled'}>멈춤</button>
            <button class="btn btn-sm" data-w="resume" ${canResume ? '' : 'disabled'}>이어서</button>
            <button class="btn btn-sm btn-danger-outline" data-w="stop" ${running || a.state === 'paused' ? '' : 'disabled'}>정지</button>`;
        }
      } else if (role === 'guard') {
        const gs = row.guard_state;
        const lv = fresh ? live.robots[r.id] : null;
        const cls = gs === 'running' ? 'run' : gs === 'alarm' ? 'bad' : gs === 'paused' ? 'warn' : '';
        let sub = '';
        if (gs === 'alarm' && row.alarm_alert_id) sub = `<a href="patrol.html#alert-${row.alarm_alert_id}">경보 #${row.alarm_alert_id}</a> 를 처리(이상 없음·오탐·기준 지도 반영)하면 다시 경비합니다`;
        else if (lv && gs === 'running') sub = `${esc(DETAIL[lv.state] || lv.state)}${lv.points ? ` · 지점 ${Math.min(lv.waypoint + 1, lv.points)}/${lv.points} · ${lv.laps}바퀴` : ''}`;
        else if (gs === 'running' && !g.running) sub = '경비 노드를 기다리는 중';
        st = `<span><span class="badge ${cls}">${GUARD[gs] || gs}</span></span>
          ${sub ? `<span class="sub">${sub}</span>` : ''}${row.message && gs !== 'running' ? `<span class="sub">${esc(row.message)}</span>` : ''}`;
        acts = `<button class="btn btn-sm btn-primary" data-g="start" ${gs === 'stopped' ? '' : 'disabled'}>경비 시작</button>
          <button class="btn btn-sm" data-g="pause" ${gs === 'running' || gs === 'alarm' ? '' : 'disabled'}>멈춤</button>
          <button class="btn btn-sm${gs === 'alarm' ? ' btn-primary' : ''}" data-g="resume" ${gs === 'paused' || gs === 'alarm' ? '' : 'disabled'}>재개</button>
          <button class="btn btn-sm btn-danger-outline" data-g="stop" ${gs === 'stopped' ? 'disabled' : ''}>정지</button>`;
      } else {
        st = '<span class="muted">수동 조종만</span>';
        acts = `<a class="btn btn-sm" href="${App.robotUrl(r.id, 'index.html')}" style="text-decoration: none">대시보드</a>`;
      }
      return `<div class="rcard${row.guard_state === 'alarm' ? ' alarm' : ''}" data-robot="${esc(r.id)}">
        <div style="display: flex; gap: 8px; align-items: center; min-width: 0"><span class="swatch" style="background: ${esc(r.color)}"></span>
          <span style="display: flex; flex-direction: column; min-width: 0"><b>${esc(r.name)}</b><span class="muted" style="font-size: 12px">${esc(r.label)}${r.online ? '' : ' · 오프라인'}</span></span></div>
        <select data-role aria-label="${esc(r.name)} 역할">${Object.entries(ROLE).map(([v, t]) => `<option value="${v}"${v === role ? ' selected' : ''}>${t}</option>`).join('')}</select>
        <div class="st">${st}</div>
        <div class="acts">${acts}</div>
      </div>`;
    }).join('') || '<div class="empty" style="padding: 16px">등록된 로봇이 없습니다 → <a href="setup.html">로봇 등록</a></div>';
    // 누르려던 버튼·열어 둔 select 가 사라지지 않게 내용이 바뀔 때만 다시 그림
    const focused = document.activeElement && document.activeElement.closest('#role-list select');
    if (html !== lastHtml.v && !focused) { $('#role-list').innerHTML = html; lastHtml.v = html; }
  }

  $('#role-list').addEventListener('change', async (e) => {
    const sel = e.target.closest('[data-role]');
    if (!sel) return;
    const id = sel.closest('[data-robot]').dataset.robot;
    const r = robots.find((x) => x.id === id);
    const row = data.roles.find((x) => x.robot_id === id) || {};
    const busy = (row.role === 'guard' && row.guard_state !== 'stopped') || (row.role === 'worker' && (assigns.find((a) => a.robot_id === id) || {}).state === 'running');
    if (busy && !confirm(`${r.name} 은(는) 지금 ${ROLE[row.role]} 로 움직이는 중입니다. 역할을 바꾸면 멈춥니다. 바꿀까요?`)) { sel.value = row.role || ''; return; }
    try {
      await App.adminJson(`/api/roles/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify({ role: sel.value || null }) });
      toast(`${r.name}: 역할 → ${ROLE[sel.value]}`, 'ok', 2500);
    } catch (err) { toast(`역할을 바꾸지 못했습니다: ${err.message}`, 'danger', 6000); sel.value = row.role || ''; }
    lastHtml.v = '';
    load();
  });

  $('#role-list').addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-w], button[data-g]');
    if (!b || b.disabled) return;
    const id = b.closest('[data-robot]').dataset.robot;
    const r = robots.find((x) => x.id === id);
    const enc = encodeURIComponent(id);
    try {
      if (b.dataset.w) {
        const act = b.dataset.w;
        if (act === 'start' && !confirm(`${r.name}: 경로를 처음(1바퀴째 첫 지점)부터 시작할까요? 로봇이 스스로 움직입니다.`)) return;
        const url = `/api/assignments/${enc}/${act}`;
        if (act === 'start' || act === 'resume') await App.adminJson(url, { method: 'POST', body: '{}' });
        else { const res = await fetch(url, { method: 'POST' }); if (!res.ok) throw new Error((await res.json()).error || res.status); }
        toast(`${r.name}: ${{ start: '경로 시작', resume: '이어서 감', pause: '멈춤', stop: '정지' }[act]}`, act === 'start' || act === 'resume' ? 'ok' : 'warn', 2000);
      } else {
        const act = b.dataset.g;
        if (act === 'start' && !confirm(`${r.name}: 경비를 시작할까요? 로봇이 지도 전체를 스스로 돌아다닙니다.`)) return;
        const url = `/api/roles/${enc}/guard/${act}`;
        if (act === 'start' || act === 'resume') await App.adminJson(url, { method: 'POST', body: '{}' });
        else {
          ros.publish('/fleet/patrol_cmd', 'std_msgs/msg/String', { data: JSON.stringify({ cmd: 'pause', robot: id }) });   // 바로 멈추게
          const res = await fetch(url, { method: 'POST' }); if (!res.ok) throw new Error((await res.json()).error || res.status);
        }
        toast(`${r.name}: ${{ start: '경비 시작', resume: '경비 재개', pause: '경비 멈춤', stop: '경비 정지' }[act]}`, act === 'start' || act === 'resume' ? 'ok' : 'warn', 2000);
      }
    } catch (err) { toast(`실패: ${err.message}`, 'danger', 6000); }
    lastHtml.v = '';
    load();
  });

  // 최근 역할·경비 상태 변경 (DB role_changes)
  const VAL = { ...GUARD, worker: '워커', guard: '경비' };
  async function loadHistory() {
    try {
      const h = (await (await fetch('/api/roles/history?limit=15', { cache: 'no-store' })).json()).changes || [];
      $('#role-hist').innerHTML = h.length ? h.map((c) => {
        const r = robots.find((x) => x.id === c.robot_id) || { name: c.robot_id, color: '#8B949E' };
        const what = c.kind === 'role' ? '역할' : '경비';
        return `<div style="display: flex; gap: 8px; flex-wrap: wrap"><span class="muted mono">${new Date(c.t * 1000).toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
          <span><span class="swatch" style="background: ${esc(r.color)}"></span> ${esc(r.name)}</span><b>${what}</b>
          <span>${esc(VAL[c.previous] || c.previous || '없음')} → ${esc(VAL[c.value] || c.value || '없음')}</span><span class="muted">${esc(c.message)}</span></div>`;
      }).join('') : '<div class="empty">아직 변경이 없습니다</div>';
    } catch (e) { /* 다음에 */ }
  }
  loadHistory();
  setInterval(loadHistory, 5000);

  document.addEventListener('tb3:estop', () => setTimeout(load, 300));
  load();
  setInterval(load, 2000);
  setInterval(render, 1000);
});
