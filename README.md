# 로봇 관제센터 (웹)

TurtleBot3 와 Pinky Pro 여러 대를 브라우저에서 관제하는 웹 앱입니다. **PC 에서** 실행하며, 웹 때문에 로봇에 추가로 설치하는 것은 없습니다.
여러 대는 **같은 ROS_DOMAIN_ID + 로봇별 네임스페이스**(`/tb3_01`, `/pinky_01` …)로 구분하고, rosbridge 하나로 모두 연결합니다.

```
브라우저 ──WebSocket(9090)── rosbridge + rosapi (PC) ──DDS(ROS_DOMAIN_ID=30)── /tb3_01/*  /pinky_01/*  /pinky_02/* …
        └─HTTP(8080)── ros2-fleet-console/serve.py (정적 파일 + /api/fleet)
```

구조와 데이터 흐름: [docs/architecture.md](docs/architecture.md)

## 실행

```bash
cd ~/turtlebot3
pixi run web                                # http://localhost:8080/fleet.html
pixi run web http_port:=8000 ws_port:=9091  # 포트 변경
pixi run web bind:=127.0.0.1                # 이 PC 에서만 접속 (웹·rosbridge 모두)
```

### 처음 시작: 지도 만들기 → 이동 구역 → 로봇별 Nav2

```mermaid
flowchart LR
    A["1. 로봇 bringup<br/>(네임스페이스)"] --> B["2. 지도 작성<br/>mapping.html<br/>로봇 한 대 + slam_toolbox"]
    B --> C["3. 지도 저장<br/>config/maps/이름<br/>→ 활성 지도"]
    C --> D["4. 이동 구역<br/>zones.html<br/>금지·허용 다각형, 로봇별"]
    D --> E["5. 로봇별 Nav2<br/>nav2_ns.launch.py map:=이름<br/>(구역 마스크 자동)"]
    E --> F["6. 플릿 화면<br/>초기 위치 → 목표"]
```

1. 로봇들을 네임스페이스로 bringup 합니다 (아래 표).
2. 플릿 화면에 지도가 없으면 **지도 작성 시작** 안내가 뜹니다. **지도 작성**에서 로봇 한 대를 고르고 **시작**하면 PC 에서 그 로봇용 slam_toolbox 가 뜹니다. 웹에서 천천히 조종해 공간을 한 바퀴 돈 뒤 이름을 붙여 **저장**하고(활성 지도로 지정), **끝내기**.
3. **이동 구역**에서 그 지도 위에 금지(빨강)·허용(초록) 구역을 그리고, 구역마다 적용할 로봇을 고른 뒤 **저장하고 적용**합니다.
4. 로봇마다 Nav2 를 띄웁니다. 활성 지도의 구역 마스크가 있으면 자동으로 Nav2 keepout 필터가 켜집니다.
   ```bash
   pixi run ros2 launch ros2-fleet-console/robot/nav2_ns.launch.py namespace:=tb3_01 frame_prefix:=tb3_01/ map:=lab
   pixi run ros2 launch ros2-fleet-console/robot/nav2_ns.launch.py namespace:=pinky_01 stamped:=false map:=lab
   ```
5. 플릿 화면에서 로봇마다 **초기 위치**를 찍고 **목표 지정**.

구역을 나중에 바꿔 저장하면 실행 중인 Nav2 에 바로 반영됩니다 (재시작 불필요).

### 로봇 쪽 (네임스페이스로 bringup)

자세한 순서·확인 방법·문제 해결: [robot/README.md](robot/README.md)

| 로봇 | 명령 | 웹 등록 값 (네임스페이스 / TF 토픽 / TF 접두사) |
| --- | --- | --- |
| TurtleBot3 + 로봇별 Nav2 | `ros2 launch tb3_ns.launch.py namespace:=tb3_01` ([robot/tb3_ns.launch.py](robot/tb3_ns.launch.py) 를 로봇에 복사) | `/tb3_01` / `/tb3_01/tf` / `tb3_01/` |
| TurtleBot3 (조종·모니터링만) | `ros2 launch turtlebot3_bringup robot.launch.py namespace:=tb3_01` | `/tb3_01` / `/tf` / `tb3_01/` |
| Pinky Pro | `ros2 launch pinky_ns.launch.py namespace:=pinky_01` ([robot/pinky_ns.launch.py](robot/pinky_ns.launch.py) 를 Pinky 에 복사) | `/pinky_01` / `/pinky_01/tf` / (비움) |
| 기존 단일 로봇 | 네임스페이스 없이 bringup | (비움) / `/tf` / (비움) |

- 모든 로봇과 PC 의 `ROS_DOMAIN_ID` 가 같아야 합니다 (현재 30).
- TurtleBot3 Jazzy bringup 은 `namespace` 인자를 지원하고, TF 프레임에 `tb3_01/` 접두사를 붙여 전역 `/tf` 로 보냅니다. 로봇별 Nav2 는 `<ns>/tf` 를 읽으므로 그때는 `tb3_ns.launch.py` 로 TF 도 `/tb3_01/tf` 로 보냅니다.
- Pinky Pro bringup 은 namespace 인자가 없고 TF 프레임 이름이 고정(`odom`, `base_footprint`)이라, 래퍼 launch 가 네임스페이스를 씌우고 TF 를 `/pinky_01/tf` 로 분리합니다. LED·표정 서버도 같은 네임스페이스로 함께 띄웁니다.
- 공유 지도 위에 로봇을 그리려면 각 로봇의 `map → <접두사>odom` TF 가 필요합니다 (로봇별 AMCL 또는 SLAM). 없으면 카드·표에는 보이지만 지도에는 "지도 위치 없음" 으로 표시됩니다.
- 로봇별 Nav2 는 PC 에서 `pixi run ros2 launch ros2-fleet-console/robot/nav2_ns.launch.py namespace:=tb3_01 frame_prefix:=tb3_01/ map:=...` (Pinky 는 `stamped:=false`) 로 띄우고, 플릿 화면의 **초기 위치**로 AMCL 을 시작합니다. 자세히: [robot/README.md 7장](robot/README.md#7-nav2--공유-지도)

### 로봇 등록

`로봇 등록` 페이지(`setup.html`)에서:
1. **네임스페이스 자동 검색** → 토픽 목록에서 `<ns>/odom` 을 찾아 종류(TurtleBot3/Pinky)를 추정합니다.
2. **폼에 채우기** → 이름·색 확인 → **연결 테스트**(토픽·서비스·TF 프레임 확인) → **목록에 추가** → **저장**.

저장하면 `ros2-fleet-console/config/fleet.json` 에 기록됩니다. 직접 편집해도 됩니다.
저장에는 **관리자 토큰**이 필요합니다. 처음 저장할 때 묻고 그 브라우저에 기억합니다. 토큰은 처음 실행할 때 `ros2-fleet-console/config/admin_token` 에 만들어지고(권한 600), `cat ~/turtlebot3/ros2-fleet-console/config/admin_token` 으로 확인합니다. 환경 변수 `TB3_WEB_TOKEN` 으로 정할 수도 있습니다.

## 페이지

| 페이지 | 기능 |
| --- | --- |
| `fleet.html` 플릿 | 로봇 카드(상태·배터리·작업·조종자), 공유 지도(로봇별 색, 경로, 목표, 선택 로봇 LiDAR, 로봇 간 최소 거리), 선택 로봇에 **초기 위치**·Nav2 목표, 종류별 장치 패널, 상태 비교 표, 전체 이벤트 |
| `index.html?robot=<id>` 로봇 대시보드 | 한 로봇의 실시간 뷰, WASD/버튼 조종, 위치·속도, 토픽 주기, 로그(이 로봇만 필터) |
| `interfaces.html` ROS 2 인터페이스 | 전체 노드·토픽·서비스·액션·파라미터 탐색과 호출 (쓰기 잠금) |
| `mobile.html?robot=<id>` 모바일 조종 | 로봇 전환 칩, 조이스틱, 미니 LiDAR, Pinky 표정·LED 바로가기 |
| `setup.html` 로봇 등록 | 위 "로봇 등록" 참고 |
| `mapping.html` 지도 작성 | 로봇 한 대 선택 → PC 에서 slam_toolbox 시작 → 웹 조종으로 지도 넓히기 → 저장(활성 지도 지정) → 끝내기 |
| `zones.html` 이동 구역 | 저장된 지도 위에 금지/허용 다각형 그리기, 로봇별 적용, 로봇별 미리보기, 저장 시 Nav2 keepout 마스크 생성·즉시 반영 |

### 이동 구역 규칙

| 구역 | 의미 | Nav2 (계획·주행) | 웹 수동 조종 |
| --- | --- | --- | --- |
| 금지 (keepout) | 안으로 들어가면 안 됨 | 마스크로 통과 불가 + 주변에 비용 띠(경계에 붙지 않게) | 0.6초 앞을 예측해 몸체가 구역에 닿을 명령은 0 으로 바꾸고 알림 |
| 허용 (allowed) | 이 구역들 **밖**이 모두 금지 | 위와 같음 (밖이 통과 불가) | 위와 같음 |

- 구역마다 "모든 로봇" 또는 특정 로봇에만 적용합니다. 로봇마다 `config/masks/<지도>/<네임스페이스>.yaml` 마스크가 만들어집니다.
- 이미 구역 안에 있는 로봇은 수동 조종으로 빠져나올 수 있게 막지 않습니다.
- 수동 조종 차단은 **이 웹에서 보낸 명령**에만 적용됩니다. 다른 teleop 이나 노드가 보내는 `cmd_vel` 은 막지 못합니다.

### 종류별 장치

| TurtleBot3 | Pinky Pro |
| --- | --- |
| 모터 전원 `<ns>/motor_power` (SetBool) · 사운드 `<ns>/sound` · odom 초기화 `<ns>/reset_odometry` | LED `<ns>/set_led` (fill/clear) · 밝기 `<ns>/set_brightness` · LCD 표정 `<ns>/set_emotion` (8종) |
| cmd_vel: TwistStamped (자동 확인) · 최대 0.22 m/s, 2.84 rad/s | cmd_vel: Twist · 웹 제한 0.2 m/s, 2.0 rad/s (보수적으로 설정, `js/robot.js`) |
| 배터리 `<ns>/battery_state` | 배터리 `<ns>/battery/percent`, `<ns>/battery/voltage` (5초 간격) |

## 안전

- **전체 비상정지**(모든 페이지, `Esc`): 등록된 **모든 로봇**에 0 속도를 보내고 이 브라우저가 보낸 goal 을 모두 취소합니다. 플릿·모바일에는 **이 로봇 정지**도 있습니다.
- 소프트웨어 정지입니다. TurtleBot3 모터 전원 차단은 장치 패널의 "모터 끄기" 를 쓰세요.
- 조종은 손을 떼거나 창 포커스를 잃으면 정지합니다 (데드맨). 속도는 로봇 종류별 최대값으로 잘립니다.
- **조종 권한**: 한 로봇은 한 브라우저만 조종합니다. 조종하는 동안 3초짜리 권한을 1초마다 갱신하고, 손을 떼면 놓습니다. 다른 브라우저는 "○○ 이(가) 조종 중" 알림과 **넘겨받기** 버튼을 봅니다. 비상정지는 권한과 상관없이 동작합니다.
- 로봇 간 거리가 0.5 m 미만이면 플릿 지도에 빨간 경고와 이벤트를 띄웁니다.
- 이동 구역: Nav2 는 keepout 마스크로, 웹 수동 조종은 명령 예측으로 금지 구역 진입을 막습니다 (위 "이동 구역 규칙").
- 지도 작성 시작·정지·저장, 구역 저장, 활성 지도 지정은 관리자 토큰이 필요합니다.
- 인터페이스 페이지는 기본 쓰기 잠금입니다. 로봇 목록 저장은 관리자 토큰이 필요하고, 쓰기 요청은 같은 출처(다른 웹사이트가 아닌 이 페이지)에서만 받습니다.
- rosbridge 자체에는 인증이 없습니다. **신뢰할 수 있는 네트워크에서만** 쓰고, 필요하면 `bind:=` 로 열어 둘 주소를 제한하세요.

## 구조

```
ros2-fleet-console/
  config/fleet.json      로봇 목록, 활성 지도(mapFile) (setup.html·지도 화면에서 저장)
  config/admin_token     관리자 토큰 (처음 실행 때 생성, 600)
  config/nav2/burger.yaml   기본 Nav2 파라미터 (실제 TurtleBot3 에서 찾은 collision_monitor 수정 포함)
  config/maps/<이름>.yaml|pgm      저장한 지도 (지도 작성)
  config/zones/<이름>.json         그 지도의 이동 구역
  config/masks/<이름>/<ns>.yaml|pgm 로봇별 keepout 마스크 (구역 저장 때 생성)
  logs/mapping.log       지도 작성(slam_toolbox) 로그
  launch/web.launch.py   rosbridge(+rosapi) 와 serve.py 실행 (bind 인자)
  serve.py               정적 파일 + /api/fleet · /api/control · /api/maps · /api/zones · /api/mapping
  maps.py                지도 목록·이미지, 구역 검증, 마스크 생성, 지도 작성 프로세스 관리
  robot/tb3_ns.launch.py    TurtleBot3 네임스페이스 + TF 분리 래퍼 (로봇에서 실행)
  robot/pinky_ns.launch.py  Pinky Pro 네임스페이스 + TF 분리 래퍼 (로봇에서 실행)
  robot/nav2_ns.launch.py   로봇별 Nav2 + keepout 필터 (PC 에서 실행)
  robot/mapping_ns.launch.py 로봇 한 대 slam_toolbox (PC, 웹 지도 작성이 실행)
  docs/architecture.md, docs/test-report.md
  web/
    fleet.html, index.html, interfaces.html, mobile.html, setup.html, mapping.html, zones.html
    css/app.css
    js/ros.js            rosbridge 클라이언트
    js/robot.js          로봇 종류 프로필 + Robot 클래스 (ns·TF·배터리·cmd_vel·서비스)
    js/common.js         fleet 로딩, 현재 로봇, 상단바, 전체 비상정지
    js/view.js           2D 캔버스 (단일/여러 로봇)
    js/fleet.js, dashboard.js, interfaces.js, mobile.js, setup.js, mapping.js, zones.js
```

## 알려진 제약

- 로봇 없이 PC 에서 가짜 로봇으로 시험한 결과: [docs/test-report.md](docs/test-report.md). **실제 TurtleBot3·Pinky Pro 로는 아직 시험하지 않았습니다.**
- PC 의 `ros2_ws/src/pinky_interfaces` 는 Pinky Pro 공개 저장소의 인터페이스 패키지 사본입니다 (LED·표정 서비스 타입을 rosbridge 가 알기 위함).
- rosbridge 2.7.1 의 `/rosapi/action_type` 은 rosapi 를 종료시키므로 쓰지 않습니다 (액션 타입은 이름으로 추정).
- `/map` 은 PNG 압축으로 3초 간격으로 받습니다 (1000×1000 지도 2.97 MB → 44 KB).
- 플릿 화면은 통신량을 줄이려고 odom 을 최대 10 Hz, LiDAR 는 2초 간격(선택 로봇을 LiDAR 레이어로 볼 때만 0.2초)으로 받습니다. 한 로봇의 정확한 주기는 로봇 대시보드에서 봅니다.
- Pinky 용 Nav2 파라미터(로봇 크기·속도)는 따로 만들지 않았습니다 (burger.yaml 사용).
- 지도 작성과 이동 구역은 가짜 로봇으로만 시험했습니다. 구역을 피하는 경로·주행, 마스크 즉시 반영, 수동 조종 차단은 확인했지만, 이 PC 의 가짜 로봇 환경이 느려 Nav2 가 목표까지 끝까지 가는 것은 (구역이 없어도) 확인하지 못했습니다.
- 지도 작성은 수동 조종입니다 (자동 탐색 아님). 한 번에 한 로봇만 지도를 그립니다.
