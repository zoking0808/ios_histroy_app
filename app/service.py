#!/usr/bin/env python3
"""Private Apple download jobs behind the site's HTTPS reverse proxy."""
from __future__ import annotations
import base64, hashlib, hmac, json, math, os, re, secrets, shutil, sys, threading, time, zipfile
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, quote
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ios_download as cli
from app.catalog import Catalog, CatalogError

COOKIE = '__Host-ios-history'
ACTIVE = {'authenticating', 'awaiting_code', 'downloading', 'packaging'}
MAX_FILE = 2 * 1024 ** 3


class APIError(Exception):
    def __init__(self, status, message): self.status, self.message = status, message


def b64(data): return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def unb64(text):
    if not isinstance(text, str) or len(text) > 20000: raise ValueError('invalid encoding')
    return base64.urlsafe_b64decode(text + '=' * ((-len(text)) % 4))


def apple_download_url(value):
    target = urlsplit(value)
    if target.scheme != 'https' or target.username or target.password or target.port not in (None,
                                                                                             443) or not target.hostname or not target.hostname.endswith(
            ('.apple.com', '.mzstatic.com')):
        raise APIError(502, 'Apple返回了无法识别的下载地址。')
    return value


class AppleRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        apple_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Limiter:
    def __init__(self):
        self.items = defaultdict(deque);self.lock = threading.Lock()

    def take(self, key, limit, window):
        now = time.time()
        with self.lock:
            values = self.items[key]
            while values and values[0] < now - window: values.popleft()
            if len(values) >= limit: raise APIError(429, '操作较频繁，请稍后再试。')
            values.append(now)
            if len(self.items) > 4000:
                for old in list(self.items):
                    if not self.items[old] or self.items[old][-1] < now - 3600: del self.items[old]


class Backend:
    def __init__(self):
        self.node = cli.runtime_node()

    def authenticate(self, job, code):
        if job.auth is None: job.auth = cli.AppleAuthSession(self.node, job.work)
        return job.auth.request({'appleAccount': job.account, 'password': job.password, 'code': code,
                                 'resolveDownload': job.mode == 'official', 'appId': job.app_id,
                                 'versionId': job.version_id})

    def official(self, song):
        if not isinstance(song, dict) or not song.get('URL'): raise APIError(502, 'Apple尚未返回该版本的下载地址。')
        url = apple_download_url(song['URL']);
        opener = build_opener(AppleRedirect())
        # Check the actual CDN link without forwarding Apple account cookies/tokens.
        for method in ('HEAD', 'GET'):
            try:
                request = Request(url, method=method, headers={'User-Agent': 'Mozilla/5.0', 'Range': 'bytes=0-0'})
                with opener.open(request, timeout=20) as response:
                    if response.status not in (200, 206): raise ValueError()
                    size = response.headers.get('Content-Range', '').split('/')[-1] or response.headers.get(
                        'Content-Length', '0')
                    return url, int(size) if size.isdigit() else 0
            except HTTPError as error:
                if method == 'HEAD' and error.code in (400, 403, 405): continue
                raise APIError(502, 'Apple直链当前无法匿名访问，请选择完整IPA模式或稍后重试。') from None
            except APIError:
                raise
            except Exception:
                raise APIError(502, '暂时无法验证Apple直链的可访问性，请稍后重试。') from None

    def download(self, job, user):
        staging = job.work / 'output';
        staging.mkdir(exist_ok=True, mode=0o700)
        payload = {'appId': job.app_id, 'versionId': job.version_id, 'appleAccount': job.account,
                   'password': 'account-authenticated-in-private-session', 'code': '',
                   'authEngine': 'ipatool', 'authorizedUser': user,
                   'staging': str(staging), 'maxFileSize': MAX_FILE, 'diagnose': True}
        payload['resolvedSong'] = job.song
        # The engine expects ordinary Base64 for its 256-bit session key.
        payload['sessionKey'] = base64.b64encode(secrets.token_bytes(32)).decode()

        def update(event):
            with job.lock:
                if job.cancelled: return
                if event['type'] == 'progress':
                    match = re.search(r'\((\d+(?:\.\d+)?)%\)', event.get('message', ''))
                    if match: job.progress = min(99, float(match[1]))
                    job.message = '正在下载安装包…'
                elif '校验' in event.get('message', ''):
                    job.state = 'packaging';
                    job.message = '正在校验并写入账号授权数据…'

        def process(proc):
            job.process = proc
            if job.cancelled: cli.stop_process(proc)

        result = cli.run_worker(self.node, payload, job.work, on_event=update, process_callback=process)
        if result.get('type') != 'success':
            code = str(result.get('code', 'DOWNLOAD_FAILED'))
            job.error_code = code if re.fullmatch(r'[A-Z0-9_-]{1,64}', code) else 'DOWNLOAD_FAILED'
            if result.get('code') == 'LICENSE_REQUIRED': raise APIError(422,
                                                                        'Apple未向当前会话返回有效许可，请核对购买账号和App Store地区；不能仅据此判断未购买。')
            if result.get('code') == 'VERSION_MISMATCH': raise APIError(422,
                                                                        'Apple返回的版本与所选版本不符，已停止下载。')
            raise APIError(502, 'Apple未提供可用的安装包，或下载/校验失败。请确认账号许可、商店地区及版本可用性。')
        path = Path(result['path']).resolve()
        if path.parent != staging.resolve() or path.stat().st_size > MAX_FILE: raise APIError(422,
                                                                                              '安装包大小或文件位置不符合要求。')
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if 'iTunesMetadata.plist' not in names or not any(
                    re.fullmatch(r'Payload/[^/]+\.app/SC_Info/[^/]+\.sinf', n, re.I) for n in names):
                raise APIError(422, '安装包缺少账号授权数据。')
        job.file_name = cli.package_filename(path, job.app_id, job.version_id)
        return path


class Job:
    def __init__(self, root, owner, app_id, version_id, account, password, mode='package'):
        self.id = secrets.token_hex(16);
        self.owner = owner;
        self.app_id = app_id;
        self.version_id = version_id
        self.root = root / self.id;
        self.root.mkdir(mode=0o700);
        self.work = self.root / 'work';
        self.work.mkdir(mode=0o700)
        self.account = account;
        self.password = password;
        self.auth = None;
        self.process = None
        self.state = 'authenticating';
        self.message = '正在连接Apple验证账号…';
        self.progress = 0
        self.created = time.time();
        self.expires = self.created + 600;
        self.attempts = 0;
        self.cancelled = False
        self.file = None;
        self.size = 0;
        self.sha256 = '';
        self.lock = threading.RLock()
        self.mode = mode;
        self.song = None;
        self.official_url = '';
        self.error_code = ''
        self.file_name = cli.friendly_filename(app_id, version_id)

    def public(self):
        return {'id': self.id, 'appId': self.app_id, 'versionId': self.version_id, 'state': self.state,
                'message': self.message, 'progress': self.progress, 'createdAt': int(self.created),
                'expiresAt': int(self.expires), 'size': self.size, 'sha256': self.sha256, 'mode': self.mode,
                'errorCode': self.error_code, 'fileName': self.file_name}

    def release(self):
        self.account = '';
        self.password = '';
        self.song = None
        if self.auth:
            try:
                self.auth.close()
            except Exception:
                pass
            self.auth = None
        self.process = None
        shutil.rmtree(self.work, ignore_errors=True)


class Service:
    def __init__(self, data, origin='http://localhost:8088', backend=None, secret_path=None, min_free=8 * 1024 ** 3):
        parsed = urlsplit(origin)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in (
                '', '/'):
            raise ValueError('PUBLIC_ORIGIN必须是完整站点地址，不包含路径、账号或查询参数。')
        if parsed.scheme != 'https' and not (
                parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
            raise ValueError('非本机地址必须使用HTTPS；本机可用http://localhost:8088。')
        self.secure_cookie = parsed.scheme == 'https'
        self.cookie_name = COOKIE if self.secure_cookie else 'ios-history-local'
        self.catalog = Catalog();
        self.trust_proxy = os.environ.get('TRUST_PROXY') == '1'
        os.umask(0o077)
        self.data = Path(data);
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = self.data / 'jobs';
        self.root.mkdir(exist_ok=True, mode=0o700)
        secret = Path(secret_path) if secret_path else self.data / 'service.key'
        if not secret.exists(): secret.write_bytes(secrets.token_bytes(48));secret.chmod(0o600)
        self.secret = secret.read_bytes()
        if len(self.secret) < 32: raise RuntimeError('service key missing')
        keyfile = self.data / 'envelope.pem'
        if not keyfile.exists():
            key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()));
            keyfile.chmod(0o600)
        self.key = serialization.load_pem_private_key(keyfile.read_bytes(), password=None)
        self.public_key = self.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                             serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.origin = origin.rstrip('/');
        self.backend = backend or Backend();
        self.min_free = min_free
        self.jobs = {};
        self.lock = threading.RLock();
        self.limiter = Limiter();
        self.stopped = threading.Event()
        for directory in self.root.iterdir():
            # Preserve completed downloads only; credentials never survive a restart.
            try:
                meta = json.loads((directory / 'result.json').read_text())
                if meta['expires'] <= time.time(): raise ValueError()
                job = object.__new__(Job);
                job.__dict__.update(meta)
                job.root = directory;
                job.work = directory / 'work';
                job.file = directory / 'result.ipa'
                job.lock = threading.RLock();
                job.auth = None;
                job.process = None;
                job.account = '';
                job.password = '';
                job.cancelled = False
                job.mode = 'package';
                job.song = None;
                job.official_url = '';
                job.error_code = ''
                if not job.file.is_file(): raise ValueError()
                job.file_name = cli.package_filename(job.file, job.app_id, job.version_id)
                self.jobs[job.id] = job
            except Exception:
                shutil.rmtree(directory, ignore_errors=True)

    def mac(self, text):
        return hmac.new(self.secret, text.encode(), hashlib.sha256).hexdigest()

    def session(self, raw, create=False):
        token = None
        try:
            value = SimpleCookie(raw or '')[self.cookie_name].value
            candidate, expiry, signature = value.split('.')
            if re.fullmatch(r'[a-f0-9]{64}', candidate) and int(expiry) > time.time() and hmac.compare_digest(signature,
                                                                                                              self.mac(
                                                                                                                      'session:' + candidate + '.' + expiry)):
                token = candidate
        except Exception:
            pass
        if token is None:
            if not create: raise APIError(401, '浏览器会话已过期，请刷新页面后重试。')
            token = secrets.token_hex(32)
        expiry = str(int(time.time() + 7200));
        value = token + '.' + expiry
        cookie = f'{self.cookie_name}={value}.{self.mac("session:" + value)}; Path=/; ' + (
            'Secure; ' if self.secure_cookie else '') + 'HttpOnly; SameSite=Strict; Max-Age=7200'
        return self.mac('owner:' + token), self.mac('csrf:' + token), cookie

    def decrypt(self, envelope, csrf):
        try:
            key = self.key.decrypt(unb64(envelope['wrappedKey']),
                                   padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(),
                                                label=None))
            nonce = unb64(envelope['nonce'])
            if len(key) != 32 or len(nonce) != 12: raise ValueError()
            payload = json.loads(AESGCM(key).decrypt(nonce, unb64(envelope['ciphertext']), csrf.encode()))
            stamp = float(payload['createdAt']) / 1000
            if not math.isfinite(stamp) or abs(time.time() - stamp) > 120: raise ValueError()
            return payload
        except Exception:
            raise APIError(400, '加密提交无效或已过期，请刷新页面重试。') from None

    def owned(self, id, owner):
        with self.lock:
            job = self.jobs.get(id)
        if not job or not hmac.compare_digest(job.owner, owner): raise APIError(404, '任务不存在。')
        if job.expires <= time.time(): raise APIError(410, '任务或下载文件已过期。')
        return job

    def create(self, owner, ip, body, csrf):
        if (self.data / 'maintenance').exists(): raise APIError(503, '下载服务正在更新，请稍后重试。')
        app_id = body.get('appId', '');
        version_id = body.get('versionId', '')
        mode = body.get('mode', 'package')
        if mode not in ('official', 'package'): raise APIError(400, '下载方式无效。')
        if not isinstance(app_id, str) or not isinstance(version_id, str): raise APIError(400, '应用或版本编号无效。')
        try:
            app_id = cli.validated_id(app_id);version_id = cli.validated_id(version_id, True)
        except ValueError:
            raise APIError(400, '请填写数字App ID及版本ID。') from None
        if body.get('consent') is not True: raise APIError(400, '请先确认账号使用说明。')
        credentials = self.decrypt(body.get('envelope', {}), csrf)
        account = credentials.get('account', '');
        password = credentials.get('password', '')
        if not isinstance(account, str) or not 1 <= len(account.strip()) <= 254 or not isinstance(password,
                                                                                                  str) or not 1 <= len(
                password) <= 1024:
            raise APIError(400, '账号或密码格式无效。')
        with self.lock:
            if any(j.state in ACTIVE for j in self.jobs.values()): raise APIError(409, '当前有任务处理中，请稍后再试。')
            stored = sum(j.size for j in self.jobs.values() if j.mode == 'package')
            needed = self.min_free if mode == 'package' else min(self.min_free, 100 * 1024 ** 2)
            if len(self.jobs) >= 100 or (mode == 'package' and stored > 8 * 1024 ** 3 - MAX_FILE) or shutil.disk_usage(
                self.data).free < needed: raise APIError(503, '下载空间暂时不足，请稍后再试。')
            self.limiter.take('owner:' + owner, 6, 3600);
            self.limiter.take('create:' + ip, 6, 3600)
            job = Job(self.root, owner, app_id, version_id, account.strip(), password, mode);
            self.jobs[job.id] = job
            self.start(job, '')
        return job.public()

    def start(self, job, code):
        threading.Thread(target=self.execute, args=(job, code), daemon=True).start()

    def submit_code(self, job, body, csrf):
        value = self.decrypt(body.get('envelope', {}), csrf).get('code', '')
        if not isinstance(value, str) or not re.fullmatch(r'\d{6}', value): raise APIError(400, '请输入6位验证码。')
        with job.lock:
            if job.state != 'awaiting_code' or job.attempts >= 3: raise APIError(409,
                                                                                 '当前任务不能提交验证码，请查看任务状态。')
            job.state = 'authenticating';
            job.message = '正在验证验证码…';
            self.start(job, value)
        return job.public()

    def execute(self, job, code):
        try:
            with job.lock:
                job.attempts += 1
            result = self.backend.authenticate(job, code)
            if job.cancelled: return
            if result['type'] == 'error':
                if result.get('code') in ('AUTH_OR_2FA', 'NEEDS_2FA') and job.attempts < 3:
                    with job.lock:
                        job.state = 'awaiting_code';
                        job.message = 'Apple要求进一步验证。收到本次登录验证码后填写；未收到时请检查账号密码。'
                    return
                job.error_code = str(result.get('code', 'APPLE_AUTH_FAILED'))[:64]
                if job.error_code == 'DOWNLOAD_LICENSE_UNCONFIRMED':
                    raise APIError(422,
                                   'Apple登录已通过，但当前商店会话未返回此版本的许可。请核对购买账号、App Store地区及该历史版本是否仍可获取；这不等同于未购买。')
                if job.error_code == 'DOWNLOAD_AUTH_EXPIRED': raise APIError(401,
                                                                             '登录已通过，但Apple下载会话失效，请重新验证账号。')
                if job.error_code == 'DOWNLOAD_UNAVAILABLE': raise APIError(502,
                                                                            '登录已通过，但Apple未返回可用的指定版本。错误详情：' + str(
                                                                                result.get('message', ''))[:240])
                raise APIError(502, 'Apple认证未完成。请检查账号、验证码或网络，稍后重新开始。')
            job.password = ''
            job.song = result.get('downloadInfo')
            if job.mode == 'official':
                with job.lock:
                    job.state = 'downloading';job.message = '登录通过，正在验证Apple官方下载地址…'
                url, size = self.backend.official(job.song)
                meta = job.song.get('metadata', {})
                with job.lock:
                    if job.cancelled: return
                    job.official_url = url;
                    job.size = size;
                    job.state = 'ready';
                    job.progress = 100;
                    job.expires = time.time() + 900
                    job.file_name = cli.friendly_filename(
                        meta.get('bundleDisplayName') or meta.get('itemName') or job.app_id,
                        meta.get('bundleShortVersionString') or job.version_id)
                    job.message = 'Apple官方直链已就绪，本站没有下载或保存IPA。原始包可能还需写入账号授权数据才能安装；Apple控制链接有效期。'
                return
            with job.lock:
                job.state = 'downloading';
                job.message = '认证通过，正在获取所选版本…';
                job.expires = time.time() + 1800
            path = self.backend.download(job, result['user'])
            if job.cancelled: return
            target = job.root / 'result.ipa';
            shutil.move(str(path), target);
            os.chmod(target, 0o600)
            digest = hashlib.sha256()
            with target.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''): digest.update(chunk)
            with job.lock:
                if job.cancelled: target.unlink(missing_ok=True);return
                job.file = target;
                job.size = target.stat().st_size;
                job.sha256 = digest.hexdigest()
                job.state = 'ready';
                job.progress = 100;
                job.message = '安装包已就绪。下载地址仅本浏览器可用，一小时后失效。';
                job.expires = time.time() + 3600
                fields = ('id', 'owner', 'app_id', 'version_id', 'state', 'message', 'progress', 'created', 'expires',
                          'attempts', 'size', 'sha256', 'file_name')
                (job.root / 'result.json').write_text(json.dumps({k: getattr(job, k) for k in fields}))
        except Exception as error:
            with job.lock:
                if not job.cancelled:
                    job.state = 'failed';
                    job.message = error.message if isinstance(error, APIError) else '任务执行失败，请稍后重试。';
                    job.expires = time.time() + 300
                    if not job.error_code: job.error_code = 'TASK_FAILED'
                    print(json.dumps({'event': 'job_failed', 'appId': job.app_id, 'versionId': job.version_id,
                                      'code': job.error_code}), flush=True)
        finally:
            if job.state != 'awaiting_code': job.release()

    def cancel(self, job):
        with self.lock, job.lock:
            idle = job.state not in ACTIVE or job.state == 'awaiting_code';
            job.cancelled = True;
            job.state = 'cancelled';
            job.message = '任务已取消。';
            job.expires = time.time() + 60
            if job.process:
                try:
                    cli.stop_process(job.process)
                except Exception:
                    pass
            if job.auth:
                try:
                    cli.stop_process(job.auth.process)
                except Exception:
                    pass
            job.account = '';
            job.password = ''
            job.official_url = ''
            if idle: job.release()
            if job.file: job.file.unlink(missing_ok=True)
            (job.root / 'result.json').unlink(missing_ok=True)

    def link(self, job):
        if job.state == 'ready' and job.mode == 'official' and job.official_url: return apple_download_url(
            job.official_url)
        if job.state != 'ready' or not job.file or not job.file.exists(): raise APIError(409, '安装包尚未就绪。')
        expiry = int(job.expires)
        signature = self.mac(f'file:{job.id}:{job.owner}:{expiry}')
        return self.origin + f'/api/v1/download/{job.id}?expires={expiry}&signature={signature}'

    def sweep(self):
        while not self.stopped.wait(15):
            for job in list(self.jobs.values()):
                if job.expires <= time.time():
                    self.cancel(job)
                    with self.lock: self.jobs.pop(job.id, None)
                    shutil.rmtree(job.root, ignore_errors=True)


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'IOSHistory';
        sys_version = ''

        def log_message(self, *args):
            pass  # Do not log request targets, headers or bodies.

        def setup(self):
            super().setup();self.connection.settimeout(20)

        def json(self, status, payload, cookie=None):
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status);
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store');
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(data)))
            if cookie: self.send_header('Set-Cookie', cookie)
            self.end_headers()
            if self.command != 'HEAD': self.wfile.write(data)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def do_DELETE(self):
            self.dispatch()

        def static(self, path):
            allowed = {'/': 'index.html', '/index.html': 'index.html', '/assets/base.css': 'assets/base.css',
                       '/assets/catalog.css': 'assets/catalog.css', '/assets/catalog.js': 'assets/catalog.js',
                       '/assets/download.js': 'assets/download.js', '/assets/favicon.svg': 'assets/favicon.svg'}
            if path not in allowed: return False
            if path in ('/', '/index.html') and self.headers.get('Host', '').lower() != urlsplit(
                    service.origin).netloc.lower():
                self.send_response(307);
                self.send_header('Location', service.origin + '/');
                self.send_header('Content-Length', '0');
                self.end_headers();
                return True
            file = ROOT / 'web' / allowed[path]
            content = file.read_bytes();
            kind = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
                    '.js': 'text/javascript; charset=utf-8', '.svg': 'image/svg+xml'}[file.suffix]
            self.send_response(200);
            self.send_header('Content-Type', kind);
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-cache');
            self.send_header('X-Content-Type-Options', 'nosniff');
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy',
                             "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https://*.mzstatic.com data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            if self.command != 'HEAD': self.wfile.write(content)
            return True

        def dispatch(self):
            try:
                path = urlsplit(self.path).path;
                ip = self.headers.get('X-Real-IP', self.client_address[0]) if service.trust_proxy else \
                self.client_address[0]
                service.limiter.take('requests:' + service.mac(ip), 180, 60)
                if self.headers.get('Sec-Fetch-Site') == 'cross-site' or self.headers.get('Origin',
                                                                                          service.origin) != service.origin:
                    raise APIError(403, '请求来源不被允许。')
                if self.command in ('GET', 'HEAD') and self.static(path): return
                if path == '/api/v1/health' and self.command in ('GET', 'HEAD'):
                    return self.json(200, {'ok': True, 'version': '1.0.0', 'maxActiveJobs': 1,
                                           'activeJobs': sum(j.state in ACTIVE for j in service.jobs.values())})
                if path == '/api/v1/apps' and self.command == 'GET':
                    query = parse_qs(urlsplit(self.path).query)
                    return self.json(200,
                                     service.catalog.apps(query.get('q', [''])[0], query.get('country', ['cn'])[0]))
                version_match = re.fullmatch(r'/api/v1/versions/([1-9][0-9]{4,14})', path)
                if version_match and self.command == 'GET': return self.json(200,
                                                                             service.catalog.versions(version_match[1]))
                owner, csrf, cookie = service.session(self.headers.get('Cookie'),
                                                      create=path == '/api/v1/session' and self.command == 'GET')
                if self.command in ('POST', 'DELETE'):
                    supplied = self.headers.get('X-CSRF-Token', '')
                    if not re.fullmatch(r'[a-f0-9]{64}', supplied) or not hmac.compare_digest(supplied,
                                                                                              csrf): raise APIError(403,
                                                                                                                    '页面验证已失效，请刷新后重试。')
                    if self.headers.get('Origin') != service.origin: raise APIError(403, '请求来源不被允许。')
                if path == '/api/v1/session' and self.command == 'GET':
                    return self.json(200, {'csrfToken': csrf, 'publicKey': service.public_key, 'maxFileSize': MAX_FILE,
                                           'retentionSeconds': 3600, 'modes': ['official', 'package']}, cookie)
                if path == '/api/v1/jobs' and self.command == 'GET':
                    return self.json(200, {'jobs': [j.public() for j in list(service.jobs.values()) if
                                                    j.owner == owner and j.expires > time.time()]}, cookie)
                body = {}
                if self.command == 'POST':
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json': raise APIError(415,
                                                                                                                '需要JSON请求。')
                    try:
                        size = int(self.headers.get('Content-Length', '0'))
                    except ValueError:
                        raise APIError(400, '请求长度无效。') from None
                    if not 0 < size <= 16384: raise APIError(413, '请求体过大或为空。')
                    try:
                        body = json.loads(self.rfile.read(size))
                    except Exception:
                        raise APIError(400, 'JSON格式无效。') from None
                    if not isinstance(body, dict): raise APIError(400, '请求格式无效。')
                if path == '/api/v1/jobs' and self.command == 'POST':
                    return self.json(202, service.create(owner, service.mac(ip), body, csrf), cookie)
                match = re.fullmatch(r'/api/v1/jobs/([a-f0-9]{32})(/code|/link)?', path)
                if match:
                    job = service.owned(match[1], owner);
                    action = match[2]
                    if self.command == 'GET' and not action: return self.json(200, job.public(), cookie)
                    if self.command == 'DELETE' and not action: service.cancel(job);return self.json(200, {'ok': True},
                                                                                                     cookie)
                    if self.command == 'POST' and action == '/code': return self.json(202,
                                                                                      service.submit_code(job, body,
                                                                                                          csrf), cookie)
                    if self.command == 'POST' and action == '/link': return self.json(200, {'url': service.link(job),
                                                                                            'expiresAt': int(
                                                                                                job.expires),
                                                                                            'mode': job.mode,
                                                                                            'appleExpiryUnknown': job.mode == 'official',
                                                                                            'fileName': job.file_name},
                                                                                      cookie)
                match = re.fullmatch(r'/api/v1/download/([a-f0-9]{32})', path)
                if match and self.command in ('GET', 'HEAD'):
                    job = service.owned(match[1], owner);
                    params = parse_qs(urlsplit(self.path).query)
                    if job.mode != 'package': raise APIError(404, '官方直链模式没有本站文件。')
                    expiry = params.get('expires', [''])[0];
                    signature = params.get('signature', [''])[0]
                    if not expiry.isdigit() or not re.fullmatch(r'[a-f0-9]{64}', signature) or int(expiry) > int(
                            job.expires) or int(expiry) < time.time() or not hmac.compare_digest(signature, service.mac(
                            f'file:{job.id}:{owner}:{expiry}')):
                        raise APIError(403, '下载链接无效或已过期。')
                    service.link(job)
                    start, end = 0, job.size - 1;
                    partial = False
                    if self.headers.get('Range'):
                        bounds = re.fullmatch(r'bytes=(\d+)-(\d*)', self.headers['Range'])
                        if not bounds: raise APIError(416, '下载范围无效。')
                        start = int(bounds[1]);
                        end = min(int(bounds[2]) if bounds[2] else end, end);
                        partial = True
                        if start > end or start >= job.size: raise APIError(416, '下载范围无效。')
                    with job.file.open('rb') as stream:
                        self.send_response(206 if partial else 200)
                        self.send_header('Content-Type', 'application/octet-stream')
                        fallback = job.file_name if job.file_name.isascii() else f'{job.app_id}-{job.version_id}.ipa'
                        self.send_header('Content-Disposition',
                                         f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(job.file_name, safe="")}')
                        self.send_header('Content-Length', str(end - start + 1));
                        self.send_header('Accept-Ranges', 'bytes')
                        self.send_header('Cache-Control', 'private, no-store');
                        self.send_header('Referrer-Policy', 'no-referrer');
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        if partial: self.send_header('Content-Range', f'bytes {start}-{end}/{job.size}')
                        self.end_headers()
                        if self.command == 'HEAD': return
                        stream.seek(start);
                        remaining = end - start + 1
                        while remaining:
                            block = stream.read(min(256 * 1024, remaining))
                            if not block: break
                            self.wfile.write(block);
                            remaining -= len(block)
                    return
                raise APIError(404, '接口不存在。')
            except APIError as error:
                self.json(error.status, {'message': error.message})
            except CatalogError as error:
                self.json(error.status, {'message': error.message})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except Exception:
                self.json(500, {'message': '服务暂时不可用。'})

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(32);super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self.slots.acquire(False): request.close();return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release();raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


def main():
    service = Service(os.environ.get('DATA_DIR', '/data'), os.environ.get('PUBLIC_ORIGIN', 'http://localhost:8088'),
                      secret_path=os.environ.get('SERVICE_SECRET_FILE'),
                      min_free=float(os.environ.get('MIN_FREE_GB', '8')) * 1024 ** 3)
    threading.Thread(target=service.sweep, daemon=True).start()
    server = Server(('0.0.0.0', 8080), handler_for(service))
    print('iOS History API ready', flush=True)
    try:
        server.serve_forever()
    finally:
        service.stopped.set()
        for job in list(service.jobs.values()):
            if job.state in ACTIVE: service.cancel(job)
        server.server_close()


if __name__ == '__main__': main()
