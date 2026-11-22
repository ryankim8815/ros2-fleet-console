"""로봇 한 대의 Nav2 를 네임스페이스로 실행 (PC 에서, 로봇마다 한 번씩).

nav2_bringup/bringup_launch.py 를 namespace + use_namespace:=True 로 감싸고, 파라미터 파일을 로봇에 맞게 고칩니다.
  - 프레임 이름(odom, base_link, base_footprint, base_scan)에 frame_prefix 를 붙임 (TurtleBot3: 'tb3_01/', Pinky: '')
  - '/scan', '/local_costmap/...' 같은 절대 토픽을 상대 이름으로 바꿔 네임스페이스 안을 보게 함
  - enable_stamped_cmd_vel 을 로봇에 맞춤 (TurtleBot3: true = TwistStamped, Pinky: false = Twist)
Nav2 는 네임스페이스 모드에서 /tf 를 <ns>/tf 로 읽으므로, 로봇도 TF 를 <ns>/tf 로 보내야 합니다
(TurtleBot3: tb3_ns.launch.py, Pinky: pinky_ns.launch.py).

사용 (PC, pixi 환경):
  pixi run ros2 launch ros2-fleet-console/robot/nav2_ns.launch.py namespace:=tb3_01 frame_prefix:=tb3_01/ map:=$HOME/map.yaml
  pixi run ros2 launch ros2-fleet-console/robot/nav2_ns.launch.py namespace:=pinky_01 stamped:=false map:=$HOME/map.yaml

그 다음 웹 플릿 화면에서 로봇을 고르고 [초기 위치] 로 AMCL 초기 자세를 준 뒤 목표를 지정합니다.
지도는 로봇마다 <ns>/map 으로 나오므로 웹 로봇 등록의 '지도 토픽'을 그중 하나(/tb3_01/map 등)로 둡니다.
"""

import os
import tempfile

import yaml
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, OpaqueFunction
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration

FRAMES = {'odom', 'base_link', 'base_footprint', 'base_scan', 'rplidar_link'}


def rewrite_params(src, prefix, stamped):
    """Return path of a rewritten copy of the Nav2 params file."""
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
                if key == 'enable_stamped_cmd_vel':
                    node[k] = stamped
                    changes.append((p, v, stamped))
                elif isinstance(v, str) and (key.endswith('frame') or key.endswith('frame_id')) and v in FRAMES and prefix:
                    node[k] = prefix + v
                    changes.append((p, v, node[k]))
                elif isinstance(v, str) and ('topic' in key) and v.startswith('/'):
                    node[k] = v.lstrip('/')
                    changes.append((p, v, node[k]))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f'{path}[{i}]')

    walk(data, '')
    fd, out = tempfile.mkstemp(prefix='nav2_ns_', suffix='.yaml')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        yaml.safe_dump(data, f, sort_keys=False)
    return out, changes


def launch_setup(context):
    ns = LaunchConfiguration('namespace').perform(context).strip('/')
    prefix = LaunchConfiguration('frame_prefix').perform(context)
    stamped = LaunchConfiguration('stamped').perform(context).lower() in ('true', '1')
    params, changes = rewrite_params(LaunchConfiguration('params_file').perform(context), prefix, stamped)
    print(f'[nav2_ns] namespace=/{ns} frame_prefix={prefix!r} stamped={stamped} params={params} ({len(changes)} values rewritten)')
    bringup = os.path.join(get_package_share_directory('nav2_bringup'), 'launch', 'bringup_launch.py')
    return [IncludeLaunchDescription(
        PythonLaunchDescriptionSource(bringup),
        launch_arguments={
            'namespace': ns,
            'use_namespace': 'True',
            'map': LaunchConfiguration('map').perform(context),
            'params_file': params,
            'use_sim_time': LaunchConfiguration('use_sim_time').perform(context),
            'autostart': 'True',
            'use_composition': LaunchConfiguration('use_composition').perform(context),
        }.items(),
    )]


def generate_launch_description():
    default_params = os.path.join(get_package_share_directory('turtlebot3_navigation2'), 'param', 'burger.yaml')
    return LaunchDescription([
        DeclareLaunchArgument('namespace', description='로봇 네임스페이스 (tb3_01)'),
        DeclareLaunchArgument('frame_prefix', default_value='', description="TF 프레임 접두사 (TurtleBot3: 'tb3_01/', Pinky: '')"),
        DeclareLaunchArgument('stamped', default_value='true', description='cmd_vel 을 TwistStamped 로 (Pinky 는 false)'),
        DeclareLaunchArgument('map', description='지도 yaml 경로'),
        DeclareLaunchArgument('params_file', default_value=default_params, description='Nav2 파라미터 (기본: turtlebot3_navigation2 burger.yaml)'),
        DeclareLaunchArgument('use_sim_time', default_value='False'),
        DeclareLaunchArgument('use_composition', default_value='True'),
        OpaqueFunction(function=launch_setup),
    ])
