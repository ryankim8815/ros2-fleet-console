"""LiDAR 안전 구역 설정: 가까운 물체가 있으면 정지·서행. 플릿 기본값 + 로봇별 덮어쓰기.

저장: data/fleet.db (fleet_db.py, 스키마 v2)
  safety_settings  플릿 기본값 한 줄 (id = 1): enabled, stop_dist, slow_dist, slow_speed, updated_at
  robot_safety     로봇별 덮어쓰기 (robot_id 마다 한 줄). 줄이 없는 로봇은 기본값을 따름
  safety_changes   바꿀 때마다 한 줄: t, robot_id(NULL = 기본값), action(set | clear), 바뀐 뒤 값, by(바꾼 화면)

  - stop_dist  (m)   로봇 중심(base_footprint)에서 이 거리 안에 물체가 있으면 정지
  - slow_dist  (m)   이 거리 안에 물체가 있으면 직진 속도를 slow_speed 로 제한 (서행)
  - slow_speed (m/s) 서행 속도
  값 범위는 fleet_db.SAFETY_LIMITS (DB 도 CHECK 로 막음).

쓰는 곳 (로봇마다 '적용 값' = 덮어쓰기가 있으면 그것, 없으면 기본값):
  - Nav2 (자율 주행·이동 경로·경비 순찰): robot/nav2_params.py 가 실행할 때 read_for_ns(<네임스페이스>) 로 읽어
    collision_monitor 의 LidarStop·LidarSlow 원형 구역으로 넣음.
    켜기·끄기는 실행 중에도 바로 반영 (웹이 <ns>/collision_monitor 의 LidarStop.enabled·LidarSlow.enabled 를 바꿈).
    거리·속도는 Nav2 를 다시 띄워야 반영 (collision_monitor 가 실행 중 변경을 받지 않음, 확인함).
  - 웹 수동 조종 (web/js/robot.js Robot.cmdVel): 같은 값으로 바로 반영.

토큰: 바꾼 뒤 어떤 로봇의 적용 값도 '꺼짐으로 바뀌거나 거리·속도가 달라지지' 않으면(= 켜기만) 토큰 없이, 그 밖은 관리자 토큰.
"""
import sqlite3
import time

import fleet_db

DEFAULTS = fleet_db.SAFETY_DEFAULTS
LIMITS = fleet_db.SAFETY_LIMITS
MIN_GAP = fleet_db.SAFETY_MIN_GAP
KEYS = ('stop_dist', 'slow_dist', 'slow_speed')


def _row(r):
    return {'enabled': bool(r['enabled']), **{k: float(r[k]) for k in KEYS}}


def _vals(v):
    return (int(v['enabled']), v['stop_dist'], v['slow_dist'], v['slow_speed'])


def _open_ro(path):
    con = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def read_safety(con=None, path=fleet_db.DB_PATH):
    """플릿 기본값 {enabled, stop_dist, slow_dist, slow_speed, since}.

    con 이 없으면(Nav2 launch 등 다른 프로세스) DB 를 읽기 전용으로 열어 읽음: DB 를 새로 만들거나 버전을 올리지 않음
    (그건 서버 몫). DB 가 없거나 아직 v2 가 아니면 기본값."""
    own = con is None
    try:
        if own:
            con = _open_ro(path)
        r = con.execute('SELECT * FROM safety_settings WHERE id = 1').fetchone()
        if r:
            return {**_row(r), 'since': float(r['updated_at'])}
    except sqlite3.Error:
        pass
    finally:
        if own and con is not None:
            con.close()
    return dict(DEFAULTS, since=0)


def robot_overrides(con):
    """{robot_id: {enabled, stop_dist, slow_dist, slow_speed, since}} — 덮어쓰기가 있는 로봇만"""
    return {r['robot_id']: {**_row(r), 'since': float(r['updated_at'])}
            for r in con.execute('SELECT * FROM robot_safety ORDER BY robot_id')}


def effective(con, robot_id):
    """그 로봇에 적용되는 값 + source('robot' | 'fleet')"""
    r = con.execute('SELECT * FROM robot_safety WHERE robot_id = ?', (robot_id,)).fetchone()
    if r:
        return {**_row(r), 'source': 'robot'}
    f = read_safety(con)
    return {k: f[k] for k in ('enabled', *KEYS)} | {'source': 'fleet'}


def read_for_ns(ns, path=fleet_db.DB_PATH):
    """Nav2 launch 용: 네임스페이스('tb3_01', '' …)로 로봇을 찾아 적용 값. 읽기 전용, 못 찾으면 기본값."""
    want = '/' + ns.strip('/') if ns.strip('/') else ''
    try:
        con = _open_ro(path)
    except sqlite3.Error:
        return dict(DEFAULTS, source='fleet', robot=None)
    try:
        rows = con.execute('SELECT id, ns FROM robots WHERE active = 1').fetchall()
        hit = [r['id'] for r in rows if ('/' + r['ns'].strip('/') if r['ns'].strip('/') else '') == want]
        if len(hit) == 1:
            return {**effective(con, hit[0]), 'robot': hit[0]}
        return {**{k: v for k, v in read_safety(con).items() if k != 'since'}, 'source': 'fleet', 'robot': None}
    except sqlite3.Error:
        return dict(DEFAULTS, source='fleet', robot=None)
    finally:
        con.close()


def validate(body, cur):
    """입력 검사 → 정리한 값 (enabled, stop_dist, slow_dist, slow_speed). 빠진 키는 지금 값(cur)."""
    if not isinstance(body, dict):
        raise ValueError('JSON 객체여야 합니다')
    en = body.get('enabled', cur['enabled'])
    if not isinstance(en, bool):
        raise ValueError('enabled 는 true/false 여야 합니다')
    out = {'enabled': en}
    for k, (lo, hi) in LIMITS.items():
        v = body.get(k, cur[k])
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f'{k} 는 숫자여야 합니다')
        v = round(float(v), 3)
        if not lo <= v <= hi:
            raise ValueError(f'{k} 는 {lo} ~ {hi} 사이여야 합니다 (받은 값 {v})')
        out[k] = v
    if out['slow_dist'] - out['stop_dist'] < MIN_GAP - 1e-6:
        raise ValueError(f'서행 거리는 정지 거리보다 {MIN_GAP * 100:.0f} cm 이상 커야 합니다')
    return out


def _safer_or_same(before, after):
    """적용 값이 켜기만 바뀌었는지 (꺼지지 않고, 거리·속도가 같음)"""
    return (after['enabled'] or not before['enabled']) and all(abs(after[k] - before[k]) < 1e-9 for k in KEYS)


def _robot_ids(con):
    return [r['id'] for r in con.execute('SELECT id FROM robots WHERE active = 1')]


# ------------------------------------------------------------------ 플릿 기본값
def fleet_needs_token(con, body):
    """기본값을 따르는 로봇 중 하나라도 꺼지거나 값이 바뀌면 토큰 필요"""
    cur = read_safety(con)
    new = validate(body, cur)
    return not _safer_or_same(cur, new)


def write_safety(con, body):
    """플릿 기본값 저장 + 변경 이력 한 줄. 값이 그대로면 이력은 남기지 않음."""
    cur = read_safety(con)
    new = validate(body, cur)
    if new != {k: cur[k] for k in new}:
        now = time.time()
        with con:
            con.execute('UPDATE safety_settings SET enabled = ?, stop_dist = ?, slow_dist = ?, slow_speed = ?, updated_at = ? '
                        'WHERE id = 1', (*_vals(new), now))
            _log(con, now, None, 'set', new, body.get('by', ''))
    return read_safety(con)


# ------------------------------------------------------------------ 로봇별 덮어쓰기
def robot_needs_token(con, robot_id, body):
    """body 가 None 이면 덮어쓰기 지우기(기본값으로). 그 로봇의 적용 값이 켜기만 바뀌면 토큰 불필요"""
    before = effective(con, robot_id)
    if body is None:
        f = read_safety(con)
        after = {k: f[k] for k in ('enabled', *KEYS)}
    else:
        after = validate(body, before)
    return not _safer_or_same(before, after)


def write_robot(con, robot_id, body):
    """로봇 덮어쓰기 저장 (빠진 키는 지금 적용 값). 반환: 그 로봇의 적용 값"""
    if robot_id not in _robot_ids(con):
        raise KeyError(robot_id)
    before = effective(con, robot_id)
    new = validate(body, before)
    if before['source'] != 'robot' or new != {k: before[k] for k in new}:
        now = time.time()
        with con:
            con.execute('INSERT INTO robot_safety (robot_id, enabled, stop_dist, slow_dist, slow_speed, updated_at) '
                        'VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (robot_id) DO UPDATE SET enabled = excluded.enabled, '
                        'stop_dist = excluded.stop_dist, slow_dist = excluded.slow_dist, slow_speed = excluded.slow_speed, '
                        'updated_at = excluded.updated_at', (robot_id, *_vals(new), now))
            _log(con, now, robot_id, 'set', new, body.get('by', ''))
    return effective(con, robot_id)


def clear_robot(con, robot_id, by=''):
    """로봇 덮어쓰기를 지워 기본값으로. 반환: 그 로봇의 적용 값"""
    if robot_id not in _robot_ids(con):
        raise KeyError(robot_id)
    if con.execute('SELECT 1 FROM robot_safety WHERE robot_id = ?', (robot_id,)).fetchone():
        now = time.time()
        with con:
            con.execute('DELETE FROM robot_safety WHERE robot_id = ?', (robot_id,))
            _log(con, now, robot_id, 'clear', read_safety(con), by)
    return effective(con, robot_id)


# ------------------------------------------------------------------ 이력·한 번에 읽기
def _log(con, t, robot_id, action, v, by):
    con.execute('INSERT INTO safety_changes (t, robot_id, action, enabled, stop_dist, slow_dist, slow_speed, by) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (t, robot_id, action, *_vals(v), str(by or '')[:40]))


def list_changes(con, limit=10, robot_id=None):
    q, args = 'SELECT * FROM safety_changes', []
    if robot_id:
        q += ' WHERE robot_id = ?'
        args.append(robot_id)
    rows = con.execute(q + ' ORDER BY t DESC, id DESC LIMIT ?', (*args, max(1, min(int(limit), 100)))).fetchall()
    return [{'t': r['t'], 'robot': r['robot_id'], 'action': r['action'], 'by': r['by'], **_row(r)} for r in rows]


def snapshot(con, limit=10):
    """GET /api/safety: 기본값(위 키들) + robots{id: 적용 값(source 포함)} + overrides + changes"""
    return {**read_safety(con),
            'robots': {rid: effective(con, rid) for rid in _robot_ids(con)},
            'changes': list_changes(con, limit)}
