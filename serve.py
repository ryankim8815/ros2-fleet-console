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
- GET  /api/mapping           : 지도 작성 상태
- POST /api/mapping/start|stop|save : 로봇 한 대로 slam_toolbox 실행·정지, 지도 저장 (토큰)

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

import maps

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
        except FileNotFoundError:
            return self._json(404, {'error': '없는 지도입니다'})
        except ValueError as e:
            return self._json(400, {'error': str(e)})
        return super().do_GET()

    def do_POST(self):
        path = self.path.split('?')[0]
        if path == '/api/control':
            if not self._same_origin():
                return self._json(403, {'error': '다른 출처의 요청은 허용하지 않습니다'})
            try:
                return self._json(200, control(self._body()))
            except (ValueError, json.JSONDecodeError) as e:
                return self._json(400, {'error': str(e)})
        m = re.match(r'^/api/mapping/(start|stop|save)$', path)
        if not m:
            return self._json(404, {'error': 'not found'})
        if not self._authorized():
            return None
        try:
            body = self._body()
            if m.group(1) == 'start':
                return self._json(200, MAPPING.start(self._robot(str(body.get('robot', '')))))
            if m.group(1) == 'stop':
                return self._json(200, MAPPING.stop())
            return self._json(200, MAPPING.save(str(body.get('name', ''))))
        except FileNotFoundError as e:
            return self._json(500, {'error': f'실행 파일을 찾지 못했습니다 (ROS 환경에서 실행했는지 확인): {e}'})
        except (ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            return self._json(400, {'error': str(e)})

    def do_PUT(self):
        path = self.path.split('?')[0]
        m = re.match(r'^/api/zones/([^/]+)$', path)
        if path != '/api/fleet' and not m:
            return self._json(404, {'error': 'not found'})
        if not self._authorized():
            return None
        try:
            if m:
                zones, masks = maps.write_zones(m.group(1), self._body(), read_fleet().get('robots', []))
                return self._json(200, {'zones': zones, 'masks': masks})
            data = validate_fleet(self._body())
            write_fleet(data)
            return self._json(200, data)
        except FileNotFoundError:
            return self._json(404, {'error': '없는 지도입니다'})
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {'error': str(e)})


MAPPING = maps.Mapping()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--bind', default='0.0.0.0')
    ap.add_argument('--dir', default=os.path.join(APP_DIR, 'web'))
    a = ap.parse_args()
    admin_token()  # 없으면 생성
    handler = functools.partial(Handler, directory=a.dir)
    with http.server.ThreadingHTTPServer((a.bind, a.port), handler) as srv:
        print(f'TB3 web: http://{a.bind}:{a.port}/ ({a.dir}), fleet: {FLEET_PATH}', flush=True)
        print(f'로봇 목록 저장용 관리자 토큰: {TOKEN_PATH} (또는 환경 변수 TB3_WEB_TOKEN)', flush=True)
        try:
            srv.serve_forever()
        finally:
            MAPPING.stop()   # 서버를 끄면 지도 작성 프로세스도 정지


if __name__ == '__main__':
    main()
