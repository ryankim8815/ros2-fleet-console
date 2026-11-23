"""로봇 한 대로 지도 작성: slam_toolbox(online async)를 그 로봇의 네임스페이스로 실행 (PC 에서).

웹 '지도 작성' 화면이 serve.py 를 통해 이 파일을 실행합니다. 직접 실행해도 됩니다:
  pixi run ros2 launch ros2-fleet-console/robot/mapping_ns.launch.py namespace:=tb3_01 frame_prefix:=tb3_01/ tf_topic:=/tb3_01/tf
  pixi run ros2 launch ros2-fleet-console/robot/mapping_ns.launch.py namespace:=pinky_01 tf_topic:=/pinky_01/tf

  - 지도는 /<ns>/map 으로 나오고, map → <prefix>odom TF 를 로봇의 TF 토픽(tf_topic)으로 보냅니다.
  - LiDAR 는 /<ns>/scan, 프레임은 <prefix>odom, <prefix>base_footprint 를 씁니다.
  - 저장: 웹의 [지도 저장] (또는 map_saver_cli -t /<ns>/map -f <경로>)
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, EmitEvent, LogInfo, OpaqueFunction, RegisterEventHandler
from launch.events import matches_action
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import LifecycleNode
from launch_ros.event_handlers import OnStateTransition
from launch_ros.events.lifecycle import ChangeState
from lifecycle_msgs.msg import Transition


def launch_setup(context):
    ns = LaunchConfiguration('namespace').perform(context).strip('/')
    prefix = LaunchConfiguration('frame_prefix').perform(context)
    tf_topic = LaunchConfiguration('tf_topic').perform(context)
    params = os.path.join(get_package_share_directory('slam_toolbox'), 'config', 'mapper_params_online_async.yaml')

    remaps = []
    if tf_topic != '/tf':   # 로봇이 TF 를 /<ns>/tf 로 보내는 경우 (tb3_ns, pinky_ns)
        remaps = [('/tf', tf_topic), ('/tf_static', f'{tf_topic}_static')]

    slam = LifecycleNode(
        package='slam_toolbox', executable='async_slam_toolbox_node', name='slam_toolbox',
        namespace=ns, output='screen',
        parameters=[params, {
            'use_sim_time': LaunchConfiguration('use_sim_time').perform(context).lower() == 'true',
            'odom_frame': f'{prefix}odom',
            'base_frame': f'{prefix}base_footprint',
            'map_frame': LaunchConfiguration('map_frame').perform(context),   # 협동 지도 만들기: '<ns>/map'
            'scan_topic': 'scan',          # 상대 이름 → /<ns>/scan
            'map_name': 'map',             # 기본값 '/map' 대신 /<ns>/map
            'use_lifecycle_manager': False,
            'enable_interactive_mode': False,
            'map_update_interval': 2.0,
        }],
        remappings=remaps,
    )
    configure = EmitEvent(event=ChangeState(lifecycle_node_matcher=matches_action(slam),
                                            transition_id=Transition.TRANSITION_CONFIGURE))
    activate = RegisterEventHandler(OnStateTransition(
        target_lifecycle_node=slam, start_state='configuring', goal_state='inactive',
        entities=[LogInfo(msg=f'[mapping_ns] slam_toolbox 활성화: /{ns}'),
                  EmitEvent(event=ChangeState(lifecycle_node_matcher=matches_action(slam),
                                              transition_id=Transition.TRANSITION_ACTIVATE))]))
    return [LogInfo(msg=f'[mapping_ns] namespace=/{ns} frame_prefix={prefix!r} tf_topic={tf_topic}'), slam, configure, activate]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('namespace', description='로봇 네임스페이스 (tb3_01)'),
        DeclareLaunchArgument('frame_prefix', default_value='', description="TF 프레임 접두사 (TurtleBot3: 'tb3_01/')"),
        DeclareLaunchArgument('tf_topic', default_value='/tf', description='로봇의 TF 토픽 (/tf 또는 /<ns>/tf)'),
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument('map_frame', default_value='map',
                              description="SLAM 지도 프레임. 여러 대가 함께 그릴 때는 로봇마다 다르게 ('tb3_01/map')"),
        OpaqueFunction(function=launch_setup),
    ])
