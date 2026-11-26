"""관제센터 DB 하나 (SQLite, data/fleet.db): 스키마와 연결.

테이블 (스키마 버전 4, PRAGMA user_version)
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
  안전     safety_settings    LiDAR 안전 구역 플릿 기본값 (켜짐, 정지·서행 거리, 서행 속도). 한 줄 (v2)
           robot_safety       로봇별 덮어쓰기 (줄이 없는 로봇은 기본값) (v2)
  역할     robot_roles        로봇 역할 worker(이동 경로 반복) | guard(자율 경비) + 경비 상태·대기 중인 경보 (v2)
           safety_changes     안전 구역을 바꿀 때마다 한 줄 (시각, 값, 바꾼 화면) (v2)
  장애물   map_exits          지도마다 출구 위치 (장애물을 옮겨 둘 곳) (v3)
           obstacle_runs      장애물 처리 한 번마다 한 줄: 단계·진행·계획(밀 길, 로봇 자리)·끝나고 다시 시작할 역할 (v3)
           obstacle_run_events 처리 안에서 일어난 일 (단계, 구간마다 잰 위치) (v4)
           obstacle_settings  장애물 처리 값 (모일 간격, 미는 속도, 구간 길이, 도착 허용 …). 한 줄 (v4)
  설정     robot_types        로봇 종류 프로필 (최대 속도, 몸체·Nav2 반지름, 배터리 전압) (v4)
           fleet_settings     플릿 전체 한 줄: 공유 지도 토픽, 활성 지도, 운영 모드 (v4, 예전 config/fleet.json·mode.json)
           zones, zone_points, zone_robots   지도마다 이동 구역 (v4, 예전 config/zones/<지도>.json)
           guard_settings     지도마다 경비 감지·순회 설정 (v4, 예전 config/patrol/<지도>.json)
           mapping_starts     함께 지도 만들기의 로봇별 시작 위치 (v4, 예전 브라우저 저장소)
  이력     mode_changes       운영 모드가 바뀔 때마다 (v4)
           role_changes       역할·경비 상태가 바뀔 때마다 (v4)
           events             운영 이벤트 (프로세스 재시작, 모드, 장애물 처리 …) (v4)
  robots 에는 v4 에서 tf_topic, tf_prefix, color, sort 가 더해져 로봇 설정의 원본이 됨 (예전 config/fleet.json)

읽고 쓰는 함수는 places.py(지점·경로·할당), patrol_store.py(경보), safety.py(LiDAR 안전 구역)에 있습니다.
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


# LiDAR 안전 구역 값 범위 (safety.py 가 같은 값으로 검사하고, DB 도 CHECK 로 막음)
SAFETY_DEFAULTS = {'enabled': True, 'stop_dist': 0.20, 'slow_dist': 0.50, 'slow_speed': 0.05}
SAFETY_LIMITS = {'stop_dist': (0.05, 1.0), 'slow_dist': (0.10, 2.0), 'slow_speed': (0.01, 0.22)}
SAFETY_MIN_GAP = 0.05   # 서행 거리 ≥ 정지 거리 + 이 값 (m)

# 로봇 역할 (robot_roles)
ROLES = ('worker', 'guard')
# 경비 상태: stopped 안 함 · running 경비 중 · paused 멈춤(사용자·비상정지·모드 변경·서버 재시작) · alarm 장애물 앞 대기
GUARD_STATES = ('stopped', 'running', 'paused', 'alarm')
# 운영 모드 (v4 fleet_settings.mode)
MODES = ('operate', 'mapping', 'obstacle')
# 장애물 처리 단계: 모이기 → 소리(5초) → 밀기 → 소리(5초) → 끝 | 실패 | 중단
OBSTACLE_STATES = ('gathering', 'signal1', 'pushing', 'signal2', 'done', 'failed', 'aborted')


def _v2(con):
    """LiDAR 안전 구역: 플릿 기본값 한 줄(safety_settings, id = 1) + 로봇별 덮어쓰기(robot_safety)
    + 바꿀 때마다 한 줄(safety_changes). 로봇 역할(robot_roles: 워커·경비, 경비 상태)"""
    (s0, s1), (l0, l1), (v0, v1) = (SAFETY_LIMITS[k] for k in ('stop_dist', 'slow_dist', 'slow_speed'))
    cols = f'''
  enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
  stop_dist REAL NOT NULL CHECK (stop_dist BETWEEN {s0} AND {s1}),     -- 이 거리(m) 안 물체 → 정지
  slow_dist REAL NOT NULL CHECK (slow_dist BETWEEN {l0} AND {l1}),     -- 이 거리(m) 안 물체 → 서행
  slow_speed REAL NOT NULL CHECK (slow_speed BETWEEN {v0} AND {v1})'''   # 서행 속도 (m/s). 뒤에 ',' 가 붙으므로 SQL 주석 없이
    gap = f'CHECK (slow_dist - stop_dist >= {SAFETY_MIN_GAP - 1e-6})'   # 표 제약은 열 정의 뒤에
    dbutil.script(con, f'''
CREATE TABLE safety_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),   -- 플릿 전체에 한 줄
{cols},
  updated_at REAL NOT NULL,
  {gap}
);
CREATE TABLE robot_safety (
  robot_id TEXT PRIMARY KEY REFERENCES robots(id),   -- 줄이 없는 로봇은 플릿 기본값(safety_settings)을 따름
{cols},
  updated_at REAL NOT NULL,
  {gap}
);
CREATE TABLE safety_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  t REAL NOT NULL,
  robot_id TEXT REFERENCES robots(id),   -- NULL = 플릿 기본값을 바꿈
  action TEXT NOT NULL DEFAULT 'set' CHECK (action IN ('set', 'clear')),   -- clear = 로봇 덮어쓰기를 지우고 기본값으로
{cols},                                  -- 바꾼 뒤 그 대상(기본값 또는 로봇)에 적용되는 값
  by TEXT NOT NULL DEFAULT '' CHECK (length(by) <= 40),  -- 바꾼 화면 (예: 'PC ab12')
  {gap}
);
CREATE INDEX safety_changes_t ON safety_changes(t);
CREATE INDEX safety_changes_robot ON safety_changes(robot_id, t);

CREATE TABLE robot_roles (
  robot_id TEXT PRIMARY KEY REFERENCES robots(id),   -- 줄이 없는 로봇 = 역할 없음 (수동 조종만)
  role TEXT NOT NULL CHECK (role IN {ROLES}),          -- worker = 이동 경로 반복, guard = 자율 경비
  guard_state TEXT NOT NULL DEFAULT 'stopped' CHECK (guard_state IN {GUARD_STATES}),
  alarm_alert_id INTEGER REFERENCES alerts(id) ON DELETE SET NULL,   -- alarm: 이 경보(새로 생긴 장애물) 앞에서 대기
  message TEXT NOT NULL DEFAULT '' CHECK (length(message) <= 200),
  updated_at REAL NOT NULL,
  CHECK (role = 'guard' OR guard_state = 'stopped'),
  CHECK (guard_state = 'alarm' OR alarm_alert_id IS NULL)
);
''')
    d = SAFETY_DEFAULTS
    con.execute('INSERT INTO safety_settings (id, enabled, stop_dist, slow_dist, slow_speed, updated_at) '
                'VALUES (1, ?, ?, ?, ?, 0)', (int(d['enabled']), d['stop_dist'], d['slow_dist'], d['slow_speed']))


def _v3(con):
    """장애물 처리: 지도마다 출구(map_exits) + 처리 한 번마다 한 줄(obstacle_runs)"""
    dbutil.script(con, f'''
CREATE TABLE map_exits (
  map TEXT PRIMARY KEY REFERENCES maps(name) ON UPDATE CASCADE,   -- 지도마다 출구 하나 (장애물을 옮겨 둘 곳)
  x REAL NOT NULL CHECK (abs(x) <= {MAX_COORD}),
  y REAL NOT NULL CHECK (abs(y) <= {MAX_COORD}),
  updated_at REAL NOT NULL
);
CREATE TABLE obstacle_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  alert_id INTEGER REFERENCES alerts(id) ON DELETE SET NULL,      -- 처리한 경보 (새로 생긴 장애물)
  map TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN {OBSTACLE_STATES}),
  progress REAL NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),   -- 미는 중: 출구까지 간 비율
  message TEXT NOT NULL DEFAULT '' CHECK (length(message) <= 300),
  obstacle TEXT NOT NULL,      -- JSON {{x, y, r}}: 처음 본 장애물 (지도 좌표, 반지름 추정)
  exit TEXT NOT NULL,          -- JSON {{x, y}}
  plan TEXT NOT NULL DEFAULT '{{}}',   -- JSON {{path, robots: {{id: {{slot, row, state}}}}, est}}
  resume TEXT NOT NULL DEFAULT '{{}}', -- JSON {{routes: [로봇 id], guards: [로봇 id]}}: 끝나면 다시 시작할 역할
  started_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  finished_at REAL
);
CREATE INDEX obstacle_runs_alert ON obstacle_runs(alert_id);
''')


# 로봇 종류 기본값 (v4 robot_types 의 첫 값). 웹·구역 마스크·경비 순회·장애물 처리·Nav2 가 모두 이 표를 씀
ROBOT_TYPE_DEFAULTS = {
    #             이름          최대 m/s  rad/s  몸체 반지름  Nav2 반지름  저전압  전압 범위
    'turtlebot3': ('TurtleBot3', 0.22, 2.84, 0.105, 0.10, 10.5, 9.9, 12.6),
    'pinky': ('Pinky Pro', 0.20, 2.00, 0.07, 0.08, 6.8, 6.4, 8.4),
}
# 경비 감지·순회 설정 기본값과 범위 (v4 guard_settings, 지도마다)
GUARD_DEFAULTS = {'min_area': 0.05, 'persist': 2, 'max_range': 3.5, 'tol': 0.15, 'decay': 10.0, 'escalate': True,
                  'spacing': 1.0, 'alarm_interval': 5.0}
GUARD_LIMITS = {'min_area': (0.01, 5), 'persist': (1, 20), 'max_range': (0.5, 12), 'tol': (0.02, 1), 'decay': (1, 600),
                'spacing': (0.5, 3), 'alarm_interval': (2, 60)}
# 장애물 처리 설정 기본값과 범위 (v4 obstacle_settings, 한 줄). 소리 5초는 요구사항이라 설정이 아님 (obstacle.SIGNAL_SECONDS)
OBSTACLE_DEFAULTS = {'standoff': 0.30, 'robot_gap': 0.06, 'push_speed': 0.05, 'approach_speed': 0.04, 'segment': 1.0,
                     'exit_tol': 0.30, 'exit_radius': 1.0, 'max_segments': 30, 'gather_timeout': 180.0, 'max_deviation': 0.35}
OBSTACLE_LIMITS = {'standoff': (0.15, 1.0), 'robot_gap': (0.0, 0.3), 'push_speed': (0.01, 0.15), 'approach_speed': (0.01, 0.10),
                   'segment': (0.3, 3.0), 'exit_tol': (0.1, 1.0), 'exit_radius': (0.3, 3.0), 'max_segments': (1, 100),
                   'gather_timeout': (30, 900), 'max_deviation': (0.1, 1.0)}
EVENT_LEVELS = ('INFO', 'WARN', 'ERROR')


def _checks(limits):
    return ',\n  '.join(f'{k} {"INTEGER" if isinstance(lo, int) and isinstance(hi, int) and k in ("persist", "max_segments") else "REAL"} '
                         f'NOT NULL CHECK ({k} BETWEEN {lo} AND {hi})' for k, (lo, hi) in limits.items())


def _v4(con):
    """설정·상태를 파일에서 DB 로: 로봇 전체 설정(TF·색·순서), 로봇 종류 프로필, 플릿 설정(활성 지도·모드),
    이동 구역, 경비 설정(지도마다), 장애물 처리 설정, 이력(모드·역할·운영 이벤트·장애물 처리 단계), 지도 만들기 시작 위치.
    예전 파일(config/fleet.json, mode.json, zones/, patrol/)은 서버를 켤 때 fleet_store.import_files 가 한 번 가져옴."""
    dbutil.script(con, f'''
ALTER TABLE robots ADD COLUMN tf_topic TEXT NOT NULL DEFAULT '/tf';
ALTER TABLE robots ADD COLUMN tf_prefix TEXT NOT NULL DEFAULT '';
ALTER TABLE robots ADD COLUMN color TEXT NOT NULL DEFAULT '#2DD4BF' CHECK (color GLOB '#[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]');
ALTER TABLE robots ADD COLUMN sort INTEGER NOT NULL DEFAULT 0;      -- 로봇 목록 순서 (등록 화면 순서)

CREATE TABLE robot_types (
  type TEXT PRIMARY KEY CHECK (type GLOB '[a-z]*' AND length(type) <= 30),
  label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 40),
  max_lin REAL NOT NULL CHECK (max_lin BETWEEN 0.01 AND 2.0),       -- 웹 수동 조종 최대 직진 (m/s)
  max_ang REAL NOT NULL CHECK (max_ang BETWEEN 0.1 AND 10.0),       -- 최대 회전 (rad/s)
  radius REAL NOT NULL CHECK (radius BETWEEN 0.03 AND 1.0),         -- 몸체 반지름 (m): 구역 판정·마스크·경비 순회·장애물 대형
  nav_radius REAL NOT NULL CHECK (nav_radius BETWEEN 0.03 AND 1.0), -- Nav2 costmap robot_radius (m, 여유 포함)
  low_voltage REAL NOT NULL CHECK (low_voltage > 0),                 -- 이 전압 이하 저전압 경고 (V)
  volt_min REAL NOT NULL CHECK (volt_min > 0),                       -- 잔량 추정 0 % 전압
  volt_max REAL NOT NULL,                                            -- 잔량 추정 100 % 전압
  updated_at REAL NOT NULL,
  CHECK (volt_max > volt_min)
);

CREATE TABLE fleet_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),                             -- 플릿 전체 한 줄
  map_topic TEXT NOT NULL DEFAULT '/map' CHECK (map_topic GLOB '/*'),   -- 공유 지도 토픽 (플릿 화면)
  active_map TEXT REFERENCES maps(name) ON UPDATE CASCADE ON DELETE SET NULL,   -- 활성(기준) 지도
  mode TEXT NOT NULL DEFAULT 'operate' CHECK (mode IN {MODES}),
  mode_since REAL NOT NULL DEFAULT 0,
  obstacle_run INTEGER REFERENCES obstacle_runs(id) ON DELETE SET NULL,   -- 장애물 처리 모드일 때 그 처리
  updated_at REAL NOT NULL
);
CREATE TABLE mode_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  t REAL NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN {MODES}),
  previous TEXT,
  reason TEXT NOT NULL DEFAULT '' CHECK (length(reason) <= 200),     -- 사용자 / 장애물 발견 / 처리 끝 / 서버 재시작 …
  obstacle_run INTEGER REFERENCES obstacle_runs(id) ON DELETE SET NULL
);
CREATE INDEX mode_changes_t ON mode_changes(t);

CREATE TABLE zones (
  map TEXT NOT NULL REFERENCES maps(name) ON UPDATE CASCADE,
  id TEXT NOT NULL CHECK (length(id) BETWEEN 1 AND 41),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 40),
  type TEXT NOT NULL CHECK (type IN ('keepout', 'allowed')),
  all_robots INTEGER NOT NULL DEFAULT 1 CHECK (all_robots IN (0, 1)),   -- 1 = 모든 로봇, 0 = zone_robots 의 로봇만
  sort INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,
  PRIMARY KEY (map, id)
);
CREATE TABLE zone_points (
  map TEXT NOT NULL,
  zone_id TEXT NOT NULL,
  seq INTEGER NOT NULL CHECK (seq >= 0),
  x REAL NOT NULL CHECK (abs(x) <= {MAX_COORD}),
  y REAL NOT NULL CHECK (abs(y) <= {MAX_COORD}),
  PRIMARY KEY (map, zone_id, seq),
  FOREIGN KEY (map, zone_id) REFERENCES zones(map, id) ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE TABLE zone_robots (
  map TEXT NOT NULL,
  zone_id TEXT NOT NULL,
  robot_id TEXT NOT NULL REFERENCES robots(id),
  PRIMARY KEY (map, zone_id, robot_id),
  FOREIGN KEY (map, zone_id) REFERENCES zones(map, id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE guard_settings (
  map TEXT PRIMARY KEY REFERENCES maps(name) ON UPDATE CASCADE,
  {_checks({k: v for k, v in GUARD_LIMITS.items()})},
  escalate INTEGER NOT NULL CHECK (escalate IN (0, 1)),
  updated_at REAL NOT NULL
);
CREATE TABLE obstacle_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  {_checks(OBSTACLE_LIMITS)},
  updated_at REAL NOT NULL
);

CREATE TABLE role_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  t REAL NOT NULL,
  robot_id TEXT NOT NULL REFERENCES robots(id),
  kind TEXT NOT NULL CHECK (kind IN ('role', 'guard')),             -- role = 역할 바꿈, guard = 경비 상태 바뀜
  previous TEXT,
  value TEXT,
  message TEXT NOT NULL DEFAULT '' CHECK (length(message) <= 200)
);
CREATE INDEX role_changes_robot ON role_changes(robot_id, t);
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  t REAL NOT NULL,
  level TEXT NOT NULL CHECK (level IN {EVENT_LEVELS}),
  source TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 30),     -- server, patrol, obstacle, routes …
  robot_id TEXT REFERENCES robots(id),
  message TEXT NOT NULL CHECK (length(message) <= 300)
);
CREATE INDEX events_t ON events(t);
CREATE TABLE obstacle_run_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES obstacle_runs(id) ON DELETE CASCADE,
  t REAL NOT NULL,
  kind TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 30),         -- state, measure, segment, robot …
  message TEXT NOT NULL DEFAULT '' CHECK (length(message) <= 300),
  data TEXT NOT NULL DEFAULT '{{}}'                                   -- JSON (잰 위치, 남은 길 등)
);
CREATE INDEX obstacle_run_events_run ON obstacle_run_events(run_id, t);

CREATE TABLE mapping_starts (
  robot_id TEXT PRIMARY KEY REFERENCES robots(id),   -- 함께 지도 만들기: 로봇별 시작 위치 (첫 로봇 기준, 줄자로 잰 값)
  x REAL NOT NULL CHECK (abs(x) <= {MAX_COORD}),
  y REAL NOT NULL CHECK (abs(y) <= {MAX_COORD}),
  yaw REAL NOT NULL CHECK (yaw BETWEEN -3.1416 AND 3.1416),
  use INTEGER NOT NULL DEFAULT 1 CHECK (use IN (0, 1)),
  updated_at REAL NOT NULL
);
''')
    for t, (label, ml, ma, r, nr, lv, vmin, vmax) in ROBOT_TYPE_DEFAULTS.items():
        con.execute('INSERT INTO robot_types VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)', (t, label, ml, ma, r, nr, lv, vmin, vmax))
    con.execute("INSERT INTO fleet_settings (id, map_topic, active_map, mode, mode_since, updated_at) VALUES (1, '/map', NULL, 'operate', 0, 0)")
    o = OBSTACLE_DEFAULTS
    con.execute(f'INSERT INTO obstacle_settings (id, {", ".join(o)}, updated_at) VALUES (1, {", ".join("?" * len(o))}, 0)', tuple(o.values()))


MIGRATIONS = [_v1, _v2, _v3, _v4]


def db(path=DB_PATH):
    """연결 열기 (파일이 없으면 만들고, 버전이 낮으면 올림). 여러 스레드에서 쓰므로 잠금은 호출하는 쪽."""
    return dbutil.connect(path, MIGRATIONS)
