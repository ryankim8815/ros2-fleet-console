"""경비 모드 저장소: 운영 모드, 순찰 경로·감지 설정(지도별 JSON), 경보 기록(SQLite).

serve.py 와 robot/patrol_node.py 가 함께 씁니다.

  config/mode.json             {"mode": "operate"|"mapping"|"patrol", "since": epoch, "by": ...}
  config/patrol/<지도>.json    {"routes": {robot_id: {"points": [[x,y],...], "enabled": true}}, "settings": {...}}
  data/alerts.db               경보 (스키마 버전 2, PRAGMA user_version)
    alerts         경보 한 건 (위치·크기·등급·처리 상태·발견 당시 스캔)
    alert_robots   그 경보를 본 로봇마다 한 줄 (처음·마지막 본 시각, 횟수) → 로봇별 검색
  로봇 id 는 data/places.db 의 robots 와 같은 값이지만 다른 파일이라 외래 키는 걸 수 없습니다
  (경보는 자주, 지점은 가끔 써서 파일을 나눠 잠금 충돌을 줄임).
"""
import json
import os
import re
import tempfile
import time

import dbutil

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(APP_DIR, 'config')
MODE_PATH = os.path.join(CONFIG, 'mode.json')
PATROL_DIR = os.path.join(CONFIG, 'patrol')
DB_PATH = os.path.join(APP_DIR, 'data', 'alerts.db')

MODES = ('operate', 'mapping', 'patrol')
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$')
ID_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,40}$')

DEFAULT_SETTINGS = {
    'min_area': 0.05,      # 경보로 볼 최소 크기 (m²)
    'persist': 2,          # 연속으로 보여야 하는 스캔 수 (같은 로봇)
    'max_range': 3.5,      # 이 거리 안의 스캔만 비교 (m)
    'tol': 0.15,           # 기준 지도 벽에서 이 거리 안은 차이로 보지 않음 (위치 오차 여유, m)
    'decay': 10.0,         # 이 시간(초) 동안 다시 안 보이면 증거를 지움 (지나가는 사람 등)
    'escalate': True,      # 다른 로봇이 같은 곳을 다시 보면 등급 올리기
}

SCHEMA_V1 = '''
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  map TEXT NOT NULL,
  kind TEXT NOT NULL,              -- new (새로 생긴 것) | missing (사라진 것)
  x REAL, y REAL, w REAL, h REAL,  -- 중심과 크기 (map 프레임, m)
  area REAL,
  first_seen REAL, last_seen REAL,
  robots TEXT,                     -- JSON 로봇 id 목록
  seen INTEGER DEFAULT 1,          -- 확인된 횟수
  severity TEXT,                   -- high | medium | low
  status TEXT DEFAULT 'open',      -- open | ok (이상 없음) | false (오탐) | baseline (기준 지도에 반영)
  note TEXT DEFAULT '',
  scan TEXT,                       -- 발견 당시 주변 스캔 점 JSON [[x,y],...]
  handled_at REAL
);
CREATE INDEX IF NOT EXISTS alerts_status ON alerts(map, status);
'''


def _v1(con):
    """버전 1: 버전 관리 전의 구조 (alerts.robots 는 JSON 배열)"""
    dbutil.script(con, SCHEMA_V1)


def _v2(con):
    """버전 2: 본 로봇을 alert_robots 로 분리 (로봇별 검색), 목록 정렬용 인덱스, 상태 값 CHECK"""
    dbutil.script(con, '''
CREATE TABLE alert_robots (
  alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
  robot_id TEXT NOT NULL,          -- data/places.db robots.id (다른 파일이라 외래 키 없음)
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  seen INTEGER NOT NULL DEFAULT 1 CHECK (seen >= 1),
  PRIMARY KEY (alert_id, robot_id)
);
INSERT INTO alert_robots (alert_id, robot_id, first_seen, last_seen, seen)
  SELECT a.id, j.value, COALESCE(a.first_seen, a.last_seen, 0), COALESCE(a.last_seen, a.first_seen, 0), 1
  FROM alerts a, json_each(COALESCE(a.robots, '[]')) j
  WHERE j.type = 'text';
CREATE INDEX alert_robots_robot ON alert_robots(robot_id, last_seen);
CREATE INDEX alerts_recent ON alerts(map, last_seen);
CREATE TRIGGER alerts_check_insert BEFORE INSERT ON alerts
  WHEN NEW.kind NOT IN ('new', 'missing') OR NEW.status NOT IN ('open', 'ok', 'false', 'baseline')
    OR NEW.severity NOT IN ('high', 'medium', 'low')
  BEGIN SELECT RAISE(ABORT, 'alerts: kind/status/severity 값이 올바르지 않습니다'); END;
CREATE TRIGGER alerts_check_update BEFORE UPDATE OF kind, status, severity ON alerts
  WHEN NEW.kind NOT IN ('new', 'missing') OR NEW.status NOT IN ('open', 'ok', 'false', 'baseline')
    OR NEW.severity NOT IN ('high', 'medium', 'low')
  BEGIN SELECT RAISE(ABORT, 'alerts: kind/status/severity 값이 올바르지 않습니다'); END;
ALTER TABLE alerts DROP COLUMN robots;
''')


MIGRATIONS = [_v1, _v2]


def _atomic_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


# ------------------------------------------------------------------ 모드
def read_mode():
    try:
        with open(MODE_PATH, encoding='utf-8') as f:
            m = json.load(f)
        if m.get('mode') in MODES:
            return m
    except (FileNotFoundError, ValueError):
        pass
    return {'mode': 'operate', 'since': 0}


def write_mode(mode, **extra):
    if mode not in MODES:
        raise ValueError(f'모드는 {MODES} 중 하나')
    m = {'mode': mode, 'since': time.time(), **extra}
    _atomic_json(MODE_PATH, m)
    return m


# ------------------------------------------------------------------ 순찰 경로 · 설정
def read_patrol(map_name):
    if not NAME_RE.match(map_name or ''):
        raise ValueError('지도 이름 형식이 올바르지 않습니다')
    try:
        with open(os.path.join(PATROL_DIR, f'{map_name}.json'), encoding='utf-8') as f:
            d = json.load(f)
    except FileNotFoundError:
        d = {}
    return {'map': map_name, 'routes': d.get('routes', {}), 'settings': {**DEFAULT_SETTINGS, **d.get('settings', {})}}


def write_patrol(map_name, data, robot_ids):
    cur = read_patrol(map_name)
    routes = data.get('routes', cur['routes'])
    if not isinstance(routes, dict):
        raise ValueError('routes 는 {로봇 id: {points, enabled}} 형식')
    clean = {}
    for rid, r in routes.items():
        if rid not in robot_ids:
            raise ValueError(f'등록되지 않은 로봇: {rid}')
        pts = r.get('points', [])
        if not isinstance(pts, list) or len(pts) > 100:
            raise ValueError(f'{rid}: 지점은 100개 이하')
        ok = []
        for p in pts:
            if not (isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p)):
                raise ValueError(f'{rid}: 지점 형식 오류')
            ok.append([round(float(p[0]), 3), round(float(p[1]), 3)])
        clean[rid] = {'points': ok, 'enabled': bool(r.get('enabled', True))}
    s = {**cur['settings']}
    for k, v in (data.get('settings') or {}).items():
        if k not in DEFAULT_SETTINGS:
            raise ValueError(f'알 수 없는 설정: {k}')
        s[k] = bool(v) if isinstance(DEFAULT_SETTINGS[k], bool) else float(v)
    if not (0.01 <= s['min_area'] <= 5 and 1 <= s['persist'] <= 20 and 0.5 <= s['max_range'] <= 12
            and 0.02 <= s['tol'] <= 1 and 1 <= s['decay'] <= 600):
        raise ValueError('설정 값이 범위를 벗어났습니다')
    s['persist'] = int(s['persist'])
    out = {'routes': clean, 'settings': s}
    _atomic_json(os.path.join(PATROL_DIR, f'{map_name}.json'), out)
    return {'map': map_name, **out}


# ------------------------------------------------------------------ 경보 DB
def db(path=DB_PATH):
    return dbutil.connect(path, MIGRATIONS)   # 노드·서버의 여러 스레드에서 (잠금은 호출하는 쪽)


def alert_robot_ids(con, aid):
    return [r[0] for r in con.execute('SELECT robot_id FROM alert_robots WHERE alert_id = ? ORDER BY first_seen, robot_id', (aid,))]


def record_sighting(con, aid, robot_id, now):
    """그 로봇이 경보를 (다시) 봄. 트랜잭션 안에서 부름."""
    con.execute('INSERT INTO alert_robots (alert_id, robot_id, first_seen, last_seen) VALUES (?,?,?,?) '
                'ON CONFLICT(alert_id, robot_id) DO UPDATE SET last_seen = excluded.last_seen, seen = seen + 1',
                (aid, robot_id, now, now))


def _row(r, with_scan=False, con=None):
    d = dict(r)
    d['robots'] = alert_robot_ids(con, d['id']) if con is not None else []
    if with_scan:
        d['scan'] = json.loads(d.get('scan') or '[]')
    else:
        d.pop('scan', None)
    return d


def list_alerts(con, map_name=None, status=None, limit=100, since_id=0, robot=None):
    q, args = 'SELECT * FROM alerts WHERE id > ?', [since_id]
    if map_name:
        q += ' AND map = ?'
        args.append(map_name)
    if robot:
        q += ' AND id IN (SELECT alert_id FROM alert_robots WHERE robot_id = ?)'
        args.append(robot)
    if status == 'open':
        q += " AND status = 'open'"
    q += ' ORDER BY last_seen DESC LIMIT ?'
    args.append(min(int(limit), 500))
    return [_row(r, con=con) for r in con.execute(q, args).fetchall()]


def get_alert(con, aid):
    r = con.execute('SELECT * FROM alerts WHERE id = ?', (aid,)).fetchone()
    return _row(r, with_scan=True, con=con) if r else None


def set_status(con, aid, status, note=''):
    if status not in ('open', 'ok', 'false', 'baseline'):
        raise ValueError('status 는 open | ok | false | baseline')
    with con:
        n = con.execute('UPDATE alerts SET status = ?, note = ?, handled_at = ? WHERE id = ?',
                        (status, str(note)[:200], time.time(), aid)).rowcount
    if not n:
        raise KeyError(aid)
    return get_alert(con, aid)


def counts(con, map_name):
    r = con.execute("SELECT SUM(status='open') AS open, COUNT(*) AS total FROM alerts WHERE map = ?", (map_name,)).fetchone()
    return {'open': r['open'] or 0, 'total': r['total'] or 0}
