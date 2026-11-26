"""여러 로봇이 함께 지도 만들기 (PC 에서). 웹의 '지도 만들기 모드'가 serve.py 를 통해 실행합니다.

로봇마다:
  - slam_toolbox (mapping_ns.launch.py): 지도 /<ns>/map, 프레임 '<ns>/map' (로봇끼리 겹치지 않게)
  - explore:=true 이면 Nav2 navigation(지도 서버·AMCL 없이) + explore_lite 로 미탐색 경계를 스스로 찾아감
    (TF 를 /<ns>/tf 로 보내는 로봇만: tb3_ns.launch.py, pinky_ns.launch.py)
그리고 한 번만:
  - map_merge_node.py: 로봇 지도들을 시작 자세로 맞춰 /map_merged (프레임 'map') 로 합침

robots (JSON): [{"ns":"/tb3_01","prefix":"tb3_01/","tf":"/tb3_01/tf","type":"turtlebot3","x":0,"y":0,"yaw":0}, ...]
  x, y, yaw: 공통 지도('map') 기준 그 로봇의 시작 자세. 처음 지도를 만들 때는 기준이 되는 로봇을 (0,0,0) 으로.

직접 실행:
  pixi run ros2 launch ros2-fleet-console/robot/coop_mapping.launch.py robots:='[...]' explore:=false
"""

import json
import os
import sys
import tempfile

import yaml
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (DeclareLaunchArgument, GroupAction, ExecuteProcess, IncludeLaunchDescription,
                            LogInfo, OpaqueFunction, TimerAction)
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node, PushROSNamespace

HERE = os.path.dirname(os.path.realpath(__file__))
APP_DIR = os.path.abspath(os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
from nav2_params import nav_radius_for_type, params_for_type, rewrite_params, stamped_for_type  # noqa: E402


def launch_setup(context):
    robots = json.loads(LaunchConfiguration('robots').perform(context))
    explore = LaunchConfiguration('explore').perform(context).lower() in ('true', '1')
    nav2_params = LaunchConfiguration('params_file').perform(context)
    actions = [LogInfo(msg=f'[coop_mapping] 로봇 {len(robots)}대, 자동 탐색={explore}')]

    for r in robots:
        ns = r['ns'].strip('/')
        prefix, tf = r.get('prefix', ''), r.get('tf', '/tf')
        slam_frame = f'{ns}/map' if ns else 'map'
        actions.append(IncludeLaunchDescription(
            PythonLaunchDescriptionSource(os.path.join(HERE, 'mapping_ns.launch.py')),
            launch_arguments={'namespace': ns, 'frame_prefix': prefix, 'tf_topic': tf, 'map_frame': slam_frame}.items()))
        if not explore:
            continue
        if tf == '/tf':
            actions.append(LogInfo(msg=f'[coop_mapping] /{ns}: TF 가 전역 /tf 라 자동 탐색을 건너뜀 (tb3_ns.launch.py 로 bringup 필요)'))
            continue
        # 파라미터 파일: auto 면 로봇 종류별 (TurtleBot3 burger.yaml, Pinky pinky.yaml)
        src = params_for_type(r.get('type')) if nav2_params in ('', 'auto') else nav2_params
        params, _ = rewrite_params(src, prefix, stamped_for_type(r.get('type')), False, ns, map_frame=slam_frame,
                                   nav_radius=nav_radius_for_type(r.get('type')))
        # navigation_launch.py 는 네임스페이스를 직접 씌우지 않으므로(bringup_launch 가 하던 일) 여기서 PushROSNamespace
        nav = GroupAction([PushROSNamespace(ns), IncludeLaunchDescription(
            PythonLaunchDescriptionSource(os.path.join(get_package_share_directory('nav2_bringup'), 'launch', 'navigation_launch.py')),
            launch_arguments={'namespace': ns, 'params_file': params, 'use_sim_time': 'False', 'autostart': 'True',
                              'use_composition': 'False'}.items())])
        explore_node = Node(
            package='explore_lite', executable='explore', name='explore_node', namespace=ns, output='screen',
            parameters=[{'robot_base_frame': f'{prefix}base_link', 'costmap_topic': 'map', 'costmap_updates_topic': 'map_updates',
                         'visualize': False, 'planner_frequency': 0.15, 'progress_timeout': 30.0, 'potential_scale': 3.0,
                         'orientation_scale': 0.0, 'gain_scale': 1.0, 'transform_tolerance': 0.3, 'min_frontier_size': 0.3,
                         'return_to_init': False, 'use_sim_time': False}],
            remappings=[('/tf', tf), ('/tf_static', f'{tf}_static')])
        actions.append(nav)
        actions.append(TimerAction(period=15.0, actions=[explore_node]))   # Nav2 가 뜬 뒤 탐색 시작

    # 지도 합치기 (파라미터 JSON 은 문자열로 넘기려고 파일로)
    fd, pfile = tempfile.mkstemp(prefix='map_merge_', suffix='.yaml')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        yaml.safe_dump({'map_merge': {'ros__parameters': {'robots': json.dumps(
            [{'ns': r['ns'], 'x': float(r.get('x', 0)), 'y': float(r.get('y', 0)), 'yaw': float(r.get('yaw', 0))} for r in robots]),
            'rate': 1.0}}}, f)
    actions.append(ExecuteProcess(cmd=[sys.executable, os.path.join(HERE, 'map_merge_node.py'), '--ros-args', '--params-file', pfile],
                                  name='map_merge', output='screen'))
    return actions


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('robots', description='JSON 목록 (위 설명)'),
        DeclareLaunchArgument('explore', default_value='false', description='로봇마다 explore_lite 로 자동 탐색'),
        DeclareLaunchArgument('params_file', default_value='auto', description='auto (로봇 종류별 config/nav2/burger.yaml | pinky.yaml) 또는 yaml 경로'),
        OpaqueFunction(function=launch_setup),
    ])
