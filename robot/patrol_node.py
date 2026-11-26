#!/usr/bin/env python3
"""경비 노드 (PC 에서, 운영 모드에서 경비 역할 로봇이 경비 중일 때 serve.py 가 실행·감시).

경비 역할(roles.py, DB robot_roles)인 로봇마다:
  1) 순회: 기준 지도에서 갈 수 있는 곳을 빠짐없이 도는 지점을 자동으로 만들어(coverage.py, 간격 settings.spacing)
     Nav2 FollowWaypoints(/<ns>/follow_waypoints)로 반복. 경비 로봇이 여럿이면 구역을 나눠 맡음
  2) 차이 감지: /<ns>/scan 을 지도 위치(map → 스캔 프레임 TF, 로봇의 TF 토픽)로 옮겨 기준 지도와 비교
       - 새로 생긴 것: 기준 지도에서 비어 있던 칸(벽에서 tol 이상 떨어진)에 스캔 끝점이 닿음
       - 사라진 것: 기준 지도의 벽 칸을 스캔 빛줄기가 뚫고 지나감
     같은 칸이 연속 persist 번 이상 보이면 '확정', 확정 칸을 이어 붙인 덩어리가 min_area 이상이면 경보
  3) 경보: data/fleet.db 의 alerts·alert_robots 에 기록하고 /fleet/alerts (std_msgs/String JSON) 로 알림
     가까운 곳의 열린 경보가 있으면 새로 만들지 않고 갱신 (다른 로봇이 다시 보면 등급 올림)
  4) 장애물 대기: 경비 중에 '새로 생긴 것' 경보를 새로 만들면 그 자리에 멈추고(goal 취소) 경비 상태를 alarm 으로.
     기다리는 동안 alarm_interval 초마다 알림: TurtleBot3 부저(<ns>/sound, ERROR), Pinky LED 빨강 깜빡임·표정 angry
     (pinky_interfaces 가 있을 때). 관제 웹은 alarm 상태를 보고 경고음을 냄.
     관제실이 그 경보를 처리(이상 없음·오탐·기준 지도 반영)하면 스스로 다시 경비, 또는 웹에서 '재개'.

경비 상태(running / paused / stopped / alarm)는 DB 에서 1초마다 읽음 (웹의 시작·멈춤·재개·정지는 서버가 DB 에 씀).
명령: /fleet/patrol_cmd (String JSON) — 비상정지 때 바로 멈추게
  {"cmd": "pause", "robot": id | "*"}                   goal 바로 취소 (DB 는 서버가 paused 로)
  {"cmd": "inspect", "robot": id, "x": .., "y": ..}   그 경비 로봇을 경보 위치 근처로 보낸 뒤 순회 재개
상태: /fleet/patrol_status (String JSON, 1초마다) — 로봇마다 상태·지점·바퀴·순회 지점 목록(plan)

카메라 없이 LiDAR 2D 스캔만 비교하므로 '무엇'인지는 모르고, 스캔 높이에서의 있음/없음만 압니다.
"""
import json
import math
import os
import sys
import threading
import time

import numpy as np
import rclpy
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import FollowWaypoints, NavigateToPose
from turtlebot3_msgs.srv import Sound
from rclpy.action import ActionClient
from action_msgs.msg import GoalStatus
from rclpy.action.client import ClientGoalHandle
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String
from tf2_ros import Buffer, TransformListener

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..')))
import coverage  # noqa: E402
import dbutil  # noqa: E402
import maps  # noqa: E402
import patrol_store as store  # noqa: E402
import roles  # noqa: E402

try:   # Pinky LED·표정 (ros2_ws 에 pinky_interfaces 를 빌드했을 때만)
    from pinky_interfaces.srv import Emotion, SetLed
except ImportError:
    Emotion = SetLed = None


def yaw_of(q):
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


class Baseline:
    """기준 지도 격자: occ(벽), free(비어 있음), near(벽에서 tol 이내)"""

    def __init__(self, name, tol):
        self.name = name
        self.load(tol)

    def load(self, tol):
        info = maps.map_info(self.name)
        w, h, maxval, px = maps._read_pgm(info['image'])
        g = np.frombuffer(px, dtype=np.uint8).reshape(h, w)[::-1].astype(np.int16)   # 행 0 = 아래(최소 y)
        if info['negate']:
            g = maxval - g
        self.occ = g <= 0.35 * maxval
        self.free = g >= 0.75 * maxval
        self.res, self.ox, self.oy = info['resolution'], info['origin'][0], info['origin'][1]
        self.w, self.h = w, h
        n = max(1, int(math.ceil(tol / self.res)))
        near = self.occ.copy()
        for dy in range(-n, n + 1):
            for dx in range(-n, n + 1):
                if dx * dx + dy * dy > n * n:
                    continue
                near[max(0, dy):h - max(0, -dy), max(0, dx):w - max(0, -dx)] |= \
                    self.occ[max(0, -dy):h - max(0, dy), max(0, -dx):w - max(0, dx)]
        self.near = near
        self.mtime = max(os.path.getmtime(info['yaml']), os.path.getmtime(info['image']))

    def cell(self, x, y):
        return int((x - self.ox) / self.res), int((y - self.oy) / self.res)

    def center(self, i, j):
        return self.ox + (i + 0.5) * self.res, self.oy + (j + 0.5) * self.res


class RobotWatch:
    """로봇 한 대: TF(로봇의 TF 토픽), 스캔 비교, 순회 실행, 장애물 대기 알림 (경비 역할일 때만 움직임)"""

    def __init__(self, parent, cfg):
        self.p = parent
        self.cfg = cfg
        self.id, self.name, self.ns = cfg['id'], cfg['name'], cfg['ns']
        tf = cfg.get('tfTopic', '/tf')
        # TF 토픽이 로봇마다 다를 수 있어(/pinky_01/tf) 로봇마다 노드를 따로 두고 remap
        self.node = Node(f'patrol_{self.id}', cli_args=['--ros-args', '-r', f'/tf:={tf}', '-r', f'/tf_static:={tf}_static'])
        self.buf = Buffer()
        self.tfl = TransformListener(self.buf, self.node)
        self.node.create_subscription(LaserScan, f'{self.ns}/scan', self.on_scan, qos_profile_sensor_data)
        self.follow = ActionClient(self.node, FollowWaypoints, f'{self.ns}/follow_waypoints')
        self.nav = ActionClient(self.node, NavigateToPose, f'{self.ns}/navigate_to_pose')
        self.sound = self.node.create_client(Sound, f'{self.ns}/sound') if cfg.get('type') != 'pinky' else None
        self.led = self.node.create_client(SetLed, f'{self.ns}/set_led') if cfg.get('type') == 'pinky' and SetLed else None
        self.emotion = self.node.create_client(Emotion, f'{self.ns}/set_emotion') if cfg.get('type') == 'pinky' and Emotion else None
        self.evidence = {'new': {}, 'missing': {}}   # (i,j) -> [count, last_time]
        self.last_proc = 0.0
        self.ctrl = 'stopped'      # DB 경비 상태: stopped | running | paused | alarm (역할이 경비가 아니면 stopped)
        self.state = 'idle'        # 세부 상태 (화면 표시용)
        self.points = []           # 순회 지점 [[x, y], ...] (coverage.plan)
        self.next_idx = 0          # 다음에 갈 지점 번호 (멈췄다 다시 갈 때 여기부터)
        self.sent_from = 0         # 지금 보낸 FollowWaypoints 의 첫 지점 번호
        self.waypoint = 0
        self.laps = 0
        self.goal = None
        self.inspect = None
        self.localized = False
        self.alarm_alert = None    # 대기 중인 경보 id
        self.alarm_last = 0.0      # 마지막으로 소리·LED 를 낸 시각
        self.alarm_phase = False   # Pinky LED 깜빡임

    @property
    def watching(self):
        """스캔을 비교할지: 경비 중이거나 장애물 앞에서 기다릴 때"""
        return self.ctrl in ('running', 'alarm')

    # ---------------- 스캔 비교 ----------------
    def on_scan(self, m):
        now = time.time()
        if not self.watching or now - self.last_proc < 0.4:
            return
        self.last_proc = now
        B, S = self.p.base, self.p.settings
        try:
            t = self.buf.lookup_transform('map', m.header.frame_id, Time())
        except Exception:
            self.localized = False
            return
        self.localized = True
        tr = t.transform.translation
        th = yaw_of(t.transform.rotation)
        sx, sy = tr.x, tr.y
        n = len(m.ranges)
        ang = m.angle_min + np.arange(n) * m.angle_increment + th
        r = np.asarray(m.ranges, dtype=np.float64)
        ok = np.isfinite(r) & (r > m.range_min) & (r < min(m.range_max, S['max_range']))
        ex, ey = sx + r * np.cos(ang), sy + r * np.sin(ang)
        hits_new, hits_missing = set(), set()
        res = B.res
        # 사라진 것: max_range 보다 먼 곳에서 돌아온 빛(유한값)도 max_range 까지는 비어 있다는 증거
        # (inf/NaN = 반사 없음은 유리·검은 물체일 수 있어 쓰지 않음)
        far = np.isfinite(r) & (r >= min(m.range_max, S['max_range'])) & (r <= m.range_max)
        # 벽을 비스듬히(25° 미만) 스치는 빛은 지도 벽 칸을 살짝 지나기 쉬워 '사라진 것' 오탐이 남 → 제외.
        # 입사각은 이웃 빛 두 개의 끝점을 이은 선(=표면)과 빛 방향 사이 각도로 추정
        rr = np.where(np.isfinite(r), r, np.nan)
        px, py = sx + rr * np.cos(ang), sy + rr * np.sin(ang)
        tx, ty = np.roll(px, -1) - np.roll(px, 1), np.roll(py, -1) - np.roll(py, 1)
        tl = np.hypot(tx, ty)
        with np.errstate(invalid='ignore', divide='ignore'):
            sin_inc = np.abs(np.cos(ang) * ty - np.sin(ang) * tx) / tl
        steep = np.nan_to_num(sin_inc, nan=0.0) > math.sin(math.radians(25))
        for k in np.nonzero(ok | far)[0]:
            if ok[k]:
                i, j = B.cell(ex[k], ey[k])
                if 0 <= i < B.w and 0 <= j < B.h and B.free[j, i] and not B.near[j, i]:
                    hits_new.add((i, j))
            # 빛줄기가 기준 지도의 벽을 뚫고 지나갔는지 (끝점 앞 tol 까지만)
            # 끝점 앞 여유는 표면에서 '수직으로' tol+res 만큼 → 빛줄기 따라서는 (tol+res)/sin(입사각)
            L = (r[k] - (S['tol'] + res) / max(sin_inc[k], 0.42)) if ok[k] else S['max_range']
            if L > 0.3 and steep[k]:
                c, s = math.cos(ang[k]), math.sin(ang[k])
                for d in np.arange(0.3, L, res):
                    i2, j2 = B.cell(sx + c * d, sy + s * d)
                    if not (0 <= i2 < B.w and 0 <= j2 < B.h):
                        break
                    if B.occ[j2, i2]:
                        hits_missing.add((i2, j2))
                        break
        pts = list(zip(ex[ok].round(3).tolist(), ey[ok].round(3).tolist()))
        for kind, hits in (('new', hits_new), ('missing', hits_missing)):
            ev = self.evidence[kind]
            for c in hits:
                e = ev.get(c)
                if e:
                    e[0] += 1
                    e[1] = now
                else:
                    ev[c] = [1, now]
            for c in [c for c, e in ev.items() if c not in hits and now - e[1] > S['decay']]:
                del ev[c]
            confirmed = {c for c, e in ev.items() if e[0] >= S['persist'] and now - e[1] < 2.0}
            for cl in self.clusters(confirmed):
                self.p.report(self, kind, cl, pts)

    @staticmethod
    def clusters(cells):
        cells = set(cells)
        out = []
        while cells:
            stack = [cells.pop()]
            comp = []
            while stack:
                c = stack.pop()
                comp.append(c)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nb = (c[0] + dx, c[1] + dy)
                        if nb in cells:
                            cells.remove(nb)
                            stack.append(nb)
            out.append(comp)
        return out

    # ---------------- 순회 ----------------
    def pose_xy(self):
        """지도 위 로봇 위치 (x, y) 또는 None"""
        try:
            t = self.buf.lookup_transform('map', self.cfg.get('tfPrefix', '') + 'base_footprint', Time())
            return t.transform.translation.x, t.transform.translation.y
        except Exception:
            return None

    def pose(self, x, y, yaw=0.0):
        ps = PoseStamped()
        ps.header.frame_id = 'map'
        ps.header.stamp = self.node.get_clock().now().to_msg()
        ps.pose.position.x, ps.pose.position.y = float(x), float(y)
        ps.pose.orientation.z, ps.pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
        return ps

    def set_points(self, pts):
        """순회 지점이 바뀜 (경비 로봇 수·지도·구역·간격 변경) → 지금 위치에서 가장 가까운 지점부터 다시"""
        if pts == self.points:
            return
        self.points = pts
        self.cancel()
        self.next_idx = self.nearest()

    def nearest(self):
        p = self.pose_xy()
        if not self.points or p is None:
            return 0
        return min(range(len(self.points)), key=lambda k: (self.points[k][0] - p[0]) ** 2 + (self.points[k][1] - p[1]) ** 2)

    def tick(self):
        """1초마다 (경비 상태는 PatrolNode 가 DB 에서 읽어 self.ctrl 에 넣음)"""
        if self.ctrl in ('stopped', 'paused'):
            self.cancel()
            self.inspect = None
            self.state = self.ctrl
            return
        if self.ctrl == 'alarm':
            self.cancel()
            self.state = 'alarm'
            if time.time() - self.alarm_last >= self.p.settings['alarm_interval']:
                self.alarm_signal()
            return
        # running
        if self.goal is not None:
            return
        if self.inspect:
            x, y, until = self.inspect
            if not self.nav.server_is_ready():
                # Nav2 가 아직 없으면 30초 동안 기다리며 '이동 불가'로 보여 줌 (바로 버리면 실패한 줄 모름)
                self.state = 'nav_unavailable'
                if time.time() > until:
                    self.inspect = None
                return
            self.inspect = None
            self.state = 'inspecting'
            self._send(self.nav, NavigateToPose.Goal(pose=self.pose(x, y)), lambda: None)
            return
        if not self.points:
            self.state = 'no_plan' if self.localized else 'localizing'
            return
        if not self.follow.server_is_ready():
            self.state = 'nav_unavailable'
            return
        self.state = 'patrolling'
        n = len(self.points)
        start = self.next_idx % n
        order = self.points[start:]          # 이번에 보낼 지점: 이어서 갈 곳부터 이번 바퀴 끝까지
        poses = []
        for k, (x, y) in enumerate(order):
            nx, ny = order[k + 1] if k + 1 < len(order) else self.points[0]
            poses.append(self.pose(x, y, math.atan2(ny - y, nx - x)))   # 다음 지점을 바라보게 (지점마다 돌지 않게)
        self.sent_from = start

        def done():
            self.laps += 1
            self.next_idx = 0
        self._send(self.follow, FollowWaypoints.Goal(poses=poses), done)

    def _send(self, client, goal, on_done):
        def fb(f):
            self.waypoint = self.sent_from + int(f.feedback.current_waypoint)
            self.next_idx = self.waypoint
        token = object()          # 이 요청의 표식: 그 사이 취소·새 goal 이 오면 늦게 온 콜백을 무시
        self.goal = token
        fut = client.send_goal_async(goal, feedback_callback=fb if client is self.follow else None)

        def accepted(f):
            h = f.result()
            if self.goal is not token:      # 받아들여지기 전에 취소(멈춤·대기·확인 이동)됨
                if h.accepted:
                    h.cancel_goal_async()
                return
            if not h.accepted:
                self.goal = None
                self.state = 'rejected'
                return
            self.goal = h

            def finished(rf):
                if self.goal is not h:      # 이미 취소되고 다른 goal 로 바뀜
                    return
                self.goal = None
                if rf.result().status == GoalStatus.STATUS_SUCCEEDED:
                    on_done()
            h.get_result_async().add_done_callback(finished)
        fut.add_done_callback(accepted)

    def cancel(self):
        # ClientGoalHandle 은 == 비교가 다른 타입에서 예외를 내므로 `in (None, ...)` 로 비교하지 말 것
        g, self.goal = self.goal, None
        if isinstance(g, ClientGoalHandle):
            g.cancel_goal_async()

    # ---------------- 장애물 대기 알림 ----------------
    def start_alarm(self, alert_id):
        self.cancel()
        self.inspect = None
        self.ctrl = 'alarm'
        self.alarm_alert = alert_id
        self.alarm_last = 0.0
        if self.emotion is not None and self.emotion.service_is_ready():
            self.emotion.call_async(Emotion.Request(emotion='angry'))
        self.alarm_signal()

    def alarm_signal(self):
        """소리·빛 한 번 (alarm_interval 마다). TurtleBot3 부저 ERROR, Pinky LED 빨강/끄기 번갈아"""
        self.alarm_last = time.time()
        if self.sound is not None:
            if self.sound.service_is_ready():
                self.sound.call_async(Sound.Request(value=3))   # 3 = ERROR
            else:
                self.p.warn_once(f'sound-{self.id}', f'{self.name}: {self.ns}/sound 서비스가 없어 부저를 울리지 못함')
        if self.cfg.get('type') == 'pinky':
            if self.led is None:
                self.p.warn_once('pinky-if', 'pinky_interfaces 가 없어 Pinky LED·표정 알림을 생략 (관제 웹 경고음은 남)')
            elif self.led.service_is_ready():
                self.alarm_phase = not self.alarm_phase
                self.led.call_async(SetLed.Request(command='fill' if self.alarm_phase else 'clear', pixels=[], r=255, g=0, b=0))

    def end_alarm(self):
        """대기가 끝남 (경보 처리·재개·멈춤): Pinky LED 끄고 표정 basic"""
        self.alarm_alert = None
        if self.led is not None and self.led.service_is_ready():
            self.led.call_async(SetLed.Request(command='clear', pixels=[], r=0, g=0, b=0))
        if self.emotion is not None and self.emotion.service_is_ready():
            self.emotion.call_async(Emotion.Request(emotion='basic'))


class PatrolNode(Node):
    def __init__(self):
        super().__init__('fleet_patrol')
        self.declare_parameter('map_name', '')
        self.declare_parameter('fleet', '')
        self.declare_parameter('db', store.DB_PATH)
        self.declare_parameter('config_dir', '')
        self.map_name = self.get_parameter('map_name').value
        self.fleet_path = self.get_parameter('fleet').value
        with open(self.fleet_path, encoding='utf-8') as f:
            self.fleet = json.load(f)
        self.fleet_mtime = os.path.getmtime(self.fleet_path)
        self.db_path = self.get_parameter('db').value
        self.con = store.db(self.db_path)
        self.db_lock = threading.Lock()
        self.warned = set()
        self.load_config()
        self.base = Baseline(self.map_name, self.settings['tol'])
        self.alert_pub = self.create_publisher(String, '/fleet/alerts', 10)
        self.status_pub = self.create_publisher(String, '/fleet/patrol_status', 10)
        self.create_subscription(String, '/fleet/patrol_cmd', self.on_cmd, 10)
        self.robots = [RobotWatch(self, r) for r in self.fleet.get('robots', [])]
        self.executor_ref = None        # main() 이 넣음: 로봇 목록에 새 로봇이 생기면 그 노드를 더함
        self.plan_key = None
        self.zones_mtime = None
        self.create_timer(1.0, self.tick)
        self.create_timer(5.0, self.reload)
        self.last_report = {}
        self.get_logger().info(f'경비 노드 시작: 지도 {self.map_name}, 로봇 {[r.name for r in self.robots]}')

    def warn_once(self, key, msg):
        if key not in self.warned:
            self.warned.add(key)
            self.get_logger().warn(msg)

    def load_config(self):
        self.settings = store.read_patrol(self.map_name)['settings']
        p = os.path.join(store.PATROL_DIR, f'{self.map_name}.json')
        self.cfg_mtime = os.path.getmtime(p) if os.path.exists(p) else 0

    def reload(self):
        with self.db_lock:   # 실행 중 DB 파일이 지워졌으면 새 파일로 (지워진 파일에 경보를 쓰지 않게)
            self.con, fresh = dbutil.ensure(self.con, self.db_path, store.MIGRATIONS)
        if fresh:
            self.get_logger().warn('DB 파일(data/fleet.db)이 새로 만들어졌습니다')
        p = os.path.join(store.PATROL_DIR, f'{self.map_name}.json')
        if os.path.exists(p) and os.path.getmtime(p) != self.cfg_mtime:
            self.load_config()
            self.get_logger().info('감지·순회 설정 다시 읽음')
        try:   # 로봇 목록에 새로 등록된 로봇
            if os.path.getmtime(self.fleet_path) != self.fleet_mtime:
                with open(self.fleet_path, encoding='utf-8') as f:
                    self.fleet = json.load(f)
                self.fleet_mtime = os.path.getmtime(self.fleet_path)
                have = {r.id for r in self.robots}
                for cfg in self.fleet.get('robots', []):
                    if cfg['id'] not in have:
                        r = RobotWatch(self, cfg)
                        self.robots.append(r)
                        if self.executor_ref is not None:
                            self.executor_ref.add_node(r.node)
                        self.get_logger().info(f'새 로봇: {r.name}')
        except (OSError, ValueError) as e:
            self.get_logger().warn(f'로봇 목록 다시 읽기 실패: {e}')
        try:
            info = maps.map_info(self.map_name)
            if max(os.path.getmtime(info['yaml']), os.path.getmtime(info['image'])) != self.base.mtime:
                self.base.load(self.settings['tol'])
                for r in self.robots:
                    r.evidence = {'new': {}, 'missing': {}}
                self.plan_key = None          # 기준 지도가 바뀌면 순회 지점도 다시
                self.get_logger().info('기준 지도 다시 읽음')
        except Exception as e:
            self.get_logger().warn(f'기준 지도 확인 실패: {e}')

    def zones_key(self):
        p = os.path.join(maps.ZONES_DIR, f'{self.map_name}.json')
        return os.path.getmtime(p) if os.path.exists(p) else 0

    def replan(self):
        """경비 중·대기 중인 로봇들의 순회 지점. 로봇 구성·지도·구역·간격이 바뀌었을 때만 다시 계산"""
        active = [r for r in self.robots if r.watching]
        key = (tuple(r.id for r in active), self.base.mtime, self.zones_key(), self.settings['spacing'])
        if key == self.plan_key:
            return
        # 지도 위 위치를 아직 모르는 로봇이 있으면(초기 위치 전) 기다림 — 위치를 알아야 이어진 영역을 고름
        guards = [{'id': r.id, 'type': r.cfg.get('type'), 'pose': r.pose_xy()} for r in active]
        if any(g['pose'] is None for g in guards):
            for r in active:
                if r.pose_xy() is None:
                    r.state = 'localizing'
            return
        t0 = time.time()
        try:
            plan = coverage.plan(self.map_name, guards, self.settings['spacing'])
        except Exception as e:  # noqa: BLE001
            self.get_logger().error(f'순회 지점 계산 실패: {e}')
            return
        self.plan_key = key
        for r in self.robots:
            r.set_points(plan.get(r.id, []) if r.watching else [])
        self.get_logger().info(f'순회 지점 {sum(len(v) for v in plan.values())}개 ({time.time() - t0:.2f}s): '
                               + ', '.join(f'{r.name} {len(r.points)}' for r in active))

    def tick(self):
        # DB 의 경비 상태 (서버가 시작·멈춤·재개·정지를 씀)
        with self.db_lock:
            try:
                rows = roles.guard_rows(self.con)
            except Exception as e:  # noqa: BLE001
                self.get_logger().warn(f'경비 상태 읽기 실패: {e}')
                rows = None
        if rows is not None:
            for r in self.robots:
                row = rows.get(r.id)
                ctrl = row['guard_state'] if row else 'stopped'
                if r.ctrl == 'alarm' and ctrl != 'alarm':
                    r.end_alarm()                      # 재개·멈춤·정지로 대기가 끝남
                if ctrl == 'alarm' and r.ctrl != 'alarm':
                    r.start_alarm(row['alarm_alert_id'])   # 노드를 다시 켰을 때 대기 중이던 로봇
                r.ctrl = ctrl
            with self.db_lock:
                self.check_alarms()
        self.replan()
        for r in self.robots:
            r.tick()
        self.status_pub.publish(String(data=json.dumps({
            'map': self.map_name, 'time': time.time(),
            'robots': {r.id: {'ctrl': r.ctrl, 'state': r.state, 'waypoint': r.waypoint, 'laps': r.laps,
                              'points': len(r.points), 'plan': r.points, 'localized': r.localized,
                              'alarm_alert_id': r.alarm_alert} for r in self.robots if r.ctrl != 'stopped'}})))

    def check_alarms(self):
        """대기 중인 경보를 관제실이 처리했으면(열림이 아니면) 다시 경비"""
        for r in self.robots:
            if r.ctrl != 'alarm' or r.alarm_alert is None:
                continue
            a = self.con.execute('SELECT status FROM alerts WHERE id = ?', (r.alarm_alert,)).fetchone()
            if a is None or a['status'] != 'open':
                done = {'ok': '이상 없음', 'false': '오탐', 'baseline': '기준 지도 반영'}.get(a['status'] if a else '', '경보 없음')
                if roles.clear_alarm(self.con, r.id, f'경보 #{r.alarm_alert} 처리됨({done}) → 경비 재개'):
                    self.get_logger().info(f'{r.name}: 경보 #{r.alarm_alert} 처리됨({done}) → 경비 재개')
                    r.end_alarm()
                    r.ctrl = 'running'

    def on_cmd(self, msg):
        try:
            c = json.loads(msg.data)
        except ValueError:
            return
        if c.get('robot') == '*' and c.get('cmd') == 'pause':   # 비상정지: 모두 바로 멈춤 (DB 는 서버가 paused 로)
            for r in self.robots:
                r.cancel()
            return
        r = next((x for x in self.robots if x.id == c.get('robot')), None)
        if r is None:
            return
        if c.get('cmd') == 'pause':
            r.cancel()
        elif c.get('cmd') == 'inspect':
            if r.ctrl != 'running':
                self.get_logger().info(f'{r.name}: 경비 중이 아니라 확인 이동을 하지 않음')
                return
            r.cancel()
            p = r.pose_xy() if r.localized else None
            x, y = float(c['x']), float(c['y'])
            if p is not None:   # 경보 위치에서 로봇 쪽으로 0.6 m 떨어진 곳
                dx, dy = p[0] - x, p[1] - y
                d = math.hypot(dx, dy) or 1.0
                x, y = x + dx / d * 0.6, y + dy / d * 0.6
            r.inspect = (x, y, time.time() + 30.0)

    # ---------------- 경보 ----------------
    def report(self, robot, kind, cells, pts):
        with self.db_lock:
            self._report(robot, kind, cells, pts)

    def _report(self, robot, kind, cells, pts):
        B, S = self.base, self.settings
        xs = [B.center(i, j)[0] for i, j in cells]
        ys = [B.center(i, j)[1] for i, j in cells]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        w, h = max(xs) - min(xs) + B.res, max(ys) - min(ys) + B.res
        # LiDAR 는 물체의 겉면(선)만 보므로 칸 수 × 칸 넓이는 실제보다 훨씬 작다.
        # 겉면을 감싸는 상자 넓이로 보되, 한 면만 보인 경우를 위해 두께를 최소 0.1 m 로 친다.
        area = max(w, 0.1) * max(h, 0.1)
        if area < S['min_area'] or len(cells) < 3:
            return
        now = time.time()
        # 같은 곳(0.5 m) 같은 종류: 열린 경보면 갱신, 최근 30분 안에 처리(이상 없음/오탐)했으면 다시 알리지 않음
        rows = self.con.execute(
            'SELECT * FROM alerts WHERE map = ? AND kind = ? AND ((x - ?)*(x - ?) + (y - ?)*(y - ?)) < 0.25 '
            "AND (status = 'open' OR handled_at > ?) ORDER BY id DESC LIMIT 1",
            (self.map_name, kind, cx, cx, cy, cy, now - 1800)).fetchall()
        if rows:
            a = rows[0]
            if a['status'] != 'open':
                return
            key = (a['id'], robot.id)
            if now - self.last_report.get(key, 0) < 3.0:
                return
            self.last_report[key] = now
            robots = sorted(set(store.alert_robot_ids(self.con, a['id'])) | {robot.id})
            # 다른 쪽 면이 보이면 상자를 합쳐 키움 (물체 하나로 볼 만한 2 m 까지)
            x0, x1 = min(a['x'] - a['w'] / 2, cx - w / 2), max(a['x'] + a['w'] / 2, cx + w / 2)
            y0, y1 = min(a['y'] - a['h'] / 2, cy - h / 2), max(a['y'] + a['h'] / 2, cy + h / 2)
            if x1 - x0 <= 2.0 and y1 - y0 <= 2.0:
                ux, uy, uw, uh = (x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0
            else:
                ux, uy, uw, uh = a['x'], a['y'], a['w'], a['h']
            area = max(area, a['area'], max(uw, 0.1) * max(uh, 0.1))
            sev = self.severity(kind, area, robots)
            with self.con:
                store.ensure_refs(self.con, self.map_name, robot.id, now)
                self.con.execute('UPDATE alerts SET last_seen=?, seen=seen+1, area=?, severity=?, x=?, y=?, w=?, h=? WHERE id=?',
                                 (now, area, sev, ux, uy, uw, uh, a['id']))
                store.record_sighting(self.con, a['id'], robot.id, now)
            self.publish('update', a['id'])
            return
        near = [[x, y] for x, y in pts if (x - cx) ** 2 + (y - cy) ** 2 < 2.25][:400]
        sev = self.severity(kind, area, [robot.id])
        with self.con:
            store.ensure_refs(self.con, self.map_name, robot.id, now)
            cur = self.con.execute(
                'INSERT INTO alerts (map, kind, x, y, w, h, area, first_seen, last_seen, seen, severity, status, scan) '
                "VALUES (?,?,?,?,?,?,?,?,?,1,?,'open',?)",
                (self.map_name, kind, round(cx, 3), round(cy, 3), round(w, 3), round(h, 3), round(area, 4), now, now,
                 sev, json.dumps(near)))
            store.record_sighting(self.con, cur.lastrowid, robot.id, now)
        self.get_logger().warn(f'경보 #{cur.lastrowid}: {kind} ({cx:.2f}, {cy:.2f}) {area:.2f} m² · {robot.name}')
        self.publish('new', cur.lastrowid)
        # 지도에 없던 장애물(새로 생긴 것)을 경비 중에 새로 찾음 → 그 자리에 멈추고 알림, 관제실 처리까지 대기
        if kind == 'new' and robot.ctrl == 'running':
            msg = f'경보 #{cur.lastrowid}: 지도에 없던 장애물 ({cx:.1f}, {cy:.1f}) → 멈춰서 알림, 관제실 처리 대기'
            if roles.set_alarm(self.con, robot.id, cur.lastrowid, msg):
                robot.start_alarm(cur.lastrowid)
                self.get_logger().warn(f'{robot.name}: {msg}')

    def severity(self, kind, area, robots):
        if area >= 0.25 or (self.settings['escalate'] and len(robots) >= 2):
            return 'high'
        if area < 0.08 and kind == 'new':
            return 'low'
        return 'medium'

    def publish(self, event, aid):
        a = store.get_alert(self.con, aid)
        a.pop('scan', None)
        self.alert_pub.publish(String(data=json.dumps({'event': event, 'alert': a}, ensure_ascii=False)))


def main():
    rclpy.init()
    node = PatrolNode()
    ex = MultiThreadedExecutor()
    ex.add_node(node)
    for r in node.robots:
        ex.add_node(r.node)
    node.executor_ref = ex
    try:
        ex.spin()
    except KeyboardInterrupt:
        pass
    finally:
        for r in node.robots:
            r.cancel()


if __name__ == '__main__':
    main()
