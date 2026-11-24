"""지점(좌표), 이동 경로, 로봇별 경로 할당과 실행 이력 저장소 (SQLite, data/places.db).

테이블 (스키마 버전 2, PRAGMA user_version)
  maps               지도 이름 (config/maps/<name>.yaml 을 저장·서버 시작 때 등록)
  robots             로봇 (config/fleet.json 과 동기화, 목록에서 빠지면 active = 0)
  places             지점: 지도별, 이름은 지도 안에서 겹치지 않음
  routes             이동 경로: 지도별, 이름은 지도 안에서 겹치지 않음
  route_points       경로의 지점 순서. (route_id, map)·(place_id, map) 복합 외래 키로 같은 지도의 지점만
  route_assignments  로봇마다 경로 하나 + 반복 횟수(repeat_count) + 지금 실행(current_run_id)
  route_runs         시작할 때마다 한 줄: 상태·진행(바퀴, 지점)·시각 (실행기가 진행을 씀)
  route_run_events   실행 중 일어난 일 (시작·멈춤·이어서·바퀴 완료·실패·경로 수정 …)

좌표는 그 지도의 map 프레임(m), 방향 yaw 는 라디안(-pi..pi)입니다.
값의 범위·상태 값은 CHECK 제약으로 DB 가 직접 막습니다 (다른 도구로 고쳐도 깨지지 않게).
serve.py 가 /api/places · /api/routes · /api/assignments 로 읽고 쓰고,
robot/route_runner_node.py 가 실행 중인 run 을 Nav2 로 돌리며 진행을 기록합니다.
"""
import math
import os
import re
import sys
import time

import dbutil

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, 'data', 'places.db')

MAP_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$')
ROBOT_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,40}$')
CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')
MAX_PER_MAP = 500
MAX_COORD = 10000.0
MAX_ROUTE_POINTS = 100
MAX_REPEAT = 10000
RUN_STATES = ('running', 'paused', 'done', 'failed', 'stopped')
EVENT_KINDS = ('start', 'resume', 'pause', 'stop', 'loop', 'done', 'fail', 'edit')


# ------------------------------------------------------------------ 스키마 · 마이그레이션
def _v1(con):
    """버전 1: 버전 관리 전의 구조 (이미 있는 DB 는 그대로, 새 DB 는 만들기만 하고 바로 2 로)"""
    dbutil.script(con, '''
CREATE TABLE IF NOT EXISTS places (
  id INTEGER PRIMARY KEY AUTOINCREMENT, map TEXT NOT NULL, name TEXT NOT NULL,
  x REAL NOT NULL, y REAL NOT NULL, yaw REAL NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE (map, name));
CREATE TABLE IF NOT EXISTS routes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, map TEXT NOT NULL, name TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE (map, name));
CREATE TABLE IF NOT EXISTS route_points (
  route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE, seq INTEGER NOT NULL,
  place_id INTEGER NOT NULL REFERENCES places(id), PRIMARY KEY (route_id, seq));
CREATE TABLE IF NOT EXISTS route_assignments (
  robot_id TEXT PRIMARY KEY, route_id INTEGER NOT NULL REFERENCES routes(id),
  repeat_count INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'stopped',
  loops_done INTEGER NOT NULL DEFAULT 0, waypoint INTEGER NOT NULL DEFAULT 0,
  run_state TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL, updated_at REAL NOT NULL, started_at REAL, finished_at REAL, heartbeat REAL);
''')


def _v2(con):
    """버전 2: 지도·로봇 참조 테이블, 같은 지도 강제(복합 외래 키), CHECK 제약, 할당과 실행 이력 분리"""
    now = time.time()
    bad = con.execute('SELECT COUNT(*) FROM route_points p JOIN routes r ON r.id = p.route_id '
                      'LEFT JOIN places pl ON pl.id = p.place_id AND pl.map = r.map WHERE pl.id IS NULL').fetchone()[0]
    if bad:
        print(f'places.db 마이그레이션: 경로와 다른 지도의 지점(또는 없는 지점) {bad}개를 경로에서 뺍니다', file=sys.stderr, flush=True)
    dbutil.script(con, f'''
CREATE TABLE maps (
  name TEXT PRIMARY KEY CHECK (length(name) BETWEEN 1 AND 41),
  created_at REAL NOT NULL
);
CREATE TABLE robots (
  id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 41),
  name TEXT NOT NULL DEFAULT '',
  ns TEXT NOT NULL DEFAULT '',
  type TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),   -- fleet.json 에서 빠지면 0 (지우지 않음: 이력 보존)
  updated_at REAL NOT NULL
);
INSERT INTO maps (name, created_at)
  SELECT map, MIN(created_at) FROM (SELECT map, created_at FROM places UNION ALL SELECT map, created_at FROM routes) GROUP BY map;
INSERT INTO robots (id, active, updated_at) SELECT robot_id, 0, {now} FROM route_assignments;

CREATE TABLE places_v2 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  map TEXT NOT NULL REFERENCES maps(name) ON UPDATE CASCADE,
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 40),
  x REAL NOT NULL CHECK (abs(x) <= {MAX_COORD}),
  y REAL NOT NULL CHECK (abs(y) <= {MAX_COORD}),
  yaw REAL NOT NULL DEFAULT 0 CHECK (yaw BETWEEN -3.1416 AND 3.1416),
  note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 200),
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (map, name),
  UNIQUE (id, map)                      -- route_points 의 복합 외래 키 대상
);
INSERT INTO places_v2 SELECT id, map, name, x, y, yaw, note, created_at, updated_at FROM places;

CREATE TABLE routes_v2 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  map TEXT NOT NULL REFERENCES maps(name) ON UPDATE CASCADE,
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 40),
  note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 200),
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (map, name),
  UNIQUE (id, map)
);
INSERT INTO routes_v2 SELECT id, map, name, note, created_at, updated_at FROM routes;

CREATE TABLE route_points_v2 (
  route_id INTEGER NOT NULL,
  seq INTEGER NOT NULL CHECK (seq >= 0),
  place_id INTEGER NOT NULL,
  map TEXT NOT NULL,                    -- 경로와 지점이 같은 지도인지 DB 가 확인하게 하는 열
  PRIMARY KEY (route_id, seq),
  FOREIGN KEY (route_id, map) REFERENCES routes(id, map) ON DELETE CASCADE,
  FOREIGN KEY (place_id, map) REFERENCES places(id, map)   -- 경로에서 쓰는 지점은 지울 수 없음
);
INSERT INTO route_points_v2 (route_id, seq, place_id, map)
  SELECT p.route_id, p.seq, p.place_id, r.map FROM route_points p
  JOIN routes r ON r.id = p.route_id JOIN places pl ON pl.id = p.place_id AND pl.map = r.map;

CREATE TABLE route_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  robot_id TEXT NOT NULL REFERENCES robots(id),
  route_id INTEGER REFERENCES routes(id) ON DELETE SET NULL,   -- 경로를 지워도 이력은 남김
  route_name TEXT NOT NULL,             -- 시작할 때의 경로 이름 (경로가 지워져도 보이게)
  map TEXT NOT NULL,
  repeat_count INTEGER NOT NULL CHECK (repeat_count BETWEEN 0 AND {MAX_REPEAT}),   -- 0 = 멈출 때까지 계속
  state TEXT NOT NULL CHECK (state IN {RUN_STATES}),
  loops_done INTEGER NOT NULL DEFAULT 0 CHECK (loops_done >= 0),
  waypoint INTEGER NOT NULL DEFAULT 0 CHECK (waypoint >= 0),   -- 이번 바퀴에서 다음에 갈 지점 순번 (0 부터)
  run_state TEXT NOT NULL DEFAULT '',   -- 실행기가 쓰는 세부 상태: moving | nav_unavailable
  message TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL,
  finished_at REAL,
  heartbeat REAL,                       -- 실행기가 마지막으로 진행을 쓴 시각
  updated_at REAL NOT NULL
);
INSERT INTO route_runs (robot_id, route_id, route_name, map, repeat_count, state, loops_done, waypoint,
                        run_state, message, started_at, finished_at, heartbeat, updated_at)
  SELECT a.robot_id, a.route_id, r.name, r.map, a.repeat_count, a.state, a.loops_done, a.waypoint,
         a.run_state, a.message, a.started_at, a.finished_at, a.heartbeat, a.updated_at
  FROM route_assignments a JOIN routes r ON r.id = a.route_id WHERE a.started_at IS NOT NULL;

CREATE TABLE route_run_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES route_runs(id) ON DELETE CASCADE,
  t REAL NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN {EVENT_KINDS}),
  loop INTEGER,                         -- 그때 끝낸 바퀴 수
  waypoint INTEGER,
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE route_assignments_v2 (
  robot_id TEXT PRIMARY KEY REFERENCES robots(id),
  route_id INTEGER NOT NULL REFERENCES routes(id),            -- 할당된 경로는 지울 수 없음
  repeat_count INTEGER NOT NULL DEFAULT 1 CHECK (repeat_count BETWEEN 0 AND {MAX_REPEAT}),
  current_run_id INTEGER REFERENCES route_runs(id) ON DELETE SET NULL,   -- 아직 시작 안 했으면 NULL
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
INSERT INTO route_assignments_v2 (robot_id, route_id, repeat_count, current_run_id, created_at, updated_at)
  SELECT a.robot_id, a.route_id, a.repeat_count,
         (SELECT MAX(id) FROM route_runs x WHERE x.robot_id = a.robot_id), a.created_at, a.updated_at
  FROM route_assignments a;

DROP TABLE route_assignments;
DROP TABLE route_points;
DROP TABLE routes;
DROP TABLE places;
ALTER TABLE places_v2 RENAME TO places;
ALTER TABLE routes_v2 RENAME TO routes;
ALTER TABLE route_points_v2 RENAME TO route_points;
ALTER TABLE route_assignments_v2 RENAME TO route_assignments;

CREATE INDEX route_points_place ON route_points(place_id);          -- "이 지점을 쓰는 경로" 찾기
CREATE INDEX route_assignments_route ON route_assignments(route_id);
CREATE INDEX route_runs_robot ON route_runs(robot_id, started_at);
CREATE INDEX route_runs_state ON route_runs(state);
CREATE INDEX route_run_events_run ON route_run_events(run_id, t);
''')


MIGRATIONS = [_v1, _v2]


def db(path=DB_PATH):
    return dbutil.connect(path, MIGRATIONS)


# ------------------------------------------------------------------ 검증
def _map_name(v):
    if not isinstance(v, str) or not MAP_RE.match(v):
        raise ValueError('지도 이름 형식이 올바르지 않습니다')
    return v


def _name(v):
    if not isinstance(v, str):
        raise ValueError('이름은 문자열이어야 합니다')
    v = ' '.join(v.split())
    if not (1 <= len(v) <= 40) or CTRL_RE.search(v):
        raise ValueError('이름은 1~40자여야 합니다')
    return v


def _coord(v, key):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > MAX_COORD:
        raise ValueError(f'{key} 는 숫자여야 합니다')
    return round(float(v), 3)


def _yaw(v):
    v = _coord(v, 'yaw')
    return round(math.atan2(math.sin(v), math.cos(v)), 4)    # -pi..pi 로


def _note(v):
    if not isinstance(v, str) or len(v) > 200:
        raise ValueError('메모는 200자 이하')
    return CTRL_RE.sub(' ', v).strip()


def _row(r):
    return dict(r) if r else None


# ------------------------------------------------------------------ 지도 · 로봇 (참조 테이블)
def ensure_map(con, name):
    """지도 등록 (지도 파일이 있는지는 부르는 쪽에서 확인)"""
    with con:
        con.execute('INSERT OR IGNORE INTO maps (name, created_at) VALUES (?, ?)', (_map_name(name), time.time()))


def sync_maps(con, names):
    """서버 시작 때: config/maps 의 지도를 모두 등록 (파일이 없어진 지도도 지점·경로가 있으면 남김)"""
    for n in names:
        if MAP_RE.match(n or ''):
            ensure_map(con, n)


def sync_robots(con, robots):
    """config/fleet.json 의 로봇 목록과 맞춤: 있는 로봇은 active = 1, 빠진 로봇은 active = 0 (이력 때문에 지우지 않음)"""
    now = time.time()
    ids = []
    with con:
        for r in robots:
            if not ROBOT_RE.match(str(r.get('id', ''))):
                continue
            ids.append(r['id'])
            con.execute('INSERT INTO robots (id, name, ns, type, active, updated_at) VALUES (?,?,?,?,1,?) '
                        'ON CONFLICT(id) DO UPDATE SET name = excluded.name, ns = excluded.ns, type = excluded.type, '
                        'active = 1, updated_at = excluded.updated_at',
                        (r['id'], str(r.get('name', ''))[:40], str(r.get('ns', '')), str(r.get('type', '')), now))
        q = 'UPDATE robots SET active = 0, updated_at = ? WHERE active = 1'
        if ids:
            q += f' AND id NOT IN ({",".join("?" * len(ids))})'
        con.execute(q, (now, *ids))


def _robot_active(con, robot_id):
    r = con.execute('SELECT active FROM robots WHERE id = ?', (robot_id,)).fetchone()
    return bool(r and r['active'])


# ------------------------------------------------------------------ 지점
def list_places(con, map_name):
    q = 'SELECT * FROM places WHERE map = ? ORDER BY name COLLATE NOCASE'
    return [_row(r) for r in con.execute(q, (_map_name(map_name),))]


def get_place(con, pid):
    return _row(con.execute('SELECT * FROM places WHERE id = ?', (int(pid),)).fetchone())


def _check_unique(con, map_name, name, pid=None):
    r = con.execute('SELECT id FROM places WHERE map = ? AND name = ?', (map_name, name)).fetchone()
    if r and r['id'] != pid:
        raise ValueError(f'이 지도에 이미 "{name}" 지점이 있습니다')


def create_place(con, data):
    if not isinstance(data, dict):
        raise ValueError('요청 형식이 올바르지 않습니다')
    m = _map_name(data.get('map'))
    name = _name(data.get('name'))
    x, y = _coord(data.get('x'), 'x'), _coord(data.get('y'), 'y')
    yaw = _yaw(data.get('yaw', 0))
    note = _note(data.get('note', ''))
    n = con.execute('SELECT COUNT(*) FROM places WHERE map = ?', (m,)).fetchone()[0]
    if n >= MAX_PER_MAP:
        raise ValueError(f'지도마다 지점은 최대 {MAX_PER_MAP}개입니다')
    _check_unique(con, m, name)
    ensure_map(con, m)
    now = time.time()
    with con:
        cur = con.execute('INSERT INTO places (map, name, x, y, yaw, note, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)',
                          (m, name, x, y, yaw, note, now, now))
    return get_place(con, cur.lastrowid)


def update_place(con, pid, data):
    """바꿀 항목만 받음: name, x, y, yaw, note. 없는 id 면 KeyError. 이 지점을 쓰는 경로도 함께 바뀜."""
    cur = get_place(con, pid)
    if not cur:
        raise KeyError(pid)
    if not isinstance(data, dict):
        raise ValueError('요청 형식이 올바르지 않습니다')
    unknown = set(data) - {'name', 'x', 'y', 'yaw', 'note', 'id', 'map'}
    if unknown:
        raise ValueError(f'알 수 없는 항목: {", ".join(sorted(unknown))}')
    if 'map' in data and data['map'] != cur['map']:
        raise ValueError('지점의 지도는 바꿀 수 없습니다')
    new = dict(cur)
    if 'name' in data:
        new['name'] = _name(data['name'])
        _check_unique(con, cur['map'], new['name'], cur['id'])
    if 'x' in data:
        new['x'] = _coord(data['x'], 'x')
    if 'y' in data:
        new['y'] = _coord(data['y'], 'y')
    if 'yaw' in data:
        new['yaw'] = _yaw(data['yaw'])
    if 'note' in data:
        new['note'] = _note(data['note'])
    with con:
        con.execute('UPDATE places SET name = ?, x = ?, y = ?, yaw = ?, note = ?, updated_at = ? WHERE id = ?',
                    (new['name'], new['x'], new['y'], new['yaw'], new['note'], time.time(), cur['id']))
    return get_place(con, cur['id'])


def delete_place(con, pid):
    used = [r['name'] for r in con.execute(
        'SELECT DISTINCT r.name FROM route_points p JOIN routes r ON r.id = p.route_id WHERE p.place_id = ?', (int(pid),))]
    if used:
        raise ValueError(f'이동 경로에서 쓰는 지점이라 지울 수 없습니다: {", ".join(used)}')
    with con:
        n = con.execute('DELETE FROM places WHERE id = ?', (int(pid),)).rowcount
    if not n:
        raise KeyError(pid)
    return {'deleted': int(pid)}


# ------------------------------------------------------------------ 이동 경로
def _route_points(con, rid):
    q = ('SELECT p.seq, p.place_id, pl.name, pl.x, pl.y, pl.yaw FROM route_points p '
         'JOIN places pl ON pl.id = p.place_id WHERE p.route_id = ? ORDER BY p.seq')
    return [dict(r) for r in con.execute(q, (rid,))]


def _route(con, r):
    if not r:
        return None
    d = dict(r)
    d['points'] = _route_points(con, d['id'])
    d['robots'] = [x[0] for x in con.execute('SELECT robot_id FROM route_assignments WHERE route_id = ? ORDER BY robot_id', (d['id'],))]
    return d


def list_routes(con, map_name):
    q = 'SELECT * FROM routes WHERE map = ? ORDER BY name COLLATE NOCASE'
    return [_route(con, r) for r in con.execute(q, (_map_name(map_name),)).fetchall()]


def get_route(con, rid):
    return _route(con, con.execute('SELECT * FROM routes WHERE id = ?', (int(rid),)).fetchone())


def _point_ids(con, map_name, pts):
    if not isinstance(pts, list) or not (1 <= len(pts) <= MAX_ROUTE_POINTS):
        raise ValueError(f'경로에는 지점이 1~{MAX_ROUTE_POINTS}개 있어야 합니다')
    out = []
    for v in pts:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError('points 는 지점 id 목록입니다')
        r = con.execute('SELECT map FROM places WHERE id = ?', (v,)).fetchone()
        if not r:
            raise ValueError(f'없는 지점입니다: {v}')
        if r['map'] != map_name:
            raise ValueError('다른 지도의 지점은 넣을 수 없습니다')
        out.append(v)
    for a, b in zip(out, out[1:]):
        if a == b:
            raise ValueError('같은 지점을 연달아 넣을 수 없습니다')
    return out


def _check_route_name(con, map_name, name, rid=None):
    r = con.execute('SELECT id FROM routes WHERE map = ? AND name = ?', (map_name, name)).fetchone()
    if r and r['id'] != rid:
        raise ValueError(f'이 지도에 이미 "{name}" 경로가 있습니다')


def _write_points(con, rid, map_name, ids):
    con.execute('DELETE FROM route_points WHERE route_id = ?', (rid,))
    con.executemany('INSERT INTO route_points (route_id, seq, place_id, map) VALUES (?,?,?,?)',
                    [(rid, i, p, map_name) for i, p in enumerate(ids)])


def create_route(con, data):
    if not isinstance(data, dict):
        raise ValueError('요청 형식이 올바르지 않습니다')
    m = _map_name(data.get('map'))
    name = _name(data.get('name'))
    note = _note(data.get('note', ''))
    ids = _point_ids(con, m, data.get('points'))
    _check_route_name(con, m, name)
    ensure_map(con, m)
    now = time.time()
    with con:
        cur = con.execute('INSERT INTO routes (map, name, note, created_at, updated_at) VALUES (?,?,?,?,?)', (m, name, note, now, now))
        _write_points(con, cur.lastrowid, m, ids)
    return get_route(con, cur.lastrowid)


def update_route(con, rid, data):
    """바꿀 항목만: name, note, points. 실행 중인 로봇은 실행기가 바뀐 경로로 이어서 갑니다."""
    cur = get_route(con, rid)
    if not cur:
        raise KeyError(rid)
    if not isinstance(data, dict):
        raise ValueError('요청 형식이 올바르지 않습니다')
    unknown = set(data) - {'name', 'note', 'points', 'id', 'map', 'robots'}
    if unknown:
        raise ValueError(f'알 수 없는 항목: {", ".join(sorted(unknown))}')
    if 'map' in data and data['map'] != cur['map']:
        raise ValueError('경로의 지도는 바꿀 수 없습니다')
    name = _name(data['name']) if 'name' in data else cur['name']
    _check_route_name(con, cur['map'], name, cur['id'])
    note = _note(data['note']) if 'note' in data else cur['note']
    ids = _point_ids(con, cur['map'], data['points']) if 'points' in data else None
    now = time.time()
    with con:
        con.execute('UPDATE routes SET name = ?, note = ?, updated_at = ? WHERE id = ?', (name, note, now, cur['id']))
        if ids is not None and ids != [p['place_id'] for p in cur['points']]:
            _write_points(con, cur['id'], cur['map'], ids)
            # 로봇마다 지금 실행(이어서 갈 수 있는 것)만: 지점 수가 줄어 진행 순번이 범위를 넘으면 그 바퀴의 처음으로, 이력에 남김
            live = con.execute("SELECT id, loops_done, waypoint FROM route_runs WHERE route_id = ? AND state IN "
                               "('running', 'paused', 'failed', 'stopped') "
                               'AND id IN (SELECT current_run_id FROM route_assignments)', (cur['id'],)).fetchall()
            for r in live:
                wp = r['waypoint'] if r['waypoint'] < len(ids) else 0
                con.execute('UPDATE route_runs SET waypoint = ?, updated_at = ? WHERE id = ?', (wp, now, r['id']))
                _event(con, r['id'], 'edit', f'경로 지점 {len(cur["points"])}개 → {len(ids)}개', r['loops_done'], wp)
    return get_route(con, cur['id'])


def delete_route(con, rid):
    if not get_route(con, rid):
        raise KeyError(rid)
    used = [r[0] for r in con.execute('SELECT robot_id FROM route_assignments WHERE route_id = ?', (int(rid),))]
    if used:
        raise ValueError(f'로봇에 할당된 경로라 지울 수 없습니다 (먼저 할당 해제): {", ".join(used)}')
    with con:
        con.execute('DELETE FROM routes WHERE id = ?', (int(rid),))   # 실행 이력의 route_id 는 NULL, 이름은 남음
    return {'deleted': int(rid)}


# ------------------------------------------------------------------ 할당 · 실행 (run) · 이력 (event)
def _event(con, run_id, kind, message='', loop=None, waypoint=None):
    con.execute('INSERT INTO route_run_events (run_id, t, kind, loop, waypoint, message) VALUES (?,?,?,?,?,?)',
                (run_id, time.time(), kind, loop, waypoint, str(message)[:200]))


def _run(con, run_id):
    return _row(con.execute('SELECT * FROM route_runs WHERE id = ?', (run_id,)).fetchone()) if run_id else None


def _set_run(con, run, state, message, kind, finished=False):
    now = time.time()
    con.execute('UPDATE route_runs SET state = ?, run_state = \'\', message = ?, updated_at = ?, '
                'finished_at = CASE WHEN ? THEN ? ELSE NULL END WHERE id = ?',
                (state, message, now, 1 if finished else 0, now, run['id']))
    _event(con, run['id'], kind, message, run['loops_done'], run['waypoint'])


def _assignment(con, r):
    """할당 + 지금 실행을 합친 모양 (API 응답은 버전 1 과 같은 항목 + run_id, robot_active)"""
    if not r:
        return None
    d = dict(r)
    rt = con.execute('SELECT name, map FROM routes WHERE id = ?', (d['route_id'],)).fetchone()
    d['route_name'], d['map'] = (rt['name'], rt['map']) if rt else ('', '')
    d['points'] = con.execute('SELECT COUNT(*) FROM route_points WHERE route_id = ?', (d['route_id'],)).fetchone()[0]
    d['robot_active'] = _robot_active(con, d['robot_id'])
    run = _run(con, d.pop('current_run_id'))
    d['run_id'] = run['id'] if run else None
    for k in ('state', 'loops_done', 'waypoint', 'run_state', 'message', 'started_at', 'finished_at', 'heartbeat'):
        d[k] = run[k] if run else {'state': 'idle', 'loops_done': 0, 'waypoint': 0, 'run_state': '', 'message': ''}.get(k)
    return d


def list_assignments(con):
    return [_assignment(con, r) for r in con.execute('SELECT * FROM route_assignments ORDER BY robot_id').fetchall()]


def get_assignment(con, robot_id):
    return _assignment(con, con.execute('SELECT * FROM route_assignments WHERE robot_id = ?', (robot_id,)).fetchone())


def _repeat(v):
    if isinstance(v, bool) or not isinstance(v, int) or not (0 <= v <= MAX_REPEAT):
        raise ValueError(f'반복 횟수는 0(멈출 때까지)~{MAX_REPEAT} 의 정수입니다')
    return v


def set_assignment(con, robot_id, data):
    """로봇에 경로 할당(새로 또는 바꾸기).
    경로를 바꾸면: 실행 중이면 거절, 멈춤 등 끝나지 않은 실행은 정지로 닫고 새로 시작 대기.
    반복 횟수만 바꾸면: 끝나지 않은 실행의 목표 횟수도 바꿈 (이미 그만큼 돌았으면 완료)."""
    if not ROBOT_RE.match(str(robot_id)) or not _robot_active(con, robot_id):
        raise ValueError(f'등록되지 않은 로봇: {robot_id}')
    if not isinstance(data, dict):
        raise ValueError('요청 형식이 올바르지 않습니다')
    cur = con.execute('SELECT * FROM route_assignments WHERE robot_id = ?', (robot_id,)).fetchone()
    rid = data.get('route_id', cur['route_id'] if cur else None)
    if isinstance(rid, bool) or not isinstance(rid, int) or not get_route(con, rid):
        raise ValueError('없는 경로입니다')
    rep = _repeat(data.get('repeat_count', cur['repeat_count'] if cur else 1))
    run = _run(con, cur['current_run_id']) if cur else None
    now = time.time()
    with con:
        if cur is None:
            con.execute('INSERT INTO route_assignments (robot_id, route_id, repeat_count, created_at, updated_at) VALUES (?,?,?,?,?)',
                        (robot_id, rid, rep, now, now))
        elif rid != cur['route_id']:
            if run and run['state'] == 'running':
                raise ValueError('실행 중에는 경로를 바꿀 수 없습니다 (먼저 멈춤·정지)')
            if run and run['state'] in ('paused', 'failed'):
                _set_run(con, run, 'stopped', '할당 경로가 바뀌어 닫음', 'stop', finished=True)
            con.execute('UPDATE route_assignments SET route_id = ?, repeat_count = ?, current_run_id = NULL, updated_at = ? '
                        'WHERE robot_id = ?', (rid, rep, now, robot_id))
        else:
            con.execute('UPDATE route_assignments SET repeat_count = ?, updated_at = ? WHERE robot_id = ?', (rep, now, robot_id))
            if run and run['state'] != 'done':
                con.execute('UPDATE route_runs SET repeat_count = ?, updated_at = ? WHERE id = ?', (rep, now, run['id']))
                if run['state'] == 'running' and rep and run['loops_done'] >= rep:
                    _set_run(con, run, 'done', '반복 횟수를 줄여 끝냄', 'done', finished=True)
    return get_assignment(con, robot_id)


def delete_assignment(con, robot_id):
    cur = con.execute('SELECT * FROM route_assignments WHERE robot_id = ?', (robot_id,)).fetchone()
    if not cur:
        raise KeyError(robot_id)
    run = _run(con, cur['current_run_id'])
    if run and run['state'] == 'running':
        raise ValueError('실행 중에는 해제할 수 없습니다 (먼저 멈춤·정지)')
    with con:
        if run and run['state'] in ('paused', 'failed'):
            _set_run(con, run, 'stopped', '할당 해제로 닫음', 'stop', finished=True)
        con.execute('DELETE FROM route_assignments WHERE robot_id = ?', (robot_id,))   # 실행 이력은 남김
    return {'deleted': robot_id}


def control_assignment(con, robot_id, action, active_map=None):
    """start: 새 실행(처음부터) · resume: 지금 실행을 이어서 · pause: 멈춤(진행 유지) · stop: 정지(실행 닫음)"""
    cur = con.execute('SELECT * FROM route_assignments WHERE robot_id = ?', (robot_id,)).fetchone()
    if not cur:
        raise KeyError(robot_id)
    if action not in ('start', 'resume', 'pause', 'stop'):
        raise ValueError('action 은 start | resume | pause | stop')
    a = get_assignment(con, robot_id)
    run = _run(con, cur['current_run_id'])
    if action in ('start', 'resume'):
        if not a['robot_active']:
            raise ValueError('로봇 목록(fleet.json)에서 빠진 로봇입니다')
        if a['map'] != active_map:
            raise ValueError(f'경로의 지도({a["map"]})가 활성 지도({active_map or "없음"})가 아닙니다. 로봇은 활성 지도에서 위치를 잡습니다')
        if not a['points']:
            raise ValueError('경로에 지점이 없습니다')
    now = time.time()
    with con:
        if action == 'start':
            if run and run['state'] in ('running', 'paused', 'failed'):
                _set_run(con, run, 'stopped', '새로 시작해서 닫음', 'stop', finished=True)
            c = con.execute('INSERT INTO route_runs (robot_id, route_id, route_name, map, repeat_count, state, started_at, updated_at) '
                            "VALUES (?,?,?,?,?,'running',?,?)",
                            (robot_id, cur['route_id'], a['route_name'], a['map'], cur['repeat_count'], now, now))
            con.execute('UPDATE route_assignments SET current_run_id = ?, updated_at = ? WHERE robot_id = ?', (c.lastrowid, now, robot_id))
            _event(con, c.lastrowid, 'start', f'{cur["repeat_count"] or "계속"} 반복', 0, 0)
        elif action == 'resume':
            if not run or run['state'] not in ('paused', 'failed', 'stopped'):
                raise ValueError('멈춤·실패·정지 상태에서만 이어서 갈 수 있습니다')
            if run['route_id'] != cur['route_id']:
                raise ValueError('할당된 경로가 바뀌었습니다. 시작으로 새로 도세요')
            con.execute("UPDATE route_runs SET state = 'running', run_state = '', message = '', finished_at = NULL, "
                        'repeat_count = ?, updated_at = ? WHERE id = ?', (cur['repeat_count'], now, run['id']))
            _event(con, run['id'], 'resume', '', run['loops_done'], run['waypoint'])
        elif action == 'pause':
            if run and run['state'] == 'running':
                _set_run(con, run, 'paused', '사용자가 멈춤', 'pause')
        elif run and run['state'] in ('running', 'paused', 'failed'):
            _set_run(con, run, 'stopped', '사용자가 정지', 'stop', finished=True)
    return get_assignment(con, robot_id)


def pause_all(con, message):
    """비상정지·모드 변경·서버 재시작: 실행 중인 모든 경로를 멈춤 (진행 유지, 이어서 가기 가능)"""
    runs = [dict(r) for r in con.execute("SELECT * FROM route_runs WHERE state = 'running'").fetchall()]
    with con:
        for r in runs:
            _set_run(con, r, 'paused', message, 'pause')
    return len(runs)


def any_running(con):
    return con.execute("SELECT 1 FROM route_runs WHERE state = 'running' LIMIT 1").fetchone() is not None


def list_runs(con, robot_id, limit=10, events=30):
    """로봇의 최근 실행과 각 실행의 이력 (최근 것부터)"""
    out = []
    for r in con.execute('SELECT * FROM route_runs WHERE robot_id = ? ORDER BY id DESC LIMIT ?', (robot_id, min(int(limit), 100))).fetchall():
        d = dict(r)
        d['events'] = [dict(e) for e in con.execute(
            'SELECT t, kind, loop, waypoint, message FROM route_run_events WHERE run_id = ? ORDER BY id DESC LIMIT ?', (r['id'], events))]
        out.append(d)
    return out


# ------------------------------------------------------------------ 실행기(robot/route_runner_node.py) 용
def runner_jobs(con):
    """할당의 지금 실행 중 run: {robot_id: {run_id, route_id, repeat_count, loops_done, waypoint, points: [(x, y, yaw), ...]}}"""
    out = {}
    q = ("SELECT r.* FROM route_runs r JOIN route_assignments a ON a.current_run_id = r.id WHERE r.state = 'running'")
    for r in con.execute(q).fetchall():
        pts = [(p['x'], p['y'], p['yaw']) for p in _route_points(con, r['route_id'])] if r['route_id'] else []
        out[r['robot_id']] = {'run_id': r['id'], 'route_id': r['route_id'], 'repeat_count': r['repeat_count'],
                              'loops_done': r['loops_done'], 'waypoint': r['waypoint'], 'points': pts}
    return out


def runner_progress(con, run_id, run_state, message='', waypoint=None, loops_done=None, event=None):
    """실행 중일 때만 진행을 씀 (그 사이 사용자가 멈췄으면 아무것도 안 함). event 를 주면 이력에도 남김."""
    sets, args = ['run_state = ?', 'message = ?', 'heartbeat = ?', 'updated_at = ?'], [run_state, str(message)[:200], time.time(), time.time()]
    if waypoint is not None:
        sets.append('waypoint = ?')
        args.append(int(waypoint))
    if loops_done is not None:
        sets.append('loops_done = ?')
        args.append(int(loops_done))
    with con:
        n = con.execute(f"UPDATE route_runs SET {', '.join(sets)} WHERE id = ? AND state = 'running'", (*args, run_id)).rowcount
        if n and event:
            _event(con, run_id, event, message, loops_done, waypoint)
    return n


def runner_finish(con, run_id, state, message='', loops_done=None):
    if state not in ('done', 'failed'):
        raise ValueError(state)
    now = time.time()
    with con:
        n = con.execute("UPDATE route_runs SET state = ?, run_state = '', message = ?, finished_at = ?, updated_at = ?, heartbeat = ?, "
                        "loops_done = COALESCE(?, loops_done) WHERE id = ? AND state = 'running'",
                        (state, str(message)[:200], now, now, now, loops_done, run_id)).rowcount
        if n:
            r = _run(con, run_id)
            _event(con, run_id, 'done' if state == 'done' else 'fail', message, r['loops_done'], r['waypoint'])
    return n
