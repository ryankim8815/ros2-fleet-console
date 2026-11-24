#!/usr/bin/env python3
"""이동 경로 실행기 (PC 에서, serve.py 가 경로를 시작할 때 띄움).

data/fleet.db 에서 할당(route_assignments)의 지금 실행(route_runs, state = 'running')마다:
  - 경로 지점들(places 좌표·방향)을 Nav2 FollowWaypoints(/<ns>/follow_waypoints)로 한 바퀴씩 보냄
  - 한 바퀴를 끝내면 loops_done + 1 (이력 'loop'), repeat_count 만큼 돌면 state = 'done' (0 이면 멈출 때까지 계속)
  - 진행 중인 지점 순번(waypoint)을 기록해 두어, 멈췄다 이어 가면 그 지점부터 다시 감
  - Nav2 가 실패하면 state = 'failed' (경로 화면에서 '이어서' 로 다시 시도)
사용자가 멈춤·정지하거나 할당이 사라지면(0.5초 안에 DB 에서 확인) 그 로봇의 goal 을 취소합니다.
경로 지점이 바뀌면 진행 중인 goal 을 취소하고 바뀐 경로로 이어서 갑니다.

명령: /fleet/route_cmd (std_msgs/String JSON)
  {"cmd": "pause", "robot": "*"|id}   비상정지: 바로 goal 취소 + DB 상태 paused
"""
import json
import math
import os
import signal
import sys
import threading
import time

import rclpy
from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import FollowWaypoints
from rclpy.action import ActionClient
from rclpy.action.client import ClientGoalHandle
from rclpy.node import Node
from rclpy.signals import SignalHandlerOptions
from std_msgs.msg import String

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..')))
import dbutil  # noqa: E402
import places as store  # noqa: E402

NAV_WAIT_SEC = 30.0   # Nav2 가 없을 때 이 시간 동안 기다린 뒤 실패로 기록


class RobotRun:
    """로봇 한 대의 경로 실행"""

    def __init__(self, parent, cfg):
        self.p = parent
        self.id, self.name, self.ns = cfg['id'], cfg['name'], cfg['ns']
        self.client = ActionClient(parent, FollowWaypoints, f'{self.ns}/follow_waypoints')
        self.goal = None          # None | 보내는 중 표식(object) | ClientGoalHandle
        self.sig = None           # 보낸 goal 의 (run_id, 지점들)
        self.offset = 0           # 보낸 goal 의 첫 지점 순번
        self.job = None
        self.waiting_since = None
        self.last_wp = None

    def log(self, msg):
        self.p.get_logger().info(f'[{self.name}] {msg}')

    def pose(self, x, y, yaw):
        ps = PoseStamped()
        ps.header.frame_id = 'map'
        ps.header.stamp = self.p.get_clock().now().to_msg()
        ps.pose.position.x, ps.pose.position.y = float(x), float(y)
        ps.pose.orientation.z, ps.pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
        return ps

    def tick(self, job):
        """0.5초마다: DB 의 할당(job, 실행 중이 아니면 None)에 맞춰 goal 을 보내거나 취소"""
        self.job = job
        if job is None:
            if self.goal is not None:
                self.log('멈춤/정지 → goal 취소')
                self.cancel()
            self.waiting_since = None
            return
        sig = (job['run_id'], tuple(job['points']))
        if self.goal is not None:
            if sig != self.sig:
                self.log('경로가 바뀜 → 지금 goal 을 취소하고 바뀐 경로로 이어서 감')
                self.cancel()
            return
        pts = job['points']
        if not pts:
            self.p.finish(self, job, 'failed', '경로에 지점이 없습니다')
            return
        if not self.client.server_is_ready():
            now = time.time()
            self.waiting_since = self.waiting_since or now
            if now - self.waiting_since > NAV_WAIT_SEC:
                self.p.finish(self, job, 'failed', f'{self.ns}/follow_waypoints 가 없습니다 (Nav2 실행·초기 위치 확인)')
                self.waiting_since = None
            else:
                self.p.progress(self, job, 'nav_unavailable', f'Nav2 기다리는 중 ({self.ns}/follow_waypoints)')
            return
        self.waiting_since = None
        start = job['waypoint'] if 0 <= job['waypoint'] < len(pts) else 0
        self.send(job, sig, start)

    def send(self, job, sig, start):
        pts = job['points'][start:]
        token = object()          # 받아들여지기 전에 취소·새 goal 이 오면 늦게 온 콜백을 무시
        self.goal, self.sig, self.offset, self.last_wp = token, sig, start, start
        rid = job['run_id']
        loop = job['loops_done'] + 1
        self.log(f'{loop}바퀴째 지점 {start + 1}~{len(job["points"])} 보냄')
        self.p.progress(self, job, 'moving', f'{loop}바퀴째', waypoint=start)

        def feedback(f):
            wp = self.offset + int(f.feedback.current_waypoint)
            if self.goal is not None and wp != self.last_wp and self.job and self.job['run_id'] == rid:
                self.last_wp = wp
                self.p.progress(self, self.job, 'moving', f'{loop}바퀴째', waypoint=wp)

        fut = self.client.send_goal_async(FollowWaypoints.Goal(poses=[self.pose(*p) for p in pts]), feedback_callback=feedback)

        def accepted(f):
            h = f.result()
            if self.goal is not token:
                if h.accepted:
                    h.cancel_goal_async()
                return
            if not h.accepted:
                self.goal = None
                self.p.finish(self, job, 'failed', 'Nav2 가 goal 을 거부했습니다')
                return
            self.goal = h

            def finished(rf):
                if self.goal is not h:     # 우리가 취소했고 이미 다른 goal 로 바뀜
                    return
                self.goal = None
                res, status = rf.result().result, rf.result().status
                cur = self.job
                if cur is None or cur['run_id'] != rid:
                    return
                missed = list(getattr(res, 'missed_waypoints', []) or [])
                if status == GoalStatus.STATUS_SUCCEEDED and not missed:
                    done = cur['loops_done'] + 1
                    if cur['repeat_count'] and done >= cur['repeat_count']:
                        self.log(f'{done}바퀴 완료 → 끝')
                        self.p.finish(self, cur, 'done', f'{done}바퀴 완료', loops_done=done)
                    else:
                        self.log(f'{done}바퀴 완료')
                        self.p.progress(self, cur, 'moving', f'{done}바퀴 완료', waypoint=0, loops_done=done, event='loop')
                        cur['loops_done'], cur['waypoint'] = done, 0
                    return
                if missed:
                    idx = getattr(missed[0], 'index', missed[0])
                    msg = f'지점 {self.offset + int(idx) + 1} 에 가지 못했습니다'
                elif status == GoalStatus.STATUS_CANCELED:
                    msg = '다른 곳에서 goal 을 취소했습니다'
                else:
                    msg = f'Nav2 실패 (지점 {self.last_wp + 1}, status {status})'
                err = getattr(res, 'error_msg', '')
                self.log(msg + (f': {err}' if err else ''))
                self.p.finish(self, cur, 'failed', msg + (f' · {err}' if err else ''))
            h.get_result_async().add_done_callback(finished)
        fut.add_done_callback(accepted)

    def cancel(self):
        # ClientGoalHandle 은 다른 타입과 == 비교하면 예외 → isinstance 로
        g, self.goal = self.goal, None
        self.sig = None
        if isinstance(g, ClientGoalHandle):
            g.cancel_goal_async()


class RouteRunner(Node):
    def __init__(self):
        super().__init__('fleet_route_runner')
        self.declare_parameter('fleet', '')
        self.declare_parameter('db', store.DB_PATH)
        self.fleet_path = self.get_parameter('fleet').value
        self.db_path = self.get_parameter('db').value
        self.con = store.db(self.db_path)
        self.robots = {}
        self.fleet_mtime = None
        self.load_fleet()
        self.create_subscription(String, '/fleet/route_cmd', self.on_cmd, 10)
        self.timer = self.create_timer(0.5, self.tick)
        self.get_logger().info(f'이동 경로 실행기 시작: 로봇 {[r.name for r in self.robots.values()]}')

    def load_fleet(self):
        try:
            m = os.path.getmtime(self.fleet_path)
        except OSError:
            return
        if m == self.fleet_mtime:
            return
        self.fleet_mtime = m
        with open(self.fleet_path, encoding='utf-8') as f:
            fleet = json.load(f)
        cfgs = {r['id']: r for r in fleet.get('robots', [])}
        for rid in list(self.robots):
            if rid not in cfgs or cfgs[rid]['ns'] != self.robots[rid].ns:
                self.robots.pop(rid).cancel()
        for rid, cfg in cfgs.items():
            if rid not in self.robots:
                self.robots[rid] = RobotRun(self, cfg)

    def tick(self):
        self.load_fleet()
        self.con, fresh = dbutil.ensure(self.con, self.db_path, store.MIGRATIONS)   # 실행 중 DB 파일이 지워졌으면 새 파일로
        if fresh:
            self.get_logger().warn('DB 파일이 새로 만들어졌습니다 → 실행 중인 경로 없음, 모든 goal 취소')
        jobs = store.runner_jobs(self.con)
        for rid, job in jobs.items():
            if rid not in self.robots:
                store.runner_finish(self.con, job['run_id'], 'failed', '로봇 목록(fleet.json)에 없는 로봇입니다')
        for rid, r in self.robots.items():
            r.tick(jobs.get(rid))

    def progress(self, r, job, run_state, msg, waypoint=None, loops_done=None, event=None):
        store.runner_progress(self.con, job['run_id'], run_state, msg, waypoint=waypoint, loops_done=loops_done, event=event)

    def finish(self, r, job, state, msg, loops_done=None):
        store.runner_finish(self.con, job['run_id'], state, msg, loops_done=loops_done)
        r.job = None

    def on_cmd(self, msg):
        try:
            c = json.loads(msg.data)
        except ValueError:
            return
        if c.get('cmd') != 'pause':
            return
        targets = list(self.robots.values()) if c.get('robot') == '*' else [r for r in self.robots.values() if r.id == c.get('robot')]
        for r in targets:
            r.cancel()
        if c.get('robot') == '*':
            store.pause_all(self.con, '비상정지')
        else:
            for r in targets:
                store.control_assignment(self.con, r.id, 'pause')
        self.get_logger().warn(f'멈춤 명령: {[r.name for r in targets]}')


def main():
    # rclpy 기본 SIGINT 처리는 컨텍스트를 바로 닫아 goal 취소를 못 보냄 → 직접 받아서
    # 모든 goal 을 취소한 뒤 끝냄 (실행기를 꺼도 로봇이 혼자 경로를 계속 가지 않게)
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    node = RouteRunner()
    try:
        while rclpy.ok() and not stop.is_set():
            rclpy.spin_once(node, timeout_sec=0.2)
    finally:
        node.timer.cancel()               # 끄는 동안 tick 이 새 goal 을 보내지 않게 먼저 멈춤
        for r in node.robots.values():
            r.job = None                  # 늦게 온 결과 콜백도 진행·완료를 쓰지 않게
            r.cancel()
        end = time.time() + 1.5
        while time.time() < end:          # 취소 요청을 실제로 내보내고 응답 받기
            rclpy.spin_once(node, timeout_sec=0.1)
        node.get_logger().info('이동 경로 실행기 종료 (goal 취소함)')
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
