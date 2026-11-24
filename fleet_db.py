"""관제센터 DB 하나 (SQLite, data/fleet.db): 스키마와 연결.

테이블 (스키마 버전 1, PRAGMA user_version)
  참조     maps               지도 이름 (config/maps 를 서버 시작·지도 저장 때 등록)
           robots             로봇 (config/fleet.json 과 동기화, 목록에서 빠지면 active = 0, 지우지 않음)
  지점·경로 places             지점: 지도별, 이름은 지도 안에서 겹치지 않음
           routes             이동 경로: 지도별, 이름은 지도 안에서 겹치지 않음
           route_points       경로의 지점 순서. (route_id, map)·(place_id, map) 복합 외래 키로 같은 지도의 지점만
  경로 실행 route_assignments  로봇마다 경로 하나 + 반복 횟수(repeat_count) + 지금 실행(current_run_id)
           route_runs         시작할 때마다 한 줄: 상태·진행(바퀴, 지점)·시각
           route_run_events   실행 중 일어난 일 (시작·멈춤·이어서·바퀴 완료·실패·경로 수정 …)
  경비     alerts             경보 한 건 (위치·크기·등급·처리 상태·발견 당시 스캔)
           alert_robots       경보를 본 로봇마다 한 줄 (처음·마지막 본 시각, 횟수)

읽고 쓰는 함수는 places.py(지점·경로·할당)와 patrol_store.py(경보)에 있고, 둘 다 이 파일의 db() 로 엽니다.
파일은 저장소에 없고 서버를 켤 때 없으면 만듭니다 (dbutil.connect). 스키마를 바꿀 때는 MIGRATIONS 끝에 함수를 더합니다.
"""
import os

import dbutil

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, 'data', 'fleet.db')

MAX_COORD = 10000.0
MAX_REPEAT = 10000
RUN_STATES = ('running', 'paused', 'done', 'failed', 'stopped')
EVENT_KINDS = ('start', 'resume', 'pause', 'stop', 'loop', 'done', 'fail', 'edit')


def _v1(con):
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
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),   -- fleet.json 에서 빠지면 0 (이력 보존)
  updated_at REAL NOT NULL
);

CREATE TABLE places (
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
CREATE TABLE routes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  map TEXT NOT NULL REFERENCES maps(name) ON UPDATE CASCADE,
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 40),
  note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 200),
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (map, name),
  UNIQUE (id, map)
);
CREATE TABLE route_points (
  route_id INTEGER NOT NULL,
  seq INTEGER NOT NULL CHECK (seq >= 0),
  place_id INTEGER NOT NULL,
  map TEXT NOT NULL,                    -- 경로와 지점이 같은 지도인지 DB 가 확인하게 하는 열
  PRIMARY KEY (route_id, seq),
  FOREIGN KEY (route_id, map) REFERENCES routes(id, map) ON DELETE CASCADE,
  FOREIGN KEY (place_id, map) REFERENCES places(id, map)   -- 경로에서 쓰는 지점은 지울 수 없음
);

CREATE TABLE route_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  robot_id TEXT NOT NULL REFERENCES robots(id),
  route_id INTEGER REFERENCES routes(id) ON DELETE SET NULL,   -- 경로를 지워도 이력은 남김
  route_name TEXT NOT NULL,             -- 시작할 때의 경로 이름
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
CREATE TABLE route_run_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES route_runs(id) ON DELETE CASCADE,
  t REAL NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN {EVENT_KINDS}),
  loop INTEGER,                         -- 그때 끝낸 바퀴 수
  waypoint INTEGER,
  message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE route_assignments (
  robot_id TEXT PRIMARY KEY REFERENCES robots(id),
  route_id INTEGER NOT NULL REFERENCES routes(id),            -- 할당된 경로는 지울 수 없음
  repeat_count INTEGER NOT NULL DEFAULT 1 CHECK (repeat_count BETWEEN 0 AND {MAX_REPEAT}),
  current_run_id INTEGER REFERENCES route_runs(id) ON DELETE SET NULL,   -- 아직 시작 안 했으면 NULL
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  map TEXT NOT NULL REFERENCES maps(name) ON UPDATE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('new', 'missing')),        -- 새로 생긴 것 | 사라진 것
  x REAL, y REAL, w REAL, h REAL,       -- 중심과 크기 (map 프레임, m)
  area REAL,
  first_seen REAL, last_seen REAL,
  seen INTEGER DEFAULT 1,               -- 확인된 횟수
  severity TEXT CHECK (severity IN ('high', 'medium', 'low')),
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'ok', 'false', 'baseline')),
  note TEXT DEFAULT '',
  scan TEXT,                            -- 발견 당시 주변 스캔 점 JSON [[x,y],...]
  handled_at REAL
);
CREATE TABLE alert_robots (
  alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
  robot_id TEXT NOT NULL REFERENCES robots(id),
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  seen INTEGER NOT NULL DEFAULT 1 CHECK (seen >= 1),
  PRIMARY KEY (alert_id, robot_id)
);

CREATE INDEX route_points_place ON route_points(place_id);          -- "이 지점을 쓰는 경로" 찾기
CREATE INDEX route_assignments_route ON route_assignments(route_id);
CREATE INDEX route_runs_robot ON route_runs(robot_id, started_at);
CREATE INDEX route_runs_state ON route_runs(state);
CREATE INDEX route_run_events_run ON route_run_events(run_id, t);
CREATE INDEX alerts_status ON alerts(map, status);
CREATE INDEX alerts_recent ON alerts(map, last_seen);
CREATE INDEX alert_robots_robot ON alert_robots(robot_id, last_seen);
''')


MIGRATIONS = [_v1]


def db(path=DB_PATH):
    """연결 열기 (파일이 없으면 만들고, 버전이 낮으면 올림). 여러 스레드에서 쓰므로 잠금은 호출하는 쪽."""
    return dbutil.connect(path, MIGRATIONS)
