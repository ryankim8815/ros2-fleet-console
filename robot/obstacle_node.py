#!/usr/bin/env python3
"""장애물 처리 노드 (PC 에서, serve.py 가 '장애물 처리' 모드로 바꿀 때 실행). 흐름과 값은 obstacle.py 참고.

  0. 재기  : 참여 로봇들의 LiDAR 로 장애물을 다시 잼 (원 맞춤). 경보 상자는 보이는 면이라 중심이 로봇 쪽으로 치우침
  1. 모이기: 워커·경비 로봇마다 장애물 뒤(출구 반대쪽) 자리를 정해 Nav2 NavigateToPose 로 보낸 뒤 cmd_vel 로 정확히 맞춤.
            자리에서 가까이 다시 재어 자리를 고침
  2. 소리  : 모두 자리에 서면 함께 소리, 첫 소리부터 5초 (1초마다 TurtleBot3 부저 / Pinky LED·표정)
  3. 밀기  : 한 번에 SEGMENT(1 m)씩. 앞줄이 장애물에 닿을 때까지 곧게 다가간 뒤, 장애물이 갈 길을 옆으로 평행 이동한 길을
            같은 속도로 함께 감 (가장 늦은 앞줄 로봇에 맞춰 속도를 줄임) → 물러나서(STANDOFF) LiDAR 로 다시 잼
            → 출구에 닿았으면 끝, 아니면 잰 위치에서 길·자리를 다시 계획해 반복
            (밀다가 장애물이 옆으로 미끄러져도 다음 구간에서 바로잡음. LDS-02 는 0.16 m 안을 못 보므로 닿은 채로는 못 잼)
  4. 소리  : 출구에 있는 것을 잰 뒤 5초
진행은 data/fleet.db 의 obstacle_runs 에, 단계·구간마다 잰 위치는 obstacle_run_events 에 씀 (웹이 읽음).
값(모일 간격, 미는 속도, 구간 길이, 도착 허용 …)은 DB obstacle_settings, 로봇 반지름은 robot_types, 구역은 zones 에서 읽음.
끝나면 종료 → 서버가 역할을 다시 시작.

멈춤: SIGINT/SIGTERM(서버의 중단·비상정지) 또는 /fleet/obstacle_cmd {"cmd": "abort"} → 모든 로봇 정지, 'aborted'.
실패: 자리에 못 감, 위치를 잃음, 길에서 0.35 m 넘게 벗어남, 장애물을 다시 못 찾음, 두 구간 밀어도 장애물이 안 움직임,
      시간 초과 → 정지, 'failed'. 출구에 있는 것을 재어 확인하기 전에는 '끝'으로 하지 않음.

주의: 밀기는 Nav2 를 쓰지 않고 cmd_vel 을 직접 보내므로 collision_monitor(LiDAR 안전 구역)와 costmap 을 거치지 않습니다.
      닿아야 밀 수 있기 때문입니다. 대신 속도를 0.05 m/s 로 낮추고 위의 조건에서 바로 멈춥니다.
"""
import json
import math
import os
import signal
import sys
import threading
import time

import numpy as np
import rclpy
from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped, Twist, TwistStamped
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.signals import SignalHandlerOptions
from rclpy.time import Time
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String
from tf2_ros import Buffer, TransformListener
from turtlebot3_msgs.srv import Sound

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..')))
import coverage  # noqa: E402
import fleet_store  # noqa: E402
import maps  # noqa: E402
import obstacle as O  # noqa: E402
import patrol_store as store  # noqa: E402

try:   # Pinky LED·표정 (ros2_ws 에 pinky_interfaces 를 빌드했을 때만)
    from pinky_interfaces.srv import Emotion, SetLed
except ImportError:
    Emotion = SetLed = None

# 현장에서 맞추는 값은 DB obstacle_settings (self.S): standoff, robot_gap, push_speed, approach_speed, segment, exit_tol,
# exit_radius, max_segments, gather_timeout, max_deviation. 아래는 맞출 일이 거의 없는 제어 값
ALIGN_TIMEOUT = 30.0       # 자리 정밀 맞춤 한도 (초)
POSE_LOST = 1.5            # 위치를 이만큼 못 받으면 멈춤 (초)
LOOKAHEAD = 0.25           # 길 따라가기 앞보기 거리 (m)
EXTEND = 1.5               # 장애물 뒤로 늘인 길 (로봇이 장애물 뒤에서 시작하므로)
RENAV = 0.35               # 다시 잰 뒤 새 자리까지 이보다 멀면 Nav2 로 (가까우면 직접 맞춤)


class Stop(Exception):
    """사용자 중단"""


class Fail(Exception):
    """처리 실패 (사유)"""


def yaw_of(q):
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


def wrap(a):
    return math.atan2(math.sin(a), math.cos(a))


class Bot:
    """로봇 한 대: 위치(TF), LiDAR, Nav2 goal, cmd_vel, 소리·LED"""

    def __init__(self, cfg, radius):
        self.cfg = cfg
        self.id, self.name, self.ns = cfg['id'], cfg['name'], cfg.get('ns', '')
        self.prefix = cfg.get('tfPrefix', '')
        tf = cfg.get('tfTopic', '/tf')
        self.node = Node(f'obstacle_{self.id}', cli_args=['--ros-args', '-r', f'/tf:={tf}', '-r', f'/tf_static:={tf}_static'])
        self.buf = Buffer()
        self.tfl = TransformListener(self.buf, self.node)
        self.nav = ActionClient(self.node, NavigateToPose, f'{self.ns}/navigate_to_pose')
        self.stamped = cfg.get('type') != 'pinky'          # TurtleBot3 Jazzy: TwistStamped, Pinky: Twist
        self.cmd = self.node.create_publisher(TwistStamped if self.stamped else Twist, f'{self.ns}/cmd_vel', 10)
        self.scan = None
        self.node.create_subscription(LaserScan, f'{self.ns}/scan', lambda m: setattr(self, 'scan', m), qos_profile_sensor_data)
        self.sound = self.node.create_client(Sound, f'{self.ns}/sound') if cfg.get('type') != 'pinky' else None
        pinky = cfg.get('type') == 'pinky'
        self.led = self.node.create_client(SetLed, f'{self.ns}/set_led') if pinky and SetLed else None
        self.emotion = self.node.create_client(Emotion, f'{self.ns}/set_emotion') if pinky and Emotion else None
        self.radius = radius                     # 몸체 반지름 (DB robot_types)
        self.goal = None
        self.slot = None
        self.state = 'waiting'
        self.blink = False

    def pose(self):
        """(x, y, th) 지도 위 자세, 최근 TF 가 없으면 None"""
        try:
            t = self.buf.lookup_transform('map', self.prefix + 'base_footprint', Time())
        except Exception:
            return None
        st = t.header.stamp
        age = self.node.get_clock().now().nanoseconds * 1e-9 - (st.sec + st.nanosec * 1e-9)
        if st.sec and age > POSE_LOST + 1.0:
            return None
        tr = t.transform.translation
        return tr.x, tr.y, yaw_of(t.transform.rotation)

    def scan_points(self):
        """최근 스캔의 끝점들을 지도 좌표로 [[x, y], ...]"""
        m = self.scan
        if m is None:
            return []
        try:
            t = self.buf.lookup_transform('map', m.header.frame_id, Time())
        except Exception:
            return []
        tr = t.transform.translation
        th = yaw_of(t.transform.rotation)
        r = np.asarray(m.ranges, dtype=float)
        a = m.angle_min + np.arange(len(r)) * m.angle_increment + th
        ok = np.isfinite(r) & (r >= m.range_min) & (r <= min(m.range_max, 3.0))
        return np.column_stack([tr.x + r[ok] * np.cos(a[ok]), tr.y + r[ok] * np.sin(a[ok])]).tolist()

    def drive(self, v, w):
        tw = Twist()
        tw.linear.x, tw.angular.z = float(v), float(w)
        if self.stamped:
            m = TwistStamped()
            m.header.stamp = self.node.get_clock().now().to_msg()
            m.header.frame_id = self.prefix + 'base_link'
            m.twist = tw
            self.cmd.publish(m)
        else:
            self.cmd.publish(tw)

    def halt(self):
        for _ in range(3):
            self.drive(0.0, 0.0)
            time.sleep(0.03)

    def send_goal(self, x, y, th):
        ps = PoseStamped()
        ps.header.frame_id = 'map'
        ps.header.stamp = self.node.get_clock().now().to_msg()
        ps.pose.position.x, ps.pose.position.y = float(x), float(y)
        ps.pose.orientation.z, ps.pose.orientation.w = math.sin(th / 2), math.cos(th / 2)
        self.state = 'going'
        self.goal = {'handle': None, 'done': None}
        g = self.goal

        def accepted(f):
            h = f.result()
            if not h.accepted:
                g['done'] = False
                return
            g['handle'] = h
            h.get_result_async().add_done_callback(lambda rf: g.__setitem__('done', rf.result().status == GoalStatus.STATUS_SUCCEEDED))
        self.nav.send_goal_async(NavigateToPose.Goal(pose=ps)).add_done_callback(accepted)

    def cancel(self):
        g, self.goal = self.goal, None
        if g and g.get('handle') is not None and g.get('done') is None:
            g['handle'].cancel_goal_async()

    def signal(self, kind):
        """소리·빛 한 번. kind: 'gather'(모여서) | 'arrive'(도착해서)"""
        if self.sound is not None and self.sound.service_is_ready():
            self.sound.call_async(Sound.Request(value=3 if kind == 'gather' else 1))   # 3 = ERROR, 1 = ON
        if self.led is not None and self.led.service_is_ready():
            self.blink = not self.blink
            r, g = (255, 0) if kind == 'gather' else (0, 255)
            self.led.call_async(SetLed.Request(command='fill' if self.blink else 'clear', pixels=[], r=r, g=g, b=0))
        if self.emotion is not None and self.emotion.service_is_ready() and not self.blink:
            self.emotion.call_async(Emotion.Request(emotion='angry' if kind == 'gather' else 'happy'))

    def signal_end(self):
        if self.led is not None and self.led.service_is_ready():
            self.led.call_async(SetLed.Request(command='clear', pixels=[], r=0, g=0, b=0))
        if self.emotion is not None and self.emotion.service_is_ready():
            self.emotion.call_async(Emotion.Request(emotion='basic'))


class Handler(Node):
    def __init__(self):
        super().__init__('fleet_obstacle')
        self.declare_parameter('run_id', 0)
        self.declare_parameter('robots_file', '')   # 참여 로봇 설정 JSON 파일 (serve.py 가 만듦)
        self.declare_parameter('db', store.DB_PATH)
        self.run_id = int(self.get_parameter('run_id').value)
        self.con = store.db(self.get_parameter('db').value)
        self.db_lock = threading.Lock()
        self.run = O.get_run(self.con, self.run_id)
        if self.run is None:
            raise RuntimeError(f'장애물 처리 기록 #{self.run_id} 가 없습니다')
        self.S = O.get_settings(self.con)            # 장애물 처리 값 (DB obstacle_settings)
        types = fleet_store.robot_types(self.con)
        rad = lambda t: (types.get(t) or types.get('turtlebot3') or {'radius': 0.105})['radius']   # noqa: E731
        with open(self.get_parameter('robots_file').value, encoding='utf-8') as f:
            self.bots = [Bot(c, rad(c.get('type'))) for c in json.load(f)]
        self.stop_evt = threading.Event()
        self.create_subscription(String, '/fleet/obstacle_cmd', self.on_cmd, 10)
        self.ob = dict(self.run['obstacle'])            # 지금 알고 있는 장애물 {x, y, r}
        self.exit = self.run['exit']
        self.plan = {'path': [], 'robots': {}, 'est': {'x': self.ob['x'], 'y': self.ob['y']}, 'segment': 0}
        self.last_write = 0.0
        self.L0 = None                                   # 처음 잰 위치에서 출구까지 길이 (진행률 기준)
        # 기준 지도의 벽 근처 (장애물을 잴 때 벽 점을 빼려고)
        self.info, occ, _ = coverage.load_grid(self.run['map'])
        self.near_wall = coverage.disk_dilate(occ, 0.10 / self.info['resolution'])
        zones = maps.read_zones(self.con, self.run['map'])['zones']
        self.forbidden = coverage.forbidden_grid(self.info, zones, '*')

    def on_cmd(self, msg):
        try:
            c = json.loads(msg.data)
        except ValueError:
            return
        if c.get('cmd') == 'abort':
            self.get_logger().warn('중단 명령')
            self.stop_evt.set()

    # ---------------- 공용 ----------------
    def log(self, msg):
        self.get_logger().info(msg)

    def write(self, state=None, message=None, progress=None, force=False):
        """진행 기록 (0.5초에 한 번, 단계가 바뀌면 바로)"""
        now = time.time()
        if not force and state is None and message is None and now - self.last_write < 0.5:
            return
        self.last_write = now
        for b in self.bots:
            p = b.pose()
            if b.id in self.plan['robots']:
                self.plan['robots'][b.id].update({'state': b.state, 'pose': [round(p[0], 3), round(p[1], 3), round(p[2], 3)] if p else None})
        with self.db_lock:
            O.update_run(self.con, self.run_id, state=state, message=message, progress=progress, plan=self.plan)
            if message:   # 이력 (obstacle_run_events)
                O.add_event(self.con, self.run_id, 'state' if state else 'progress', message,
                            {'state': state, 'segment': self.plan.get('segment'), 'robots': [b.id for b in self.bots]})
        if message:
            self.log(message)

    def sleep(self, sec):
        if self.stop_evt.wait(sec):
            raise Stop()

    def halt_all(self):
        for b in self.bots:
            b.cancel()
            b.halt()

    def progress(self, remaining):
        return 0.0 if not self.L0 else max(0.0, min(1.0, 1.0 - remaining / self.L0))

    # ---------------- 0. 장애물 재기 ----------------
    def measure(self, guess, seconds=1.0):
        """로봇들의 LiDAR 로 장애물 중심·반지름. 추정 중심에서 MEASURE_R 안의 점 중 로봇 몸체·기준 지도 벽 근처를 뺀 것으로
        원 맞춤. 못 재면 None"""
        res, ox, oy = self.info['resolution'], self.info['origin'][0], self.info['origin'][1]
        h, w = self.near_wall.shape
        pts, viewers = [], []
        t0 = time.time()
        while time.time() - t0 < seconds:
            poses = [b.pose() for b in self.bots]
            for b, p in zip(self.bots, poses):
                got = 0
                for x, y in b.scan_points():
                    if (x - guess[0]) ** 2 + (y - guess[1]) ** 2 > O.MEASURE_R ** 2:
                        continue
                    if any(q and (x - q[0]) ** 2 + (y - q[1]) ** 2 < (bb.radius + 0.05) ** 2 for bb, q in zip(self.bots, poses)):
                        continue   # 다른(또는 자기) 로봇 몸체
                    i, j = int((x - ox) / res), int((y - oy) / res)
                    if 0 <= i < w and 0 <= j < h and self.near_wall[j, i]:
                        continue   # 원래 있던 벽
                    pts.append([x, y])
                    got += 1
                if got and p:
                    viewers.append(p[:2])
            self.sleep(0.2)
        return O.fit_circle(pts, guess, self.ob['r'], viewers)

    # ---------------- 1. 자리 계획 · 모이기 ----------------
    def plan_slots(self):
        """지금 장애물 위치에서 출구까지 밀 길과 로봇 자리"""
        ob, ex = self.ob, self.exit
        team = [{'id': b.id, 'type': b.cfg.get('type'), 'radius': b.radius, 'pose': b.pose()[:2] if b.pose() else None}
                for b in self.bots]
        guess = O.formation(ob, [[ob['x'], ob['y']], [ex['x'], ex['y']]], team, self.S)
        front = [s for s in guess.values() if s['row'] == 'front']
        half_w = max(abs(s['lat']) for s in front) + max(s['radius'] for s in front)
        try:
            path = O.plan_push_path(self.run['map'], (ob['x'], ob['y']), (ex['x'], ex['y']), max(ob['r'], half_w) + 0.05, self.forbidden)
        except ValueError as e:
            raise Fail(str(e)) from e
        slots = O.formation(ob, path, team, self.S)
        robots = {k: {**dict(v), 'state': self.plan['robots'].get(k, {}).get('state', 'waiting')} for k, v in slots.items()}
        self.plan.update({'path': path, 'length': round(O.path_length(path), 3), 'robots': robots,
                          'est': {'x': ob['x'], 'y': ob['y']}, 'obstacle': dict(ob)})
        for b in self.bots:
            b.slot = slots[b.id]
        if self.L0 is None:
            self.L0 = O.path_length(path)
        return path

    def go_to_slots(self, nav_all):
        """자리로: nav_all 이면 모두 Nav2, 아니면 멀리(RENAV) 떨어진 로봇만 Nav2. 그다음 모두 직접 정밀 맞춤"""
        sent = []
        for b in self.bots:
            p = b.pose()
            if nav_all or p is None or math.hypot(p[0] - b.slot['x'], p[1] - b.slot['y']) > RENAV:
                b.send_goal(b.slot['x'], b.slot['y'], b.slot['th'])
                sent.append(b)
            else:
                b.state = 'aligning'
        t0 = time.time()
        while any(b.state == 'going' for b in sent):
            self.sleep(0.5)
            for b in sent:
                if b.state == 'going' and b.goal and b.goal['done'] is not None:
                    b.state = 'aligning' if b.goal['done'] else 'failed'
                    if not b.goal['done']:
                        self.log(f'{b.name}: 자리에 가지 못함 (Nav2 실패·거부)')
            if time.time() - t0 > self.S['gather_timeout']:
                for b in sent:
                    if b.state == 'going':
                        b.cancel()
                        b.state = 'failed'
                        self.log(f'{b.name}: {self.S["gather_timeout"]:.0f}초 안에 자리에 못 감')
            self.write()
        self.align([b for b in self.bots if b.state == 'aligning'])
        lost = [b for b in self.bots if b.state != 'ready']
        self.bots = [b for b in self.bots if b.state == 'ready']
        for b in lost:
            self.plan['robots'].pop(b.id, None)
        if not self.bots:
            raise Fail('자리에 선 로봇이 없습니다')
        if not any(b.slot['row'] == 'front' for b in self.bots):
            # 앞줄이 하나도 못 왔으면 뒷줄 중 가운데 로봇을 앞줄로 올리고 그 자리로 다시 맞춤
            b = min(self.bots, key=lambda q: abs(q.slot['lat']))
            back = self.ob['r'] + b.radius + self.S['standoff']
            th = b.slot['th']
            b.slot.update({'row': 'front', 'lat': 0.0, 'x': self.ob['x'] - math.cos(th) * back, 'y': self.ob['y'] - math.sin(th) * back})
            self.plan['robots'][b.id].update(b.slot)
            self.log(f'{b.name}: 앞줄 로봇이 없어 앞줄로')
            b.state = 'aligning'
            self.align([b])
            if b.state != 'ready':
                raise Fail('미는 로봇이 자리에 서지 못했습니다')

    def align(self, bots):
        """Nav2 의 도착 허용 오차(0.25 m)가 커서, 자리·방향을 cmd_vel 로 정밀하게 맞춤 (5 cm, 3°)"""
        t0 = time.time()
        pending = list(bots)
        while pending and time.time() - t0 < ALIGN_TIMEOUT:
            for b in list(pending):
                p = b.pose()
                if p is None:
                    b.drive(0, 0)
                    continue
                s = b.slot
                dx, dy = s['x'] - p[0], s['y'] - p[1]
                d = math.hypot(dx, dy)
                if d > 0.05:
                    err = wrap(math.atan2(dy, dx) - p[2])
                    if abs(err) > math.pi / 2:      # 뒤쪽이면 후진으로 (돌아서 다가가면 장애물에 부딪힐 수 있음)
                        err = wrap(err + math.pi)
                        v = -min(0.06, d)
                    else:
                        v = min(0.06, d)
                    b.drive(v * max(0.0, math.cos(err)), max(-0.6, min(0.6, 1.5 * err)))
                    continue
                err = wrap(s['th'] - p[2])
                if abs(err) > math.radians(3):
                    b.drive(0, max(-0.5, min(0.5, 1.5 * err)) if abs(err) > 0.05 else (0.1 if err > 0 else -0.1))
                    continue
                b.drive(0, 0)
                b.state = 'ready'
                pending.remove(b)
            self.write()
            self.sleep(0.1)
        for b in pending:
            b.halt()
            b.state = 'failed'
            self.log(f'{b.name}: 자리를 정밀하게 맞추지 못함')

    # ---------------- 2·4. 소리 ----------------
    def signal(self, state, kind, message):
        """모두 함께 첫 소리 → SIGNAL_SECONDS 동안 1초마다"""
        t0 = time.time()
        self.write(state=state, message=message, force=True)
        while time.time() - t0 < O.SIGNAL_SECONDS:
            for b in self.bots:
                b.signal(kind)
            self.sleep(min(1.0, max(0.0, O.SIGNAL_SECONDS - (time.time() - t0))))
        for b in self.bots:
            b.signal_end()

    # ---------------- 3. 밀기 (한 구간) ----------------
    def drive_straight(self, dist, speed, label):
        """모두 같은 거리를 곧게 (다가가기 +, 물러나기 -)"""
        starts = {b.id: b.pose() for b in self.bots}
        for b in self.bots:
            b.state = label
        t0 = time.time()
        while True:
            moving = 0
            for b in self.bots:
                p = b.pose()
                if p is None:
                    raise Fail(f'{b.name}: 위치를 잃음')
                s = starts[b.id]
                if math.hypot(p[0] - s[0], p[1] - s[1]) < abs(dist):
                    b.drive(math.copysign(speed, dist), 0)
                    moving += 1
                else:
                    b.drive(0, 0)
            if not moving:
                break
            if time.time() - t0 > abs(dist) / speed * 2 + 10:
                raise Fail(f'{label} 시간 초과 (막힘?)')
            self.write()
            self.sleep(0.1)
        for b in self.bots:
            b.halt()

    def push_segment(self, path):
        """앞줄이 닿을 때까지 다가간 뒤 길을 따라 SEGMENT 만큼 (또는 출구까지) 함께 밂. 반환: 장애물 추정 위치·남은 길"""
        ob = self.ob
        th0 = O.heading_at_start(path)
        ext = [[path[0][0] - math.cos(th0) * EXTEND, path[0][1] - math.sin(th0) * EXTEND]] + path
        L = O.path_length(path)
        goal = min(L - self.S['exit_tol'] * 0.5, self.S['segment'])        # 이번에 밀 거리 (마지막 구간은 출구 바로 앞까지)
        ds = 0.05
        dense = O.resample(path, ds)
        self.drive_straight(self.S['standoff'] + 0.02, self.S['approach_speed'], 'approaching')
        for b in self.bots:
            b.ref = O.offset_path(ext, b.slot['lat'])
            # 닿았을 때 장애물 중심과 로봇 중심 사이 (앞줄), 뒷줄은 앞줄 뒤 간격 유지
            b.gap = ob['r'] + b.radius + (0 if b.slot['row'] == 'front' else 2 * b.radius + self.S['robot_gap'] + 0.10)
            b.idx = O.project(b.ref, *b.pose()[:2])
            b.state = 'pushing'
        front = [b for b in self.bots if b.slot['row'] == 'front']
        t0 = time.time()
        limit = goal / self.S['push_speed'] * 2 + 30
        last_pose = {b.id: time.time() for b in self.bots}
        S = 0.0
        while True:
            now = time.time()
            prog, poses = {}, {}
            for b in self.bots:
                p = b.pose()
                if p is None:
                    if now - last_pose[b.id] > POSE_LOST:
                        raise Fail(f'{b.name}: 위치를 잃음 (AMCL·TF)')
                    continue
                last_pose[b.id] = now
                poses[b.id] = p
                b.idx = O.project(b.ref, p[0], p[1], hint=b.idx, window=40)
                rx, ry = b.ref[b.idx]
                if math.hypot(p[0] - rx, p[1] - ry) > self.S['max_deviation']:
                    raise Fail(f'{b.name}: 밀 길에서 {self.S["max_deviation"]} m 넘게 벗어남')
                prog[b.id] = b.idx * ds - EXTEND + b.gap     # 이 로봇 기준 장애물 중심이 길에서 간 거리
            if not poses:
                self.sleep(0.1)
                continue
            fp = [prog[b.id] for b in front if b.id in prog]
            S = min(fp) if fp else min(prog.values())
            est = dense[max(0, min(len(dense) - 1, int(S / ds)))]
            self.plan['est'] = {'x': round(est[0], 3), 'y': round(est[1], 3)}
            if S >= goal:
                break
            if now - t0 > limit:
                raise Fail(f'밀기 시간 초과 ({limit:.0f}초)')
            for b in self.bots:
                if b.id not in poses:
                    b.drive(0, 0)
                    continue
                p = poses[b.id]
                ahead = prog[b.id] - S                     # 앞서 간 만큼 늦춤 (대형 유지)
                v = self.S['push_speed'] * max(0.2, min(1.0, 1.0 - 5.0 * ahead))
                tx, ty = b.ref[min(len(b.ref) - 1, b.idx + int(LOOKAHEAD / ds))]
                alpha = wrap(math.atan2(ty - p[1], tx - p[0]) - p[2])
                if abs(alpha) > 1.0:
                    raise Fail(f'{b.name}: 밀 방향에서 너무 틀어짐')
                w = max(-0.5, min(0.5, 2 * v * math.sin(alpha) / LOOKAHEAD))
                b.drive(v * math.cos(alpha), w)
            self.write(progress=self.progress(L - S))
            self.sleep(0.1)
        self.halt_all()
        # 물러나서 LiDAR 로 볼 수 있게 (LDS-02 는 0.16 m 안을 못 봄)
        self.drive_straight(-self.S['standoff'], 0.05, 'backing')
        return (est[0], est[1]), L - S

    # ---------------- 전체 ----------------
    def run_all(self):
        ex = self.exit
        # 0. 위치를 아는 로봇만
        self.write(message='로봇 위치 확인 중')
        t0 = time.time()
        while time.time() - t0 < 10 and any(b.pose() is None for b in self.bots):
            self.sleep(0.5)
        for b in [b for b in self.bots if b.pose() is None]:
            self.log(f'{b.name}: 지도 위 위치를 몰라 빠짐')
        self.bots = [b for b in self.bots if b.pose() is not None]
        if not self.bots:
            raise Fail('지도 위 위치를 아는 워커·경비 로봇이 없습니다')
        m = self.measure((self.ob['x'], self.ob['y']))
        if m:
            self.ob = {'x': m['x'], 'y': m['y'], 'r': m['r']}
            self.log(f'장애물을 다시 잼: ({m["x"]:.2f}, {m["y"]:.2f}) r {m["r"]:.2f} ({m["fit"]}, 점 {m["n"]}개)')
            with self.db_lock:
                O.add_event(self.con, self.run_id, 'measure', f'처음 잰 장애물 ({m["x"]:.2f}, {m["y"]:.2f}) r {m["r"]:.2f}',
                            {'segment': 0, **{k: m[k] for k in ('x', 'y', 'r', 'n', 'fit')}})
        # 1. 모이기
        self.plan_slots()
        self.write(message=f'로봇 {len(self.bots)}대가 장애물 뒤로 모이는 중 (밀 길 {self.plan["length"]:.1f} m)', force=True)
        self.go_to_slots(nav_all=True)
        m = self.measure((self.ob['x'], self.ob['y']))   # 가까이서 다시 재고 자리 고침
        if m:
            self.ob = {'x': m['x'], 'y': m['y'], 'r': m['r']}
            self.plan_slots()
            self.go_to_slots(nav_all=False)
        n = len(self.bots)
        # 2. 소리
        self.signal('signal1', 'gather', f'로봇 {n}대가 모두 모였습니다 · {O.SIGNAL_SECONDS:.0f}초 동안 소리')
        # 3. 구간마다 밀고 → 물러나 재고 → 다시 계획
        still = 0
        for seg in range(1, self.S['max_segments'] + 1):
            self.plan['segment'] = seg
            path = self.plan['path']
            self.write(state='pushing', message=f'{seg}번째 구간 미는 중 (남은 길 {self.plan["length"]:.1f} m)', force=True)
            est, _ = self.push_segment(path)
            m = self.measure(est)
            if not m:
                raise Fail('밀고 난 뒤 장애물을 찾지 못했습니다 (놓쳤을 수 있음)')
            moved = math.hypot(m['x'] - self.ob['x'], m['y'] - self.ob['y'])
            self.ob = {'x': m['x'], 'y': m['y'], 'r': m['r']}
            d_exit = math.hypot(m['x'] - ex['x'], m['y'] - ex['y'])
            self.log(f'{seg}번째 구간 뒤 장애물 ({m["x"]:.2f}, {m["y"]:.2f}) · {moved:.2f} m 움직임 · 출구까지 {d_exit:.2f} m')
            with self.db_lock:
                O.add_event(self.con, self.run_id, 'measure', f'{seg}번째 구간 뒤 장애물 ({m["x"]:.2f}, {m["y"]:.2f}) · '
                            f'{moved:.2f} m 움직임 · 출구까지 {d_exit:.2f} m',
                            {'segment': seg, 'x': m['x'], 'y': m['y'], 'r': m['r'], 'fit': m['fit'], 'moved': round(moved, 3),
                             'to_exit': round(d_exit, 3)})
            if d_exit <= self.S['exit_tol']:
                break
            still = still + 1 if moved < 0.08 else 0
            if still >= 2:
                raise Fail('두 구간을 밀어도 장애물이 움직이지 않습니다 (무겁거나 걸림)')
            self.plan_slots()
            self.write(progress=self.progress(self.plan['length']), message=f'자리 고치는 중 (장애물이 {moved:.2f} m 움직임)', force=True)
            self.go_to_slots(nav_all=False)
        else:
            raise Fail(f'{self.S["max_segments"]}구간을 밀어도 출구에 닿지 못했습니다')
        self.plan['est'] = {'x': self.ob['x'], 'y': self.ob['y']}
        for b in self.bots:
            b.state = 'done'
        # 4. 소리
        self.signal('signal2', 'arrive', f'장애물을 출구로 옮겼습니다 · {O.SIGNAL_SECONDS:.0f}초 동안 소리')
        self.write(state='done', message=f'장애물을 출구로 옮겼습니다 (로봇 {n}대, {self.plan["segment"]}구간, 출구에서 '
                                         f'{math.hypot(self.ob["x"] - ex["x"], self.ob["y"] - ex["y"]):.2f} m)', progress=1.0, force=True)

    def main(self):
        try:
            self.run_all()
        except Stop:
            self.halt_all()
            with self.db_lock:
                O.finish_if_active(self.con, self.run_id, 'aborted', '사용자가 중단 (로봇 정지)')
            self.log('중단: 모든 로봇 정지')
        except Fail as e:
            self.halt_all()
            self.write(state='failed', message=f'실패: {e} (로봇 정지, 역할은 멈춤 상태)', force=True)
        except Exception as e:  # noqa: BLE001
            self.halt_all()
            self.write(state='failed', message=f'오류: {e}', force=True)
            raise
        finally:
            for b in self.bots:
                b.signal_end()


def main():
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    h = Handler()
    signal.signal(signal.SIGINT, lambda *_: h.stop_evt.set())
    signal.signal(signal.SIGTERM, lambda *_: h.stop_evt.set())
    ex = MultiThreadedExecutor()
    ex.add_node(h)
    for b in h.bots:
        ex.add_node(b.node)
    threading.Thread(target=ex.spin, daemon=True).start()
    try:
        h.main()
    finally:
        time.sleep(0.3)        # 마지막 정지 명령이 나가게
        ex.shutdown()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
