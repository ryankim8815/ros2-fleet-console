#!/usr/bin/env python3
"""여러 로봇의 SLAM 지도를 하나로 합치는 노드 (시작 위치를 알 때).

로봇마다 slam_toolbox 가 /<ns>/map (프레임 '<ns>/map') 을 만들고, 이 노드가 각 지도를 그 로봇의
시작 자세(공통 지도 'map' 기준 x, y, yaw)만큼 돌리고 옮겨 하나의 격자로 합칩니다.
  - 합칠 때: 한 칸이라도 '점유'면 점유, 아니면 '비어 있음'이 하나라도 있으면 비어 있음, 나머지 미탐색
  - 결과: /map_merged (nav_msgs/OccupancyGrid, 프레임 'map', transient local), 1초마다
  - 진행 상황: /map_merged/stats (std_msgs/String JSON: 로봇별 기여 면적, 전체 탐색 면적)

m-explore-ros2 의 map_merge 는 이 PC 의 OpenCV 에서 빌드되지 않아(cv::AKAZE 없음) 직접 만들었습니다.
시작 위치를 모르는 경우(특징 매칭으로 맞추기)는 지원하지 않습니다.

파라미터:
  robots (string, JSON): [{"ns": "/tb3_01", "x": 0.0, "y": 0.0, "yaw": 0.0}, ...]
  rate (double, Hz) = 1.0
"""
import json
import math

import numpy as np
import rclpy
from nav_msgs.msg import OccupancyGrid
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String


def yaw_of(q):
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


class MapMerge(Node):
    def __init__(self):
        super().__init__('map_merge')
        self.declare_parameter('robots', '[]')
        self.declare_parameter('rate', 1.0)
        self.robots = json.loads(self.get_parameter('robots').value)
        self.maps = {}
        qos_in = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL, reliability=ReliabilityPolicy.RELIABLE)
        for r in self.robots:
            ns = r['ns'].rstrip('/')
            self.create_subscription(OccupancyGrid, f'{ns}/map', lambda m, ns=ns: self.maps.__setitem__(ns, m), qos_in)
        self.pub = self.create_publisher(OccupancyGrid, '/map_merged', QoSProfile(
            depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL, reliability=ReliabilityPolicy.RELIABLE))
        self.stats = self.create_publisher(String, '/map_merged/stats', 10)
        self.create_timer(1.0 / self.get_parameter('rate').value, self.merge)
        self.get_logger().info(f'합칠 로봇: {[r["ns"] for r in self.robots]}')

    def merge(self):
        layers = []
        for r in self.robots:
            m = self.maps.get(r['ns'].rstrip('/'))
            if m is None or m.info.width == 0:
                continue
            g = np.asarray(m.data, dtype=np.int16).reshape(m.info.height, m.info.width)
            # 로봇 지도 프레임 → 공통 'map': T = 시작 자세 ∘ (지도 원점)
            o = m.info.origin
            oyaw = yaw_of(o.orientation)
            c, s = math.cos(r['yaw']), math.sin(r['yaw'])
            ox = r['x'] + c * o.position.x - s * o.position.y
            oy = r['y'] + s * o.position.x + c * o.position.y
            layers.append({'ns': r['ns'], 'grid': g, 'res': m.info.resolution, 'x': ox, 'y': oy, 'yaw': r['yaw'] + oyaw})
        if not layers:
            return
        res = min(l['res'] for l in layers)
        # 합친 지도의 범위: 각 지도 네 모서리를 공통 프레임으로
        xs, ys = [], []
        for l in layers:
            h, w = l['grid'].shape
            c, s = math.cos(l['yaw']), math.sin(l['yaw'])
            for u, v in ((0, 0), (w * l['res'], 0), (0, h * l['res']), (w * l['res'], h * l['res'])):
                xs.append(l['x'] + c * u - s * v)
                ys.append(l['y'] + s * u + c * v)
        x0, y0 = min(xs), min(ys)
        W, H = int(math.ceil((max(xs) - x0) / res)) + 1, int(math.ceil((max(ys) - y0) / res)) + 1
        occ = np.zeros((H, W), dtype=bool)
        free = np.zeros((H, W), dtype=bool)
        contrib = {}
        for l in layers:
            # 합친 격자의 각 칸 중심 → 로봇 지도 칸 (역변환, 최근접)
            h, w = l['grid'].shape
            c, s = math.cos(l['yaw']), math.sin(l['yaw'])
            jj, ii = np.mgrid[0:H, 0:W]
            px = x0 + (ii + 0.5) * res - l['x']
            py = y0 + (jj + 0.5) * res - l['y']
            u = ((c * px + s * py) / l['res']).astype(np.int64)
            v = ((-s * px + c * py) / l['res']).astype(np.int64)
            inside = (u >= 0) & (u < w) & (v >= 0) & (v < h)
            vals = np.full((H, W), -1, dtype=np.int16)
            vals[inside] = l['grid'][v[inside], u[inside]]
            o_ = vals >= 65
            f_ = (vals >= 0) & (vals < 25)
            occ |= o_
            free |= f_
            known = np.count_nonzero(l['grid'] >= 0)
            contrib[l['ns']] = round(known * l['res'] * l['res'], 2)
        out = np.full((H, W), -1, dtype=np.int8)
        out[free] = 0
        out[occ] = 100
        msg = OccupancyGrid()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = 'map'
        msg.info.resolution = res
        msg.info.width, msg.info.height = W, H
        msg.info.origin.position.x, msg.info.origin.position.y = x0, y0
        msg.info.origin.orientation.w = 1.0
        msg.data = out.ravel().tolist()
        self.pub.publish(msg)
        known_total = np.count_nonzero(out >= 0) * res * res
        self.stats.publish(String(data=json.dumps({'known_m2': round(known_total, 2), 'robots': contrib,
                                                   'width': W, 'height': H, 'merged': len(layers)})))


def main():
    rclpy.init()
    rclpy.spin(MapMerge())


if __name__ == '__main__':
    main()
