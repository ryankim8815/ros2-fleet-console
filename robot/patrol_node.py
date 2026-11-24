#!/usr/bin/env python3
"""경비 모드 노드 (PC 에서, serve.py 가 경비 모드로 바뀔 때 실행).

로봇마다:
  1) 순찰: config/patrol/<지도>.json 의 지점들을 Nav2 FollowWaypoints(/<ns>/follow_waypoints)로 반복
  2) 차이 감지: /<ns>/scan 을 지도 위치(map → 스캔 프레임 TF, 로봇의 TF 토픽)로 옮겨 기준 지도와 비교
       - 새로 생긴 것: 기준 지도에서 비어 있던 칸(벽에서 tol 이상 떨어진)에 스캔 끝점이 닿음
       - 사라진 것: 기준 지도의 벽 칸을 스캔 빛줄기가 뚫고 지나감
     같은 칸이 연속 persist 번 이상 보이면 '확정', 확정 칸을 이어 붙인 덩어리가 min_area 이상이면 경보
  3) 경보: data/fleet.db 의 alerts·alert_robots 에 기록하고 /fleet/alerts (std_msgs/String JSON) 로 알림
     가까운 곳의 열린 경보가 있으면 새로 만들지 않고 갱신 (다른 로봇이 다시 보면 등급 올림)

명령: /fleet/patrol_cmd (String JSON)
  {"cmd": "inspect", "robot": id, "x": .., "y": ..}   그 로봇을 경보 위치 근처로 보낸 뒤 순찰 재개
  {"cmd": "pause"|"resume", "robot": id}              순찰 멈춤/재개
상태: /fleet/patrol_status (String JSON, 1초마다)

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
import dbutil  # noqa: E402
import maps  # noqa: E402
import patrol_store as store  # noqa: E402


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
    """로봇 한 대: TF(로봇의 TF 토픽), 스캔 비교, 순찰 실행"""

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
        self.evidence = {'new': {}, 'missing': {}}   # (i,j) -> [count, last_time]
        self.last_proc = 0.0
        self.state = 'idle'
        self.waypoint = 0
        self.laps = 0
        self.goal = None
        self.paused = False
        self.inspect = None
        self.localized = False

    # ---------------- 스캔 비교 ----------------
    def on_scan(self, m):
        now = time.time()
        if now - self.last_proc < 0.4:
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

    # ---------------- 순찰 ----------------
    def route(self):
        r = self.p.routes.get(self.id)
        return r['points'] if r and r.get('enabled', True) and r.get('points') else []

    def pose(self, x, y, yaw=0.0):
        ps = PoseStamped()
        ps.header.frame_id = 'map'
        ps.header.stamp = self.node.get_clock().now().to_msg()
        ps.pose.position.x, ps.pose.position.y = float(x), float(y)
        ps.pose.orientation.z, ps.pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
        return ps

    def tick(self):
        """1초마다: 할 일이 없으면 순찰(또는 확인 이동) 시작"""
        if self.goal is not None or self.paused:
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
        pts = self.route()
        if not pts:
            self.state = 'no_route'
            return
        if not self.follow.server_is_ready():
            self.state = 'nav_unavailable'
            return
        self.state = 'patrolling'

        def done():
            self.laps += 1
        self._send(self.follow, FollowWaypoints.Goal(poses=[self.pose(x, y) for x, y in pts]), done)

    def _send(self, client, goal, on_done):
        fb = (lambda f: setattr(self, 'waypoint', int(f.feedback.current_waypoint))) if client is self.follow else None
        token = object()          # 이 요청의 표식: 그 사이 취소·새 goal 이 오면 늦게 온 콜백을 무시
        self.goal = token
        fut = client.send_goal_async(goal, feedback_callback=fb)

        def accepted(f):
            h = f.result()
            if self.goal is not token:      # 받아들여지기 전에 취소(멈춤·확인 이동)됨
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


class PatrolNode(Node):
    def __init__(self):
        super().__init__('fleet_patrol')
        self.declare_parameter('map_name', '')
        self.declare_parameter('fleet', '')
        self.declare_parameter('db', store.DB_PATH)
        self.declare_parameter('config_dir', '')
        self.map_name = self.get_parameter('map_name').value
        with open(self.get_parameter('fleet').value, encoding='utf-8') as f:
            self.fleet = json.load(f)
        self.db_path = self.get_parameter('db').value
        self.con = store.db(self.db_path)
        self.db_lock = threading.Lock()
        self.load_config()
        self.base = Baseline(self.map_name, self.settings['tol'])
        self.alert_pub = self.create_publisher(String, '/fleet/alerts', 10)
        self.status_pub = self.create_publisher(String, '/fleet/patrol_status', 10)
        self.create_subscription(String, '/fleet/patrol_cmd', self.on_cmd, 10)
        self.robots = [RobotWatch(self, r) for r in self.fleet.get('robots', [])]
        self.create_timer(1.0, self.tick)
        self.create_timer(5.0, self.reload)
        self.last_report = {}
        self.get_logger().info(f'경비 시작: 지도 {self.map_name}, 로봇 {[r.name for r in self.robots]}')

    def load_config(self):
        cfg = store.read_patrol(self.map_name)
        self.routes, self.settings = cfg['routes'], cfg['settings']
        p = os.path.join(store.PATROL_DIR, f'{self.map_name}.json')
        self.cfg_mtime = os.path.getmtime(p) if os.path.exists(p) else 0

    def reload(self):
        with self.db_lock:   # 실행 중 DB 파일이 지워졌으면 새 파일로 (지워진 파일에 경보를 쓰지 않게)
            self.con, fresh = dbutil.ensure(self.con, self.db_path, store.MIGRATIONS)
        if fresh:
            self.get_logger().warn('DB 파일(data/fleet.db)이 새로 만들어졌습니다')
        p = os.path.join(store.PATROL_DIR, f'{self.map_name}.json')
        if os.path.exists(p) and os.path.getmtime(p) != self.cfg_mtime:
            old = json.dumps(self.routes, sort_keys=True)
            self.load_config()
            if json.dumps(self.routes, sort_keys=True) != old:   # 경로가 바뀐 로봇은 새 경로로 다시 시작
                for r in self.robots:
                    r.cancel()
            self.get_logger().info('순찰 설정 다시 읽음')
        try:
            info = maps.map_info(self.map_name)
            if max(os.path.getmtime(info['yaml']), os.path.getmtime(info['image'])) != self.base.mtime:
                self.base.load(self.settings['tol'])
                for r in self.robots:
                    r.evidence = {'new': {}, 'missing': {}}
                self.get_logger().info('기준 지도 다시 읽음')
        except Exception as e:
            self.get_logger().warn(f'기준 지도 확인 실패: {e}')

    def tick(self):
        for r in self.robots:
            r.tick()
        self.status_pub.publish(String(data=json.dumps({
            'map': self.map_name, 'time': time.time(),
            'robots': {r.id: {'state': r.state, 'waypoint': r.waypoint, 'laps': r.laps, 'points': len(r.route()),
                              'paused': r.paused, 'localized': r.localized} for r in self.robots}})))

    def on_cmd(self, msg):
        try:
            c = json.loads(msg.data)
        except ValueError:
            return
        if c.get('robot') == '*' and c.get('cmd') in ('pause', 'resume'):   # 비상정지: 모두 멈춤
            for r in self.robots:
                self.on_cmd(String(data=json.dumps({**c, 'robot': r.id})))
            return
        r = next((x for x in self.robots if x.id == c.get('robot')), None)
        if r is None:
            return
        if c.get('cmd') == 'inspect':
            r.cancel()
            p = r.buf.lookup_transform('map', r.cfg.get('tfPrefix', '') + 'base_footprint', Time()) if r.localized else None
            x, y = float(c['x']), float(c['y'])
            if p is not None:   # 경보 위치에서 로봇 쪽으로 0.6 m 떨어진 곳
                dx, dy = p.transform.translation.x - x, p.transform.translation.y - y
                d = math.hypot(dx, dy) or 1.0
                x, y = x + dx / d * 0.6, y + dy / d * 0.6
            r.inspect = (x, y, time.time() + 30.0)
            r.paused = False
        elif c.get('cmd') == 'pause':
            r.paused = True
            r.cancel()
            r.state = 'paused'
        elif c.get('cmd') == 'resume':
            r.paused = False

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
    try:
        ex.spin()
    except KeyboardInterrupt:
        pass
    finally:
        for r in node.robots:
            r.cancel()


if __name__ == '__main__':
    main()
