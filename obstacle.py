"""장애물 처리: 출구 위치, 처리 기록(DB), 장애물을 출구까지 밀 길과 로봇 대형 계산 (serve.py 와 robot/obstacle_node.py 가 씀).

흐름 (운영 모드, 활성 지도에 출구가 있을 때):
  경비 로봇이 지도에 없던 장애물(새로 생긴 것)을 찾음 → 서버가 '장애물 처리' 모드로 바꾸고 워커·경비를 모두 멈춘 뒤
  robot/obstacle_node.py 를 띄움:
    1. 모이기  : 모든 워커·경비 로봇이 장애물 뒤(출구 반대쪽)의 자기 자리로 Nav2 로 가서 정확히 맞춤
    2. 소리    : 모두 모여 첫 소리를 낸 때부터 SIGNAL_SECONDS(5초) 동안 소리 (TurtleBot3 부저, Pinky LED·표정, 관제 웹)
    3. 밀기    : 앞줄 로봇들이 나란히 장애물에 닿을 때까지 다가간 뒤, 장애물이 갈 길(push_path)을 따라 함께 천천히 밀어 출구로
                 (Nav2 를 쓰지 않고 cmd_vel 을 직접 보냄 — 닿아야 밀 수 있으므로 LiDAR 안전 구역을 거치지 않음)
    4. 소리    : 출구에 닿으면 조금 물러나 5초 동안 소리
    5. 끝      : 서버가 경보를 '처리됨'으로 하고 운영 모드로 돌아가 멈췄던 역할(이동 경로·경비)을 다시 시작
  실패·중단이면 로봇을 세우고 운영 모드로 돌아가되 역할은 멈춘 채로 둠 (관제실이 확인 후 재개). 경보도 열린 채로.

DB (fleet_db.py): map_exits(지도마다 출구), obstacle_runs(처리 한 번마다 한 줄), obstacle_run_events(그 안에서 일어난 일),
obstacle_settings(아래 값들, 한 줄 — 경비 화면에서 바꿈). 아래 상수는 DB 가 없을 때의 기본값입니다.
"""
import heapq
import json
import math
import time

import numpy as np

import coverage
import fleet_db
import maps

SIGNAL_SECONDS = 5.0       # 모여서·도착해서 소리 내는 시간 (첫 소리부터) — 요구사항이라 설정이 아님
STANDOFF = 0.30            # 모일 때 로봇 앞면과 장애물 표면 사이 간격 (m). 그 뒤 직접 다가가 닿음
ROBOT_GAP = 0.06           # 나란히 선 로봇 사이 간격 (m)
PUSH_SPEED = 0.05          # 미는 속도 (m/s)
APPROACH_SPEED = 0.04      # 닿을 때까지 다가가는 속도 (m/s)
EXIT_TOL = 0.30            # 장애물 중심이 출구에서 이만큼 안이면 도착 (m)
EXIT_RADIUS = 1.0          # 출구 둘레: 경비가 '새로 생긴 것'으로 보지 않는 곳 (옮겨 둔 장애물이 다시 경보가 되지 않게)
BACK_OFF = 0.30            # 다 밀고 물러나는 거리 (m)
SEGMENT = 1.0              # 한 번에 미는 거리 (m). 그다음 물러나 LiDAR 로 장애물을 다시 재고 길·자리를 고쳐 다시 밂
MAX_SEGMENTS = 30          # 이만큼 밀어도 출구에 못 가면 실패
MEASURE_R = 0.7            # 장애물을 잴 때 추정 중심에서 이 거리 안의 점만 (m)
MIN_R, MAX_R = 0.10, 0.60  # 장애물 반지름 추정 범위 (LiDAR 는 보이는 면만 봄)
ACTIVE = ('gathering', 'signal1', 'pushing', 'signal2')
STATE_LABEL = {'gathering': '모이는 중', 'signal1': '모여서 소리', 'pushing': '출구로 미는 중', 'signal2': '도착해서 소리',
               'done': '끝 (출구로 옮김)', 'failed': '실패', 'aborted': '중단'}


# ------------------------------------------------------------------ 출구
def get_exit(con, map_name):
    r = con.execute('SELECT x, y, updated_at FROM map_exits WHERE map = ?', (map_name,)).fetchone()
    return {'map': map_name, 'x': r['x'], 'y': r['y'], 'updated_at': r['updated_at']} if r else None


def set_exit(con, map_name, x, y):
    """출구 저장 (지도 안, 빈 칸이어야 함)"""
    info, occ, free = coverage.load_grid(map_name)
    for v in (x, y):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError('x, y 는 숫자여야 합니다')
    res, ox, oy = info['resolution'], info['origin'][0], info['origin'][1]
    i, j = int((x - ox) / res), int((y - oy) / res)
    if not (0 <= i < info['width'] and 0 <= j < info['height']):
        raise ValueError('출구가 지도 밖입니다')
    if not free[j, i]:
        raise ValueError('출구는 지도의 빈 곳(흰색)에 두어야 합니다')
    now = time.time()
    with con:
        con.execute('INSERT OR IGNORE INTO maps (name, created_at) VALUES (?, ?)', (map_name, now))
        con.execute('INSERT INTO map_exits (map, x, y, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT (map) DO UPDATE SET '
                    'x = excluded.x, y = excluded.y, updated_at = excluded.updated_at', (map_name, round(x, 3), round(y, 3), now))
    return get_exit(con, map_name)


def clear_exit(con, map_name):
    with con:
        con.execute('DELETE FROM map_exits WHERE map = ?', (map_name,))


# ------------------------------------------------------------------ 처리 기록
def _run(r):
    if not r:
        return None
    d = dict(r)
    for k in ('obstacle', 'exit', 'plan', 'resume'):
        d[k] = json.loads(d[k] or '{}')
    d['label'] = STATE_LABEL.get(d['state'], d['state'])
    return d


def get_run(con, run_id):
    return _run(con.execute('SELECT * FROM obstacle_runs WHERE id = ?', (run_id,)).fetchone())


def current_run(con):
    """진행 중인 처리 (없으면 None)"""
    return _run(con.execute(f'SELECT * FROM obstacle_runs WHERE state IN {ACTIVE} ORDER BY id DESC LIMIT 1').fetchone())


def last_run(con):
    return _run(con.execute('SELECT * FROM obstacle_runs ORDER BY id DESC LIMIT 1').fetchone())


def handled_alert(con, alert_id):
    """이 경보로 이미 처리를 시작한 적이 있는지 (실패했어도 다시 자동으로 하지 않음)"""
    return con.execute('SELECT 1 FROM obstacle_runs WHERE alert_id = ?', (alert_id,)).fetchone() is not None


def start_run(con, alert, map_name, exit_xy, resume):
    """alert: alerts 행(dict). 장애물 반지름은 경보 상자에서 추정"""
    r = min(MAX_R, max(MIN_R, max(alert['w'], alert['h']) / 2))
    ob = {'x': alert['x'], 'y': alert['y'], 'r': round(r, 3)}
    now = time.time()
    with con:
        cur = con.execute('INSERT INTO obstacle_runs (alert_id, map, state, message, obstacle, exit, resume, started_at, updated_at) '
                          "VALUES (?, ?, 'gathering', ?, ?, ?, ?, ?, ?)",
                          (alert['id'], map_name, f'경보 #{alert["id"]} 장애물 처리 시작', json.dumps(ob),
                           json.dumps({'x': exit_xy['x'], 'y': exit_xy['y']}), json.dumps(resume), now, now))
    return get_run(con, cur.lastrowid)


def update_run(con, run_id, state=None, message=None, progress=None, plan=None):
    sets, args = ['updated_at = ?'], [time.time()]
    if state is not None:
        sets.append('state = ?')
        args.append(state)
        if state not in ACTIVE:
            sets.append('finished_at = ?')
            args.append(time.time())
    if message is not None:
        sets.append('message = ?')
        args.append(str(message)[:300])
    if progress is not None:
        sets.append('progress = ?')
        args.append(float(min(1.0, max(0.0, progress))))
    if plan is not None:
        sets.append('plan = ?')
        args.append(json.dumps(plan))
    with con:
        con.execute(f'UPDATE obstacle_runs SET {", ".join(sets)} WHERE id = ?', (*args, run_id))


def finish_if_active(con, run_id, state, message):
    """아직 진행 중이면 끝냄 (서버: 중단·노드가 죽음). 바뀌었으면 True"""
    with con:
        n = con.execute(f'UPDATE obstacle_runs SET state = ?, message = ?, updated_at = ?, finished_at = ? '
                        f'WHERE id = ? AND state IN {ACTIVE}', (state, message[:300], time.time(), time.time(), run_id)).rowcount
    return n > 0


# ------------------------------------------------------------------ 밀 길 (장애물 중심 → 출구)
def plan_push_path(map_name, start, goal, clearance, zones_forbidden=None):
    """기준 지도에서 장애물 중심이 지나갈 길 [[x, y], ...]. clearance = 장애물·미는 대형이 벽에서 떨어져야 할 거리.
    8방향 A* (큰 지도는 칸을 2배로 묶어 계산), 직선으로 갈 수 있는 곳은 줄임. 못 찾으면 ValueError."""
    info, occ, free = coverage.load_grid(map_name)
    res, ox, oy = info['resolution'], info['origin'][0], info['origin'][1]
    blocked = ~free
    if zones_forbidden is not None:
        blocked = blocked | zones_forbidden
    blocked[0, :] = blocked[-1, :] = True
    blocked[:, 0] = blocked[:, -1] = True
    step = 2 if blocked.size > 250_000 else 1
    if step > 1:   # 칸 묶기: 묶음 안에 막힌 칸이 하나라도 있으면 막힘
        h2, w2 = blocked.shape[0] // step, blocked.shape[1] // step
        blocked = blocked[:h2 * step, :w2 * step].reshape(h2, step, w2, step).any(axis=(1, 3))
    r = res * step
    safe = ~coverage.disk_dilate(blocked, clearance / r)
    h, w = safe.shape

    def cell(p):
        return int((p[1] - oy) / r), int((p[0] - ox) / r)

    def snap(p, what):
        j, i = cell(p)
        k = int(math.ceil(0.6 / r))   # 0.6 m 안에서 가장 가까운 지나갈 수 있는 칸
        best, bd = None, None
        for dj in range(-k, k + 1):
            for di in range(-k, k + 1):
                jj, ii = j + dj, i + di
                if 0 <= jj < h and 0 <= ii < w and safe[jj, ii]:
                    d = dj * dj + di * di
                    if bd is None or d < bd:
                        best, bd = (jj, ii), d
        if best is None:
            raise ValueError(f'{what} 근처에 장애물이 지나갈 만큼 넓은 곳이 없습니다 (벽에서 {clearance:.2f} m 필요)')
        return best

    s, g = snap(start, '장애물'), snap(goal, '출구')
    # A*
    openh = [(0.0, 0.0, s)]
    came = {s: None}
    cost = {s: 0.0}
    nbrs = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0), (-1, -1, 1.414), (-1, 1, 1.414), (1, -1, 1.414), (1, 1, 1.414)]
    while openh:
        _, c, cur = heapq.heappop(openh)
        if cur == g:
            break
        if c > cost[cur]:
            continue
        for dj, di, sc in nbrs:
            nb = (cur[0] + dj, cur[1] + di)
            if not (0 <= nb[0] < h and 0 <= nb[1] < w) or not safe[nb]:
                continue
            nc = c + sc
            if nc < cost.get(nb, 1e18):
                cost[nb] = nc
                came[nb] = cur
                heapq.heappush(openh, (nc + math.hypot(nb[0] - g[0], nb[1] - g[1]), nc, nb))
    if g not in came:
        raise ValueError('장애물을 출구까지 옮길 길이 없습니다 (통로가 좁거나 막힘)')
    cells = []
    c = g
    while c is not None:
        cells.append(c)
        c = came[c]
    cells.reverse()

    def visible(a, b):   # 두 칸 사이 직선이 모두 지나갈 수 있는지
        n = int(max(abs(a[0] - b[0]), abs(a[1] - b[1]))) + 1
        for t in range(n + 1):
            jj = round(a[0] + (b[0] - a[0]) * t / n)
            ii = round(a[1] + (b[1] - a[1]) * t / n)
            if not safe[jj, ii]:
                return False
        return True

    # 직선으로 갈 수 있으면 건너뜀 (꺾는 곳만 남김)
    keep = [cells[0]]
    k = 0
    while k < len(cells) - 1:
        nxt = len(cells) - 1
        while nxt > k + 1 and not visible(cells[k], cells[nxt]):
            nxt -= 1
        keep.append(cells[nxt])
        k = nxt
    pts = [[ox + (i + 0.5) * r, oy + (j + 0.5) * r] for j, i in keep]
    pts[0] = [float(start[0]), float(start[1])]          # 시작은 실제 장애물 중심, 끝은 실제 출구
    pts[-1] = [float(goal[0]), float(goal[1])]
    return [[round(x, 3), round(y, 3)] for x, y in pts]


def resample(path, ds=0.05):
    """경로를 ds 간격 점으로 (따라가기 계산용)"""
    out = [list(path[0])]
    for a, b in zip(path, path[1:]):
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(1, int(L / ds))
        for k in range(1, n + 1):
            out.append([a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n])
    return out


def path_length(path):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:]))


def heading_at_start(path, look=0.5):
    """밀기 시작 방향: 경로 시작에서 look m 앞 점 쪽"""
    pts = resample(path)
    tgt = pts[min(len(pts) - 1, int(look / 0.05))]
    return math.atan2(tgt[1] - pts[0][1], tgt[0] - pts[0][0])


# ------------------------------------------------------------------ 설정 (DB obstacle_settings)
SETTING_KEYS = tuple(fleet_db.OBSTACLE_DEFAULTS)


def get_settings(con):
    """장애물 처리 값 {standoff, robot_gap, push_speed, approach_speed, segment, exit_tol, exit_radius, max_segments,
    gather_timeout, max_deviation}. v4 전 DB 면 기본값"""
    try:
        r = con.execute('SELECT * FROM obstacle_settings WHERE id = 1').fetchone()
    except Exception:  # noqa: BLE001
        r = None
    if not r:
        return dict(fleet_db.OBSTACLE_DEFAULTS)
    return {k: (int(r[k]) if k == 'max_segments' else float(r[k])) for k in SETTING_KEYS}


def set_settings(con, body):
    cur = get_settings(con)
    for k, v in (body or {}).items():
        if k not in SETTING_KEYS:
            raise ValueError(f'알 수 없는 설정: {k}')
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f'{k} 는 숫자여야 합니다')
        lo, hi = fleet_db.OBSTACLE_LIMITS[k]
        if not lo <= v <= hi:
            raise ValueError(f'{k} 는 {lo} ~ {hi} 사이여야 합니다')
        cur[k] = int(v) if k == 'max_segments' else round(float(v), 3)
    with con:
        con.execute(f'UPDATE obstacle_settings SET {", ".join(f"{k} = ?" for k in SETTING_KEYS)}, updated_at = ? WHERE id = 1',
                    (*[cur[k] for k in SETTING_KEYS], time.time()))
    return get_settings(con)


def add_event(con, run_id, kind, message='', data=None):
    """처리 안에서 일어난 일 한 줄 (단계, 구간마다 잰 위치 …)"""
    try:
        with con:
            con.execute('INSERT INTO obstacle_run_events (run_id, t, kind, message, data) VALUES (?, ?, ?, ?, ?)',
                        (run_id, time.time(), str(kind)[:30], str(message)[:300], json.dumps(data or {})))
    except Exception:  # noqa: BLE001  (v4 전 DB)
        pass


def run_events(con, run_id):
    try:
        return [dict(r, data=json.loads(r['data'] or '{}')) for r in con.execute(
            'SELECT * FROM obstacle_run_events WHERE run_id = ? ORDER BY t, id', (run_id,))]
    except Exception:  # noqa: BLE001
        return []


def formation(obstacle, path, robots, settings=None):
    """로봇 자리: 장애물 뒤(밀 방향 반대쪽)에 나란히. 장애물 폭에 들어가는 만큼은 앞줄(미는 로봇), 나머지는 뒷줄(따라감).
    robots: [{'id', 'type', 'radius'(몸체, DB robot_types), 'pose': (x, y) 또는 None}].
    반환 {id: {'row', 'lat', 'x', 'y', 'th', 'radius'}}. lat = 밀 방향 기준 옆으로 떨어진 거리 (+ 왼쪽)."""
    S = settings or fleet_db.OBSTACLE_DEFAULTS
    rad = lambda r: r.get('radius') or maps.ROBOT_RADIUS.get(r.get('type'), 0.105)   # noqa: E731
    th = heading_at_start(path)
    dx, dy = math.cos(th), math.sin(th)
    nx, ny = -dy, dx
    rr = max(rad(r) for r in robots)
    pitch = 2 * rr + S['robot_gap']
    ro = obstacle['r']
    # 앞줄: 장애물 폭에 들어가는 만큼 (양 끝 로봇은 몸체의 40% 까지 장애물 밖으로 나와도 됨)
    n_front = max(1, min(len(robots), int((2 * ro + pitch * 0.6) / pitch)))
    n_back = len(robots) - n_front

    def lats(n):
        return [(k - (n - 1) / 2) * pitch for k in range(n)]
    slots = [('front', l) for l in lats(n_front)] + [('back', l) for l in lats(n_back)]
    out = {}
    free = list(robots)
    for row, lat in slots:   # 자리마다 가장 가까운 로봇 (모르면 순서대로)
        back = ro + rr + S['standoff'] if row == 'front' else ro + rr + S['standoff'] + pitch + 0.10
        sx = obstacle['x'] - dx * back + nx * lat
        sy = obstacle['y'] - dy * back + ny * lat
        pick = min(free, key=lambda r: (r['pose'][0] - sx) ** 2 + (r['pose'][1] - sy) ** 2 if r.get('pose') else 1e9)
        free.remove(pick)
        out[pick['id']] = {'row': row, 'lat': round(lat, 3), 'x': round(sx, 3), 'y': round(sy, 3), 'th': round(th, 4),
                           'radius': rad(pick), 'back': round(back, 3)}
    return out


def offset_path(path, lat):
    """경로를 옆으로 lat 만큼 (+ 왼쪽) 평행 이동 (ds 간격 점)"""
    pts = resample(path)
    out = []
    for k, (x, y) in enumerate(pts):
        a, b = pts[max(0, k - 2)], pts[min(len(pts) - 1, k + 2)]
        th = math.atan2(b[1] - a[1], b[0] - a[0])
        out.append([x - math.sin(th) * lat, y + math.cos(th) * lat])
    return out


def fit_circle(points, guess, r_guess, viewers=()):
    """LiDAR 점들로 장애물(원) 중심·반지름 추정. points: [[x, y], ...] (지도 좌표, 장애물 둘레의 점만)
    대수적 원 맞춤(Kåsa). LiDAR 는 보이는 면(호)만 보므로 점이 적거나 맞춤이 이상하면,
    점들의 중심에서 보는 로봇들(viewers) 반대쪽으로 반지름만큼 민 곳을 중심으로 봄. 점이 4개 미만이면 None."""
    if len(points) < 4:
        return None
    P = np.asarray(points, dtype=float)
    cx0, cy0 = P.mean(axis=0)
    A = np.column_stack([P[:, 0], P[:, 1], np.ones(len(P))])
    b = -(P[:, 0] ** 2 + P[:, 1] ** 2)
    try:
        (D, E, F), *_ = np.linalg.lstsq(A, b, rcond=None)
        cx, cy = -D / 2, -E / 2
        r = math.sqrt(max(0.0, cx * cx + cy * cy - F))
        if MIN_R * 0.5 <= r <= MAX_R + 0.2 and math.hypot(cx - cx0, cy - cy0) <= r + 0.05 \
                and math.hypot(cx - guess[0], cy - guess[1]) <= 0.6:
            return {'x': round(float(cx), 3), 'y': round(float(cy), 3), 'r': round(min(MAX_R, max(MIN_R, r)), 3), 'n': len(P), 'fit': 'circle'}
    except (np.linalg.LinAlgError, ValueError):
        pass
    # 맞춤 실패: 보이는 면의 중심에서 로봇들 반대쪽으로 반지름만큼
    if viewers:
        vx, vy = np.mean([v[0] for v in viewers]), np.mean([v[1] for v in viewers])
        dx, dy = cx0 - vx, cy0 - vy
        d = math.hypot(dx, dy) or 1.0
        cx0, cy0 = cx0 + dx / d * r_guess * 0.7, cy0 + dy / d * r_guess * 0.7
    return {'x': round(float(cx0), 3), 'y': round(float(cy0), 3), 'r': r_guess, 'n': len(P), 'fit': 'centroid'}


def project(pts, x, y, hint=0, window=None):
    """점(x, y)에 가장 가까운 경로 점 번호 (hint 부터 window 안에서 찾음 — 겹치는 경로에서 뒤로 튀지 않게)"""
    lo = max(0, hint - 5)
    hi = len(pts) if window is None else min(len(pts), hint + window)
    arr = np.asarray(pts[lo:hi])
    k = int(np.argmin((arr[:, 0] - x) ** 2 + (arr[:, 1] - y) ** 2))
    return lo + k
