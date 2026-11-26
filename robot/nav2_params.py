"""Nav2 파라미터 파일을 로봇(네임스페이스)에 맞게 고치는 공통 함수 (nav2_ns, coop_mapping launch 에서 사용).

  - 프레임 이름(odom, base_link, base_footprint, base_scan, rplidar_link)에 frame_prefix 를 붙임
  - map_frame 을 주면 'map' 프레임도 그 이름으로 (지도 만들기: 로봇마다 자기 SLAM 지도 프레임 '<ns>/map')
  - '/scan' 같은 절대 토픽을 상대 이름으로 바꿔 네임스페이스 안을 보게 함
  - enable_stamped_cmd_vel 을 로봇에 맞춤 (TurtleBot3 true, Pinky false)
  - keepout: 전역·지역 costmap 에 KeepoutFilter 추가 (토픽은 /<ns>/… 절대 이름)
  - 로봇 종류별 기본 파라미터 파일: TurtleBot3 config/nav2/burger.yaml, Pinky config/nav2/pinky.yaml
    (종류는 config/fleet.json 에서 네임스페이스로 찾음, robot_for_ns)
  - safety: collision_monitor 의 LiDAR 안전 구역 LidarStop(정지)·LidarSlow(서행) 값을 DB 의 그 로봇 적용 값(로봇별 덮어쓰기 > 플릿 기본값)으로 채움
"""
import json
import os
import sys
import tempfile

import yaml

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)), '..'))
sys.path.insert(0, APP_DIR)
from safety import read_for_ns  # noqa: E402

# 로봇 종류 → 기본 Nav2 파라미터 파일 (config/nav2/). 모르는 종류는 TurtleBot3(burger)
PARAMS_BY_TYPE = {'turtlebot3': 'burger.yaml', 'pinky': 'pinky.yaml'}


def robot_for_ns(ns, fleet_path=os.path.join(APP_DIR, 'config', 'fleet.json')):
    """config/fleet.json 에서 네임스페이스('tb3_01', '/pinky_01', '' …)가 같은 로봇 설정. 없거나 여러 개면 None"""
    norm = lambda v: str(v or '').strip('/')   # noqa: E731
    try:
        with open(fleet_path, encoding='utf-8') as f:
            robots = json.load(f).get('robots', [])
    except (FileNotFoundError, ValueError):
        return None
    hit = [r for r in robots if norm(r.get('ns')) == norm(ns)]
    return hit[0] if len(hit) == 1 else None


def params_for_type(robot_type):
    """그 종류의 기본 파라미터 파일 경로 (config/nav2/<burger|pinky>.yaml)"""
    return os.path.join(APP_DIR, 'config', 'nav2', PARAMS_BY_TYPE.get(robot_type, PARAMS_BY_TYPE['turtlebot3']))


def stamped_for_type(robot_type):
    """cmd_vel 이 TwistStamped 인지 (TurtleBot3 Jazzy true, Pinky false)"""
    return robot_type != 'pinky'

FRAMES = {'odom', 'base_link', 'base_footprint', 'base_scan', 'rplidar_link'}
KEEPOUT_INFO = 'keepout_costmap_filter_info'
KEEPOUT_MASK = 'keepout_filter_mask'


def keepout_topics(ns):
    """costmap 노드는 /<ns>/global_costmap 안에 있어 상대 이름이 그 아래로 풀리므로 절대 이름을 씀"""
    base = f'/{ns}' if ns else ''
    return f'{base}/{KEEPOUT_INFO}', f'{base}/{KEEPOUT_MASK}'


def apply_safety(data, s, changes):
    """collision_monitor 의 LidarStop·LidarSlow 원형 구역에 안전 설정(safety.py) 을 넣음. 구역이 없는 파라미터 파일이면 추가."""
    p = data.get('collision_monitor', {}).get('ros__parameters')
    if p is None:
        return
    polys = list(p.get('polygons', []))
    for name in ('LidarSlow', 'LidarStop'):
        if name not in polys:
            polys.insert(0, name)
    p['polygons'] = polys
    stop = p.setdefault('LidarStop', {})
    stop.update({'type': 'circle', 'action_type': 'stop', 'radius': s['stop_dist'], 'enabled': s['enabled']})
    stop.setdefault('min_points', 3)
    slow = p.setdefault('LidarSlow', {})
    slow.update({'type': 'circle', 'action_type': 'limit', 'radius': s['slow_dist'], 'linear_limit': s['slow_speed'],
                 'enabled': s['enabled']})
    slow.setdefault('min_points', 3)
    slow.setdefault('angular_limit', 10.0)
    changes.append(('collision_monitor.LidarStop/LidarSlow', None,
                    f"enabled={s['enabled']} stop={s['stop_dist']} slow={s['slow_dist']}@{s['slow_speed']}"))


def rewrite_params(src, prefix, stamped, keepout=False, ns='', map_frame='map', safety=None):
    """Return (path of a rewritten copy of the Nav2 params file, list of changes).

    safety: LiDAR 안전 구역 설정 dict (safety.read_for_ns()). None 이면 DB 에서 그 네임스페이스 로봇의 적용 값을 읽음 (덮어쓰기 > 플릿 기본값, 읽기 전용, 없으면 기본값)."""
    with open(src, encoding='utf-8') as f:
        data = yaml.safe_load(f)
    changes = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                p = f'{path}.{k}' if path else k
                if isinstance(v, (dict, list)):
                    walk(v, p)
                    continue
                key = str(k)
                is_frame = isinstance(v, str) and (key.endswith('frame') or key.endswith('frame_id'))
                if key == 'enable_stamped_cmd_vel':
                    node[k] = stamped
                    changes.append((p, v, stamped))
                elif is_frame and v in FRAMES and prefix:
                    node[k] = prefix + v
                    changes.append((p, v, node[k]))
                elif is_frame and v == 'map' and map_frame != 'map':
                    node[k] = map_frame
                    changes.append((p, v, node[k]))
                elif isinstance(v, str) and ('topic' in key) and v.startswith('/'):
                    node[k] = v.lstrip('/')
                    changes.append((p, v, node[k]))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f'{path}[{i}]')

    walk(data, '')
    apply_safety(data, safety if safety is not None else read_for_ns(ns), changes)

    if keepout:
        # 전역·지역 costmap 모두에 KeepoutFilter (지역 costmap 에도 넣어야 제어기가 구역 안으로 들어가지 않음)
        for cm in ('global_costmap', 'local_costmap'):
            p = data.get(cm, {}).get(cm, {}).get('ros__parameters')
            if p is None:
                continue
            filters = list(p.get('filters', []))
            if 'keepout_filter' not in filters:
                filters.append('keepout_filter')
            p['filters'] = filters
            p['keepout_filter'] = {'plugin': 'nav2_costmap_2d::KeepoutFilter', 'enabled': True,
                                   'filter_info_topic': keepout_topics(ns)[0]}
            changes.append((f'{cm}.filters', None, filters))

    fd, out = tempfile.mkstemp(prefix='nav2_ns_', suffix='.yaml')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        yaml.safe_dump(data, f, sort_keys=False)
    return out, changes
