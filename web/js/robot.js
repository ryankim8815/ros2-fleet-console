// 로봇 종류별 프로필과 Robot 클래스 (네임스페이스 기반 멀티 로봇)
//
// 로봇 설정 (ros2-fleet-console/config/fleet.json 의 robots[]):
//   id, name, type('turtlebot3'|'pinky'), ns('' 또는 '/tb3_01'), tfTopic('/tf' 또는 '/pinky_01/tf'),
//   tfPrefix('' 또는 'tb3_01/'), color('#RRGGBB')
//
// TF 처리:
//   TurtleBot3 bringup 은 namespace:=X 로 실행하면 프레임에 'X/' 접두사를 붙이고 전역 /tf 로 보냅니다.
//   Pinky Pro 는 프레임 이름이 코드에 고정(odom, base_footprint)이라 /<ns>/tf 로 TF 토픽을 분리합니다.
(function () {
  'use strict';

  const Profiles = {
    turtlebot3: {
      label: 'TurtleBot3',
      model: 'TurtleBot3 Burger',
      limits: { lin: 0.22, ang: 2.84 },               // Burger 사양
      cmdVelDefault: 'geometry_msgs/msg/TwistStamped', // Jazzy 기본. 연결 시 실제 타입 확인
      expect: { scan: 5, odom: 30 },
      battery: [{ topic: 'battery_state', type: 'sensor_msgs/msg/BatteryState', field: 'state' }],
      services: {
        motor_power: 'std_srvs/srv/SetBool',
        sound: 'turtlebot3_msgs/srv/Sound',
        reset_odometry: 'std_srvs/srv/Trigger',
      },
      sounds: [[0, 'OFF'], [1, 'ON'], [2, 'LOW_BATTERY'], [3, 'ERROR'], [4, 'BUTTON1'], [5, 'BUTTON2']],
      lowVoltage: 10.5,
      voltageRange: [9.9, 12.6],
      radius: 0.105,                                    // 몸체 반지름 (m), 구역 판정용
    },
    pinky: {
      label: 'Pinky Pro',
      model: 'Pinky Pro',
      // bringup 에 m/s 제한이 없어(바퀴 100 RPM ≈ 0.28 m/s) 보수적으로 잡은 값
      limits: { lin: 0.2, ang: 2.0 },
      cmdVelDefault: 'geometry_msgs/msg/Twist',
      expect: { odom: 30 },
      battery: [
        { topic: 'battery/percent', type: 'std_msgs/msg/Float32', field: 'percent' },
        { topic: 'battery/voltage', type: 'std_msgs/msg/Float32', field: 'voltage' },
      ],
      services: {
        set_led: 'pinky_interfaces/srv/SetLed',
        set_brightness: 'pinky_interfaces/srv/SetBrightness',
        set_emotion: 'pinky_interfaces/srv/Emotion',
      },
      emotions: ['hello', 'basic', 'happy', 'fun', 'interest', 'bored', 'sad', 'angry'],
      lowVoltage: 6.8,                                  // bringup.py LOW_BATTERY_THRESHOLD
      voltageRange: [6.4, 8.4],                         // 2S 추정 (Pinky 배터리 사양 미확인)
      radius: 0.07,                                     // 몸체 반지름 추정 (바퀴 간격 0.096 m)
    },
  };

  // ---- 2D 자세 유틸 ----
  const Pose2 = {
    yaw(q) { return Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)); },
    quat(th) { return { x: 0, y: 0, z: Math.sin(th / 2), w: Math.cos(th / 2) }; },
    compose(a, b) { // a ∘ b
      const c = Math.cos(a.th), s = Math.sin(a.th);
      return { x: a.x + c * b.x - s * b.y, y: a.y + s * b.x + c * b.y, th: a.th + b.th };
    },
    inverse(a) {
      const c = Math.cos(a.th), s = Math.sin(a.th);
      return { x: -(c * a.x + s * a.y), y: -(-s * a.x + c * a.y), th: -a.th };
    },
    apply(a, x, y) { const c = Math.cos(a.th), s = Math.sin(a.th); return [a.x + c * x - s * y, a.y + s * x + c * y]; },
    identity: { x: 0, y: 0, th: 0 },
  };

  const P2 = () => Pose2;

  // ---- 이동 구역 (금지/허용) 판정 ----
  function inPolygon(x, y, pts) {
    let inside = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i, i += 1) {
      const [xi, yi] = pts[i], [xj, yj] = pts[j];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }
  // 점(x,y)이 이 로봇에게 금지인지: keepout 안이거나, allowed 구역이 있는데 그 밖
  function forbidden(zones, x, y) {
    if (!zones || !zones.length) return false;
    if (zones.some((z) => z.type === 'keepout' && inPolygon(x, y, z.points))) return true;
    const allowed = zones.filter((z) => z.type === 'allowed');
    return allowed.length > 0 && !allowed.some((z) => inPolygon(x, y, z.points));
  }
  // 로봇 몸체(반지름 r)의 둘레 8점 + 중심 중 하나라도 금지면 금지
  function bodyForbidden(zones, p, r) {
    if (forbidden(zones, p.x, p.y)) return true;
    for (let k = 0; k < 8; k += 1) {
      const a = (k * Math.PI) / 4;
      if (forbidden(zones, p.x + r * Math.cos(a), p.y + r * Math.sin(a))) return true;
    }
    return false;
  }
  const norm = (f) => (f || '').replace(/^\//, '');

  class Robot {
    constructor(ros, cfg) {
      this.ros = ros;
      this.cfg = cfg;
      this.id = cfg.id;
      this.name = cfg.name;
      this.color = cfg.color;
      this.type = cfg.type;
      this.profile = Profiles[cfg.type] || Profiles.turtlebot3;
      this.ns = cfg.ns || '';
      this.tfTopic = cfg.tfTopic || '/tf';
      this.tfPrefix = cfg.tfPrefix || '';
      this.cmdVelType = this.profile.cmdVelDefault;
      this.limits = { ...this.profile.limits };
      // 실시간 상태
      this.pose = null;            // odom 프레임
      this.v = NaN; this.w = NaN;
      this.mapToOdom = null;       // {pose, t}
      this.battery = { voltage: NaN, percent: NaN, t: 0 };
      this.seen = {};              // topic key -> [timestamps]
      this.unsubs = [];
      this.services = new Set();   // 이 로봇 ns 아래에서 발견된 서비스 (짧은 이름)
      this.listeners = new Set();
      this.staticTf = new Map();   // child frame -> {parent, pose}
    }

    // base_footprint 기준 센서 프레임의 2D 자세 (tf_static 체인). 모르면 원점(오프셋 없음)
    sensorOffset(frame) {
      const base = norm(`${this.tfPrefix}base_footprint`);
      let f = norm(frame);
      let acc = Pose2.identity;
      for (let i = 0; i < 10 && f && f !== base; i += 1) {
        const e = this.staticTf.get(f);
        if (!e) return Pose2.identity;
        acc = Pose2.compose(e.pose, acc);
        f = e.parent;
      }
      return f === base ? acc : Pose2.identity;
    }

    // ns 를 붙인 절대 토픽/서비스 이름
    topic(name) { return `${this.ns}/${name.replace(/^\//, '')}`; }
    get odomFrame() { return `${this.tfPrefix}odom`; }
    get label() { return this.profile.label; }
    get logPrefix() { return norm(this.ns).replace(/\//g, '.'); } // rosout 로거 이름 접두사

    onChange(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
    emit() { this.listeners.forEach((fn) => fn(this)); }

    mark(key) {
      const arr = (this.seen[key] ||= []);
      const now = performance.now();
      arr.push(now);
      while (arr.length && now - arr[0] > 5000) arr.shift();
    }
    hz(key) {
      const arr = this.seen[key] || [];
      const now = performance.now();
      while (arr.length && now - arr[0] > 5000) arr.shift();
      if (arr.length < 2) return arr.length ? 0.2 : 0;
      return (arr.length - 1) / ((arr[arr.length - 1] - arr[0]) / 1000);
    }
    age(key) { const a = this.seen[key]; return a && a.length ? performance.now() - a[a.length - 1] : Infinity; }
    // 최근 5초 안에서 가장 긴 수신 공백 (ms)
    maxGap(key) {
      const a = this.seen[key] || [];
      let g = 0;
      for (let i = 1; i < a.length; i += 1) g = Math.max(g, a[i] - a[i - 1]);
      return Math.max(g, this.age(key) === Infinity ? 0 : this.age(key));
    }
    // 연결 상태: 'ok' (정상) | 'unstable' (1초 넘는 공백이 최근 5초 안에 있음) | 'offline' (5초 넘게 없음)
    get link() {
      const age = this.age('odom');
      if (age > Robot.OFFLINE_MS) return 'offline';
      if (age > Robot.UNSTABLE_MS || this.maxGap('odom') > Robot.UNSTABLE_MS) return 'unstable';
      return 'ok';
    }
    get online() { return this.link !== 'offline'; }

    // 맵 위의 자세 (map→odom TF 가 최근 10초 이내에 있을 때만)
    get mapPose() {
      if (!this.pose || !this.mapToOdom || performance.now() - this.mapToOdom.t > 10000) return null;
      return P2().compose(this.mapToOdom.pose, this.pose);
    }

    // 전압으로 추정한 잔량 (%). TurtleBot3: 3S LiPo 9.9–12.6 V, Pinky: 2S 6.4–8.4 V (대략)
    voltagePercent() {
      const v = this.battery.voltage;
      const [lo, hi] = this.profile.voltageRange || [NaN, NaN];
      if (!Number.isFinite(v) || v <= 1 || !Number.isFinite(lo)) return NaN;
      return Math.max(0, Math.min(100, ((v - lo) / (hi - lo)) * 100));
    }
    // 잔량 (%). percent 값이 0–1(sensor_msgs 표준) 인지 0–100 인지 로봇마다 달라서:
    //   1 보다 크면 0–100, 0–1 이면 전압 추정치에 더 가까운 쪽, 전압이 없으면 표준(0–1)으로 해석
    batteryPercent() {
      const raw = this.battery.percent;
      const est = this.voltagePercent();
      if (Number.isFinite(raw) && raw >= 0) {
        if (raw > 1) return Math.min(100, raw);
        if (Number.isFinite(est)) return Math.abs(raw * 100 - est) <= Math.abs(raw - est) ? raw * 100 : raw;
        return raw * 100;
      }
      return est;
    }
    batteryText() {
      const p = this.batteryPercent();
      const parts = [];
      if (Number.isFinite(this.battery.voltage)) parts.push(`${this.battery.voltage.toFixed(1)} V`);
      parts.push(Number.isFinite(p) ? `${Math.round(p)}%` : '—%');
      return parts.join(' · ');
    }
    batteryLow() {
      const p = this.batteryPercent();
      return (Number.isFinite(p) && p < 20) || (Number.isFinite(this.battery.voltage) && this.battery.voltage > 1 && this.battery.voltage <= this.profile.lowVoltage);
    }

    // ---- 구독 시작 (기본 상태: odom, 배터리, TF) ----
    // odomThrottle(ms): 여러 대를 보는 화면(플릿·모바일 칩)은 서버에서 odom 을 줄여 받아 통신량을 낮춤
    start({ tf = true, odomThrottle = 0 } = {}) {
      const R = this.ros;
      this.odomThrottle = odomThrottle;
      this.unsubs.push(R.subscribe(this.topic('odom'), 'nav_msgs/msg/Odometry', (m) => {
        // 오프라인이었다가 다시 들어오면 (로봇을 페이지보다 늦게 켠 경우 등) cmd_vel 타입·서비스 다시 확인
        if (this.age('odom') > Robot.OFFLINE_MS) { this.detectCmdVelType(); this.refreshServices(); }
        this.mark('odom');
        const p = m.pose.pose;
        this.pose = { x: p.position.x, y: p.position.y, th: P2().yaw(p.orientation) };
        this.v = m.twist.twist.linear.x;
        this.w = m.twist.twist.angular.z;
        this.emit();
      }, { serverThrottle: odomThrottle }));
      this.profile.battery.forEach((b) => {
        this.unsubs.push(R.subscribe(this.topic(b.topic), b.type, (m) => {
          this.mark('battery');
          if (b.field === 'state') { this.battery.voltage = m.voltage; this.battery.percent = m.percentage; }
          else if (b.field === 'percent') this.battery.percent = m.data;
          else if (b.field === 'voltage') this.battery.voltage = m.data;
          this.battery.t = performance.now();
          this.emit();
        }));
      });
      // 고정 TF (base_footprint → base_link → LiDAR): 스캔을 로봇 중심이 아니라 실제 LiDAR 위치에서 그리기 위함
      this.unsubs.push(R.subscribe(`${this.tfTopic}_static`, 'tf2_msgs/msg/TFMessage', (m) => {
        for (const t of m.transforms || []) {
          const tr = t.transform.translation;
          this.staticTf.set(norm(t.child_frame_id), { parent: norm(t.header.frame_id), pose: { x: tr.x, y: tr.y, th: P2().yaw(t.transform.rotation) } });
        }
      }, { queue: 10 }));
      if (tf) {
        const want = norm(this.odomFrame);
        this.unsubs.push(R.subscribe(this.tfTopic, 'tf2_msgs/msg/TFMessage', (m) => {
          for (const t of m.transforms || []) {
            if (norm(t.header.frame_id) === 'map' && norm(t.child_frame_id) === want) {
              const tr = t.transform.translation;
              this.mapToOdom = { pose: { x: tr.x, y: tr.y, th: P2().yaw(t.transform.rotation) }, t: performance.now() };
            }
          }
        }));
      }
      return this;
    }

    stopSubscriptions() { this.unsubs.forEach((u) => u()); this.unsubs = []; }

    // ---- cmd_vel ----
    // 로봇(구독자)이 쓰는 cmd_vel 타입 확인. 우리 publisher 가 그래프에 있으면 그 타입이 보일 수 있어 먼저 내림
    // 로봇이 막 켜진 직후에는 그래프에 구독자가 아직 안 보일 수 있어 빈 결과면 2초 간격으로 5번까지 다시 확인
    async detectCmdVelType(tries = 5) {
      if (this.ros.adverts.has(this.topic('cmd_vel'))) {
        this.ros.unadvertise(this.topic('cmd_vel'));
        await new Promise((r) => setTimeout(r, 300));
      }
      let type = '';
      try { type = ((await this.ros.callService('/rosapi/topic_type', { topic: this.topic('cmd_vel') })) || {}).type || ''; } catch (e) { /* 기본값 유지 */ }
      if (type && type !== this.cmdVelType) { this.cmdVelType = type; this.emit(); }
      this.cmdVelChecked = !!type;
      if (!type && tries > 1) setTimeout(() => this.detectCmdVelType(tries - 1), 2000);
    }

    // 수동 조종이 금지 구역으로 들어가려 하면 true (0.6초 앞을 예측). 이미 금지 구역 안이면 빠져나가는 것은 허용
    zoneBlocks(lin, ang) {
      const p = this.mapPose;
      if (!this.zones || !this.zones.length || !p || (!lin && !ang)) return false;
      const r = this.profile.radius || 0.1;
      // 이미 금지 구역에 걸쳐 있으면 막지 않음 (그래야 조종해서 빠져나올 수 있음)
      if (bodyForbidden(this.zones, p, r)) return false;
      const dt = 0.6, steps = 6;
      let q = { ...p };
      for (let i = 0; i < steps; i += 1) {
        q = { x: q.x + Math.cos(q.th) * lin * (dt / steps), y: q.y + Math.sin(q.th) * lin * (dt / steps), th: q.th + ang * (dt / steps) };
        if (bodyForbidden(this.zones, q, r)) return true;
      }
      return false;
    }

    // ---- LiDAR 안전 구역 (웹 수동 조종) ----
    // this.safety = {enabled, stop_dist, slow_dist, slow_speed} (common.js 가 /api/safety 로 채움, safety.py)
    // 로봇 중심(base_footprint)에서 진행 방향 쪽(전진이면 앞쪽 반, 후진이면 뒤쪽 반) 가장 가까운 물체까지 거리로:
    //   stop_dist 안 → 직진 0 (반대쪽으로 빠져나가기·제자리 회전은 허용), slow_dist 안 → 직진 속도를 slow_speed 로 제한
    // 스캔이 1초 넘게 없으면 서행으로 제한 (모르면 안전 쪽). Nav2 쪽은 collision_monitor 가 같은 값으로 따로 막음.
    watchSafetyScan() {
      if (this._safetyUnsub) return;
      this._safetyMem = [];        // 빛줄기별 마지막 유효 거리 {r, t}
      this._safetySince = performance.now();
      this._safetyUnsub = this.ros.subscribe(this.topic('scan'), 'sensor_msgs/msg/LaserScan', (m) => this.onSafetyScan(m));
      this.unsubs.push(() => { this._safetyUnsub(); this._safetyUnsub = null; });
    }

    onSafetyScan(m) {
      const off = this.sensorOffset(m.header.frame_id);
      const now = performance.now();
      const mem = this._safetyMem;
      const slow = (this.safety && this.safety.slow_dist) || 0.5;
      let front = Infinity, back = Infinity;
      const ranges = m.ranges || [];
      const pose = this.pose ? { ...this.pose } : null;
      // 기억한 뒤로 로봇이 3 cm 또는 10° 넘게 움직였으면 그 기억은 버림 (odom 이 없으면 3초)
      const stale = (e) => (e.pose && pose
        ? Math.hypot(pose.x - e.pose.x, pose.y - e.pose.y) > 0.03 || Math.abs(Math.atan2(Math.sin(pose.th - e.pose.th), Math.cos(pose.th - e.pose.th))) > 0.17
        : now - e.t > 3000);
      for (let i = 0; i < ranges.length; i += 1) {
        let r = ranges[i];
        let hidden = false;
        if (r !== null && Number.isFinite(r) && r >= m.range_min && r <= m.range_max) mem[i] = { r, t: now, pose };
        else {
          // LiDAR 최소 거리(LDS-02 0.16 m 등)보다 가까워지면 값이 사라짐 → 같은 빛줄기에 가까운 물체가 있었고 그 뒤로
          // 로봇이 움직이지 않았으면 아직 거기 있는 것으로 보고 최소 거리에 둠 (물체가 안 보이게 되어 정지가 풀리는 것 방지).
          // 물체를 치웠는데 정지가 남으면, 허용되는 반대 방향·회전으로 조금 움직이면 풀림
          const e = mem[i];
          if (!e || e.r > slow || stale(e)) continue;
          r = m.range_min; hidden = true;
        }
        const a = m.angle_min + i * m.angle_increment;
        const [x, y] = Pose2.apply(off, r * Math.cos(a), r * Math.sin(a));
        // 안 보이게 된 물체는 LiDAR 가 잴 수 없을 만큼 가까운 것 → 거리 0 (그쪽 방향 정지)
        const d = hidden ? 0 : Math.hypot(x, y);
        if (x >= 0) front = Math.min(front, d); else back = Math.min(back, d);
      }
      this.safetyNear = { front, back, t: now };
    }

    // 반환 {lin, ang, state: 'off' | 'clear' | 'slow' | 'stop' | 'noscan', d}
    safetyGate(lin, ang) {
      const s = this.safety;
      if (!s || !s.enabled || (!lin && !ang)) return { lin, ang, state: 'off' };
      this.watchSafetyScan();
      const clampLin = (v) => Math.max(-s.slow_speed, Math.min(s.slow_speed, v));
      const n = this.safetyNear;
      if (!n || performance.now() - n.t > 1000) return { lin: clampLin(lin), ang, state: 'noscan' };
      if (!lin) return { lin, ang, state: 'clear' };                    // 제자리 회전은 허용 (빠져나갈 방향 잡기)
      const d = lin > 0 ? n.front : n.back;
      if (d < s.stop_dist) return { lin: 0, ang, state: 'stop', d };
      if (d < s.slow_dist && Math.abs(lin) > s.slow_speed) return { lin: clampLin(lin), ang, state: 'slow', d };
      return { lin, ang, state: d < s.slow_dist ? 'slow' : 'clear', d };
    }

    cmdVel(lin, ang) {
      lin = Math.max(-this.limits.lin, Math.min(this.limits.lin, lin || 0));
      ang = Math.max(-this.limits.ang, Math.min(this.limits.ang, ang || 0));
      const g = this.safetyGate(lin, ang);
      if (g.state !== 'off') {
        ({ lin, ang } = g);
        this.safetyState = g;
        // 상태가 바뀔 때, 같은 상태는 3초에 한 번만 알림 (정지·서행·스캔 없음)
        const now = performance.now();
        // 막 구독을 시작해 첫 스캔을 기다리는 동안(1.5초)의 '스캔 없음'은 알리지 않음 (서행 제한은 그대로)
        const warming = g.state === 'noscan' && now - (this._safetySince || 0) < 1500;
        if (g.state !== 'clear' && !warming && (g.state !== this._lastSafetyEv || now - (this._lastSafetyEvT || 0) > 3000)) {
          this._lastSafetyEv = g.state; this._lastSafetyEvT = now;
          document.dispatchEvent(new CustomEvent('tb3:safety-limit', { detail: { robot: this, ...g } }));
        }
        if (g.state === 'clear') this._lastSafetyEv = 'clear';
      }
      if (this.zoneBlocks(lin, ang)) {
        lin = 0; ang = 0;
        const now = performance.now();
        if (now - (this.lastZoneBlock || 0) > 2000) {
          this.lastZoneBlock = now;
          document.dispatchEvent(new CustomEvent('tb3:zone-block', { detail: this }));
        }
      }
      const twist = { linear: { x: lin, y: 0, z: 0 }, angular: { x: 0, y: 0, z: ang } };
      let msg = twist;
      if (this.cmdVelType.endsWith('TwistStamped')) {
        const ms = Date.now();
        msg = { header: { stamp: { sec: Math.floor(ms / 1000), nanosec: (ms % 1000) * 1e6 }, frame_id: `${this.tfPrefix}base_link` }, twist };
      }
      return this.ros.publish(this.topic('cmd_vel'), this.cmdVelType, msg);
    }

    halt() {
      let n = 0;
      const send = () => { this.cmdVel(0, 0); n += 1; if (n < 5) setTimeout(send, 60); };
      send();
    }

    // ---- 서비스 ----
    async refreshServices() {
      try {
        const r = await this.ros.callService('/rosapi/services', {});
        const pre = `${this.ns}/`;
        this.services = new Set((r.services || []).filter((s) => s.startsWith(pre) && !s.slice(pre.length).includes('/')).map((s) => s.slice(pre.length)));
        this.emit();
      } catch (e) { /* ignore */ }
    }
    has(service) { return this.services.has(service); }
    call(service, args = {}, timeout = 8000) {
      return this.ros.callService(this.topic(service), args, { type: this.profile.services[service], timeout });
    }

    // ---- AMCL 초기 자세 (<ns>/initialpose) ----
    setInitialPose(x, y, th) {
      const cov = new Array(36).fill(0);
      cov[0] = 0.25; cov[7] = 0.25; cov[35] = 0.0685; // x, y, yaw 분산 (rviz 기본값)
      const ms = Date.now();
      return this.ros.publish(this.topic('initialpose'), 'geometry_msgs/msg/PoseWithCovarianceStamped', {
        header: { frame_id: 'map', stamp: { sec: Math.floor(ms / 1000), nanosec: (ms % 1000) * 1e6 } },
        pose: { pose: { position: { x, y, z: 0 }, orientation: P2().quat(th) }, covariance: cov },
      });
    }

    // ---- Nav2 ----
    // Nav2 주행 쪽(planner·controller·bt_navigator, lifecycle_manager_navigation)이 켜져 있는지 확인하고, 꺼져 있으면 다시 켬.
    // Nav2 는 실행 후 일정 시간(기본 60초) 안에 map → base_link TF 가 없으면 주행 쪽을 켜다가 포기하는데,
    // 이 TF 는 AMCL 초기 위치를 준 뒤에야 생깁니다. 그래서 초기 위치를 늦게 주면 목표를 보낼 수 없던 문제를
    // 초기 위치 직후·목표 전에 RESET → STARTUP 으로 다시 켜서 풉니다 (위치 추정 AMCL 은 따로 관리되어 그대로).
    // 반환: 'active' (이미 켜짐) | 'restarted' (다시 켬) | 'failed' | 'absent' (Nav2 lifecycle manager 없음)
    async ensureNavActive() {
      const base = this.topic('lifecycle_manager_navigation');
      const isActive = async () => !!(await this.ros.callService(`${base}/is_active`, {}, { type: 'std_srvs/srv/Trigger', timeout: 5000 })).success;
      try { if (await isActive()) return 'active'; } catch (e) { return 'absent'; }
      if (this._navStarting) return this._navStarting;
      const manage = (command, timeout) => this.ros.callService(`${base}/manage_nodes`, { command },
        { type: 'nav2_msgs/srv/ManageLifecycleNodes', timeout });
      this._navStarting = (async () => {
        try {
          await manage(3, 60000);                 // RESET: 반쯤 켜진 노드들을 처음 상태로
          const r = await manage(0, 120000);      // STARTUP: configure → activate (map TF 를 기다림)
          return r && r.success && await isActive() ? 'restarted' : 'failed';
        } catch (e) { return 'failed'; } finally { this._navStarting = null; }
      })();
      return this._navStarting;
    }

    // ---- 지금 가는 길 (Nav2 계획 경로 <ns>/plan) ----
    // Nav2 는 주행 중 1초마다 다시 계획(navigate_to_pose_w_replanning: RateController 1 Hz)하고 그때마다 plan 을 냄.
    // 누가 보낸 goal 이든(웹 목표, 워커의 이동 경로, 경비 순회) 같은 토픽이라 여기서 한 번에 봄.
    watchPlan(throttleMs = 500) {
      if (this._planUnsub) return;
      this._planUnsub = this.ros.subscribe(this.topic('plan'), 'nav_msgs/msg/Path', (m) => {
        let T = Pose2.identity;
        if (norm(m.header.frame_id) !== 'map' && this.mapToOdom) T = this.mapToOdom.pose;   // odom 프레임 경로 → map
        this.plan = { pts: (m.poses || []).map((ps) => Pose2.apply(T, ps.pose.position.x, ps.pose.position.y)), t: performance.now() };
      }, { serverThrottle: throttleMs });
      this.unsubs.push(() => { this._planUnsub(); this._planUnsub = null; });
    }

    // 앞으로 갈 길 [[x, y], ...] (map 프레임): 로봇 위치에서 시작해, 계획에서 이미 지나온 부분은 뺌.
    // 계획을 PLAN_STALE_MS 넘게 못 받았으면(도착·취소·멈춤) null — 끝난 길이 지도에 남지 않게
    remainingPlan(pose = this.mapPose) {
      const p = this.plan;
      if (!p || !p.pts.length || performance.now() - p.t > Robot.PLAN_STALE_MS) return null;
      if (!pose) return p.pts;
      let best = 0, bd = Infinity;
      p.pts.forEach(([x, y], i) => { const d = (x - pose.x) ** 2 + (y - pose.y) ** 2; if (d < bd) { bd = d; best = i; } });
      const rest = p.pts.slice(best + 1);
      return rest.length ? [[pose.x, pose.y], ...rest] : null;
    }

    navGoal(x, y, th, frame = 'map', onFeedback) {
      return this.ros.sendActionGoal(this.topic('navigate_to_pose'), 'nav2_msgs/action/NavigateToPose', {
        pose: { header: { frame_id: frame, stamp: { sec: 0, nanosec: 0 } }, pose: { position: { x, y, z: 0 }, orientation: P2().quat(th) } },
        behavior_tree: '',
      }, { onFeedback });
    }
  }

  Robot.UNSTABLE_MS = 1000;
  Robot.OFFLINE_MS = 5000;
  Robot.PLAN_STALE_MS = 3000;     // Nav2 가 1초마다 다시 계획하므로 3초 넘게 없으면 주행이 끝난 것
  window.Pose2 = Pose2;
  window.Zones = { inPolygon, forbidden, bodyForbidden };
  window.Profiles = Profiles;
  window.Robot = Robot;
})();
