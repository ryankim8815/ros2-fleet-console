// 최소 rosbridge v2 프로토콜 클라이언트 (외부 라이브러리 없음)
// 토픽 구독/발행, 서비스 호출, 액션 goal 송신/취소, 자동 재연결을 지원합니다.
// 프로토콜: https://github.com/RobotWebTools/rosbridge_suite/blob/ros2/ROSBRIDGE_PROTOCOL.md
(function () {
  'use strict';

  class Ros {
    constructor(url) {
      this.url = url;
      this.ws = null;
      this.connected = false;
      this.listeners = {};          // event -> Set(fn)
      this.subs = new Map();        // topic -> {type, handlers:Set, throttle, queue}
      this.adverts = new Map();     // topic -> type
      this.pending = new Map();     // id -> {resolve, reject, timer}
      this.actions = new Map();     // id -> {onFeedback, resolve, reject, action}
      this.seq = 0;
      this.retryMs = 1000;
      this.closedByUser = false;
      this.rxBytes = 0;             // 받은 바이트 누계 (통신량 확인용)
      this.rxMsgs = 0;
    }

    on(ev, fn) { (this.listeners[ev] ||= new Set()).add(fn); return () => this.listeners[ev].delete(fn); }
    emit(ev, ...a) { (this.listeners[ev] || []).forEach((fn) => { try { fn(...a); } catch (e) { console.error(e); } }); }
    nextId(p) { this.seq += 1; return `${p}:${this.seq}`; }

    connect(url) {
      if (url) this.url = url;
      this.closedByUser = false;
      if (this.ws) { try { this.ws.onclose = null; this.ws.close(); } catch (e) { /* ignore */ } }
      this.emit('connecting', this.url);
      let ws;
      try { ws = new WebSocket(this.url); } catch (e) { this.emit('error', e); this.scheduleRetry(); return; }
      this.ws = ws;
      ws.onopen = () => {
        this.connected = true;
        this.retryMs = 1000;
        // 재연결 시 기존 구독/advertise 복원
        for (const [topic, s] of this.subs) this.sendSubscribe(topic, s);
        for (const [topic, type] of this.adverts) this.send({ op: 'advertise', topic, type });
        this.emit('connection');
      };
      ws.onmessage = (ev) => { this.rxBytes += ev.data.length; this.rxMsgs += 1; this.handle(ev.data); };
      ws.onerror = (e) => this.emit('error', e);
      ws.onclose = () => {
        const was = this.connected;
        this.connected = false;
        for (const [id, p] of this.pending) { clearTimeout(p.timer); p.reject(new Error('연결 끊김')); this.pending.delete(id); }
        for (const [id, a] of this.actions) { a.reject(new Error('연결 끊김')); this.actions.delete(id); }
        this.emit('close', was);
        if (!this.closedByUser) this.scheduleRetry();
      };
    }

    scheduleRetry() {
      clearTimeout(this.retryTimer);
      this.retryTimer = setTimeout(() => this.connect(), this.retryMs);
      this.retryMs = Math.min(this.retryMs * 2, 10000);
    }

    close() { this.closedByUser = true; clearTimeout(this.retryTimer); if (this.ws) this.ws.close(); }

    send(obj) {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
      this.ws.send(JSON.stringify(obj));
      return true;
    }

    // rosbridge PNG 압축: 원래 JSON 문자열을 RGB PNG(끝은 '\n' 패딩)로 보냄 → 픽셀에서 바이트를 꺼내 복원
    async decodePng(b64) {
      const bin = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      const bmp = await createImageBitmap(new Blob([bin], { type: 'image/png' }), { colorSpaceConversion: 'none', premultiplyAlpha: 'none' });
      const cv = new OffscreenCanvas(bmp.width, bmp.height);
      const g = cv.getContext('2d');
      g.drawImage(bmp, 0, 0);
      const rgba = g.getImageData(0, 0, bmp.width, bmp.height, { colorSpace: 'srgb' }).data;
      const rgb = new Uint8Array((rgba.length / 4) * 3);
      for (let i = 0, j = 0; i < rgba.length; i += 4) { rgb[j++] = rgba[i]; rgb[j++] = rgba[i + 1]; rgb[j++] = rgba[i + 2]; }
      return new TextDecoder().decode(rgb).replace(/\n+$/, '');
    }

    handle(raw) {
      let m;
      try { m = JSON.parse(raw); } catch (e) { return; }
      if (m.op === 'png') {
        this.decodePng(m.data).then((txt) => this.handle(txt)).catch((e) => console.warn('PNG 해제 실패', e));
        return;
      }
      switch (m.op) {
        case 'publish': {
          const s = this.subs.get(m.topic);
          if (!s) return;
          const bytes = raw.length;
          s.handlers.forEach((h) => {
            const now = performance.now();
            if (h.throttle && now - h.last < h.throttle) return;
            h.last = now;
            try { h.fn(m.msg, bytes); } catch (e) { console.error(e); }
          });
          break;
        }
        case 'service_response': {
          const p = this.pending.get(m.id);
          if (!p) return;
          clearTimeout(p.timer);
          this.pending.delete(m.id);
          if (m.result === false) p.reject(new Error(typeof m.values === 'string' ? m.values : JSON.stringify(m.values)));
          else p.resolve(m.values);
          break;
        }
        case 'action_feedback': {
          const a = this.actions.get(m.id);
          if (a && a.onFeedback) a.onFeedback(m.values);
          break;
        }
        case 'action_result': {
          const a = this.actions.get(m.id);
          if (!a) return;
          this.actions.delete(m.id);
          if (m.result === false) a.reject(new Error(String(m.values)));
          else a.resolve({ status: m.status, values: m.values });
          break;
        }
        case 'status':
          this.emit('status', m);
          break;
        default:
          break;
      }
    }

    // ---- topics ----
    sendSubscribe(topic, s) {
      const msg = { op: 'subscribe', id: `sub:${topic}`, topic, queue_length: s.queue || 1 };
      if (s.type) msg.type = s.type;
      if (s.serverThrottle) msg.throttle_rate = s.serverThrottle;
      if (s.compression) msg.compression = s.compression;   // 'png': 큰 메시지(지도) 압축
      this.send(msg);
    }

    /** 구독. opts.throttle(ms)는 클라이언트 측 콜백 제한. 반환값: 구독 해제 함수 */
    subscribe(topic, type, fn, opts = {}) {
      let s = this.subs.get(topic);
      if (!s) {
        s = { type, handlers: new Set(), queue: opts.queue || 1, serverThrottle: opts.serverThrottle || 0, compression: opts.compression };
        this.subs.set(topic, s);
        this.sendSubscribe(topic, s);
      }
      const h = { fn, throttle: opts.throttle || 0, last: 0 };
      s.handlers.add(h);
      return () => {
        s.handlers.delete(h);
        if (s.handlers.size === 0) {
          this.subs.delete(topic);
          this.send({ op: 'unsubscribe', id: `sub:${topic}`, topic });
        }
      };
    }

    advertise(topic, type) {
      if (this.adverts.get(topic) === type) return;
      this.adverts.set(topic, type);
      this.send({ op: 'advertise', topic, type });
    }

    unadvertise(topic) {
      if (!this.adverts.has(topic)) return;
      this.adverts.delete(topic);
      this.send({ op: 'unadvertise', topic });
    }

    publish(topic, type, msg) {
      this.advertise(topic, type);
      return this.send({ op: 'publish', topic, msg });
    }

    // ---- services ----
    callService(service, args = {}, { type, timeout = 6000 } = {}) {
      return new Promise((resolve, reject) => {
        if (!this.connected) { reject(new Error('rosbridge 미연결')); return; }
        const id = this.nextId('srv');
        const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`시간 초과: ${service}`)); }, timeout);
        this.pending.set(id, { resolve, reject, timer });
        // timeout(초)도 rosbridge 에 넘김: rosbridge 기본은 5초라, 오래 걸리는 서비스(Nav2 STARTUP 등)를
        // 브라우저가 기다리는 동안 rosbridge 가 먼저 실패로 끝내 버리지 않게
        const msg = { op: 'call_service', id, service, args, timeout: timeout / 1000 };
        if (type) msg.type = type;
        this.send(msg);
      });
    }

    // ---- actions ----
    /** 반환: {id, result: Promise<{status, values}>, cancel()} */
    sendActionGoal(action, actionType, args, { onFeedback } = {}) {
      const id = this.nextId('act');
      const result = new Promise((resolve, reject) => {
        if (!this.connected) { reject(new Error('rosbridge 미연결')); return; }
        this.actions.set(id, { onFeedback, resolve, reject, action });
        this.send({ op: 'send_action_goal', id, action, action_type: actionType, args, feedback: true });
      });
      return { id, action, result, cancel: () => this.send({ op: 'cancel_action_goal', id, action }) };
    }
  }

  // action_msgs/GoalStatus
  Ros.GOAL_STATUS = { 0: 'UNKNOWN', 1: 'ACCEPTED', 2: 'EXECUTING', 3: 'CANCELING', 4: 'SUCCEEDED', 5: 'CANCELED', 6: 'ABORTED' };

  window.Ros = Ros;
})();
