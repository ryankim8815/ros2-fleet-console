"""로봇 한 대의 Nav2 를 네임스페이스로 실행 (PC 에서, 로봇마다 한 번씩).

nav2_bringup/bringup_launch.py 를 namespace + use_namespace:=True 로 감싸고, 파라미터 파일을 로봇에 맞게 고칩니다.
  - 프레임 이름(odom, base_link, base_footprint, base_scan)에 frame_prefix 를 붙임 (TurtleBot3: 'tb3_01/', Pinky: '')
  - '/scan', '/local_costmap/...' 같은 절대 토픽을 상대 이름으로 바꿔 네임스페이스 안을 보게 함
  - enable_stamped_cmd_vel 을 로봇에 맞춤 (TurtleBot3: true = TwistStamped, Pinky: false = Twist)
  - 이동 구역 마스크가 있으면 Nav2 KeepoutFilter 를 켜고 마스크 서버를 함께 띄움
  - LiDAR 안전 구역(data/fleet.db, 웹 상단 LiDAR 안전 칩에서 바꿈): collision_monitor 의 LidarStop(정지)·LidarSlow(서행) 원형 구역 값
Nav2 는 네임스페이스 모드에서 /tf 를 <ns>/tf 로 읽으므로, 로봇도 TF 를 <ns>/tf 로 보내야 합니다
(TurtleBot3: tb3_ns.launch.py, Pinky: pinky_ns.launch.py).

사용 (PC, pixi 환경):
  pixi run ros2 launch ros2-fleet-console/robot/nav2_ns.launch.py namespace:=tb3_01 frame_prefix:=tb3_01/ map:=lab
  pixi run ros2 launch ros2-fleet-console/robot/nav2_ns.launch.py namespace:=pinky_01 map:=lab
  pixi run ros2 launch ros2-fleet-console/robot/nav2_ns.launch.py map:=lab      # 네임스페이스 없는 로봇 (namespace 생략)

  map:=lab        → ros2-fleet-console/config/maps/lab.yaml (웹 '지도 작성'으로 저장한 이름) 또는 yaml 경로
  keepout:=auto   → config/masks/lab/<ns>.yaml 이 있으면 사용 (웹 '이동 구역'에서 저장하면 생김), keepout:=off 로 끔
  robot_type:=auto → DB 로봇 목록에서 네임스페이스로 로봇 종류를 찾음 (turtlebot3 | pinky). 못 찾으면 turtlebot3
                    costmap robot_radius 는 그 종류의 DB robot_types.nav_radius (로봇 등록 화면에서 바꿈)
  params_file:=auto → 종류별 기본 파일: TurtleBot3 config/nav2/burger.yaml, Pinky config/nav2/pinky.yaml
  stamped:=auto   → TurtleBot3 true (TwistStamped), Pinky false (Twist). true/false 로 직접 정할 수도 있음

그 다음 웹 플릿 화면에서 로봇을 고르고 [초기 위치] 로 AMCL 초기 자세를 준 뒤 목표를 지정합니다.
Nav2 는 실행 후 60초 안에 지도 위 위치(map TF)가 없으면 주행 쪽을 켜다가 포기하지만, 웹이 초기 위치 직후와
목표를 보내기 전에 lifecycle_manager_navigation 을 확인해 꺼져 있으면 다시 켭니다 (Robot.ensureNavActive).
"""

import os
import sys

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, OpaqueFunction, LogInfo
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)), '..'))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from nav2_params import keepout_topics, nav_radius_for_type, params_for_type, read_for_ns, rewrite_params, robot_for_ns, stamped_for_type  # noqa: E402


def resolve_map(arg):
    if arg.endswith('.yaml') or '/' in arg:
        return os.path.abspath(os.path.expanduser(arg)), os.path.splitext(os.path.basename(arg))[0]
    return os.path.join(APP_DIR, 'config', 'maps', f'{arg}.yaml'), arg


def launch_setup(context):
    ns = LaunchConfiguration('namespace').perform(context).strip('/')
    prefix = LaunchConfiguration('frame_prefix').perform(context)
    # 로봇 종류: 인자로 주거나(auto 면) config/fleet.json 에서 네임스페이스로 찾음
    rtype = LaunchConfiguration('robot_type').perform(context).strip().lower()
    type_note = ''
    if rtype in ('', 'auto'):
        found = robot_for_ns(ns)
        rtype = (found or {}).get('type') or 'turtlebot3'
        type_note = ' (DB 로봇 목록)' if found else ' (로봇 목록에 없어 기본값)'
    stamped_arg = LaunchConfiguration('stamped').perform(context).strip().lower()
    stamped = stamped_for_type(rtype) if stamped_arg in ('', 'auto') else stamped_arg in ('true', '1')
    params_arg = LaunchConfiguration('params_file').perform(context).strip()
    params_src = params_for_type(rtype) if params_arg in ('', 'auto') else os.path.abspath(os.path.expanduser(params_arg))
    if not os.path.exists(params_src):
        raise RuntimeError(f'Nav2 파라미터 파일이 없습니다: {params_src}')
    map_yaml, map_name = resolve_map(LaunchConfiguration('map').perform(context))
    if not os.path.exists(map_yaml):
        raise RuntimeError(f'지도 파일이 없습니다: {map_yaml}')
    keepout_arg = LaunchConfiguration('keepout').perform(context)
    leaf = ns.replace('/', '_') or 'default'
    mask = None
    if keepout_arg == 'auto':
        cand = os.path.join(APP_DIR, 'config', 'masks', map_name, f'{leaf}.yaml')
        mask = cand if os.path.exists(cand) else None
    elif keepout_arg not in ('off', 'false', ''):
        mask = os.path.abspath(os.path.expanduser(keepout_arg))

    safety = read_for_ns(ns)   # 이 로봇의 LiDAR 안전 구역 (DB: 로봇별 덮어쓰기 > 플릿 기본값) → collision_monitor LidarStop·LidarSlow
    nav_r = nav_radius_for_type(rtype)   # 로봇 종류의 Nav2 반지름 (DB robot_types)
    params, changes = rewrite_params(params_src, prefix, stamped, bool(mask), ns, safety=safety, nav_radius=nav_r)
    info_topic, mask_topic = keepout_topics(ns)
    use_sim_time = LaunchConfiguration('use_sim_time').perform(context).lower() == 'true'
    shown = os.path.relpath(params_src, APP_DIR) if params_src.startswith(APP_DIR + os.sep) else params_src
    actions = [LogInfo(msg=f'[nav2_ns] namespace=/{ns} 종류={rtype}{type_note} params={shown} '
                           f'frame_prefix={prefix!r} stamped={stamped} map={map_yaml} '
                           f'keepout={mask or "없음"} LiDAR 안전={"켜짐" if safety["enabled"] else "꺼짐"}'
                           f'[{"로봇별" if safety["source"] == "robot" else "기본값"}] '
                           f'(정지 {safety["stop_dist"]} m · 서행 {safety["slow_dist"]} m 안 {safety["slow_speed"]} m/s) '
                           f'({len(changes)} values rewritten)')]
    bringup = os.path.join(get_package_share_directory('nav2_bringup'), 'launch', 'bringup_launch.py')
    actions.append(IncludeLaunchDescription(
        PythonLaunchDescriptionSource(bringup),
        launch_arguments={
            'namespace': ns, 'use_namespace': 'True', 'map': map_yaml, 'params_file': params,
            'use_sim_time': str(use_sim_time), 'autostart': 'True',
            'use_composition': LaunchConfiguration('use_composition').perform(context),
        }.items(),
    ))
    if mask:
        # 이동 구역 마스크: map_server(마스크) + costmap_filter_info_server + lifecycle manager
        # 마스크를 바꿀 때: 웹이 /<ns>/keepout_filter_mask_server/load_map 을 호출 (재시작 불필요)
        common = {'use_sim_time': use_sim_time}
        actions += [
            Node(package='nav2_map_server', executable='map_server', name='keepout_filter_mask_server',
                 namespace=ns, output='screen',
                 parameters=[common, {'yaml_filename': mask, 'topic_name': mask_topic, 'frame_id': 'map'}]),
            Node(package='nav2_map_server', executable='costmap_filter_info_server',
                 name='keepout_costmap_filter_info_server', namespace=ns, output='screen',
                 parameters=[common, {'type': 0, 'filter_info_topic': info_topic, 'mask_topic': mask_topic,
                                      'base': 0.0, 'multiplier': 1.0}]),
            Node(package='nav2_lifecycle_manager', executable='lifecycle_manager', name='lifecycle_manager_keepout',
                 namespace=ns, output='screen',
                 parameters=[common, {'autostart': True,
                                      'node_names': ['keepout_filter_mask_server', 'keepout_costmap_filter_info_server']}]),
        ]
    return actions


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('namespace', default_value='', description='로봇 네임스페이스 (tb3_01). 네임스페이스 없는 로봇은 생략'),
        DeclareLaunchArgument('frame_prefix', default_value='', description="TF 프레임 접두사 (TurtleBot3: 'tb3_01/', Pinky: '')"),
        DeclareLaunchArgument('robot_type', default_value='auto', description='turtlebot3 | pinky | auto (DB 로봇 목록에서 네임스페이스로 찾음)'),
        DeclareLaunchArgument('stamped', default_value='auto', description='cmd_vel 을 TwistStamped 로: auto (종류로: TurtleBot3 true, Pinky false) | true | false'),
        DeclareLaunchArgument('map', description='지도 이름(config/maps/<이름>.yaml) 또는 yaml 경로'),
        DeclareLaunchArgument('keepout', default_value='auto', description="이동 구역 마스크: auto | off | yaml 경로"),
        DeclareLaunchArgument('params_file', default_value='auto', description='Nav2 파라미터: auto (종류별 config/nav2/burger.yaml | pinky.yaml) 또는 yaml 경로'),
        DeclareLaunchArgument('use_sim_time', default_value='False'),
        DeclareLaunchArgument('use_composition', default_value='True'),
        OpaqueFunction(function=launch_setup),
    ])
