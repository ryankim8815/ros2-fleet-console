"""Pinky Pro 를 네임스페이스로 실행하는 래퍼 launch (Pinky 로봇에서 실행).

pinky_bringup 의 bringup_robot.launch.xml 에는 namespace 인자가 없고, bringup.py 의 TF 프레임
이름(odom, base_footprint)이 코드에 고정되어 있습니다. 여러 대를 같은 ROS_DOMAIN_ID 에서 쓰면
토픽과 TF 가 겹치므로, 이 파일이
  1) 모든 노드를 /<namespace> 아래로 넣고 (PushRosNamespace)
  2) /tf, /tf_static 을 <namespace>/tf, <namespace>/tf_static 으로 바꿉니다 (SetRemap).
그래서 TF 프레임 이름은 그대로(odom, base_footprint) 두고, 로봇마다 다른 TF 토픽을 씁니다.
(Nav2 의 멀티 로봇 예제와 같은 방식. 이 로봇용 Nav2/AMCL 도 같은 네임스페이스·remap 으로 실행해야 함)

사용 (Pinky 에서):
  ros2 launch pinky_ns.launch.py namespace:=pinky_01
  ros2 launch pinky_ns.launch.py namespace:=pinky_01 led:=false emotion:=false

웹 관제센터 로봇 등록 값: 네임스페이스 /pinky_01, TF 토픽 /pinky_01/tf, TF 접두사 (비움)

주의: 실제 Pinky Pro 에서 아직 시험하지 않았습니다. 공개 저장소(pinklab-art/pinky_pro)의
bringup_robot.launch.xml 과 노드 소스를 보고 작성했습니다.
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import AnyLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node, PushRosNamespace, SetRemap


def generate_launch_description():
    ns = LaunchConfiguration('namespace')
    bringup = os.path.join(get_package_share_directory('pinky_bringup'), 'launch', 'bringup_robot.launch.xml')

    group = GroupAction([
        PushRosNamespace(ns),
        SetRemap(src='/tf', dst='tf'),
        SetRemap(src='/tf_static', dst='tf_static'),
        # 안쪽 pinky_description/upload_robot.launch.py 도 'namespace' 인자를 선언하므로, 이 파일의
        # namespace:=pinky_01 이 그대로 새어 들어가면 robot_state_publisher 가 /pinky_01/pinky_01 에 뜨고
        # URDF 프레임에도 pinky_01/ 접두사가 붙습니다. include 안에서만 namespace 를 비워 막습니다
        # (네임스페이스는 위의 PushRosNamespace 가 이미 씌움). include 인자는 범위가 없어 GroupAction 으로 감쌈.
        GroupAction([
            IncludeLaunchDescription(AnyLaunchDescriptionSource(bringup),
                                     launch_arguments={'namespace': ''}.items()),
        ]),
        Node(package='pinky_led', executable='led_server', output='screen',
             condition=IfCondition(LaunchConfiguration('led'))),
        Node(package='pinky_emotion', executable='emotion_server', output='screen',
             condition=IfCondition(LaunchConfiguration('emotion'))),
    ])

    return LaunchDescription([
        DeclareLaunchArgument('namespace', default_value='pinky_01', description='로봇 네임스페이스 (앞에 / 없이)'),
        DeclareLaunchArgument('led', default_value='true', description='LED 서비스 서버 실행'),
        DeclareLaunchArgument('emotion', default_value='true', description='LCD 표정 서비스 서버 실행'),
        group,
    ])
