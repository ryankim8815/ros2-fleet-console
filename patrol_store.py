"""경비 저장소: 경비 감지·순회 설정(지도마다), 경보 기록. 모두 data/fleet.db (fleet_db.py).

serve.py 와 robot/patrol_node.py(경비 노드)가 함께 씁니다.

  guard_settings (v4)   지도마다 경비 감지·순회 설정 (예전 config/patrol/<지도>.json — 서버를 켤 때 가져옴)
  alerts, alert_robots  경보 (한 건), 본 로봇마다 한 줄 (robots 외래 키 → 로봇별 검색)
운영 모드는 fleet_store.py (fleet_settings, 예전 config/mode.json).
"""
import json
import re
import time

import fleet_db

DB_PATH = fleet_db.DB_PATH
MODES = fleet_db.MODES
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$')
ID_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,40}$')

# 기본값·범위는 fleet_db 에 (DB CHECK 와 같음)
#   min_area 경보로 볼 최소 크기 (m²) · persist 연속으로 보여야 하는 스캔 수 · max_range 이 거리 안의 스캔만 비교 (m)
#   tol 기준 지도 벽에서 이 거리 안은 차이로 보지 않음 (m) · decay 이 시간(초) 동안 안 보이면 증거를 지움
#   escalate 다른 로봇이 같은 곳을 다시 보면 등급 올리기 · spacing 경비 순회 지점 간격 (m)
#   alarm_interval 장애물 앞에서 기다리는 동안 소리·LED 반복 간격 (초)
DEFAULT_SETTINGS = dict(fleet_db.GUARD_DEFAULTS)
LIMITS = fleet_db.GUARD_LIMITS

MIGRATIONS = fleet_db.MIGRATIONS


# ------------------------------------------------------------------ 경비 감지·순회 설정 (지도마다)
def read_patrol(con, map_name):
    """{map, settings, updated} — 저장한 적이 없으면 기본값 (updated 0)"""
    if not NAME_RE.match(map_name or ''):
        raise ValueError('지도 이름 형식이 올바르지 않습니다')
    r = con.execute('SELECT * FROM guard_settings WHERE map = ?', (map_name,)).fetchone()
    if not r:
        return {'map': map_name, 'settings': dict(DEFAULT_SETTINGS), 'updated': 0.0}
    st = {k: (bool(r[k]) if isinstance(v, bool) else int(r[k]) if k == 'persist' else float(r[k])) for k, v in DEFAULT_SETTINGS.items()}
    return {'map': map_name, 'settings': st, 'updated': r['updated_at']}


def write_patrol(con, map_name, data, robot_ids=None):
    """감지·순회 설정 저장 (data: {"settings": {...}}). robot_ids 는 예전 호출과 맞추려고 받기만 함"""
    cur = read_patrol(con, map_name)
    s = {**cur['settings']}
    for k, v in (data.get('settings') or {}).items():
        if k not in DEFAULT_SETTINGS:
            raise ValueError(f'알 수 없는 설정: {k}')
        if isinstance(DEFAULT_SETTINGS[k], bool):
            s[k] = bool(v)
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f'{k} 는 숫자여야 합니다')
        s[k] = float(v)
    for k, (lo, hi) in LIMITS.items():
        if not lo <= s[k] <= hi:
            raise ValueError(f'{k} 는 {lo} ~ {hi} 사이여야 합니다')
    s['persist'] = int(s['persist'])
    now = time.time()
    cols = list(DEFAULT_SETTINGS)
    with con:
        con.execute('INSERT OR IGNORE INTO maps (name, created_at) VALUES (?, ?)', (map_name, now))
        con.execute(f'INSERT INTO guard_settings (map, {", ".join(cols)}, updated_at) VALUES (?, {", ".join("?" * len(cols))}, ?) '
                    f'ON CONFLICT(map) DO UPDATE SET {", ".join(f"{c} = excluded.{c}" for c in cols)}, updated_at = excluded.updated_at',
                    (map_name, *[int(s[c]) if isinstance(s[c], bool) else s[c] for c in cols], now))
    return read_patrol(con, map_name)


# ------------------------------------------------------------------ 경보 DB
def db(path=DB_PATH):
    return fleet_db.db(path)   # 노드·서버의 여러 스레드에서 (잠금은 호출하는 쪽)


def ensure_refs(con, map_name, robot_id, now):
    """경보를 쓰기 전: 지도·로봇 참조 행이 없으면 최소한으로 만듦 (DB 가 막 새로 만들어졌어도 외래 키가 맞게).
    이름 등은 서버(fleet_store·sync_maps)가 나중에 채움. 트랜잭션 안에서 부름."""
    con.execute('INSERT OR IGNORE INTO maps (name, created_at) VALUES (?, ?)', (map_name, now))
    con.execute('INSERT OR IGNORE INTO robots (id, updated_at) VALUES (?, ?)', (robot_id, now))


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
