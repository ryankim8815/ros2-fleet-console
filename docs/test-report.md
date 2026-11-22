# 로봇 없이 한 시험 결과 (2026-11-23)

실제 TurtleBot3·Pinky Pro 없이, 이 PC 에서 할 수 있는 항목을 고치고 시험한 결과입니다.
**실제 로봇에서 남는 확인**은 마지막 열과 [아래 표](#실제-로봇에서-남은-확인)에 정리했습니다.

## 시험 환경

| 구성 | 내용 |
| --- | --- |
| PC | Ubuntu 26.04, pixi(RoboStack) ROS 2 Jazzy, rosbridge 2.7.1, ROS_DOMAIN_ID 30 |
| 가짜 TurtleBot3 | Python 노드. `/<ns>/odom`·`scan`·`battery_state`·`motor_power`·`reset_odometry`, TwistStamped 구독, 프레임 `<ns>/odom` 등, LiDAR 고정 TF(base_scan x = −0.032 m) |
| 가짜 Pinky Pro | 이름과 구조를 실제와 같게 만든 `pinky_bringup`·`pinky_led`·`pinky_emotion` 패키지 (실제 launch 파일 이름, 노드 이름, 상대 토픽 이름, 고정 프레임 `odom`/`base_footprint`/`rplidar_link`). LED·표정 서버는 **실제 `pinky_interfaces`** 타입 사용 |
| Nav2 | `turtlebot3_navigation2/param/burger.yaml` + 방 모양 지도. 가짜 로봇 LiDAR 는 같은 방을 레이캐스트 |
| 브라우저 | 앱 안의 Chromium 브라우저 패널 |

## 결과

| # | 항목 | 한 일 | 시험 방법 | 결과 | 실제 로봇에서 남는 확인 |
|---|---|---|---|---|---|
| 1 | Pinky 래퍼 TF remap | (변경 없음, 검증) | `pinky_ns.launch.py namespace:=pinky_01` 을 가짜 패키지로 실행, `/tf`·`/pinky_01/tf` 등을 구독 | ✅ 모든 노드·토픽·서비스가 `/pinky_01` 아래. TF `odom→base_footprint` 는 `/pinky_01/tf`, 고정 TF 는 `/pinky_01/tf_static` 에만 있고 `/tf` 는 비어 있음 | 실제 `pinky_bringup` (dynamixel, sllidar) |
| 2 | Pinky LED·표정 웹 호출 | 공개 저장소의 `pinky_interfaces` 를 `ros2_ws/src` 에 넣어 빌드 | 플릿 화면 장치 패널에서 LED 파랑 → 표정 happy → 밝기 35 → LED 끄기 | ✅ 가짜 서버가 `fill rgb=(0,80,255)`, `happy`, `35`, `clear` 를 받음. 모바일 표정 버튼도 동작 | 실제 LED·LCD 반응 |
| 3 | TurtleBot3 네임스페이스 프레임 | (검증) | PC 에서 실제 `turtlebot3_state_publisher.launch.py namespace:=tb3_01` | ✅ `tb3_01/base_footprint→tb3_01/base_link→tb3_01/base_scan` 등. LiDAR 드라이버 소스(`ld08_driver` main.cpp)도 `namespace/base_scan` 으로 접두사 | 하드웨어 노드 (PC 에 `ld08_driver` 가 없어 bringup 전체는 못 띄움) |
| 4 | TurtleBot3 와 Nav2 TF 불일치 | `robot/tb3_ns.launch.py` 추가 (TF 를 `/<ns>/tf` 로) | 같은 구조(SetRemap + 네임스페이스 + 포함된 launch)로 state publisher 실행 | ✅ remap 없으면 `/tf_static`, 있으면 `/tb3_02/tf_static` 에만 나옴 | 실제 bringup |
| 5 | 로봇별 Nav2·AMCL | `robot/nav2_ns.launch.py` 추가 (프레임 접두사, 절대 토픽 → 상대, cmd_vel stamped 여부). 플릿에 **초기 위치** 버튼, 등록 화면에 **공유 지도 토픽** 칸 | 가짜 TurtleBot3(접두사 프레임) + 가짜 Pinky(래퍼) 에 Nav2 2벌 동시 실행 → 웹에서 초기 위치 → 두 로봇에 목표 | ✅ 두 Nav2 모두 활성, AMCL 이 (−1.50, 0.00)·(0.00, 0.00) 로 위치 추정, 두 로봇 모두 "도착" (Pinky 목표 (1,1) → (0.95, 0.96)) | 실제 센서 잡음에서 위치 추정, Pinky 용 Nav2 파라미터 |
| 6 | 여러 브라우저 동시 조종 | 서버 조종 권한(3초 lease, 1초 갱신, 손 떼면 해제, 넘겨받기) | 탭 A 가 전진 중에 탭 B 가 회전 시도 → B 넘겨받기 | ✅ B 는 거부 + "PC 4kz6 이(가) 조종 중" 알림, 로봇은 A 명령만 따름. 넘겨받기 후 B 명령(ω 1.5)만 반영, A 는 권한을 잃고 입력이 자동 해제 | 없음 |
| 7 | 로봇을 페이지보다 늦게 켬 | 온라인이 될 때 cmd_vel 타입·서비스 다시 확인, 빈 결과면 2초 간격 5회 재시도 | 처음 보는 로봇 `/tb3_09`(등록 종류 TurtleBot3 = 기본 TwistStamped, 실제 Twist 구독)를 페이지를 연 뒤에 실행 | ✅ Twist 로 바뀌고 조종됨 (v 0.15 → 손 떼면 0). 첫 시도에서 재시도가 없어 실패하던 것을 고침 | 없음 |
| 8 | 연결 불안정 상태 | `ok` / `unstable`(5초 안 1초 넘는 공백) / `offline`(5초 넘게 없음) | 10초마다 2초씩 odom 을 끊는 가짜 Pinky | ✅ "정상" ↔ "연결 불안정" 으로 바뀌고 오프라인으로 잘못 뜨지 않음 | 실제 Wi-Fi 에서 기준 값(1초·5초) 조정 |
| 9 | 이벤트를 로봇별로 구분 | (검증) | 네임스페이스 노드 3개에서 로그 → `/rosout` | ✅ 로거 이름이 `pinky_01.pinky_bringup`, `tb3_01.amcl` 형태 — 웹의 구분 규칙과 일치. Nav2 시험에서도 `tb3_01.collision_monitor` 경고가 tb3-01 로 표시됨 | 없음 |
| 10 | 배터리 계산 | percent 가 0–1 인지 0–100 인지 전압 추정치로 판별, 로봇별 전압 범위 | 9가지 경우를 브라우저에서 계산 | ✅ 예전 코드가 100% 로 잘못 보이던 "10.0 V, percent=1" 이 1% + 저전압 경고. 0.7 → 70%, 70 → 70%, 전압만 → 추정 | 실제 로봇의 percent 단위·전압 범위 (Pinky 2S 6.4–8.4 V 는 추정) |
| 11 | 비상정지 경로 | (측정) | 로봇 4대 주행 중 전체 비상정지 5회, 로봇이 0 속도를 받은 시각 기록 | ✅ 20회 모두 수신, 중앙값 5.0 ms, 최대 97.8 ms (같은 PC 안) | Wi-Fi 지연, 모터 제동 거리 |
| 12 | 로봇 수와 통신량 | 플릿 화면: odom 서버에서 10 Hz 로, LiDAR 는 2초 (선택 로봇을 볼 때만 0.2초) | 가짜 로봇 10대, 10초간 수신량·메시지 수·왕복 지연 | ✅ 523 → **214 KB/s** (−59%), 545 → 334 msg/s, 왕복 지연 중앙값 8 ms, 긴 작업(long task) 0 | Wi-Fi·휴대폰 브라우저 |
| 13 | 지도 전송 크기 | rosbridge PNG 압축 사용, 브라우저에서 풀기 | 1000×1000 가짜 지도를 압축 없이/있게 받기 | ✅ 2,969,493 → **43,578 바이트** (68배), 받기까지 293 → 167 ms, 칸 100만 개 정확히 복원 | 없음 |
| 14 | 보안 | 로봇 목록 저장에 관리자 토큰(`config/admin_token`, 600), 다른 출처의 쓰기 요청 거부, `bind:=` 인자 | curl 과 등록 화면 | ✅ 토큰 없음·틀림 → 401, 다른 출처 → 403, 맞는 토큰 → 200, `../config/admin_token` 류 경로 → 404 (토큰 노출 없음). 등록 화면은 401 이면 토큰을 묻고, 틀리면 저장하지 않음 | rosbridge(9090) 자체에는 인증 없음 — 네트워크로 제한 |
| 15 | 화면 보완 | 로봇 간 최소 거리 (0.5 m 미만 빨강 + 이벤트), LiDAR 를 고정 TF 위치에서 그림 | 가짜 로봇 2대를 가깝게 이동, 고정 TF 오프셋 계산 확인 | ✅ 0.62 m → 노랑, 0.49 m → 빨강 + "0.50 m 까지 가까워졌습니다" 이벤트. 오프셋 TB3 −0.032 m, Pinky +0.02 m | 없음 |
| 16 | 문서 Mermaid | (검증) | 문서의 다이어그램 15개를 Mermaid 11.4.1 로 해석·렌더 | ✅ 15개 모두 성공, `&lt;ns&gt;` 가 `<ns>` 로 표시 | 없음 |

## 시험 중 발견해서 고친 문제

| 문제 | 원인 | 수정 |
| --- | --- | --- |
| 오류 응답(404 등)에서 서버가 빈 응답을 보냄 | 로그 필터가 첫 인자를 문자열로 가정 | `str()` 로 비교 (`serve.py`) |
| 늦게 켜진 로봇의 cmd_vel 타입이 안 바뀜 | 로봇이 막 뜬 순간에는 rosapi 가 구독자를 아직 못 봐 빈 결과 | 빈 결과면 재시도 (`robot.js`) |
| TurtleBot3 의 TF 토픽 기본값이 `/tf` 로만 채워짐 | 등록 화면이 `tb3_ns` 사용을 몰랐음 | 자동 검색이 `<ns>/tf` 존재 여부로 실제 값을 채움 (`setup.js`) |
| 시험 프로세스가 고아로 남음 | 시험 스크립트 부모만 종료됨 | 시험 스크립트에 `trap 'kill 0'` (앱 코드와 무관) |

## 실제 로봇에서 남은 확인

| 항목 | 이유 |
| --- | --- |
| Pinky Pro 실제 bringup 을 `pinky_ns.launch.py` 로 | 가짜 패키지로만 확인 |
| TurtleBot3 실제 bringup 을 `tb3_ns.launch.py` 로 (`turtlebot3_node`, `ld08_driver` 포함) | PC 에 하드웨어·드라이버 없음 |
| 실제 센서로 AMCL·Nav2 주행, Pinky 용 Nav2 파라미터 | 가짜 LiDAR 는 잡음 없는 레이캐스트 |
| 배터리 percent 단위, Pinky 배터리 전압 범위 | 실제 값 필요 |
| Wi-Fi 에서 비상정지 지연·연결 불안정 기준·통신량 | 시험은 같은 PC 안 |
| Pinky 속도 제한(0.2 m/s, 2.0 rad/s) 조정 | 주행감 확인 필요 |
| 실제 LED·LCD 반응 | 하드웨어 필요 |

시험에 쓴 가짜 로봇과 스크립트는 저장소에 넣지 않았습니다. `ros2-fleet-console/config/fleet.json` 은 시험 전 상태(burger-01 1대)로 되돌려 두었습니다.

---

# 추가 시험: 지도 작성 · 이동 구역 (2026-11-23)

가짜 TurtleBot3(`/tb3_01`, TF `/tb3_01/tf`, 프레임 접두사) + 가짜 Pinky(래퍼) 로 시험했습니다.

| # | 항목 | 결과 |
| --- | --- | --- |
| 1 | 지도가 없을 때 플릿 화면 안내 | ✅ "아직 지도가 없습니다 · 지도 작성 시작" |
| 2 | 웹에서 slam_toolbox 시작 (로봇 네임스페이스, TF 토픽 remap) | ✅ `/tb3_01/map` 수신, 지도 위 로봇 위치 표시 |
| 3 | 웹 조종으로 지도 넓히기 | ✅ 탐색 넓이 4.0 → 11.5 m² |
| 4 | 저장 + 활성 지도 지정 | ✅ `config/maps/lab.yaml|pgm` (image 상대 경로), `fleet.json` 의 `mapFile: lab` |
| 5 | 끝내기 | ✅ slam_toolbox 정상 종료, 남은 프로세스 없음 |
| 6 | 구역 그리기 (클릭 4번 + Enter), 로봇별 적용, 저장 | ✅ 지정한 로봇만 마스크에 반영 (pinky 702칸, tb3 0칸) |
| 7 | Nav2 keepout 필터 연결 | ✅ 처음엔 상대 토픽 이름이 `/<ns>/global_costmap/…` 로 풀려 마스크를 못 받음 → 절대 이름으로 고친 뒤 global·local 모두 수신 |
| 8 | 구역을 피하는 경로 | ✅ 계획 경로 7,000여 점 중 구역 안 0, 최소 간격 0.18~0.25 m. 로봇 궤적도 구역 안 0 |
| 9 | 구역 다시 저장 → 실행 중 Nav2 에 즉시 반영 | ✅ 웹에 "Nav2 에 바로 적용됨", Nav2 로그 "New filter mask arrived … Updating old filter mask" |
| 10 | 경계에서 멈춤 문제 | ✅ 금지 셀만 둔 마스크는 DWB 가 "모든 궤적 충돌"로 멈춤 → 금지 영역 확장도 같은 문제 → 비용 띠 마스크로 바꿈 |
| 11 | 웹 수동 조종 차단 (계산) | ✅ 9개 상황 중 예상대로 동작 (구역 쪽 직진 차단, 반대·회전·멀리서·빠져나오기·허용 구역 안쪽은 허용). 막을 때 실제로 0 을 보냄 |
| 12 | 웹 수동 조종 차단 (실제 주행) | ✅ 구역 쪽으로 최대 속도 명령을 6초간 계속 보내도 몸체 앞이 구역 경계 0.10 m 앞에서 멈춤, 알림 표시 |
| 13 | Nav2 로 목표까지 끝까지 도착 | ⚠️ 확인 못 함. 이 PC 의 가짜 로봇 환경이 느려(제어 루프 10 Hz 미달, 가짜 LiDAR 지연) **구역이 없어도** 진행 실패로 중단됨. 구역 관련 동작(경로·궤적이 구역을 피함)은 위에서 확인 |

시험 중 고친 것: keepout 토픽 절대 이름, 비용 띠 마스크, 저장된 지도 색(어두운 테마), 마스크·지도 파일 권한(600 → 644), 로봇 등록 저장 시 활성 지도가 지워지던 문제, 문서의 남은 옛 경로(`turtlebot3/app/`).

시험용 지도·구역·마스크·로그는 프로젝트에서 뺐고 `config/fleet.json` 은 시험 전 상태로 되돌렸습니다.
