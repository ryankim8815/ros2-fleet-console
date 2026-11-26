"""로봇 역할: 워커(이동 경로 반복)·경비(자율 경비) — 읽고 쓰는 함수 (DB: fleet_db.py 의 robot_roles, 스키마 v2).

  워커 (worker): 이동 경로 화면에서 할당한 경로를 반복 (route_runner_node.py). 시작·이어서는 워커만 됨.
  경비 (guard):  기준 지도의 갈 수 있는 곳을 빠짐없이 도는 지점을 자동으로 만들어 반복하며(coverage.py),
                 지도에 없던 장애물을 찾으면 그 자리에 멈추고 소리(TurtleBot3 부저, Pinky LED·표정 + 관제 웹 경고음)로
                 알린 뒤, 관제실이 그 경보를 처리하거나 '재개'를 누를 때까지 기다림 (patrol_node.py).
  역할 없음:     줄이 없음. 수동 조종만.

경비 상태(guard_state)는 서버(사용자 명령)와 경비 노드(장애물 발견·처리 확인)가 함께 씀:
  stopped  경비 안 함                      ← 정지, 역할 바꿈
  running  경비 중                         ← 시작·재개 / 노드: 대기 중이던 경보가 처리되면
  paused   멈춤 (이어서 가능)               ← 멈춤, 전체 비상정지, 지도 만들기 모드, 서버 재시작
  alarm    장애물 앞에서 대기 (alarm_alert_id) ← 노드: 경비 중에 새 장애물 경보를 만들었을 때
바뀔 때마다 role_changes 에 한 줄 (역할: kind 'role', 경비 상태: kind 'guard') — fleet_db v4.
"""
import time

import fleet_db

ROLES = fleet_db.ROLES
GUARD_STATES = fleet_db.GUARD_STATES
ROLE_LABEL = {'worker': '워커', 'guard': '경비', None: '역할 없음'}


def _log(con, robot_id, kind, previous, value, message=''):
    """이력 한 줄 (트랜잭션 안에서). v4 전 DB 면 건너뜀"""
    try:
        con.execute('INSERT INTO role_changes (t, robot_id, kind, previous, value, message) VALUES (?, ?, ?, ?, ?, ?)',
                    (time.time(), robot_id, kind, previous, value, str(message)[:200]))
    except Exception:  # noqa: BLE001
        pass


def history(con, robot_id=None, limit=30):
    """최근 역할·경비 상태 변경 [{t, robot_id, kind, previous, value, message}]"""
    q, args = 'SELECT * FROM role_changes', []
    if robot_id:
        q += ' WHERE robot_id = ?'
        args.append(robot_id)
    try:
        return [dict(r) for r in con.execute(q + ' ORDER BY t DESC, id DESC LIMIT ?', (*args, max(1, min(int(limit), 200))))]
    except Exception:  # noqa: BLE001
        return []


def _row(r):
    return {'robot_id': r['robot_id'], 'role': r['role'], 'guard_state': r['guard_state'],
            'alarm_alert_id': r['alarm_alert_id'], 'message': r['message'], 'updated_at': r['updated_at']}


def get_role(con, robot_id):
    r = con.execute('SELECT * FROM robot_roles WHERE robot_id = ?', (robot_id,)).fetchone()
    return _row(r) if r else None


def role_of(con, robot_id):
    r = get_role(con, robot_id)
    return r['role'] if r else None


def list_roles(con):
    """등록된(active) 로봇 전부: 역할이 없으면 role None"""
    rows = con.execute('SELECT r.id, r.name, r.type, x.* FROM robots r LEFT JOIN robot_roles x ON x.robot_id = r.id '
                       'WHERE r.active = 1 ORDER BY r.id').fetchall()
    out = []
    for r in rows:
        d = {'robot_id': r['id'], 'name': r['name'], 'type': r['type'], 'role': r['role'],
             'guard_state': r['guard_state'] or 'stopped', 'alarm_alert_id': r['alarm_alert_id'],
             'message': r['message'] or '', 'updated_at': r['updated_at']}
        out.append(d)
    return out


def _check_robot(con, robot_id):
    r = con.execute('SELECT active FROM robots WHERE id = ?', (robot_id,)).fetchone()
    if not r or not r['active']:
        raise KeyError(robot_id)


def set_role(con, robot_id, role):
    """역할 지정 (None = 역할 없음). 경비를 그만두면 경비 상태는 stopped. 반환: (새 행 또는 None, 이전 역할)"""
    _check_robot(con, robot_id)
    if role not in (None, *ROLES):
        raise ValueError('역할은 worker | guard | null')
    before = get_role(con, robot_id)
    prev = before['role'] if before else None
    now = time.time()
    with con:
        if prev != role:
            _log(con, robot_id, 'role', prev, role, '역할 지정')
            if before and before['role'] == 'guard' and before['guard_state'] != 'stopped':
                _log(con, robot_id, 'guard', before['guard_state'], 'stopped', '역할을 바꿔 경비 정지')
        if role is None:
            con.execute('DELETE FROM robot_roles WHERE robot_id = ?', (robot_id,))
        elif prev != role:
            con.execute("INSERT INTO robot_roles (robot_id, role, guard_state, alarm_alert_id, message, updated_at) "
                        "VALUES (?, ?, 'stopped', NULL, '', ?) ON CONFLICT (robot_id) DO UPDATE SET role = excluded.role, "
                        "guard_state = 'stopped', alarm_alert_id = NULL, message = '', updated_at = excluded.updated_at",
                        (robot_id, role, now))
    return get_role(con, robot_id), prev


# ------------------------------------------------------------------ 경비 상태 (서버: 사용자 명령)
def guard_control(con, robot_id, action, message=''):
    """start | resume → running, pause → paused, stop → stopped. 경비 역할만."""
    r = get_role(con, robot_id)
    if not r or r['role'] != 'guard':
        raise ValueError('경비 역할인 로봇만 경비를 시작·멈출 수 있습니다 (역할 화면에서 경비로 지정)')
    target = {'start': 'running', 'resume': 'running', 'pause': 'paused', 'stop': 'stopped'}.get(action)
    if target is None:
        raise ValueError('action 은 start | resume | pause | stop')
    if action == 'resume' and r['guard_state'] not in ('paused', 'alarm'):
        raise ValueError('멈춤이나 장애물 대기 중일 때만 재개할 수 있습니다')
    msg = message or {'start': '시작', 'resume': '재개', 'pause': '사용자 멈춤', 'stop': '정지'}[action]
    with con:
        if r['guard_state'] != target:
            _log(con, robot_id, 'guard', r['guard_state'], target, msg)
        con.execute('UPDATE robot_roles SET guard_state = ?, alarm_alert_id = NULL, message = ?, updated_at = ? WHERE robot_id = ?',
                    (target, msg[:200], time.time(), robot_id))
    return get_role(con, robot_id)


def pause_all_guards(con, message):
    """경비 중·대기 중인 경비를 모두 멈춤 (비상정지·지도 만들기 모드·서버 재시작). 반환: 멈춘 수"""
    with con:
        for r in con.execute("SELECT robot_id, guard_state FROM robot_roles WHERE guard_state IN ('running', 'alarm')").fetchall():
            _log(con, r['robot_id'], 'guard', r['guard_state'], 'paused', message)
        return con.execute("UPDATE robot_roles SET guard_state = 'paused', alarm_alert_id = NULL, message = ?, updated_at = ? "
                           "WHERE guard_state IN ('running', 'alarm')", (message[:200], time.time())).rowcount


def any_guard_active(con):
    return con.execute("SELECT 1 FROM robot_roles WHERE guard_state IN ('running', 'alarm') LIMIT 1").fetchone() is not None


# ------------------------------------------------------------------ 경비 노드 쪽
def guard_rows(con):
    """{robot_id: 행} — 경비 역할인 로봇 전부"""
    return {r['robot_id']: _row(r) for r in con.execute("SELECT * FROM robot_roles WHERE role = 'guard'")}


def set_alarm(con, robot_id, alert_id, message):
    """경비 중(running)이던 로봇만 대기(alarm)로. 바뀌었으면 True (그 사이 사용자가 멈췄으면 False)"""
    with con:
        n = con.execute("UPDATE robot_roles SET guard_state = 'alarm', alarm_alert_id = ?, message = ?, updated_at = ? "
                        "WHERE robot_id = ? AND guard_state = 'running'", (alert_id, message[:200], time.time(), robot_id)).rowcount
        if n:
            _log(con, robot_id, 'guard', 'running', 'alarm', message)
    return n > 0


def clear_alarm(con, robot_id, message):
    """대기(alarm) 중이던 로봇만 다시 경비 중(running)으로. 바뀌었으면 True"""
    with con:
        n = con.execute("UPDATE robot_roles SET guard_state = 'running', alarm_alert_id = NULL, message = ?, updated_at = ? "
                        "WHERE robot_id = ? AND guard_state = 'alarm'", (message[:200], time.time(), robot_id)).rowcount
        if n:
            _log(con, robot_id, 'guard', 'alarm', 'running', message)
    return n > 0
