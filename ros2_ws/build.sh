#!/usr/bin/env bash
# 프로젝트 ROS 환경(.robostack/jazzy, Python 3.14)용 인터페이스 패키지 빌드.
#
# rosbridge 가 로봇의 서비스·토픽 타입을 알려면 그 타입의 Python 모듈이 PC 의 ROS 환경에 있어야 합니다.
# RoboStack 에 있는 패키지(turtlebot3_msgs, nav2 등)는 conda 로 설치하고(README "ROS 환경"),
# RoboStack 에 없는 것(예: Pinky 의 pinky_interfaces)만 소스를 src/ 에 넣어 여기서 빌드합니다.
#
#   ./ros2_ws/build.sh          src/ 의 패키지를 모두 빌드해 ros2_ws/install 에 설치
#   (bin/ros2 가 ros2_ws/install 을 자동으로 불러옵니다)
#
# colcon 을 쓰지 않는 이유: 시스템 colcon(Python 3.12)과 환경의 Python 3.14 모듈이 섞이고,
# 셸에 남은 /opt/ros 경로가 빌드에 끼어들 수 있어서. 메시지 패키지만 있으므로 cmake 로 직접 빌드합니다.
set -euo pipefail

ws="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$ws/.." && pwd)"
ros_prefix="$project_root/.robostack/jazzy"
conda_exe="${CONDA_EXE:-$(command -v conda || echo /home/kernel/anaconda3/bin/conda)}"

# 빌드할 패키지 (src/<이름>). 다른 패키지에 기대는 것이 생기면 여기 순서를 맞추세요. 예: packages=(pinky_interfaces)
packages=()
[[ ${#packages[@]} -gt 0 ]] || { echo "빌드할 패키지가 없습니다 (build.sh 의 packages 목록)"; exit 0; }

for pkg in "${packages[@]}"; do
  [[ -f "$ws/src/$pkg/package.xml" ]] || { echo "없음: $ws/src/$pkg" >&2; exit 1; }
  echo "=== $pkg"
  # 깨끗한 환경에서: 환경 자체 경로만 (셸의 /opt/ros 경로·PYTHONPATH 를 지움)
  env -u AMENT_PREFIX_PATH -u CMAKE_PREFIX_PATH -u PYTHONPATH -u LD_LIBRARY_PATH -u COLCON_PREFIX_PATH \
    "$conda_exe" run --no-capture-output --prefix "$ros_prefix" bash -c '
      set -euo pipefail
      export CMAKE_PREFIX_PATH="$CONDA_PREFIX:$1/install"
      cmake -S "$1/src/$2" -B "$1/build/$2" -DCMAKE_INSTALL_PREFIX="$1/install" -DCMAKE_BUILD_TYPE=Release \
            -DBUILD_TESTING=OFF -DPython3_EXECUTABLE="$CONDA_PREFIX/bin/python" -DPYTHON_EXECUTABLE="$CONDA_PREFIX/bin/python"
      cmake --build "$1/build/$2" -j"$(nproc)"
      cmake --install "$1/build/$2"
    ' _ "$ws" "$pkg"
done
echo "완료: $ws/install (bin/ros2 가 자동으로 불러옴, 웹 서버를 다시 켜야 rosbridge 에 반영)"
