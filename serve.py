"""ros2-fleet-console/web 정적 파일 서버 + 로봇 목록 API.

- 정적 파일: 캐시 비활성화 (파일을 고치면 새로고침만으로 반영)
- GET  /api/fleet   : ros2-fleet-console/config/fleet.json 읽기
- PUT  /api/fleet   : 검증 후 저장 (로봇 등록 페이지). 헤더 X-Admin-Token 필요 (ros2-fleet-console/config/admin_token)
- GET  /api/control : 로봇별 현재 조종자
- POST /api/control : 조종 권한 lease (3초, 조종 중 1초마다 갱신) 받기·갱신·놓기·넘겨받기
- GET  /api/maps              : 저장된 지도 목록 (config/maps)
- GET  /api/maps/<name>.png   : 지도 이미지
- GET  /api/zones/<name>      : 그 지도의 구역
- PUT  /api/zones/<name>      : 구역 저장 + 로봇별 keepout 마스크 생성 (토큰)
- GET  /api/mapping           : 지도 만들기 상태
- POST /api/mapping/start|stop|save : 여러 로봇 slam_toolbox + 지도 합치기 실행·정지, 저장 (토큰)
- GET  /api/mode, POST /api/mode    : 운영 모드 (operate | mapping | patrol, 바꾸기는 토큰)
- GET/PUT /api/patrol/<지도>        : 순찰 경로·감지 설정 (저장은 토큰)
- GET  /api/alerts?map=&status=open&robot= : 경보 목록 (robot: 그 로봇이 본 경보만), GET /api/alerts/<id> : 상세(주변 스캔 포함)
- POST /api/alerts/<id> {status}    : 이상 없음(ok) / 오탐(false) / 다시 열기(open)
- POST /api/alerts/<id>/baseline    : 기준 지도에 반영 (토큰)
- GET  /api/places?map=<지도>       : 그 지도의 이름 붙인 지점 목록
- POST /api/places {map,name,x,y,yaw,note} : 지점 추가 (토큰)
- PUT  /api/places/<id>, DELETE /api/places/<id> : 지점 고치기·지우기 (토큰)
- GET  /api/routes?map=<지도>       : 이동 경로 목록 (지점 순서 포함)
- POST /api/routes {map,name,note,points:[지점 id..]}, PUT|DELETE /api/routes/<id> : 경로 CRUD (토큰)
- GET  /api/assignments             : 로봇별 경로 할당·지금 실행의 진행 + 실행기 상태
- GET  /api/assignments/<로봇>/runs : 그 로봇의 최근 실행과 이력 (시작·멈춤·바퀴 완료·실패 …)
- PUT  /api/assignments/<로봇> {route_id, repeat_count}, DELETE : 할당·해제 (토큰)
- POST /api/assignments/<로봇>/start|resume : 시작(처음부터)·이어서 (토큰, 운영 모드·활성 지도만)
- POST /api/assignments/<로봇>/pause|stop, POST /api/assignments/pause-all : 멈춤·정지 (토큰 불필요, 같은 출처만)
- GET  /api/safety : LiDAR 안전 구역 (safety.py, DB) 플릿 기본값 + 로봇별 적용 값(robots) + 최근 변경 이력(changes)
- PUT  /api/safety {enabled, stop_dist, slow_dist, slow_speed, by} : 플릿 기본값
- PUT  /api/safety/robots/<로봇> {...}, DELETE /api/safety/robots/<로봇>?by= : 로봇별 덮어쓰기 저장·지우기(기본값으로)
       어떤 로봇의 적용 값도 켜기만 바뀌는 요청은 토큰 없이(같은 출처만), 끄기·값 바꾸기는 토큰

데이터는 SQLite 파일 하나(data/fleet.db, 스키마는 fleet_db.py)에 있고, 서버를 켤 때 없으면 만듭니다.
쓰기 요청은 같은 출처(Origin = Host)만 허용합니다. rosbridge 자체에는 인증이 없으므로
신뢰할 수 있는 네트워크에서만 사용하세요.
"""
import argparse
import functools
import hmac
import http.server
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.parse

import sqlite3

import dbutil
import fleet_db
import maps
import patrol_store as store
import places
import safety

APP_DIR = os.path.dirname(os.path.abspath(__file__))
FLEET_PATH = os.path.join(APP_DIR, 'config', 'fleet.json')
DEFAULT_FLEET = {'mapTopic': '/map', 'robots': []}

ID_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,40}$')
NS_RE = re.compile(r'^(/[A-Za-z][A-Za-z0-9_]*)*$')          # '' 또는 /a/b
TOPIC_RE = re.compile(r'^/[A-Za-z][A-Za-z0-9_/]*$')
PREFIX_RE = re.compile(r'^([A-Za-z][A-Za-z0-9_]*/)?$')       # '' 또는 name/
COLOR_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')
TYPES = {'turtlebot3', 'pinky'}


def validate_fleet(data):
    if not isinstance(data, dict) or not isinstance(data.get('robots'), list):
        raise ValueError('robots 배열이 필요합니다')
    if len(data['robots']) > 32:
        raise ValueError('로봇은 최대 32대까지 등록할 수 있습니다')
    map_topic = data.get('mapTopic', '/map')
    if not isinstance(map_topic, str) or not TOPIC_RE.match(map_topic):
        raise ValueError('mapTopic 형식이 올바르지 않습니다')
    seen_id, seen_ns, robots = set(), set(), []
    for i, r in enumerate(data['robots']):
        if not isinstance(r, dict):
            raise ValueError(f'robots[{i}] 가 객체가 아닙니다')
        out = {}
        checks = [('id', ID_RE), ('ns', NS_RE), ('tfTopic', TOPIC_RE), ('tfPrefix', PREFIX_RE), ('color', COLOR_RE)]
        for key, rx in checks:
            v = r.get(key, '')
            if not isinstance(v, str) or not rx.match(v):
                raise ValueError(f'robots[{i}].{key} 형식이 올바르지 않습니다: {v!r}')
            out[key] = v
        name = r.get('name', '')
        if not isinstance(name, str) or not (1 <= len(name) <= 40):
            raise ValueError(f'robots[{i}].name 은 1~40자여야 합니다')
        out['name'] = name
        if r.get('type') not in TYPES:
            raise ValueError(f'robots[{i}].type 은 {sorted(TYPES)} 중 하나여야 합니다')
        out['type'] = r['type']
        if out['id'] in seen_id:
            raise ValueError(f'id 중복: {out["id"]}')
        if out['ns'] in seen_ns:
            raise ValueError(f'네임스페이스 중복: {out["ns"] or "(없음)"}')
        seen_id.add(out['id'])
        seen_ns.add(out['ns'])
        robots.append(out)
    out = {'mapTopic': map_topic, 'robots': robots}
    map_file = data.get('mapFile', '')
    if map_file:
        if not isinstance(map_file, str) or not maps.NAME_RE.match(map_file):
            raise ValueError('mapFile 형식이 올바르지 않습니다')
        out['mapFile'] = map_file
    return out


def read_fleet():
    try:
        with open(FLEET_PATH, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return DEFAULT_FLEET


def write_fleet(data):
    os.makedirs(os.path.dirname(FLEET_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(FLEET_PATH), suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, FLEET_PATH)


TOKEN_PATH = os.path.join(APP_DIR, 'config', 'admin_token')
LEASE_SEC = 3.0
_control = {}                 # robot id -> {'client', 'label', 'until'}
_control_lock = threading.Lock()


def admin_token():
    """로봇 목록 저장용 관리자 토큰. 없으면 만들어 ros2-fleet-console/config/admin_token (0600) 에 저장."""
    env = os.environ.get('TB3_WEB_TOKEN')
    if env:
        return env
    try:
        with open(TOKEN_PATH, encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
        tok = secrets.token_urlsafe(12)
        fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(tok + '\n')
        return tok


def control(req):
    """조종 권한(lease). action: acquire | renew | release | force | status"""
    rid, client = str(req.get('robot', '')), str(req.get('client', ''))
    label = str(req.get('label', ''))[:40]
    action = req.get('action', 'status')
    if not ID_RE.match(rid) or (action != 'status' and not re.match(r'^[A-Za-z0-9_-]{4,64}$', client)):
        raise ValueError('robot/client 형식이 올바르지 않습니다')
    now = time.monotonic()
    with _control_lock:
        cur = _control.get(rid)
        if cur and cur['until'] < now:
            cur = None
            _control.pop(rid, None)
        ok = False
        if action in ('acquire', 'renew'):
            if cur is None or cur['client'] == client:
                _control[rid] = {'client': client, 'label': label, 'until': now + LEASE_SEC}
                ok = True
        elif action == 'force':
            _control[rid] = {'client': client, 'label': label, 'until': now + LEASE_SEC}
            ok = True
        elif action == 'release':
            if cur and cur['client'] == client:
                _control.pop(rid, None)
            ok = True
        else:
            ok = True
        cur = _control.get(rid)
        holder = {'client': cur['client'], 'label': cur['label'], 'remaining': round(cur['until'] - now, 2)} if cur else None
    return {'ok': ok, 'holder': holder}


def control_all():
    now = time.monotonic()
    with _control_lock:
        return {rid: {'client': c['client'], 'label': c['label'], 'remaining': round(c['until'] - now, 2)}
                for rid, c in _control.items() if c['until'] >= now}


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        super().end_headers()

    def log_message(self, fmt, *args):
        if args and '/api/control' in str(args[0]):
            return  # 조종 권한 갱신(초당 1회)은 로그 생략
        super().log_message(fmt, *args)

    def _same_origin(self):
        """다른 사이트에서 보낸 쓰기 요청(CSRF) 차단: Origin 이 있으면 Host 와 같아야 함."""
        origin = self.headers.get('Origin')
        if not origin:
            return True
        return urllib.parse.urlparse(origin).netloc == self.headers.get('Host', '')

    def _body(self):
        n = int(self.headers.get('Content-Length', '0'))
        if n > 512 * 1024:
            raise ValueError('요청이 너무 큽니다')
        return json.loads(self.rfile.read(n).decode('utf-8') or '{}')

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not self._same_origin():
            self._json(403, {'error': '다른 출처의 요청은 허용하지 않습니다'})
            return False
        if not hmac.compare_digest(self.headers.get('X-Admin-Token', ''), admin_token()):
            self._json(401, {'error': '관리자 토큰이 필요합니다 (ros2-fleet-console/config/admin_token)'})
            return False
        return True

    def _robot(self, rid):
        r = next((x for x in read_fleet().get('robots', []) if x['id'] == rid), None)
        if r is None:
            raise ValueError(f'등록되지 않은 로봇: {rid}')
        return r

    def _query(self):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))

    def do_GET(self):
        path = self.path.split('?')[0]
        try:
            if path == '/api/fleet':
                return self._json(200, read_fleet())
            if path == '/api/control':
                return self._json(200, control_all())
            if path == '/api/maps':
                return self._json(200, maps.list_maps())
            m = re.match(r'^/api/maps/([^/]+)\.png$', path)
            if m:
                body = maps.map_png(m.group(1))
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return None
            m = re.match(r'^/api/zones/([^/]+)$', path)
            if m:
                return self._json(200, maps.read_zones(m.group(1)))
            if path == '/api/mapping':
                return self._json(200, MAPPING.status())
            if path == '/api/mode':
                return self._json(200, mode_status())
            if path == '/api/safety':
                with DB_LOCK:
                    con = DB()
                    return self._json(200, safety.snapshot(con, self._query().get('limit', 10)))
            m = re.match(r'^/api/patrol/([^/]+)$', path)
            if m:
                return self._json(200, store.read_patrol(m.group(1)))
            if path == '/api/alerts':
                q = self._query()
                with DB_LOCK:
                    con = DB()
                    items = store.list_alerts(con, q.get('map') or None, q.get('status'), q.get('limit', 100), int(q.get('since', 0)),
                                              q.get('robot') or None)
                    cnt = store.counts(con, q['map']) if q.get('map') else None
                return self._json(200, {'alerts': items, 'counts': cnt})
            if path == '/api/routes':
                name = self._query().get('map', '')
                with DB_LOCK:
                    return self._json(200, {'map': name, 'routes': places.list_routes(DB(), name)})
            if path == '/api/assignments':
                with DB_LOCK:
                    items = places.list_assignments(DB())
                return self._json(200, {'assignments': items, 'runner': ROUTES.status(), 'mode': store.read_mode()['mode'],
                                        'map': read_fleet().get('mapFile')})
            m = re.match(r'^/api/assignments/([A-Za-z][A-Za-z0-9_]{0,40})/runs$', path)
            if m:
                with DB_LOCK:
                    return self._json(200, {'robot': m.group(1), 'runs': places.list_runs(DB(), m.group(1), int(self._query().get('limit', 10)))})
            if path == '/api/places':
                name = self._query().get('map', '')
                with DB_LOCK:
                    return self._json(200, {'map': name, 'places': places.list_places(DB(), name)})
            m = re.match(r'^/api/alerts/(\d+)$', path)
            if m:
                with DB_LOCK:
                    a = store.get_alert(DB(), int(m.group(1)))
                return self._json(200, a) if a else self._json(404, {'error': '없는 경보입니다'})
        except FileNotFoundError:
            return self._json(404, {'error': '없는 지도입니다'})
        except ValueError as e:
            return self._json(400, {'error': str(e)})
        return super().do_GET()

    def do_POST(self):
        path = self.path.split('?')[0]
        if not self._same_origin():
            return self._json(403, {'error': '다른 출처의 요청은 허용하지 않습니다'})
        try:
            if path == '/api/control':
                return self._json(200, control(self._body()))
            # 경보 처리(이상 없음 / 오탐 / 다시 열기)는 관제 담당자 누구나 (토큰 불필요, 같은 출처만)
            m = re.match(r'^/api/alerts/(\d+)$', path)
            if m:
                body = self._body()
                st = str(body.get('status', ''))
                if st == 'baseline':
                    return self._json(400, {'error': '기준 지도 반영은 /api/alerts/<id>/baseline (토큰)'})
                with DB_LOCK:
                    return self._json(200, store.set_status(DB(), int(m.group(1)), st, body.get('note', '')))
        except KeyError:
            return self._json(404, {'error': '없는 경보입니다'})
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {'error': str(e)})

        if path == '/api/places':
            return self._places_write('create')
        if path == '/api/routes':
            return self._routes_write('create')
        # 멈춤·정지(안전 쪽)는 토큰 없이, 시작·이어서(로봇이 스스로 움직임)는 토큰
        if path == '/api/assignments/pause-all':
            with DB_LOCK:
                n = places.pause_all(DB(), '비상정지')
            return self._json(200, {'paused': n})
        ma = re.match(r'^/api/assignments/([A-Za-z][A-Za-z0-9_]{0,40})/(start|resume|pause|stop)$', path)
        if ma:
            if ma.group(2) in ('start', 'resume') and not self._authorized():
                return None
            try:
                return self._json(200, assignment_action(ma.group(1), ma.group(2)))
            except KeyError:
                return self._json(404, {'error': '할당된 경로가 없습니다'})
            except ValueError as e:
                return self._json(400, {'error': str(e)})

        m = re.match(r'^/api/(mapping/(start|stop|save)|mode|alerts/(\d+)/baseline)$', path)
        if not m:
            return self._json(404, {'error': 'not found'})
        if not self._authorized():
            return None
        try:
            body = self._body()
            if path == '/api/mode':
                return self._json(200, set_mode(str(body.get('mode', ''))))
            if m.group(3):
                with DB_LOCK:
                    con = DB()
                    a = store.get_alert(con, int(m.group(3)))
                    if not a:
                        return self._json(404, {'error': '없는 경보입니다'})
                    n = maps.apply_to_baseline(a['map'], a['kind'], a['x'], a['y'], a['w'], a['h'])
                    a = store.set_status(con, a['id'], 'baseline', body.get('note', '기준 지도에 반영'))
                return self._json(200, {'alert': a, 'cells': n})
            if m.group(2) == 'start':
                robots = []
                for item in body.get('robots', []):
                    r = dict(self._robot(str(item.get('id', ''))))
                    r.update({k: float(item.get(k, 0)) for k in ('x', 'y', 'yaw')})
                    robots.append(r)
                return self._json(200, MAPPING.start(robots, bool(body.get('explore'))))
            if m.group(2) == 'stop':
                return self._json(200, MAPPING.stop())
            saved = MAPPING.save(str(body.get('name', '')))
            with DB_LOCK:
                places.ensure_map(DB(), saved['name'])
            return self._json(200, saved)
        except FileNotFoundError as e:
            return self._json(500, {'error': f'파일을 찾지 못했습니다 (ROS 환경에서 실행했는지, 지도가 있는지 확인): {e}'})
        except (ValueError, KeyError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            return self._json(400, {'error': str(e)})

    def _places_write(self, action, pid=None):
        """지점 추가·고치기·지우기 (토큰 필요)"""
        if not self._authorized():
            return None
        try:
            body = self._body() if action != 'delete' else {}
            with DB_LOCK:
                con = DB()
                if action == 'create':
                    maps.map_info(str(body.get('map', '')))    # 저장된 지도인지 확인
                    return self._json(201, places.create_place(con, body))
                if action == 'update':
                    return self._json(200, places.update_place(con, pid, body))
                return self._json(200, places.delete_place(con, pid))
        except FileNotFoundError:
            return self._json(404, {'error': '없는 지도입니다'})
        except KeyError:
            return self._json(404, {'error': '없는 지점입니다'})
        except sqlite3.IntegrityError:
            return self._json(400, {'error': '이 지도에 같은 이름의 지점이 있습니다'})
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {'error': str(e)})

    def _routes_write(self, action, key=None):
        """이동 경로(create/update/delete)·할당(assign/unassign) 쓰기 (토큰 필요)"""
        if not self._authorized():
            return None
        try:
            body = self._body() if action in ('create', 'update', 'assign') else {}
            with DB_LOCK:
                con = DB()
                if action == 'create':
                    maps.map_info(str(body.get('map', '')))
                    return self._json(201, places.create_route(con, body))
                if action == 'update':
                    return self._json(200, places.update_route(con, key, body))
                if action == 'delete':
                    return self._json(200, places.delete_route(con, key))
                if action == 'assign':
                    return self._json(200, places.set_assignment(con, key, body))
                return self._json(200, places.delete_assignment(con, key))
        except FileNotFoundError:
            return self._json(404, {'error': '없는 지도입니다'})
        except KeyError:
            return self._json(404, {'error': '없는 경로 또는 할당입니다'})
        except sqlite3.IntegrityError as e:
            return self._json(400, {'error': f'저장할 수 없습니다: {e}'})
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {'error': str(e)})

    def _safety_write(self, robot_id=None, clear=False):
        """LiDAR 안전 구역 저장: 플릿 기본값(robot_id 없음) 또는 로봇별 덮어쓰기 저장·지우기(clear).
        켜기만 하는 요청(안전 쪽)은 토큰 없이, 끄기·거리·속도 바꾸기는 토큰"""
        if not self._same_origin():
            return self._json(403, {'error': '다른 출처의 요청은 허용하지 않습니다'})
        try:
            body = None if clear else self._body()
            with DB_LOCK:
                con = DB()
                if robot_id is None:
                    need = safety.fleet_needs_token(con, body)
                else:
                    if robot_id not in safety.robot_overrides(con) and clear:
                        need = False
                    else:
                        need = safety.robot_needs_token(con, robot_id, body)
            if need and not self._authorized():
                return None
            with DB_LOCK:
                con = DB()
                if robot_id is None:
                    return self._json(200, safety.write_safety(con, body))
                if clear:
                    return self._json(200, safety.clear_robot(con, robot_id, self._query().get('by', '')))
                return self._json(200, safety.write_robot(con, robot_id, body))
        except KeyError:
            return self._json(404, {'error': '등록되지 않은 로봇입니다'})
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {'error': str(e)})

    def do_DELETE(self):
        path = self.path.split('?')[0]
        m = re.match(r'^/api/places/(\d+)$', path)
        if m:
            return self._places_write('delete', int(m.group(1)))
        m = re.match(r'^/api/routes/(\d+)$', path)
        if m:
            return self._routes_write('delete', int(m.group(1)))
        m = re.match(r'^/api/assignments/([A-Za-z][A-Za-z0-9_]{0,40})$', path)
        if m:
            return self._routes_write('unassign', m.group(1))
        m = re.match(r'^/api/safety/robots/([A-Za-z][A-Za-z0-9_]{0,40})$', path)
        if m:
            return self._safety_write(m.group(1), clear=True)
        return self._json(404, {'error': 'not found'})

    def do_PUT(self):
        path = self.path.split('?')[0]
        mpl = re.match(r'^/api/places/(\d+)$', path)
        if mpl:
            return self._places_write('update', int(mpl.group(1)))
        mrt = re.match(r'^/api/routes/(\d+)$', path)
        if mrt:
            return self._routes_write('update', int(mrt.group(1)))
        mas = re.match(r'^/api/assignments/([A-Za-z][A-Za-z0-9_]{0,40})$', path)
        if mas:
            return self._routes_write('assign', mas.group(1))
        if path == '/api/safety':
            return self._safety_write()
        msr = re.match(r'^/api/safety/robots/([A-Za-z][A-Za-z0-9_]{0,40})$', path)
        if msr:
            return self._safety_write(msr.group(1))
        m = re.match(r'^/api/zones/([^/]+)$', path)
        mp = re.match(r'^/api/patrol/([^/]+)$', path)
        if path != '/api/fleet' and not m and not mp:
            return self._json(404, {'error': 'not found'})
        if not self._authorized():
            return None
        try:
            if m:
                zones, masks = maps.write_zones(m.group(1), self._body(), read_fleet().get('robots', []))
                return self._json(200, {'zones': zones, 'masks': masks})
            if mp:
                maps.map_info(mp.group(1))
                ids = {r['id'] for r in read_fleet().get('robots', [])}
                return self._json(200, store.write_patrol(mp.group(1), self._body(), ids))
            data = validate_fleet(self._body())
            write_fleet(data)
            with DB_LOCK:   # robots 테이블도 맞춤 (빠진 로봇은 active = 0, 이력은 남김)
                places.sync_robots(DB(), data['robots'])
            return self._json(200, data)
        except FileNotFoundError:
            return self._json(404, {'error': '없는 지도입니다'})
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {'error': str(e)})


MAPPING = maps.Mapping()
PATROL = maps.Patrol()
ROUTES = maps.RouteRunner()
DB_LOCK = threading.RLock()   # data/fleet.db 연결 하나를 모든 요청 스레드가 이 잠금으로 나눠 씀
_db_con = None


def DB():
    """data/fleet.db 연결 (DB_LOCK 안에서). 파일이 없으면(처음·실행 중 지워짐) 새로 만들고
    로봇·지도 목록을 다시 채움 (지점·할당·경보의 외래 키 대상)."""
    global _db_con
    _db_con, fresh = dbutil.ensure(_db_con, fleet_db.DB_PATH, fleet_db.MIGRATIONS)
    if fresh:
        places.sync_robots(_db_con, read_fleet().get('robots', []))
        places.sync_maps(_db_con, [m['name'] for m in maps.list_maps()])
    return _db_con
MODE_LOCK = threading.RLock()
WATCHDOG = {'restarts': 0, 'last': None}


def mode_status():
    m = store.read_mode()
    return {**m, 'mapping': MAPPING.status(), 'patrol': {**PATROL.status(), 'restarts': WATCHDOG['restarts'], 'lastRestart': WATCHDOG['last']},
            'map': read_fleet().get('mapFile')}


def assignment_action(robot_id, action):
    """경로 시작·이어서·멈춤·정지. 시작·이어서는 운영 모드에서만, 실행기가 없으면 띄움."""
    with MODE_LOCK:
        if action in ('start', 'resume'):
            mode = store.read_mode()['mode']
            if mode != 'operate':
                raise ValueError(f'운영 모드에서만 경로를 시작할 수 있습니다 (지금: {mode})')
        with DB_LOCK:
            a = places.control_assignment(DB(), robot_id, action, read_fleet().get('mapFile'))
        if action in ('start', 'resume') and not ROUTES.running():
            try:
                ROUTES.start(FLEET_PATH, fleet_db.DB_PATH)
            except Exception as e:  # noqa: BLE001
                with DB_LOCK:
                    places.pause_all(DB(), f'실행기를 시작하지 못함: {e}')
                raise ValueError(f'경로 실행기를 시작하지 못했습니다 (ROS 환경에서 실행했는지 확인): {e}') from e
    return {'assignment': a, 'runner': ROUTES.status()}


def route_watchdog():
    """실행 중인 경로가 있는데 실행기가 죽어 있으면 다시 띄움 (운영 모드에서만). 연달아 죽으면 간격을 늘림."""
    wait = 5
    while True:
        time.sleep(wait)
        try:
            with MODE_LOCK:
                with DB_LOCK:
                    busy = places.any_running(DB())
                if not busy or ROUTES.running() or store.read_mode()['mode'] != 'operate':
                    wait = 5
                    continue
                print(f'경로 실행기가 멈춰 있어 다시 시작합니다 (exit={ROUTES.status().get("exited")})', flush=True)
                ROUTES.start(FLEET_PATH, fleet_db.DB_PATH)
                wait = min(60, wait * 2)
        except Exception as e:  # noqa: BLE001
            print(f'경로 실행기 감시 오류: {e}', flush=True)


def patrol_watchdog():
    """경비 모드인데 patrol_node 가 죽어 있으면 다시 띄움 (죽은 채로 두면 경보가 조용히 멈춤). 연달아 죽으면 간격을 늘림."""
    wait = 5
    while True:
        time.sleep(wait)
        try:
            with MODE_LOCK:
                if store.read_mode()['mode'] != 'patrol' or PATROL.running():
                    wait = 5
                    continue
                st = PATROL.status()
                print(f'경비 프로세스가 멈춰 있어 다시 시작합니다 (exit={st.get("exited")})', flush=True)
                PATROL.start(read_fleet().get('mapFile'), FLEET_PATH, fleet_db.DB_PATH)
                WATCHDOG['restarts'] += 1
                WATCHDOG['last'] = time.time()
                wait = min(60, wait * 2)
        except Exception as e:  # noqa: BLE001
            print(f'경비 감시 오류: {e}', flush=True)


def set_mode(mode):
    """운영 모드 바꾸기. 경비 = patrol_node 시작, 다른 모드로 가면 경비·지도 만들기 프로세스 정지."""
    if mode not in store.MODES:
        raise ValueError('모드는 operate | mapping | patrol')
    with MODE_LOCK:
        return _set_mode(mode)


def _set_mode(mode):
    if mode != 'operate':   # 경비·지도 만들기는 로봇을 따로 움직이므로 이동 경로는 멈춤 (나중에 이어서 가능)
        with DB_LOCK:
            places.pause_all(DB(), '모드 변경으로 멈춤')
        ROUTES.stop()
    if mode == 'patrol':
        map_name = read_fleet().get('mapFile')
        if not map_name:
            raise ValueError('활성 지도가 없습니다. 먼저 지도를 만들고 활성 지도로 지정하세요')
        MAPPING.stop()
        if not PATROL.running():
            PATROL.start(map_name, FLEET_PATH, fleet_db.DB_PATH)
    else:
        PATROL.stop()
        if mode != 'mapping':
            MAPPING.stop()
    store.write_mode(mode)
    return mode_status()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--bind', default='0.0.0.0')
    ap.add_argument('--dir', default=os.path.join(APP_DIR, 'web'))
    a = ap.parse_args()
    admin_token()  # 없으면 생성
    # DB 파일(data/fleet.db)이 없으면 여기서 만듦. 있으면 스키마 버전을 확인해 필요하면 올림
    with DB_LOCK:
        con = DB()
        places.sync_robots(con, read_fleet().get('robots', []))
        places.sync_maps(con, [m['name'] for m in maps.list_maps()])
        # 서버를 다시 켜도 로봇이 혼자 경로를 이어 가지 않게: 실행 중이던 경로는 멈춤
        n = places.pause_all(con, '서버 재시작으로 멈춤 (경로 화면에서 이어서)')
        ver = dbutil.version(con)
    print(f'DB: {fleet_db.DB_PATH} (스키마 버전 {ver})', flush=True)
    old = [f for f in ('places.db', 'alerts.db') if os.path.exists(os.path.join(os.path.dirname(fleet_db.DB_PATH), f))]
    if old:   # DB 를 하나로 합치기 전의 파일 (자동으로 옮기지 않음)
        print(f'참고: data/ 의 {", ".join(old)} (-wal, -shm, .bak-v* 포함)는 더 이상 쓰지 않습니다. 필요 없으면 지워도 됩니다', flush=True)
    if n:
        print(f'실행 중이던 이동 경로 {n}개를 멈춤 상태로 바꿨습니다', flush=True)
    if store.read_mode()['mode'] == 'patrol':   # 다시 켰을 때 경비 모드였으면 이어서
        try:
            set_mode('patrol')
        except Exception as e:  # noqa: BLE001
            print(f'경비 모드를 다시 시작하지 못했습니다: {e}', flush=True)
            store.write_mode('operate')
    elif store.read_mode()['mode'] == 'mapping':
        store.write_mode('operate')   # 지도 만들기는 이어서 하지 않음
    threading.Thread(target=patrol_watchdog, daemon=True).start()
    threading.Thread(target=route_watchdog, daemon=True).start()
    handler = functools.partial(Handler, directory=a.dir)
    with http.server.ThreadingHTTPServer((a.bind, a.port), handler) as srv:
        print(f'TB3 web: http://{a.bind}:{a.port}/ ({a.dir}), fleet: {FLEET_PATH}', flush=True)
        print(f'로봇 목록 저장용 관리자 토큰: {TOKEN_PATH} (또는 환경 변수 TB3_WEB_TOKEN)', flush=True)
        try:
            srv.serve_forever()
        finally:
            MAPPING.stop()   # 서버를 끄면 지도 만들기·경비·경로 실행 프로세스도 정지
            PATROL.stop()
            ROUTES.stop()


if __name__ == '__main__':
    main()
