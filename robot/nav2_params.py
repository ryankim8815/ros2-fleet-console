"""Nav2 파라미터 파일을 로봇(네임스페이스)에 맞게 고치는 공통 함수 (nav2_ns, coop_mapping launch 에서 사용).

  - 프레임 이름(odom, base_link, base_footprint, base_scan, rplidar_link)에 frame_prefix 를 붙임
  - map_frame 을 주면 'map' 프레임도 그 이름으로 (지도 만들기: 로봇마다 자기 SLAM 지도 프레임 '<ns>/map')
  - '/scan' 같은 절대 토픽을 상대 이름으로 바꿔 네임스페이스 안을 보게 함
  - enable_stamped_cmd_vel 을 로봇에 맞춤 (TurtleBot3 true, Pinky false)
  - keepout: 전역·지역 costmap 에 KeepoutFilter 추가 (토픽은 /<ns>/… 절대 이름)
"""
import os
import tempfile

import yaml

FRAMES = {'odom', 'base_link', 'base_footprint', 'base_scan', 'rplidar_link'}
KEEPOUT_INFO = 'keepout_costmap_filter_info'
KEEPOUT_MASK = 'keepout_filter_mask'


def keepout_topics(ns):
    """costmap 노드는 /<ns>/global_costmap 안에 있어 상대 이름이 그 아래로 풀리므로 절대 이름을 씀"""
    base = f'/{ns}' if ns else ''
    return f'{base}/{KEEPOUT_INFO}', f'{base}/{KEEPOUT_MASK}'


def rewrite_params(src, prefix, stamped, keepout=False, ns='', map_frame='map'):
    """Return (path of a rewritten copy of the Nav2 params file, list of changes)."""
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
