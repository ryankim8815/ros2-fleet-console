"""TurtleBot3 를 네임스페이스 + 로봇별 TF 토픽으로 실행하는 래퍼 launch (TurtleBot3 에서 실행).

turtlebot3_bringup 의 robot.launch.py 는 namespace:=X 를 주면 토픽은 /X/..., 프레임은 X/odom 처럼
접두사를 붙이지만 TF 는 전역 /tf 로 보냅니다. 그런데 Nav2 bringup 을 namespace 로 띄우면 Nav2 는
/X/tf 를 읽습니다. 그래서 Nav2 를 로봇별로 쓸 때는 이 파일로 TF 도 /X/tf, /X/tf_static 으로 보냅니다.

사용 (TurtleBot3 에서):
  ros2 launch tb3_ns.launch.py namespace:=tb3_01

웹 관제센터 등록 값: 네임스페이스 /tb3_01, TF 토픽 /tb3_01/tf, TF 접두사 tb3_01/
(Nav2 없이 쓸 때는 기존처럼 `robot.launch.py namespace:=tb3_01` + TF 토픽 /tf 로도 충분합니다)
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import SetRemap


def generate_launch_description():
    ns = LaunchConfiguration('namespace')
    usb_port = LaunchConfiguration('usb_port')
    robot = os.path.join(get_package_share_directory('turtlebot3_bringup'), 'launch', 'robot.launch.py')

    return LaunchDescription([
        DeclareLaunchArgument('namespace', default_value='tb3_01', description='로봇 네임스페이스 (앞에 / 없이)'),
        DeclareLaunchArgument('usb_port', default_value='/dev/ttyACM0', description='OpenCR 포트'),
        GroupAction([
            # robot.launch.py 가 PushRosNamespace(namespace) 를 하므로 상대 이름 'tf' 는 /<ns>/tf 가 됨
            SetRemap(src='/tf', dst='tf'),
            SetRemap(src='/tf_static', dst='tf_static'),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(robot),
                launch_arguments={'namespace': ns, 'usb_port': usb_port}.items(),
            ),
        ]),
    ])
