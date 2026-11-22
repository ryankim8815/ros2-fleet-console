"""지도 파일, 로봇별 이동 구역(금지/허용), Nav2 keepout 마스크, 지도 작성(SLAM) 프로세스 관리.

저장 위치 (ros2-fleet-console/config/):
  maps/<name>.yaml, maps/<name>.pgm          지도 (map_saver 형식)
  zones/<name>.json                           그 지도의 구역 목록
  masks/<name>/<ns_leaf>.yaml, .pgm           로봇별 keepout 마스크 (Nav2 KeepoutFilter 용, 구역 저장 때 생성)

구역 규칙 (로봇마다):
  - 'allowed'(허용) 구역이 하나라도 있으면 그 밖은 모두 금지
  - 'keepout'(금지) 구역은 항상 금지
  - robots: ['*'] 는 모든 로봇, 아니면 로봇 id 목록
"""
import io
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(APP_DIR, 'config')
MAPS_DIR = os.path.join(CONFIG, 'maps')
ZONES_DIR = os.path.join(CONFIG, 'zones')
MASKS_DIR = os.path.join(CONFIG, 'masks')
LOG_DIR = os.path.join(APP_DIR, 'logs')
MAPPING_LAUNCH = os.path.join(APP_DIR, 'robot', 'mapping_ns.launch.py')

NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$')
ID_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,40}$')
MAX_ZONES = 64
MAX_POINTS = 200


def _atomic_write(path, data, binary=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    with os.fdopen(fd, 'wb' if binary else 'w', **({} if binary else {'encoding': 'utf-8'})) as f:
        f.write(data)
    os.chmod(tmp, 0o644)   # mkstemp 기본 600 → 지도·구역·마스크는 비밀이 아니므로 다른 도구도 읽게
    os.replace(tmp, path)


def ns_leaf(ns):
    """'/tb3_01' -> 'tb3_01', '' -> 'default' (마스크 파일 이름)"""
    return ns.strip('/').replace('/', '_') or 'default'


# ------------------------------------------------------------------ 지도 파일
def _read_pgm(path):
    with open(path, 'rb') as f:
        data = f.read()
    # P5 헤더: magic, width, height, maxval (주석 허용)
    tokens, i = [], 0
    while len(tokens) < 4:
        while data[i:i + 1].isspace():
            i += 1
        if data[i:i + 1] == b'#':
            while data[i:i + 1] not in (b'\n', b''):
                i += 1
            continue
        j = i
        while not data[j:j + 1].isspace():
            j += 1
        tokens.append(data[i:j])
        i = j
    if tokens[0] != b'P5':
        raise ValueError('P5(PGM) 형식만 지원합니다')
    w, h, maxval = int(tokens[1]), int(tokens[2]), int(tokens[3])
    return w, h, maxval, data[i + 1:i + 1 + w * h]


def map_info(name):
    """지도 yaml + pgm 의 기하 정보. 없으면 FileNotFoundError."""
    import yaml
    if not NAME_RE.match(name):
        raise ValueError('지도 이름 형식이 올바르지 않습니다')
    ypath = os.path.join(MAPS_DIR, f'{name}.yaml')
    with open(ypath, encoding='utf-8') as f:
        y = yaml.safe_load(f)
    img = y['image'] if os.path.isabs(y['image']) else os.path.join(MAPS_DIR, y['image'])
    w, h, _, _ = _read_pgm(img)
    return {
        'name': name, 'yaml': ypath, 'image': img, 'width': w, 'height': h,
        'resolution': float(y['resolution']), 'origin': [float(v) for v in y['origin'][:3]],
        'negate': int(y.get('negate', 0)), 'mtime': os.path.getmtime(ypath),
    }


def list_maps():
    out = []
    if os.path.isdir(MAPS_DIR):
        for fn in sorted(os.listdir(MAPS_DIR)):
            if fn.endswith('.yaml'):
                try:
                    info = map_info(fn[:-5])
                    info = {k: v for k, v in info.items() if k not in ('yaml', 'image')}
                    info['zones'] = os.path.exists(os.path.join(ZONES_DIR, f'{info["name"]}.json'))
                    out.append(info)
                except Exception as e:  # 깨진 파일은 건너뜀
                    out.append({'name': fn[:-5], 'error': str(e)})
    return out


def map_png(name):
    """지도 이미지를 PNG 로 (브라우저 표시용). 미탐색 영역은 투명."""
    from PIL import Image
    info = map_info(name)
    w, h, maxval, px = _read_pgm(info['image'])
    gray = Image.frombytes('L', (w, h), px)
    if info['negate']:
        gray = gray.point(lambda v: maxval - v)
    # 웹 실시간 지도와 같은 색: 비어 있음 = 어두운 회색, 벽 = 밝은 회색, 미탐색(205 근처) = 투명
    occ = lambda v: v <= 0.35 * maxval
    free = lambda v: v >= 0.75 * maxval
    r = gray.point(lambda v: 201 if occ(v) else 32 if free(v) else 90)
    g = gray.point(lambda v: 209 if occ(v) else 39 if free(v) else 98)
    b = gray.point(lambda v: 217 if occ(v) else 48 if free(v) else 110)
    alpha = gray.point(lambda v: 0 if 180 <= v <= 230 else 255)
    rgba = Image.merge('RGBA', (r, g, b, alpha))
    buf = io.BytesIO()
    rgba.save(buf, 'PNG')
    return buf.getvalue()


# ------------------------------------------------------------------ 구역
def read_zones(name):
    if not NAME_RE.match(name):
        raise ValueError('지도 이름 형식이 올바르지 않습니다')
    try:
        with open(os.path.join(ZONES_DIR, f'{name}.json'), encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {'map': name, 'zones': []}


def validate_zones(name, data, robot_ids):
    if not isinstance(data, dict) or not isinstance(data.get('zones'), list):
        raise ValueError('zones 배열이 필요합니다')
    if len(data['zones']) > MAX_ZONES:
        raise ValueError(f'구역은 최대 {MAX_ZONES}개입니다')
    out, seen = [], set()
    for i, z in enumerate(data['zones']):
        zid = str(z.get('id', ''))
        if not ID_RE.match(zid) or zid in seen:
            raise ValueError(f'zones[{i}].id 형식 오류 또는 중복')
        seen.add(zid)
        zname = str(z.get('name', ''))[:40] or zid
        ztype = z.get('type')
        if ztype not in ('keepout', 'allowed'):
            raise ValueError(f'zones[{i}].type 은 keepout 또는 allowed')
        robots = z.get('robots', ['*'])
        if not isinstance(robots, list) or not robots or not all(r == '*' or r in robot_ids for r in robots):
            raise ValueError(f'zones[{i}].robots 에 등록되지 않은 로봇이 있습니다')
        pts = z.get('points')
        if not isinstance(pts, list) or not (3 <= len(pts) <= MAX_POINTS):
            raise ValueError(f'zones[{i}].points 는 점 3~{MAX_POINTS}개')
        clean = []
        for p in pts:
            if not (isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p)):
                raise ValueError(f'zones[{i}].points 형식 오류')
            clean.append([round(float(p[0]), 3), round(float(p[1]), 3)])
        out.append({'id': zid, 'name': zname, 'type': ztype, 'robots': robots, 'points': clean})
    return {'map': name, 'zones': out}


def _fill_polygon(grid, w, h, info, pts, value):
    """다각형 내부(셀 중심 기준)를 value 로 채움. grid: bytearray, row 0 = 지도 위쪽(최대 y)."""
    res, ox, oy = info['resolution'], info['origin'][0], info['origin'][1]
    cells = [((x - ox) / res, (y - oy) / res) for x, y in pts]   # 셀 좌표 (row 는 아래가 0)
    n = len(cells)
    ys = [c[1] for c in cells]
    r0, r1 = max(0, int(min(ys))), min(h - 1, int(max(ys)) + 1)
    for r in range(r0, r1 + 1):
        yc = r + 0.5
        xs = []
        for k in range(n):
            (x1, y1), (x2, y2) = cells[k], cells[(k + 1) % n]
            if (y1 <= yc < y2) or (y2 <= yc < y1):
                xs.append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
        xs.sort()
        row = (h - 1 - r) * w
        for a, b in zip(xs[0::2], xs[1::2]):
            c0, c1 = max(0, int(a + 0.5)), min(w - 1, int(b - 0.5))
            if c1 >= c0:
                grid[row + c0:row + c1 + 1] = bytes([value]) * (c1 - c0 + 1)


# Nav2 KeepoutFilter 는 금지 셀을 부풀리지(inflation) 않습니다. 금지 셀만 넣으면 경로가 구역 경계에 딱 붙어
# 로봇 몸체가 구역에 걸리고 제어기(DWB)가 "모든 궤적이 충돌"로 멈춥니다. 금지 영역을 넓히는 것도 소용없습니다
# (경로가 넓힌 경계에 다시 붙음). 그래서 마스크에 inflation 과 같은 '비용 띠'를 넣습니다:
#   구역 안 = 통과 불가(100), 로봇 반지름 이내 = 매우 높은 비용(99), 그 밖 BAND 거리까지 점점 낮아지는 비용.
# KeepoutFilter 는 중간 값을 중간 비용으로 씁니다 (mode: scale).
ROBOT_RADIUS = {'turtlebot3': 0.105, 'pinky': 0.07}
COST_BAND = 0.35      # 반지름 밖 비용이 남는 거리 (m)


def _cost_band(lethal, res, radius):
    """lethal(bool h×w) → 점유값(0~100) 배열: 구역 100, 반지름 이내 99, 그 밖 지수적으로 감소."""
    import math
    import numpy as np
    h, w = lethal.shape
    n = int(math.ceil((radius + COST_BAND) / res))
    dist = np.full((h, w), np.inf)
    dist[lethal] = 0.0
    for dy in range(-n, n + 1):
        for dx in range(-n, n + 1):
            d = math.hypot(dx, dy) * res
            if d == 0 or d > radius + COST_BAND:
                continue
            src = lethal[max(0, -dy):h - max(0, dy), max(0, -dx):w - max(0, dx)]
            dst = dist[max(0, dy):h - max(0, -dy), max(0, dx):w - max(0, -dx)]
            np.minimum(dst, np.where(src, d, np.inf), out=dst)
    occ = np.zeros((h, w), dtype=np.float64)
    near = (dist > 0) & (dist <= radius)
    band = (dist > radius) & (dist <= radius + COST_BAND)
    occ[lethal] = 100
    occ[near] = 99
    occ[band] = np.maximum(1, np.round(98 * np.exp(-3.0 * (dist[band] - radius) / COST_BAND)))
    return occ


def build_masks(name, zones, robots):
    """로봇마다 keepout 마스크(pgm+yaml, mode scale) 생성. robots: fleet robots 목록. 반환: {robot_id: {...}}"""
    import numpy as np
    info = map_info(name)
    w, h = info['width'], info['height']
    out_dir = os.path.join(MASKS_DIR, name)
    os.makedirs(out_dir, exist_ok=True)
    FREE, LETHAL = 254, 0
    made = {}
    for r in robots:
        mine = [z for z in zones if '*' in z['robots'] or r['id'] in z['robots']]
        allowed = [z for z in mine if z['type'] == 'allowed']
        grid = bytearray([LETHAL if allowed else FREE]) * (w * h)
        for z in allowed:
            _fill_polygon(grid, w, h, info, z['points'], FREE)
        for z in mine:
            if z['type'] == 'keepout':
                _fill_polygon(grid, w, h, info, z['points'], LETHAL)
        radius = ROBOT_RADIUS.get(r.get('type'), 0.105)
        lethal = np.frombuffer(bytes(grid), dtype=np.uint8).reshape(h, w) == LETHAL
        occ = _cost_band(lethal, info['resolution'], radius) if mine else np.zeros((h, w))
        # scale 모드: p = (255 - 픽셀)/255, p ≥ 0.99 → 100, 그 아래는 100·p 에 비례
        px = np.clip(np.round(255 * (1 - occ / 100.0)), 0, 255).astype(np.uint8)
        px[occ >= 100] = 0
        leaf = ns_leaf(r['ns'])
        pgm = f'{leaf}.pgm'
        _atomic_write(os.path.join(out_dir, pgm), f'P5\n{w} {h}\n255\n'.encode() + px.tobytes(), binary=True)
        o = info['origin']
        _atomic_write(os.path.join(out_dir, f'{leaf}.yaml'),
                      f'image: {pgm}\nmode: scale\nresolution: {info["resolution"]}\n'
                      f'origin: [{o[0]}, {o[1]}, {o[2]}]\nnegate: 0\noccupied_thresh: 0.99\nfree_thresh: 0.005\n')
        made[r['id']] = {'ns': r['ns'], 'yaml': os.path.join(out_dir, f'{leaf}.yaml'), 'zones': len(mine),
                         'lethalCells': int(lethal.sum()) if mine else 0}
    return made


def write_zones(name, data, robots):
    clean = validate_zones(name, data, {r['id'] for r in robots})
    map_info(name)  # 지도가 있어야 함
    _atomic_write(os.path.join(ZONES_DIR, f'{name}.json'), json.dumps(clean, ensure_ascii=False, indent=2) + '\n')
    masks = build_masks(name, clean['zones'], robots)
    return clean, masks


# ------------------------------------------------------------------ 지도 작성 (SLAM)
class Mapping:
    """로봇 한 대로 slam_toolbox 를 실행하고 지도를 저장. 한 번에 하나만."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.robot = None
        self.started = 0.0
        self.log = os.path.join(LOG_DIR, 'mapping.log')

    def status(self):
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            if self.proc is not None and not running:
                code = self.proc.returncode
                self.proc = None
                return {'running': False, 'robot': self.robot, 'exited': code, 'log': self._tail()}
            return {'running': running, 'robot': self.robot if running else None,
                    'seconds': round(time.time() - self.started) if running else 0}

    def _tail(self, n=15):
        try:
            with open(self.log, encoding='utf-8', errors='replace') as f:
                return f.readlines()[-n:]
        except FileNotFoundError:
            return []

    def start(self, robot):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                raise ValueError(f'이미 {self.robot["name"]} 로 지도를 작성하는 중입니다')
            os.makedirs(LOG_DIR, exist_ok=True)
            cmd = ['ros2', 'launch', MAPPING_LAUNCH, f'namespace:={robot["ns"].strip("/")}',
                   f'frame_prefix:={robot.get("tfPrefix", "")}', f'tf_topic:={robot.get("tfTopic", "/tf")}']
            logf = open(self.log, 'w', encoding='utf-8')
            # 새 세션: 정지할 때 launch 와 자식 노드를 한꺼번에 종료
            self.proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
            self.robot = {k: robot[k] for k in ('id', 'name', 'ns', 'tfTopic', 'tfPrefix')}
            self.started = time.time()
            return {'running': True, 'robot': self.robot, 'cmd': ' '.join(cmd)}

    def stop(self):
        with self.lock:
            p = self.proc
            if p is None or p.poll() is not None:
                self.proc = None
                return {'running': False}
            try:
                os.killpg(p.pid, signal.SIGINT)
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=5)
            self.proc = None
            return {'running': False, 'robot': self.robot}

    def save(self, name):
        if not NAME_RE.match(name or ''):
            raise ValueError('지도 이름은 영문·숫자·_- 로 1~41자')
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                raise ValueError('지도 작성이 실행 중이 아닙니다')
            ns = self.robot['ns']
        os.makedirs(MAPS_DIR, exist_ok=True)
        base = os.path.join(MAPS_DIR, name)
        cmd = ['ros2', 'run', 'nav2_map_server', 'map_saver_cli', '-t', f'{ns}/map', '-f', base,
               '--ros-args', '-p', 'map_subscribe_transient_local:=true', '-p', 'save_map_timeout:=10.0']
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        if r.returncode != 0 or not os.path.exists(base + '.yaml'):
            raise ValueError('지도 저장 실패: ' + (r.stderr or r.stdout)[-400:])
        # map_saver 는 image 를 절대 경로로 쓸 수 있어 상대 경로로 고침 (폴더째 옮겨도 동작)
        with open(base + '.yaml', encoding='utf-8') as f:
            y = f.read()
        y = re.sub(r'^image:.*$', f'image: {name}.pgm', y, flags=re.M)
        _atomic_write(base + '.yaml', y)
        return {k: v for k, v in map_info(name).items() if k not in ('yaml', 'image')}
