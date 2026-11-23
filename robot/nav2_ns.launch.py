"""로봇 한 대의 Nav2 를 네임스페이스로 실행 (PC 에서, 로봇마다 한 번씩).

nav2_bringup/bringup_launch.py 를 namespace + use_namespace:=True 로 감싸고, 파라미터 파일을 로봇에 맞게 고칩니다.
  - 프레임 이름(odom, base_link, base_footprint, base_scan)에 frame_prefix 를 붙임 (TurtleBot3: 'tb3_01/', Pinky: '')
  - '/scan', '/local_costmap/...' 같은 절대 토픽을 상대 이름으로 바꿔 네임스페이스 안을 보게 함
  - enable_stamped_cmd_vel 을 로봇에 맞춤 (TurtleBot3: true = TwistStamped, Pinky: false = Twist)
  - 이동 구역 마스크가 있으면 Nav2 KeepoutFilter 를 켜고 마스크 서버를 함께 띄움
Nav2 는 네임스페이스 모드에서 /tf 를 <ns>/tf 로 읽으므로, 로봇도 TF 를 <ns>/tf 로 보내야 합니다
(TurtleBot3: tb3_ns.launch.py, Pinky: pinky_ns.launch.py).

사용 (PC, pixi 환경):
  pixi run ros2 launch ros2-fleet-console/robot/nav2_ns.launch.py namespace:=tb3_01 frame_prefix:=tb3_01/ map:=lab
  pixi run ros2 launch ros2-fleet-console/robot/nav2_ns.launch.py namespace:=pinky_01 stamped:=false map:=lab

  map:=lab        → ros2-fleet-console/config/maps/lab.yaml (웹 '지도 작성'으로 저장한 이름) 또는 yaml 경로
  keepout:=auto   → config/masks/lab/<ns>.yaml 이 있으면 사용 (웹 '이동 구역'에서 저장하면 생김), keepout:=off 로 끔

그 다음 웹 플릿 화면에서 로봇을 고르고 [초기 위치] 로 AMCL 초기 자세를 준 뒤 목표를 지정합니다.
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
from nav2_params import keepout_topics, rewrite_params  # noqa: E402


def resolve_map(arg):
    if arg.endswith('.yaml') or '/' in arg:
        return os.path.abspath(os.path.expanduser(arg)), os.path.splitext(os.path.basename(arg))[0]
    return os.path.join(APP_DIR, 'config', 'maps', f'{arg}.yaml'), arg


def launch_setup(context):
    ns = LaunchConfiguration('namespace').perform(context).strip('/')
    prefix = LaunchConfiguration('frame_prefix').perform(context)
    stamped = LaunchConfiguration('stamped').perform(context).lower() in ('true', '1')
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

    params, changes = rewrite_params(LaunchConfiguration('params_file').perform(context), prefix, stamped, bool(mask), ns)
    info_topic, mask_topic = keepout_topics(ns)
    use_sim_time = LaunchConfiguration('use_sim_time').perform(context).lower() == 'true'
    actions = [LogInfo(msg=f'[nav2_ns] namespace=/{ns} frame_prefix={prefix!r} stamped={stamped} map={map_yaml} '
                           f'keepout={mask or "없음"} ({len(changes)} values rewritten)')]
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
    default_params = os.path.join(APP_DIR, 'config', 'nav2', 'burger.yaml')
    if not os.path.exists(default_params):
        default_params = os.path.join(get_package_share_directory('turtlebot3_navigation2'), 'param', 'burger.yaml')
    return LaunchDescription([
        DeclareLaunchArgument('namespace', description='로봇 네임스페이스 (tb3_01)'),
        DeclareLaunchArgument('frame_prefix', default_value='', description="TF 프레임 접두사 (TurtleBot3: 'tb3_01/', Pinky: '')"),
        DeclareLaunchArgument('stamped', default_value='true', description='cmd_vel 을 TwistStamped 로 (Pinky 는 false)'),
        DeclareLaunchArgument('map', description='지도 이름(config/maps/<이름>.yaml) 또는 yaml 경로'),
        DeclareLaunchArgument('keepout', default_value='auto', description="이동 구역 마스크: auto | off | yaml 경로"),
        DeclareLaunchArgument('params_file', default_value=default_params, description='Nav2 파라미터 (기본: config/nav2/burger.yaml)'),
        DeclareLaunchArgument('use_sim_time', default_value='False'),
        DeclareLaunchArgument('use_composition', default_value='True'),
        OpaqueFunction(function=launch_setup),
    ])
