# 로봇 관제센터 (웹)

TurtleBot3 와 Pinky Pro 여러 대를 브라우저에서 관제하는 웹 앱입니다. **PC 에서** 실행하며, 웹 때문에 로봇에 추가로 설치하는 것은 없습니다.
여러 대는 **같은 ROS_DOMAIN_ID + 로봇별 네임스페이스**(`/tb3_01`, `/pinky_01` …)로 구분하고, rosbridge 하나로 모두 연결합니다.

```
브라우저 ──WebSocket(9090)── rosbridge + rosapi (PC) ──DDS(ROS_DOMAIN_ID=30)── /tb3_01/*  /pinky_01/*  /pinky_02/* …
        └─HTTP(8080)── ros2-fleet-console/serve.py (정적 파일 + /api/*, 모드·경보 DB)
```

구조와 데이터 흐름: [docs/architecture.md](docs/architecture.md)

## 실행

```bash
cd ~/turtlebot3
pixi run web                                # http://localhost:8080/fleet.html
pixi run web http_port:=8000 ws_port:=9091  # 포트 변경
pixi run web bind:=127.0.0.1                # 이 PC 에서만 접속 (웹·rosbridge 모두)
```

### 운영 모드

플릿 전체가 하나의 모드로 움직입니다. 상단바의 **모드 칩**을 눌러 바꿉니다 (관리자 토큰 필요).

| 모드 | 하는 일 | 끝내면 |
| --- | --- | --- |
| 운영 | 사람이 목표를 주고 조종. 자동 동작 없음 | — |
| 지도 만들기 | 고른 로봇들이 **함께** 지도를 그림 (로봇마다 slam_toolbox → 시작 위치로 합쳐 `/map_merged`). 수동 조종 또는 **자동 탐색**(Nav2 + explore_lite) | 저장 → 활성 지도 |
| 경비 | 로봇들이 순찰 경로를 돌며 LiDAR 스캔을 기준 지도와 비교. **새로 생긴 것 / 사라진 것**을 경보로 남기고 관제실(웹)에 알림 | 운영으로 바꾸면 순찰 정지 |

경비 모드는 서버를 다시 켜도 이어지고, 감지 프로세스가 죽으면 서버가 다시 띄웁니다 (모드 칩에 "감지 멈춤" 표시).

### 처음 시작: 지도 만들기 → 이동 구역 → 로봇별 Nav2 → 경비

```mermaid
flowchart LR
    A["1. 로봇 bringup<br/>(네임스페이스)"] --> B["2. 지도 만들기<br/>mapping.html<br/>로봇 여러 대 함께"]
    B --> C["3. 지도 저장<br/>config/maps/이름<br/>→ 활성 지도"]
    C --> D["4. 이동 구역<br/>zones.html<br/>금지·허용 다각형, 로봇별"]
    D --> E["5. 로봇별 Nav2<br/>nav2_ns.launch.py map:=이름<br/>(구역 마스크 자동)"]
    E --> F["6. 플릿 화면<br/>초기 위치 → 목표"]
    E --> G["7. 경비<br/>patrol.html<br/>순찰 경로 → 경비 모드"]
```

1. 로봇들을 네임스페이스로 bringup 합니다 (아래 표).
2. 플릿 화면에 지도가 없으면 **지도 만들기** 안내가 뜹니다. **지도 만들기**에서 함께 그릴 로봇들을 고르고 각자의 **시작 위치**(첫 로봇 기준 x, y, 각도)를 넣은 뒤 **시작**합니다. 로봇마다 slam_toolbox 가 뜨고, 시작 위치로 맞춰 합친 지도(`/map_merged`)가 화면에 보입니다. 웹에서 로봇을 골라 조종하거나 **자동 탐색**을 켭니다. 다 그렸으면 이름을 붙여 **저장**(활성 지도로 지정)하고 **끝내기**.
   - 시작 위치는 줄자로 재서 넣습니다. 틀리면 합친 지도에 벽이 두 겹으로 보입니다.
   - 자동 탐색은 로봇 TF 가 로봇별 토픽(`/tb3_01/tf`)일 때만 됩니다 (Nav2 를 로봇마다 띄우기 때문).
3. **이동 구역**에서 그 지도 위에 금지(빨강)·허용(초록) 구역을 그리고, 구역마다 적용할 로봇을 고른 뒤 **저장하고 적용**합니다.
4. 로봇마다 Nav2 를 띄웁니다. 활성 지도의 구역 마스크가 있으면 자동으로 Nav2 keepout 필터가 켜집니다.
   ```bash
   pixi run ros2 launch ros2-fleet-console/robot/nav2_ns.launch.py namespace:=tb3_01 frame_prefix:=tb3_01/ map:=lab
   pixi run ros2 launch ros2-fleet-console/robot/nav2_ns.launch.py namespace:=pinky_01 stamped:=false map:=lab
   ```
5. 플릿 화면에서 로봇마다 **초기 위치**를 찍고 **목표 지정**.

6. **경비**(`patrol.html`) → **순찰 경로·감지 설정** 탭에서 로봇마다 지도를 클릭해 순찰 지점을 찍고 저장한 뒤, 모드를 **경비**로 바꿉니다. 각 로봇은 Nav2 `follow_waypoints` 로 경로를 반복합니다.

구역을 나중에 바꿔 저장하면 실행 중인 Nav2 에 바로 반영됩니다 (재시작 불필요). 순찰 경로·감지 설정도 경비 중에 저장하면 5초 안에 반영됩니다.

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
| `mapping.html` 지도 만들기 | 함께 그릴 로봇들과 시작 위치 → 로봇별 slam_toolbox + 합친 지도(로봇별 기여 면적) → 수동 조종 또는 자동 탐색 → 저장(활성 지도 지정) → 끝내기 |
| `patrol.html` 경비 | 경보 목록(미확인/전체), 지도 위 경보 상자·순찰 경로·로봇, 경보 상세(기준 지도 vs 발견 당시 스캔), 처리(이상 없음·오탐·다시 열기·기준 지도에 반영·가까운 로봇 보내기·링크), 로봇별 순찰 상태(멈춤/재개), 순찰 경로 편집, 감지 설정 |
| `alerts.html` 경보 (모바일) | 휴대폰용 경보 목록·상세·처리, 이 기기 알림 켜기, 전체 비상정지 |
| `zones.html` 이동 구역 | 저장된 지도 위에 금지/허용 다각형 그리기, 로봇별 적용, 로봇별 미리보기, 저장 시 Nav2 keepout 마스크 생성·즉시 반영 |

### 경비: 무엇을 경보로 보나

| 종류 | 판단 | 오탐을 줄이는 장치 |
| --- | --- | --- |
| 새로 생긴 것 (빨강) | 스캔 끝점이 기준 지도의 **빈 칸**이고 벽에서 `tol`(기본 0.15 m) 이상 떨어짐 | 연속 `persist`(2)번 보여야 확정, `decay`(10초) 안 보이면 잊음, 최소 크기 `min_area` |
| 사라진 것 (노랑 점선) | 빛줄기가 기준 지도의 **벽 칸을 뚫고** 지나감 (끝점 앞 tol 여유) | 벽을 25° 미만으로 스치는 빛은 제외, 여유는 표면에서 수직 거리로 계산 |

- 같은 곳(0.5 m)·같은 종류의 열린 경보는 새로 만들지 않고 갱신합니다 (본 횟수, 본 로봇, 상자 크기). 이상 없음/오탐으로 처리한 곳은 30분 동안 다시 알리지 않습니다.
- 등급: 넓이 0.25 m² 이상이거나 **두 대 이상이 같은 것을 보면** 높음, 작은 새 물체는 낮음, 나머지 보통.
- **기준 지도에 반영**: 새로 생긴 것은 벽으로, 사라진 것은 빈 칸으로 지도 그림을 고칩니다 (원본은 `.pgm.bak` 로 한 번 보관). 그 뒤로는 그 상태가 정상입니다.
- 경보는 `data/alerts.db`(SQLite) 에 남고, `/fleet/alerts` 토픽으로 열린 모든 화면에 바로 알립니다 (토스트·소리·진동·허용 시 브라우저 알림).

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

- **전체 비상정지**(모든 페이지, `Esc`): 등록된 **모든 로봇**에 0 속도를 보내고 이 브라우저가 보낸 goal 을 모두 취소하며, 경비 중이면 모든 로봇의 순찰도 멈춥니다. 플릿·모바일에는 **이 로봇 정지**도 있습니다.
- 소프트웨어 정지입니다. TurtleBot3 모터 전원 차단은 장치 패널의 "모터 끄기" 를 쓰세요.
- 조종은 손을 떼거나 창 포커스를 잃으면 정지합니다 (데드맨). 속도는 로봇 종류별 최대값으로 잘립니다.
- **조종 권한**: 한 로봇은 한 브라우저만 조종합니다. 조종하는 동안 3초짜리 권한을 1초마다 갱신하고, 손을 떼면 놓습니다. 다른 브라우저는 "○○ 이(가) 조종 중" 알림과 **넘겨받기** 버튼을 봅니다. 비상정지는 권한과 상관없이 동작합니다.
- 로봇 간 거리가 0.5 m 미만이면 플릿 지도에 빨간 경고와 이벤트를 띄웁니다.
- 이동 구역: Nav2 는 keepout 마스크로, 웹 수동 조종은 명령 예측으로 금지 구역 진입을 막습니다 (위 "이동 구역 규칙").
- 모드 변경, 지도 만들기 시작·정지·저장, 구역·순찰 경로 저장, 기준 지도 반영, 활성 지도 지정은 관리자 토큰이 필요합니다. 경보를 "이상 없음/오탐"으로 처리하는 것은 토큰 없이 됩니다 (같은 출처만).
- 경비 모드의 로봇은 스스로 움직입니다. 순찰 경로는 이동 구역 안쪽으로 잡으세요. 경비 화면의 로봇별 **멈춤**, 또는 **전체 비상정지**(모든 로봇 순찰도 멈춤)로 세웁니다. 비상정지 뒤 순찰은 경비 화면에서 로봇마다 **재개**해야 다시 돕니다.
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
  config/mode.json       현재 운영 모드 (운영 / 지도 만들기 / 경비)
  config/patrol/<이름>.json        그 지도의 순찰 경로(로봇별)와 감지 설정
  data/alerts.db         경보 (SQLite)
  logs/mapping.log, logs/patrol.log  지도 만들기·경비 프로세스 로그
  launch/web.launch.py   rosbridge(+rosapi) 와 serve.py 실행 (bind 인자)
  serve.py               정적 파일 + /api/fleet · control · maps · zones · mapping · mode · patrol · alerts, 경비 감시(재시작)
  maps.py                지도 목록·이미지, 구역 검증, 마스크 생성, 기준 지도 반영, 지도 만들기·경비 프로세스 관리
  patrol_store.py        모드 파일, 순찰 설정 검증, 경보 DB
  robot/tb3_ns.launch.py    TurtleBot3 네임스페이스 + TF 분리 래퍼 (로봇에서 실행)
  robot/pinky_ns.launch.py  Pinky Pro 네임스페이스 + TF 분리 래퍼 (로봇에서 실행)
  robot/nav2_ns.launch.py   로봇별 Nav2 + keepout 필터 (PC 에서 실행)
  robot/mapping_ns.launch.py 로봇 한 대 slam_toolbox (map_frame 지정 가능)
  robot/coop_mapping.launch.py 여러 로봇 함께 지도 만들기 (PC, 웹이 실행): 로봇별 slam + 합치기 + (선택) Nav2·explore_lite
  robot/map_merge_node.py   로봇별 지도를 시작 위치로 합쳐 /map_merged 발행
  robot/patrol_node.py      경비: 순찰(follow_waypoints) + 스캔/기준 지도 비교 + 경보 (PC, 웹이 실행)
  robot/nav2_params.py      Nav2 파라미터 고치기(프레임 접두사·토픽·keepout) 공용 함수
  docs/architecture.md, docs/test-report.md
  web/
    fleet.html, index.html, interfaces.html, mobile.html, setup.html, mapping.html, zones.html, patrol.html, alerts.html
    css/app.css
    js/ros.js            rosbridge 클라이언트
    js/robot.js          로봇 종류 프로필 + Robot 클래스 (ns·TF·배터리·cmd_vel·서비스)
    js/common.js         fleet 로딩, 현재 로봇, 상단바(모드 칩·경보 칩), 모드 바꾸기 대화상자, 경보 알림, 전체 비상정지
    js/view.js           2D 캔버스 (단일/여러 로봇)
    js/fleet.js, dashboard.js, interfaces.js, mobile.js, setup.js, mapping.js, zones.js, patrol.js
```

## 알려진 제약

- 로봇 없이 PC 에서 가짜 로봇으로 시험한 결과: [docs/test-report.md](docs/test-report.md). **실제 TurtleBot3·Pinky Pro 로는 아직 시험하지 않았습니다.**
- PC 의 `ros2_ws/src/pinky_interfaces` 는 Pinky Pro 공개 저장소의 인터페이스 패키지 사본입니다 (LED·표정 서비스 타입을 rosbridge 가 알기 위함).
- rosbridge 2.7.1 의 `/rosapi/action_type` 은 rosapi 를 종료시키므로 쓰지 않습니다 (액션 타입은 이름으로 추정).
- `/map` 은 PNG 압축으로 3초 간격으로 받습니다 (1000×1000 지도 2.97 MB → 44 KB).
- 플릿 화면은 통신량을 줄이려고 odom 을 최대 10 Hz, LiDAR 는 2초 간격(선택 로봇을 LiDAR 레이어로 볼 때만 0.2초)으로 받습니다. 한 로봇의 정확한 주기는 로봇 대시보드에서 봅니다.
- Pinky 용 Nav2 파라미터(로봇 크기·속도)는 따로 만들지 않았습니다 (burger.yaml 사용).
- 지도 작성과 이동 구역은 가짜 로봇으로만 시험했습니다. 구역을 피하는 경로·주행, 마스크 즉시 반영, 수동 조종 차단은 확인했지만, 이 PC 의 가짜 로봇 환경이 느려 Nav2 가 목표까지 끝까지 가는 것은 (구역이 없어도) 확인하지 못했습니다.
- 함께 지도 만들기는 **시작 위치를 사람이 넣는** 방식입니다 (지도끼리 자동 정합 없음). m-explore-ros2 의 `map_merge` 는 이 PC 의 OpenCV 에 없는 API(`invertAffineTransform`, `AKAZE`) 때문에 빌드되지 않아 직접 만든 합치기 노드를 씁니다.
- 자동 탐색은 `ros2_ws/src/m-explore-ros2` 의 explore_lite 를 씁니다. 원본은 목표에 도착해도 그 frontier 가 남아 있으면 같은 목표를 끝없이 다시 보내는 문제가 있어, 도착한 frontier 를 blacklist 에 넣도록 **한 줄 고쳤습니다** (`explore/src/explore.cpp`, 주석 `local patch`).
- 경비와 자동 탐색은 가짜 로봇으로 시험했습니다 ([docs/test-report.md](docs/test-report.md)). 실제 LiDAR 의 잡음·유리·사람 때문에 감지 설정(특히 `persist`, `min_area`)은 현장에서 조정해야 합니다.
