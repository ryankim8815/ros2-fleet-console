"""플릿 설정·상태 (DB, fleet_db.py v4): 로봇 목록, 로봇 종류 프로필, 활성 지도·공유 지도 토픽, 운영 모드와 이력,
운영 이벤트, 함께 지도 만들기 시작 위치. 예전 파일(config/fleet.json, mode.json, zones/, patrol/)을 가져오는 함수도.

  robots          로봇마다 한 줄 (id, name, type, ns, tf_topic, tf_prefix, color, sort, active). 목록에서 빠지면 active = 0
  robot_types     로봇 종류 프로필 (최대 속도, 몸체 반지름, Nav2 반지름, 배터리 전압)
  fleet_settings  한 줄: map_topic, active_map, mode, mode_since, obstacle_run
  mode_changes    모드가 바뀔 때마다 한 줄
  events          운영 이벤트 (프로세스 재시작, 모드, 장애물 처리, 역할 …)
  mapping_starts  함께 지도 만들기의 로봇별 시작 위치

serve.py 가 쓰고, 노드·launch(경로 실행기, 경비, 장애물 처리, nav2_ns)는 읽기만 합니다.
웹 API 모양(/api/fleet 의 {mapTopic, mapFile, robots}) 은 예전과 같습니다 (types 가 더해짐).
"""
import json
import os
import sqlite3
import time

import fleet_db

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(APP_DIR, 'config')
FLEET_JSON = os.path.join(CONFIG, 'fleet.json')
MODE_JSON = os.path.join(CONFIG, 'mode.json')
MODES = fleet_db.MODES
TYPE_KEYS = ('label', 'max_lin', 'max_ang', 'radius', 'nav_radius', 'low_voltage', 'volt_min', 'volt_max')


def open_ro(path=fleet_db.DB_PATH):
    """읽기 전용 연결 (launch·다른 프로세스용: DB 를 만들거나 버전을 올리지 않음). 없거나 실패하면 None"""
    try:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute('SELECT 1 FROM fleet_settings LIMIT 1')
        return con
    except sqlite3.Error:
        return None


# ------------------------------------------------------------------ 로봇
def _robot(r):
    return {'id': r['id'], 'name': r['name'], 'type': r['type'], 'ns': r['ns'], 'tfTopic': r['tf_topic'],
            'tfPrefix': r['tf_prefix'], 'color': r['color']}


def robots(con):
    """등록된(active) 로봇 [{id, name, type, ns, tfTopic, tfPrefix, color}] (등록 순서)"""
    return [_robot(r) for r in con.execute('SELECT * FROM robots WHERE active = 1 ORDER BY sort, id')]


def robot_by_ns(con, ns):
    want = ns.strip('/')
    hit = [r for r in robots(con) if r['ns'].strip('/') == want]
    return hit[0] if len(hit) == 1 else None


def write_robots(con, items):
    """로봇 목록 저장 (serve.validate_fleet 로 검사한 것). 빠진 로봇은 active = 0 (이력 보존)"""
    now = time.time()
    ids = [r['id'] for r in items]
    with con:
        for k, r in enumerate(items):
            con.execute('INSERT INTO robots (id, name, ns, type, tf_topic, tf_prefix, color, sort, active, updated_at) '
                        'VALUES (?,?,?,?,?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET name = excluded.name, ns = excluded.ns, '
                        'type = excluded.type, tf_topic = excluded.tf_topic, tf_prefix = excluded.tf_prefix, '
                        'color = excluded.color, sort = excluded.sort, active = 1, updated_at = excluded.updated_at',
                        (r['id'], r['name'], r['ns'], r['type'], r['tfTopic'], r['tfPrefix'], r['color'], k, now))
        q = 'UPDATE robots SET active = 0, updated_at = ? WHERE active = 1'
        if ids:
            q += f' AND id NOT IN ({",".join("?" * len(ids))})'
        con.execute(q, (now, *ids))


# ------------------------------------------------------------------ 로봇 종류
def robot_types(con):
    """{type: {label, max_lin, max_ang, radius, nav_radius, low_voltage, volt_min, volt_max}}"""
    try:
        return {r['type']: {k: r[k] for k in TYPE_KEYS} for r in con.execute('SELECT * FROM robot_types ORDER BY type')}
    except sqlite3.Error:   # v4 전 DB
        return {t: dict(zip(TYPE_KEYS, v)) for t, v in fleet_db.ROBOT_TYPE_DEFAULTS.items()}


def type_value(con, robot_type, key):
    t = robot_types(con)
    return (t.get(robot_type) or t.get('turtlebot3') or next(iter(t.values())))[key]


def write_robot_type(con, robot_type, body):
    """종류 하나 고치기 (있는 종류만). 범위는 DB CHECK 와 같게 검사"""
    cur = robot_types(con).get(robot_type)
    if cur is None:
        raise KeyError(robot_type)
    out = dict(cur)
    for k in TYPE_KEYS:
        if k not in body:
            continue
        v = body[k]
        if k == 'label':
            if not isinstance(v, str) or not 1 <= len(v.strip()) <= 40:
                raise ValueError('이름은 1~40자')
            out[k] = v.strip()
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f'{k} 는 숫자여야 합니다')
        out[k] = round(float(v), 3)
    bounds = {'max_lin': (0.01, 2.0), 'max_ang': (0.1, 10.0), 'radius': (0.03, 1.0), 'nav_radius': (0.03, 1.0)}
    for k, (lo, hi) in bounds.items():
        if not lo <= out[k] <= hi:
            raise ValueError(f'{k} 는 {lo} ~ {hi} 사이여야 합니다')
    if not (0 < out['volt_min'] < out['volt_max'] and out['low_voltage'] > 0):
        raise ValueError('전압: 0 < 0% 전압 < 100% 전압, 저전압 > 0')
    with con:
        con.execute(f'UPDATE robot_types SET {", ".join(f"{k} = ?" for k in TYPE_KEYS)}, updated_at = ? WHERE type = ?',
                    (*[out[k] for k in TYPE_KEYS], time.time(), robot_type))
        log_event(con, 'INFO', 'server', f'로봇 종류 {robot_type} 값 바꿈: 최대 {out["max_lin"]} m/s, 반지름 {out["radius"]} m '
                                         f'(Nav2 {out["nav_radius"]} m)', commit=False)
    return robot_types(con)[robot_type]


# ------------------------------------------------------------------ 플릿 설정 (활성 지도, 지도 토픽)
def settings(con):
    r = con.execute('SELECT * FROM fleet_settings WHERE id = 1').fetchone()
    return dict(r) if r else {'map_topic': '/map', 'active_map': None, 'mode': 'operate', 'mode_since': 0, 'obstacle_run': None}


def read_fleet(con):
    """예전 config/fleet.json 과 같은 모양 + types: {mapTopic, mapFile?, robots, types}"""
    s = settings(con)
    out = {'mapTopic': s['map_topic'], 'robots': robots(con), 'types': robot_types(con)}
    if s['active_map']:
        out['mapFile'] = s['active_map']
    return out


def write_fleet(con, data):
    """검사한 fleet {mapTopic, mapFile?, robots} 저장"""
    write_robots(con, data['robots'])
    mf = data.get('mapFile') or None
    now = time.time()
    with con:
        if mf:
            con.execute('INSERT OR IGNORE INTO maps (name, created_at) VALUES (?, ?)', (mf, now))
        con.execute('UPDATE fleet_settings SET map_topic = ?, active_map = ?, updated_at = ? WHERE id = 1',
                    (data.get('mapTopic', '/map'), mf, now))


# ------------------------------------------------------------------ 운영 모드
def read_mode(con):
    """{'mode', 'since', 'run'} (예전 config/mode.json 과 같은 모양)"""
    s = settings(con)
    return {'mode': s['mode'], 'since': s['mode_since'], 'run': s['obstacle_run']}


def write_mode(con, mode, reason='', run=None):
    if mode not in MODES:
        raise ValueError(f'모드는 {MODES} 중 하나')
    prev = read_mode(con)['mode']
    now = time.time()
    with con:
        con.execute('UPDATE fleet_settings SET mode = ?, mode_since = ?, obstacle_run = ?, updated_at = ? WHERE id = 1',
                    (mode, now, run, now))
        con.execute('INSERT INTO mode_changes (t, mode, previous, reason, obstacle_run) VALUES (?, ?, ?, ?, ?)',
                    (now, mode, prev, str(reason)[:200], run))
    return read_mode(con)


def mode_history(con, limit=20):
    return [dict(r) for r in con.execute('SELECT * FROM mode_changes ORDER BY t DESC, id DESC LIMIT ?', (max(1, min(int(limit), 200)),))]


# ------------------------------------------------------------------ 운영 이벤트
def log_event(con, level, source, message, robot_id=None, commit=True):
    """운영 이벤트 한 줄 (INFO | WARN | ERROR). 로봇이 목록에 없으면 robot_id 는 비움"""
    if robot_id and not con.execute('SELECT 1 FROM robots WHERE id = ?', (robot_id,)).fetchone():
        robot_id = None
    args = (time.time(), level if level in fleet_db.EVENT_LEVELS else 'INFO', str(source)[:30], robot_id, str(message)[:300])
    sql = 'INSERT INTO events (t, level, source, robot_id, message) VALUES (?, ?, ?, ?, ?)'
    if commit:
        with con:
            con.execute(sql, args)
    else:
        con.execute(sql, args)


def list_events(con, since_id=0, limit=100):
    rows = con.execute('SELECT * FROM events WHERE id > ? ORDER BY id DESC LIMIT ?', (int(since_id), max(1, min(int(limit), 500))))
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ 함께 지도 만들기 시작 위치
def mapping_starts(con):
    """{robot_id: {x, y, yaw, use}}"""
    return {r['robot_id']: {'x': r['x'], 'y': r['y'], 'yaw': r['yaw'], 'use': bool(r['use'])}
            for r in con.execute('SELECT * FROM mapping_starts')}


def write_mapping_starts(con, body):
    """{robot_id: {x, y, yaw, use}} 저장 (등록된 로봇만, 들어온 로봇만 바꿈)"""
    import math
    known = {r['id'] for r in robots(con)}
    now = time.time()
    if not isinstance(body, dict):
        raise ValueError('{로봇 id: {x, y, yaw, use}} 형식')
    with con:
        for rid, v in body.items():
            if rid not in known:
                raise ValueError(f'등록되지 않은 로봇: {rid}')
            try:
                x, y, yaw = float(v.get('x', 0)), float(v.get('y', 0)), float(v.get('yaw', 0))
            except (TypeError, ValueError, AttributeError) as e:
                raise ValueError(f'{rid}: x, y, yaw 는 숫자') from e
            if not all(math.isfinite(q) for q in (x, y, yaw)) or abs(x) > 1000 or abs(y) > 1000:
                raise ValueError(f'{rid}: 값이 범위를 벗어났습니다')
            yaw = math.atan2(math.sin(yaw), math.cos(yaw))
            con.execute('INSERT INTO mapping_starts (robot_id, x, y, yaw, use, updated_at) VALUES (?,?,?,?,?,?) '
                        'ON CONFLICT(robot_id) DO UPDATE SET x = excluded.x, y = excluded.y, yaw = excluded.yaw, '
                        'use = excluded.use, updated_at = excluded.updated_at',
                        (rid, round(x, 3), round(y, 3), round(yaw, 4), 1 if v.get('use', True) else 0, now))
    return mapping_starts(con)


# ------------------------------------------------------------------ 예전 파일 가져오기 (서버를 켤 때)
def _retire(path):
    """가져온 파일은 지우지 않고 이름을 바꿔 둠 (<파일>.migrated). 다시 같은 이름의 파일을 두면 다시 가져옴"""
    dst = path + '.migrated'
    if os.path.exists(dst):
        dst = f'{path}.migrated-{int(time.time())}'
    os.replace(path, dst)
    return os.path.basename(dst)


def import_files(con, validate_fleet, maps_mod, patrol_mod):
    """config/fleet.json, mode.json, zones/<지도>.json, patrol/<지도>.json 이 있으면 DB 로 가져오고 이름을 바꿈.
    validate_fleet: serve.validate_fleet. 반환: 한 일 목록 (로그용)"""
    done = []
    if os.path.exists(FLEET_JSON):
        with open(FLEET_JSON, encoding='utf-8') as f:
            data = validate_fleet(json.load(f))
        if data.get('mapFile') and not con.execute('SELECT 1 FROM maps WHERE name = ?', (data['mapFile'],)).fetchone():
            data.pop('mapFile')     # 없는 지도는 활성으로 두지 않음
        write_fleet(con, data)
        done.append(f'로봇 {len(data["robots"])}대·활성 지도 {data.get("mapFile") or "없음"} ← config/fleet.json ({_retire(FLEET_JSON)})')
    if os.path.exists(MODE_JSON):   # 모드는 서버를 켤 때마다 운영으로 시작하므로 내용은 쓰지 않음
        done.append(f'config/mode.json 은 더 이상 쓰지 않음 ({_retire(MODE_JSON)})')
    zdir = os.path.join(CONFIG, 'zones')
    for fn in sorted(os.listdir(zdir)) if os.path.isdir(zdir) else []:
        if not fn.endswith('.json'):
            continue
        name = fn[:-5]
        path = os.path.join(zdir, fn)
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            ids = [r['id'] for r in robots(con)]
            data['zones'] = [dict(z, robots=[r for r in z.get('robots', ['*']) if r == '*' or r in ids] or ['*'])
                             for z in data.get('zones', [])]
            maps_mod.save_zones(con, name, maps_mod.validate_zones(name, data, ids))
            done.append(f'이동 구역 {len(data["zones"])}개 ← config/zones/{fn} ({_retire(path)})')
        except Exception as e:  # noqa: BLE001  (지도가 없어졌거나 형식 오류: 파일은 그대로 둠)
            done.append(f'config/zones/{fn} 을 가져오지 못함: {e}')
    pdir = os.path.join(CONFIG, 'patrol')
    for fn in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        if not fn.endswith('.json'):
            continue
        path = os.path.join(pdir, fn)
        try:
            with open(path, encoding='utf-8') as f:
                d = json.load(f)
            patrol_mod.write_patrol(con, fn[:-5], {'settings': d.get('settings', {})})
            done.append(f'경비 설정 ← config/patrol/{fn} ({_retire(path)})')
        except Exception as e:  # noqa: BLE001
            done.append(f'config/patrol/{fn} 을 가져오지 못함: {e}')
    for line in done:
        log_event(con, 'INFO', 'server', f'파일 → DB: {line}')
    return done
