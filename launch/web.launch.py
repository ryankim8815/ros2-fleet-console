"""로봇 관제센터 웹서버 실행.

PC 에서 실행합니다 (로봇 Pi 에는 부담을 주지 않음).
  - rosbridge_websocket (+ rosapi): 브라우저 <-> ROS 2 (WebSocket, 기본 9090)
  - serve.py: ros2-fleet-console/web 정적 파일 + /api/fleet, /api/control (기본 8080)

사용:  pixi run web
       pixi run web http_port:=8000 ws_port:=9091
       pixi run web bind:=127.0.0.1          # 이 PC 에서만 접속 허용 (웹·rosbridge 모두)
       pixi run web bind:=192.168.219.10     # 특정 네트워크 카드(실험실 공유기 쪽)로만 열기
"""

import os
import sys

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, IncludeLaunchDescription, LogInfo
from launch.launch_description_sources import AnyLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from ament_index_python.packages import get_package_share_directory

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)), '..'))
WEB_DIR = os.path.join(APP_DIR, 'web')
SERVE_PY = os.path.join(APP_DIR, 'serve.py')


def generate_launch_description():
    http_port = LaunchConfiguration('http_port')
    ws_port = LaunchConfiguration('ws_port')
    bind = LaunchConfiguration('bind')

    rosbridge = IncludeLaunchDescription(
        AnyLaunchDescriptionSource(os.path.join(
            get_package_share_directory('rosbridge_server'), 'launch', 'rosbridge_websocket_launch.xml')),
        # respawn: rosapi 가 예외로 종료돼도 다시 띄움. address: 열어 둘 네트워크 주소
        launch_arguments={'port': ws_port, 'respawn': 'true', 'address': bind}.items(),
    )

    http = ExecuteProcess(
        cmd=[sys.executable, SERVE_PY, '--port', http_port, '--bind', bind, '--dir', WEB_DIR],
        output='screen',
        name='tb3_web_http',
    )

    return LaunchDescription([
        DeclareLaunchArgument('http_port', default_value='8080', description='웹 페이지 포트'),
        DeclareLaunchArgument('ws_port', default_value='9090', description='rosbridge WebSocket 포트'),
        DeclareLaunchArgument('bind', default_value='0.0.0.0', description='열어 둘 주소 (127.0.0.1 = 이 PC 만)'),
        rosbridge,
        http,
        LogInfo(msg=['로봇 관제센터: http://localhost:', http_port, '/fleet.html  (rosbridge ws 포트 ', ws_port, ', bind ', bind, ')']),
    ])
