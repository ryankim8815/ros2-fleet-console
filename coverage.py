"""경비 순회 지점 자동 생성: 기준 지도에서 로봇이 갈 수 있는 곳을 빠짐없이 도는 지점 목록 (patrol_node.py 가 씀).

  1) 갈 수 있는 칸: 기준 지도의 빈 칸(free)에서 벽·모르는 곳으로부터 (몸체 반지름 + CLEARANCE) 이상 떨어진 칸,
     그리고 그 로봇의 이동 구역(금지 구역 밖, 허용 구역이 있으면 그 안)
  2) 로봇이 있는 곳과 이어진 영역만 (없으면 가장 큰 영역) — 닫힌 방·벽 너머로는 보내지 않음
  3) spacing 간격 격자 점 중 갈 수 있는 칸에 있는 것 + 격자에서 빠진 좁은 곳(복도 등)을 메우는 점
     → 모든 갈 수 있는 칸이 어떤 지점에서 spacing × COVER 안에 들어오게
  4) 순서: 아래 줄부터 지그재그(줄마다 방향을 바꿈). 경비 로봇이 여럿이면 이 순서대로 이어진 덩어리로 나눔
     (로봇마다 자기 구역을 맡음, 그 로봇이 못 가는 점은 갈 수 있는 다른 로봇에게)

numpy 만 씀 (scipy 없음).
"""
import math
from collections import deque

import numpy as np

import maps

CLEARANCE = 0.12     # 몸체 반지름에 더하는 벽과의 여유 (m). Nav2 inflation 안쪽 깊이 들어가지 않게
COVER = 0.75         # 모든 갈 수 있는 칸이 spacing × COVER 안에 지점을 갖게 (좁은 곳 메우기)


def load_grid(map_name):
    """(info, occ, free) — 배열 행 0 = 아래(최소 y)"""
    info = maps.map_info(map_name)
    w, h, maxval, px = maps._read_pgm(info['image'])
    g = np.frombuffer(px, dtype=np.uint8).reshape(h, w)[::-1].astype(np.int16)
    if info['negate']:
        g = maxval - g
    return info, g <= 0.35 * maxval, g >= 0.75 * maxval


def disk_dilate(mask, r_cells):
    """mask 를 반지름 r_cells 원으로 부풀림"""
    h, w = mask.shape
    out = mask.copy()
    n = int(math.ceil(r_cells))
    for dy in range(-n, n + 1):
        for dx in range(-n, n + 1):
            if dx * dx + dy * dy > r_cells * r_cells or (dx == 0 and dy == 0):
                continue
            out[max(0, dy):h - max(0, -dy), max(0, dx):w - max(0, -dx)] |= \
                mask[max(0, -dy):h - max(0, dy), max(0, -dx):w - max(0, dx)]
    return out


def forbidden_grid(info, zones, robot_id):
    """그 로봇에게 금지인 칸 (bool, 행 0 = 아래). zones: maps.read_zones(con, 지도)['zones'] (DB)"""
    w, h = info['width'], info['height']
    mine = [z for z in zones if '*' in z['robots'] or robot_id in z['robots']]
    if not mine:
        return np.zeros((h, w), dtype=bool)
    FREE, LETHAL = 254, 0
    allowed = [z for z in mine if z['type'] == 'allowed']
    grid = bytearray([LETHAL if allowed else FREE]) * (w * h)
    for z in allowed:
        maps._fill_polygon(grid, w, h, info, z['points'], FREE)
    for z in mine:
        if z['type'] == 'keepout':
            maps._fill_polygon(grid, w, h, info, z['points'], LETHAL)
    return (np.frombuffer(bytes(grid), dtype=np.uint8).reshape(h, w) == LETHAL)[::-1]   # _fill_polygon 은 행 0 = 위


def components(mask):
    """4-이웃 연결 영역 번호 (0 = 영역 아님). 순수 파이썬 BFS (계획할 때 한 번)"""
    h, w = mask.shape
    lab = np.zeros((h, w), dtype=np.int32)
    flat = mask.ravel()
    lflat = lab.ravel()
    n = 0
    for start in np.flatnonzero(flat):
        if lflat[start]:
            continue
        n += 1
        lflat[start] = n
        q = deque([start])
        while q:
            c = q.popleft()
            y, x = divmod(c, w)
            for nb, ok in ((c - 1, x > 0), (c + 1, x < w - 1), (c - w, y > 0), (c + w, y < h - 1)):
                if ok and flat[nb] and not lflat[nb]:
                    lflat[nb] = n
                    q.append(nb)
    return lab, n


def reachable(info, occ, free, radius, forbidden, start_xy=None):
    """갈 수 있는 칸 (bool): 여유를 둔 빈 칸 중 시작 위치와 이어진 영역 (시작 위치가 없거나 밖이면 가장 큰 영역)"""
    res = info['resolution']
    blocked = ~free | forbidden                       # 벽·모르는 곳·금지 구역
    blocked[0, :] = blocked[-1, :] = True              # 지도 테두리 밖은 모르는 곳
    blocked[:, 0] = blocked[:, -1] = True
    safe = ~disk_dilate(blocked, (radius + CLEARANCE) / res)
    lab, n = components(safe)
    if n == 0:
        return safe
    pick = 0
    if start_xy is not None:
        i = int((start_xy[0] - info['origin'][0]) / res)
        j = int((start_xy[1] - info['origin'][1]) / res)
        h, w = safe.shape
        # 로봇이 벽 여유 안쪽에 서 있을 수 있어 0.5 m 안에서 가장 가까운 갈 수 있는 칸을 찾음
        r = int(0.5 / res)
        y0, y1, x0, x1 = max(0, j - r), min(h, j + r + 1), max(0, i - r), min(w, i + r + 1)
        if y0 < y1 and x0 < x1:
            sub = lab[y0:y1, x0:x1]
            ys, xs = np.nonzero(sub)
            if len(ys):
                k = np.argmin((ys + y0 - j) ** 2 + (xs + x0 - i) ** 2)
                pick = int(sub[ys[k], xs[k]])
    if not pick:
        pick = int(np.argmax(np.bincount(lab.ravel())[1:]) + 1)
    return lab == pick


def sample_points(info, ok, spacing):
    """갈 수 있는 칸(ok) 위의 지점: spacing 격자 + 좁은 곳 메우기. 반환 [(x, y), ...] (순서 없음)"""
    res, ox, oy = info['resolution'], info['origin'][0], info['origin'][1]
    h, w = ok.shape
    step = max(1, int(round(spacing / res)))
    pts = [(i, j) for j in range(step // 2, h, step) for i in range(step // 2, w, step) if ok[j, i]]
    # 메우기: 아직 어떤 지점에서도 spacing × COVER 밖인 갈 수 있는 칸에 지점을 더함
    rc = spacing * COVER / res
    covered = np.zeros_like(ok)
    n = int(math.ceil(rc))
    yy, xx = np.mgrid[-n:n + 1, -n:n + 1]
    disk = (xx * xx + yy * yy) <= rc * rc

    def mark(i, j):
        y0, y1, x0, x1 = max(0, j - n), min(h, j + n + 1), max(0, i - n), min(w, i + n + 1)
        covered[y0:y1, x0:x1] |= disk[y0 - j + n:y1 - j + n, x0 - i + n:x1 - i + n]

    for i, j in pts:
        mark(i, j)
    while True:
        left = np.flatnonzero((ok & ~covered).ravel())
        if not len(left):
            break
        j, i = divmod(int(left[0]), w)
        # 그 칸에서 위·오른쪽으로 rc 의 절반쯤 들어간 갈 수 있는 칸을 지점으로 (테두리에 붙지 않게)
        best = (i, j)
        for d in range(int(rc // 2), 0, -1):
            jj, ii = min(h - 1, j + d), min(w - 1, i + d)
            if ok[jj, ii]:
                best = (ii, jj)
                break
        pts.append(best)
        mark(*best)
        covered[j, i] = True     # 혹시 못 덮어도 같은 칸에서 멈추지 않게
    return [(round(ox + (i + 0.5) * res, 3), round(oy + (j + 0.5) * res, 3)) for i, j in pts]


def snake_order(points, spacing):
    """아래 줄부터 지그재그 (줄 높이 = spacing)"""
    rows = {}
    for x, y in points:
        rows.setdefault(int(math.floor(y / spacing)), []).append((x, y))
    out = []
    for k, r in enumerate(sorted(rows)):
        out += sorted(rows[r], reverse=bool(k % 2))
    return out


def plan(map_name, guards, spacing, zones=None):
    """guards: [{'id', 'type', 'radius', 'pose': (x, y) 또는 None}, ...] (순서 고정), zones: 그 지도의 구역 (DB).
    반환 {robot_id: [[x, y], ...]}

    로봇마다 갈 수 있는 곳이 다를 수 있어(크기·이동 구역) 로봇마다 계산하고, 모두의 지점을 합쳐 지그재그로 줄 세운 뒤
    로봇 수만큼 이어진 덩어리로 나눔. 덩어리의 점을 그 로봇이 못 가면 갈 수 있는 로봇에게 넘김."""
    if not guards:
        return {}
    info, occ, free = load_grid(map_name)
    zones = zones or []   # 부르는 쪽이 DB 에서 읽어 넘김 (maps.read_zones)
    ok = {}
    for g in guards:
        radius = g.get('radius') or maps.ROBOT_RADIUS.get(g.get('type'), 0.105)   # DB robot_types 몸체 반지름
        ok[g['id']] = reachable(info, occ, free, radius, forbidden_grid(info, zones, g['id']), g.get('pose'))
    union = np.zeros_like(next(iter(ok.values())))
    for m in ok.values():
        union |= m
    pts = snake_order(sample_points(info, union, spacing), spacing)
    res, ox, oy = info['resolution'], info['origin'][0], info['origin'][1]

    def can(rid, p):
        return bool(ok[rid][int((p[1] - oy) / res), int((p[0] - ox) / res)])

    ids = [g['id'] for g in guards]
    out = {rid: [] for rid in ids}
    for k, p in enumerate(pts):
        owner = ids[min(len(ids) - 1, k * len(ids) // max(1, len(pts)))]
        if not can(owner, p):
            owner = next((rid for rid in ids if can(rid, p)), None)
        if owner:
            out[owner].append([p[0], p[1]])
    return out
