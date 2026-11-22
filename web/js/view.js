// 2D 캔버스 뷰: 격자, 점유 지도, LiDAR 점, 주행 궤적, 계획 경로, 목표, 로봇
// 좌표는 모두 "표시 프레임"(map 또는 odom)의 미터 단위입니다.
(function () {
  'use strict';

  const COLORS = {
    grid: '#1A2028', grid5: '#232B35', scan: '#F87171', trail: '#2DD4BF', plan: '#60A5FA',
    goal: '#60A5FA', robot: '#2DD4BF', text: '#8B949E',
  };

  class RobotView {
    constructor(canvas, opts = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.scale = opts.scale || 70;          // px per m
      this.center = { x: 0, y: 0 };
      this.follow = opts.follow !== false;
      this.robotUp = !!opts.robotUp;          // true: 로봇 진행 방향이 화면 위쪽
      this.interactive = opts.interactive !== false;
      this.layers = { scan: true, map: true, path: true };
      this.state = { pose: null, scan: [], trail: [], plan: [], goal: null, map: null, draft: null };
      this.goalMode = false;
      this.onGoal = null;                      // (x, y, th) => void
      this.onChange = null;
      this.dirty = true;
      this.resize();
      new ResizeObserver(() => { this.resize(); this.invalidate(); }).observe(canvas);
      if (this.interactive) this.bindPointer();
      const loop = () => { if (this.dirty) { this.dirty = false; this.draw(); } requestAnimationFrame(loop); };
      requestAnimationFrame(loop);
    }

    invalidate() { this.dirty = true; }

    resize() {
      const dpr = window.devicePixelRatio || 1;
      const r = this.canvas.getBoundingClientRect();
      this.W = Math.max(1, r.width);
      this.H = Math.max(1, r.height);
      this.canvas.width = Math.round(this.W * dpr);
      this.canvas.height = Math.round(this.H * dpr);
      this.dpr = dpr;
    }

    rot() { return this.robotUp && this.state.pose ? Math.PI / 2 - this.state.pose.th : 0; }

    toScreen(x, y) {
      const r = this.rot(), s = this.scale, c = Math.cos(r), sn = Math.sin(r);
      const dx = x - this.center.x, dy = y - this.center.y;
      return [this.W / 2 + s * (c * dx - sn * dy), this.H / 2 - s * (sn * dx + c * dy)];
    }

    toWorld(px, py) {
      const r = this.rot(), s = this.scale, c = Math.cos(r), sn = Math.sin(r);
      const u = (px - this.W / 2) / s, v = -(py - this.H / 2) / s;
      return [this.center.x + c * u + sn * v, this.center.y - sn * u + c * v];
    }

    worldTransform() {
      const r = this.rot(), s = this.scale, d = this.dpr;
      const a = s * Math.cos(r), b = -s * Math.sin(r), c = -s * Math.sin(r), dd = -s * Math.cos(r);
      const e = this.W / 2 - (a * this.center.x + c * this.center.y);
      const f = this.H / 2 - (b * this.center.x + dd * this.center.y);
      this.ctx.setTransform(a * d, b * d, c * d, dd * d, e * d, f * d);
    }

    zoom(k, px, py) {
      const before = px !== undefined ? this.toWorld(px, py) : null;
      this.scale = Math.max(8, Math.min(600, this.scale * k));
      if (before && !this.follow) {
        const after = this.toWorld(px, py);
        this.center.x += before[0] - after[0];
        this.center.y += before[1] - after[1];
      }
      this.invalidate();
    }

    setFollow(on) { this.follow = on; if (this.onChange) this.onChange(); this.invalidate(); }

    bindPointer() {
      const cv = this.canvas;
      let drag = null;
      const pos = (e) => { const r = cv.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
      cv.addEventListener('pointerdown', (e) => {
        try { cv.setPointerCapture(e.pointerId); } catch (err) { /* 무시 */ }
        const [px, py] = pos(e);
        if (this.goalMode) {
          const [x, y] = this.toWorld(px, py);
          drag = { goal: true, x, y };
          this.state.draft = { x, y, th: 0 };
        } else {
          drag = { px, py, cx: this.center.x, cy: this.center.y, moved: false };
        }
        this.invalidate();
      });
      cv.addEventListener('pointermove', (e) => {
        if (!drag) return;
        const [px, py] = pos(e);
        if (drag.goal) {
          const [x, y] = this.toWorld(px, py);
          if (Math.hypot(x - drag.x, y - drag.y) > 0.05) this.state.draft.th = Math.atan2(y - drag.y, x - drag.x);
        } else {
          if (!drag.moved && Math.hypot(px - drag.px, py - drag.py) < 4) return;
          if (!drag.moved) { drag.moved = true; this.setFollow(false); }
          const [x0, y0] = this.toWorld(drag.px, drag.py);
          const [x1, y1] = this.toWorld(px, py);
          this.center.x += x0 - x1;
          this.center.y += y0 - y1;
          drag.px = px; drag.py = py;
        }
        this.invalidate();
      });
      const end = () => {
        if (drag && drag.goal && this.state.draft) {
          const g = this.state.draft;
          this.state.draft = null;
          if (this.onGoal) this.onGoal(g.x, g.y, g.th);
        }
        drag = null;
        this.invalidate();
      };
      cv.addEventListener('pointerup', end);
      cv.addEventListener('pointercancel', () => { drag = null; this.state.draft = null; this.invalidate(); });
      cv.addEventListener('wheel', (e) => {
        e.preventDefault();
        const [px, py] = pos(e);
        this.zoom(e.deltaY < 0 ? 1.15 : 1 / 1.15, px, py);
      }, { passive: false });
    }

    // OccupancyGrid -> 오프스크린 캔버스
    static mapImage(msg) {
      const { width: w, height: h } = msg.info;
      const c = document.createElement('canvas');
      c.width = w; c.height = h;
      const g = c.getContext('2d');
      const img = g.createImageData(w, h);
      const d = msg.data;
      for (let i = 0; i < w * h; i += 1) {
        const v = d[i];
        let r, gg, b, a = 255;
        if (v < 0) { r = 22; gg = 27; b = 34; a = 0; }        // 미탐색: 투명
        else if (v < 25) { r = 32; gg = 39; b = 48; }          // 비어 있음
        else if (v < 65) { r = 90; gg = 98; b = 110; }
        else { r = 201; gg = 209; b = 217; }                   // 점유
        const o = i * 4;
        img.data[o] = r; img.data[o + 1] = gg; img.data[o + 2] = b; img.data[o + 3] = a;
      }
      g.putImageData(img, 0, 0);
      return c;
    }

    draw() {
      const { ctx, W, H, dpr } = this;
      const st = this.state;
      if (this.follow && st.pose) { this.center.x = st.pose.x; this.center.y = st.pose.y; }
      else if (this.follow && st.robots) {
        const sel = st.robots.find((r) => r.selected && r.pose);
        if (sel) { this.center.x = sel.pose.x; this.center.y = sel.pose.y; }
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = '#0B0E13';
      ctx.fillRect(0, 0, W, H);

      // 지도
      if (this.layers.map && st.map) {
        const m = st.map;
        this.worldTransform();
        ctx.translate(m.x, m.y);
        ctx.rotate(m.th);
        ctx.scale(m.res, m.res);
        ctx.imageSmoothingEnabled = false;
        ctx.drawImage(m.canvas, 0, 0);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      }

      // 격자 (0.5 m, 2.5 m)
      this.worldTransform();
      const R = Math.hypot(W, H) / 2 / this.scale + 1;
      const cx = this.center.x, cy = this.center.y;
      const drawGrid = (step, color) => {
        ctx.beginPath();
        for (let x = Math.floor((cx - R) / step) * step; x <= cx + R; x += step) { ctx.moveTo(x, cy - R); ctx.lineTo(x, cy + R); }
        for (let y = Math.floor((cy - R) / step) * step; y <= cy + R; y += step) { ctx.moveTo(cx - R, y); ctx.lineTo(cx + R, y); }
        ctx.strokeStyle = color;
        ctx.lineWidth = 1 / this.scale;
        ctx.stroke();
      };
      if (this.scale > 20) drawGrid(0.5, COLORS.grid);
      drawGrid(2.5, COLORS.grid5);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      const line = (pts, color, width, dash) => {
        if (!pts || pts.length < 2) return;
        ctx.beginPath();
        pts.forEach((p, i) => { const [sx, sy] = this.toScreen(p[0], p[1]); if (i) ctx.lineTo(sx, sy); else ctx.moveTo(sx, sy); });
        ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash || []); ctx.stroke(); ctx.setLineDash([]);
      };

      if (this.layers.path) {
        line(st.trail, 'rgba(45,212,191,0.55)', 2.5, [6, 5]);
        line(st.plan, COLORS.plan, 2.5);
      }

      // LiDAR
      if (this.layers.scan && st.scan.length) {
        ctx.fillStyle = COLORS.scan;
        const sz = this.scale > 60 ? 2.6 : 2;
        for (const p of st.scan) {
          const [sx, sy] = this.toScreen(p[0], p[1]);
          ctx.fillRect(sx - sz / 2, sy - sz / 2, sz, sz);
        }
      }

      const goalMarker = (g, alpha) => {
        const [sx, sy] = this.toScreen(g.x, g.y);
        ctx.globalAlpha = alpha;
        ctx.beginPath(); ctx.arc(sx, sy, 14, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(96,165,250,0.18)'; ctx.fill();
        ctx.strokeStyle = COLORS.goal; ctx.lineWidth = 2; ctx.stroke();
        const [hx, hy] = this.toScreen(g.x + Math.cos(g.th) * 0.35, g.y + Math.sin(g.th) * 0.35);
        ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(hx, hy); ctx.stroke();
        ctx.globalAlpha = 1;
      };
      if (st.goal) goalMarker(st.goal, 1);
      if (st.draft) goalMarker(st.draft, 0.7);

      // 여러 로봇 (플릿 뷰): {pose, color, name, selected, plan, goal, scan}
      if (st.robots && st.robots.length) {
        for (const r of st.robots) {
          if (this.layers.path && r.plan) line(r.plan, r.color, 2.5);
          if (r.goal) {
            const [gx, gy] = this.toScreen(r.goal.x, r.goal.y);
            ctx.beginPath(); ctx.arc(gx, gy, 12, 0, Math.PI * 2);
            ctx.strokeStyle = r.color; ctx.lineWidth = 2; ctx.stroke();
            ctx.beginPath(); ctx.moveTo(gx - 5, gy); ctx.lineTo(gx + 5, gy); ctx.moveTo(gx, gy - 5); ctx.lineTo(gx, gy + 5); ctx.stroke();
          }
          if (this.layers.scan && r.scan && r.scan.length) {
            ctx.fillStyle = r.color;
            ctx.globalAlpha = 0.8;
            for (const p of r.scan) { const [sx, sy] = this.toScreen(p[0], p[1]); ctx.fillRect(sx - 1, sy - 1, 2, 2); }
            ctx.globalAlpha = 1;
          }
        }
        for (const r of st.robots) {
          if (!r.pose) continue;
          const p = r.pose;
          const [sx, sy] = this.toScreen(p.x, p.y);
          const rad = Math.max(11, 0.1 * this.scale);
          ctx.globalAlpha = r.stale ? 0.45 : 1;
          ctx.beginPath(); ctx.arc(sx, sy, rad + (r.selected ? 5 : 0), 0, Math.PI * 2);
          ctx.fillStyle = r.selected ? `${r.color}40` : `${r.color}22`; ctx.fill();
          ctx.strokeStyle = r.color; ctx.lineWidth = r.selected ? 2.5 : 1.5;
          if (r.stale) ctx.setLineDash([3, 3]);
          ctx.stroke(); ctx.setLineDash([]);
          const pt = (dx, dy) => this.toScreen(p.x + Math.cos(p.th) * dx - Math.sin(p.th) * dy, p.y + Math.sin(p.th) * dx + Math.cos(p.th) * dy);
          const k = rad / this.scale;
          const a = pt(0.8 * k, 0), b = pt(-0.45 * k, -0.5 * k), c = pt(-0.25 * k, 0), d = pt(-0.45 * k, 0.5 * k);
          ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.lineTo(c[0], c[1]); ctx.lineTo(d[0], d[1]); ctx.closePath();
          ctx.fillStyle = r.color; ctx.fill();
          ctx.font = '600 12px Inter, "Noto Sans KR", sans-serif';
          ctx.fillText(r.name, sx + rad + 6, sy - rad);
          ctx.globalAlpha = 1;
        }
      }

      // 로봇
      if (st.robots) {
        if (!st.robots.some((r) => r.pose)) {
          ctx.fillStyle = COLORS.text;
          ctx.font = '13px Inter, "Noto Sans KR", sans-serif';
          ctx.textAlign = 'center';
          ctx.fillText(this.emptyText || '지도 위 위치를 아는 로봇이 없습니다', W / 2, H / 2);
          ctx.textAlign = 'start';
        }
      } else if (st.pose) {
        const p = st.pose;
        const [sx, sy] = this.toScreen(p.x, p.y);
        const rad = Math.max(10, 0.105 * this.scale);
        ctx.beginPath(); ctx.arc(sx, sy, rad, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(45,212,191,0.15)'; ctx.fill();
        ctx.strokeStyle = COLORS.robot; ctx.lineWidth = 1.5; ctx.stroke();
        const pt = (dx, dy) => this.toScreen(p.x + Math.cos(p.th) * dx - Math.sin(p.th) * dy, p.y + Math.sin(p.th) * dx + Math.cos(p.th) * dy);
        const k = rad / this.scale;
        const a = pt(0.8 * k, 0), b = pt(-0.45 * k, -0.5 * k), c = pt(-0.25 * k, 0), d = pt(-0.45 * k, 0.5 * k);
        ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.lineTo(c[0], c[1]); ctx.lineTo(d[0], d[1]); ctx.closePath();
        ctx.fillStyle = COLORS.robot; ctx.fill();
      } else {
        ctx.fillStyle = COLORS.text;
        ctx.font = '13px Inter, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('/odom 수신 대기 중…', W / 2, H / 2);
        ctx.textAlign = 'start';
      }
    }
  }

  // LaserScan -> 로봇(base_footprint) 기준 점 배열. offset: base_footprint 기준 LiDAR 자세 {x, y, th}
  function scanPoints(scan, step = 1, offset) {
    const pts = [];
    const o = offset || { x: 0, y: 0, th: 0 };
    const c = Math.cos(o.th), s = Math.sin(o.th);
    const n = scan.ranges.length;
    for (let i = 0; i < n; i += step) {
      const r = scan.ranges[i];
      if (r === null || !Number.isFinite(r) || r < scan.range_min || r > scan.range_max) continue;
      const a = scan.angle_min + i * scan.angle_increment;
      const lx = r * Math.cos(a), ly = r * Math.sin(a);
      pts.push([o.x + c * lx - s * ly, o.y + s * lx + c * ly]);
    }
    return pts;
  }

  window.RobotView = RobotView;
  window.scanPoints = scanPoints;
})();
