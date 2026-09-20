#!/usr/bin/env python3
"""Interactive, local-only IPA downloader based on the pinned Pastel engine."""
from __future__ import annotations

import argparse
import base64
import contextlib
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import select
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.request
import warnings
import zipfile

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / '.runtime/upstream/pastel'
SUPPORT = ROOT / 'bridge'
STATE = ROOT / '.runtime'
ENGINE = STATE / 'engine'
COMMIT = '20934736051bfdcfdd6362920fbf04c249ce1af5'


def validated_id(value: str, version: bool = False) -> str:
    value = value.strip()
    if not re.fullmatch(r'[1-9][0-9]{0,19}' if version else r'[1-9][0-9]{4,14}', value):
        raise ValueError('请填写数字版本 ID（不是 8.0.78 这样的版本号）' if version else '请填写数字 App ID')
    return value


def friendly_filename(name: str, version: str) -> str:
    def part(value, limit):
        clean = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', '_', str(value)).strip(' .')
        return clean.encode('utf-8')[:limit].decode('utf-8', errors='ignore').rstrip(' .') or 'Unknown'
    return part(name, 140) + '_' + part(version, 40) + '.ipa'


def package_filename(path: Path, app_id: str, version_id: str) -> str:
    name, version = app_id, version_id
    try:
        with zipfile.ZipFile(path) as archive:
            if 'iTunesMetadata.plist' in archive.namelist() and archive.getinfo('iTunesMetadata.plist').file_size <= 1024*1024:
                meta=plistlib.loads(archive.read('iTunesMetadata.plist'))
                if isinstance(meta,dict):
                    name=meta.get('bundleDisplayName') or meta.get('itemName') or name
                    version=meta.get('bundleShortVersionString') or version
            for info in archive.infolist():
                if re.fullmatch(r'Payload/[^/]+\.app/Info\.plist',info.filename) and info.file_size <= 1024*1024:
                    meta=plistlib.loads(archive.read(info))
                    if isinstance(meta,dict):
                        if name==app_id: name=meta.get('CFBundleDisplayName') or meta.get('CFBundleName') or name
                        version=meta.get('CFBundleShortVersionString') or version
                    break
    except (OSError, ValueError, TypeError, zipfile.BadZipFile, plistlib.InvalidFileException):
        pass
    return friendly_filename(name,version)


def runtime_node(explicit: str | None = None) -> Path:
    candidates = [explicit] if explicit else [
        '/Applications/Pastel.app/Contents/Resources/node/bin/node',
        '/opt/homebrew/opt/node@24/bin/node', shutil.which('node')]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        result = subprocess.run([candidate, '--version'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.startswith('v24.'):
            return Path(candidate).resolve()
    raise RuntimeError('需要 Node.js 24。可使用已安装 Pastel 自带的运行时，或用 --node 指定 Node 24 路径。')


def base_environment(node: Path) -> dict[str, str]:
    env = dict(os.environ)
    for name in list(env):
        if name.startswith(('IPA_', 'STUDY97_IPA_', 'DOWNLOAD_')) or name in {
            'NODE_OPTIONS', 'NODE_TLS_REJECT_UNAUTHORIZED', 'NODE_DEBUG', 'SSLKEYLOGFILE',
            'APPLE_ID', 'APPLE_PASSWORD', 'PASSWORD', 'SESSION_KEY', 'CODE'}:
            env.pop(name, None)
    env['PATH'] = str(node.parent) + os.pathsep + env.get('PATH', '')
    return env


def checked_source(relative: str) -> bytes:
    manifest = json.loads((ROOT / 'dependencies/pastel.json').read_text())
    expected = manifest['files'][relative]
    from bridge.fetch_sources import fetch_file
    if manifest['commit'] != COMMIT:
        raise RuntimeError('Pastel固定版本不匹配')
    url = f'https://raw.githubusercontent.com/EEliberto/Pastel-macOS/{COMMIT}/{relative}'
    content = fetch_file(url, SOURCE / relative, expected)
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise RuntimeError(f'固定版本源码校验失败：{relative}')
    return content


def setup(node: Path, auth_engine: str = 'ipatool') -> None:
    if platform.system() not in ('Darwin', 'Linux') or platform.machine() not in ('arm64', 'aarch64', 'x86_64', 'amd64'):
        raise RuntimeError('当前封装支持 macOS / Linux 的 arm64 与 amd64 环境。')
    if auth_engine == 'native' and (platform.system() != 'Darwin' or platform.machine() != 'arm64' or int(platform.mac_ver()[0].split('.')[0]) < 26):
        raise RuntimeError('native认证需要Apple Silicon Mac/macOS26+；Linux请使用默认ipatool认证。')
    os.umask(0o077)
    ENGINE.mkdir(parents=True, exist_ok=True)
    (ENGINE / 'src').mkdir(exist_ok=True)
    manifest = json.loads((ROOT / 'dependencies/pastel.json').read_text())
    for relative in manifest['files']:
        if relative.startswith('NodeProject/src/') and relative.endswith('.js'):
            (ENGINE / 'src' / Path(relative).name).write_bytes(checked_source(relative))
    (ENGINE / 'package.json').write_bytes(checked_source('NodeProject/package.json'))
    locked = checked_source('NodeProject/package-lock.json')
    for item in json.loads(locked).get('packages', {}).values():
        url = item.get('resolved', '')
        if url and (not url.startswith('https://registry.npmjs.org/') or not item.get('integrity')):
            raise RuntimeError('依赖锁文件包含未允许的来源或缺少校验值')
    previous_lock = (ENGINE / 'package-lock.json').read_bytes() if (ENGINE / 'package-lock.json').exists() else b''
    (ENGINE / 'package-lock.json').write_bytes(locked)
    if not (ENGINE / 'node_modules/.package-lock.json').exists() or previous_lock != locked:
        npm = shutil.which('npm')
        if not npm:
            raise RuntimeError('缺少 npm，无法准备本项目依赖')
        print('准备本项目下载依赖（不会安装到全局）…', flush=True)
        for name in ('npm-user.conf', 'npm-global.conf'):
            (STATE / name).write_text('')
        subprocess.run([npm, 'ci', '--ignore-scripts', '--no-audit', '--no-fund',
                        '--registry=https://registry.npmjs.org', '--userconfig=' + str(STATE / 'npm-user.conf'),
                        '--globalconfig=' + str(STATE / 'npm-global.conf'), '--cache', str(STATE / 'npm-cache')],
                       cwd=ENGINE, env=base_environment(node), check=True)
    # Keep the research snapshot immutable; modifications live only in the runtime copy.
    gsa = (ENGINE / 'src/gsa.js').read_text()
    start = gsa.index('export function curlRequest(')
    end = gsa.index('\nfunction headerValue(', start)
    gsa = gsa[:start] + "import {curlRequest} from './transport.js';\nexport {curlRequest};\n" + gsa[end:]
    gsa = gsa.replace("error: new Error(t('store_token_failed')), data: null};\n    }\n\n    const failureType",
                      "error: new Error(`Apple 登录服务返回 HTTP ${status}（${res.body?.length ? '非认证格式' : '空响应'}），未取得有效认证结果。`), data: null};\n    }\n\n    const failureType")
    (ENGINE / 'src/gsa.js').write_text('// Modified by iOS History App: private transport and authentication diagnostics.\n' + gsa)
    shutil.copyfile(SUPPORT / 'transport.mjs', ENGINE / 'src/transport.js')
    # Check all HTTP redirects before forwarding download headers.
    downloader = (ENGINE / 'src/downloader.js').read_text()
    needle = 'axios.create({timeout, proxy: systemProxy()})'
    if needle not in downloader:
        raise RuntimeError('下载器源码结构变化，需要重新审查')
    downloader = downloader.replace(needle, '''axios.create({timeout, proxy: systemProxy(), maxRedirects: 4,
        beforeRedirect: options => {
            const host = options.hostname || '';
            if (options.protocol !== 'https:' || !(host.endsWith('.apple.com') || host.endsWith('.mzstatic.com'))) {
                throw new Error('拒绝跳转到非 Apple HTTPS 下载地址');
            }
        }})''')
    downloader = downloader.replace('if (!size) throw sanitize', "if (Number(process.env.STUDY97_MAX_DOWNLOAD_BYTES || 0) > 0 && size > Number(process.env.STUDY97_MAX_DOWNLOAD_BYTES)) throw new Error('安装包超过服务允许的大小');\n    if (!size) throw sanitize")
    (ENGINE / 'src/downloader.js').write_text('// Modified by iOS History App: validated redirects and download size limit.\n' + downloader)
    (ENGINE / 'LICENSE.Pastel').write_bytes(checked_source('LICENSE'))
    if auth_engine != 'native':
        return
    native = checked_source('Scripts/SAPSigner.m').decode()
    # Probe framework/classes without opening a signing session or touching an Apple account.
    native = native.replace('if (input.length == 0) {', 'if (input.length == 0 && !(argc > 1 && strcmp(argv[1], "--probe") == 0)) {')
    needle = '        PastelCKSigningSession *session ='
    native = native.replace(needle, '        if (argc > 1 && strcmp(argv[1], "--probe") == 0) { puts("CommerceKit / CKSigningSession available"); return 0; }\n\n' + needle)
    native = '#include <string.h>\n#include <stdio.h>\n' + native
    native_file = STATE / 'SAPSigner.m'
    old = native_file.read_text() if native_file.exists() else ''
    native_file.write_text(native)
    if old != native or not (STATE / 'sap-signer').exists():
        print('编译本机 Apple 签名组件…', flush=True)
        subprocess.run(['/usr/bin/xcrun', 'clang', '-fobjc-arc', '-framework', 'Foundation',
                        str(native_file), '-o', str(STATE / 'sap-signer')], check=True)


def redact(text: str, values: tuple[str, ...]) -> str:
    for value in sorted((v for v in values if v), key=len, reverse=True):
        text = text.replace(value, '[已隐藏]')
    text = re.sub(r'https?://\S+', '[服务地址]', text)
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)


def stop_process(process: subprocess.Popen) -> None:
    # Kill native signer/curl descendants too, then clean the per-run directory.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_worker(node: Path, payload: dict, job: Path, *, on_event=None, process_callback=None) -> dict:
    env = base_environment(node)
    env.update({'STUDY97_IPA_ENGINE':str(ENGINE), 'IPA_SAP_SIGNER':str(STATE / 'sap-signer'),
                'STUDY97_AUTH_HELPER':str(STATE / 'ipatool-auth'), 'STUDY97_SAP_CACHE':str(STATE / 'sap-cache'),
                'IPA_SESSION_DIR':str(job / 'sessions'), 'IPA_DEVICE_DIR':str(STATE / 'device'),
                'IPA_LANG':'zh-Hans', 'TMPDIR':str(job / 'tmp')})
    (job / 'tmp').mkdir(exist_ok=True, mode=0o700)
    values = tuple(str(payload.get(key, '')) for key in ('appleAccount','password','code','sessionKey'))
    result = None
    process = subprocess.Popen([str(node), str(SUPPORT / 'worker.mjs')], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                               cwd=job, env=env, start_new_session=True)
    try:
        if process_callback:
            process_callback(process)
        process.stdin.write(json.dumps(payload) + '\n')
        process.stdin.close()
        for line in process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get('type') in ('phase', 'progress'):
                message = redact(event.get('message', ''), values)
                if on_event:
                    on_event(dict(event, message=message))
                else:
                    print(message, flush=True)
            elif event.get('type') == 'diagnostic':
                print('[诊断] ' + json.dumps({k:v for k,v in event.items() if k != 'type'}, ensure_ascii=False), flush=True)
            elif event.get('type') in ('success', 'error'):
                result = event
        code = process.wait()
        if not result or (code and result.get('type') == 'success'):
            return {'type':'error', 'code':'WORKER_EXIT', 'message':f'下载程序异常退出（{code}），请先运行 --check。'}
        if result.get('message'):
            result['message'] = redact(result['message'], values)
        return result
    finally:
        stop_process(process)
        process.stdout.close()


def secret_input(prompt: str, *, required: bool = True) -> str:
    with warnings.catch_warnings():
        # Never fall back to echoing a password when no real terminal is available.
        warnings.simplefilter('error', getpass.GetPassWarning)
        value = getpass.getpass(prompt)
    if required and not value:
        raise ValueError('输入不能为空')
    return value


def input_code() -> str:
    code = secret_input('验证码（6位；未收到可直接回车）：', required=False).strip().replace(' ', '')
    if code and not re.fullmatch(r'[0-9]{6}', code):
        raise ValueError('验证码必须为6位数字')
    return code


class AppleAuthSession:
    """One private helper process and cookie/SAP session for both login steps."""
    def __init__(self, node: Path, job: Path):
        env = base_environment(node)
        env.update({'IPA_DEVICE_DIR': str(STATE / 'device'),
                    'STUDY97_SAP_CACHE': str(STATE / 'sap-cache'), 'TMPDIR': str(job / 'tmp')})
        (job / 'tmp').mkdir(exist_ok=True, mode=0o700)
        device_script = 'import {getDeviceGuid} from ' + json.dumps((ENGINE / 'src/device.js').as_uri()) + '; process.stdout.write(getDeviceGuid());'
        result = subprocess.run([str(node), '--input-type=module', '-e', device_script],
                                env=env, capture_output=True, text=True, timeout=10, check=True)
        self.guid = result.stdout.strip()
        if not re.fullmatch(r'[0-9A-F]{12}', self.guid):
            raise RuntimeError('无法取得有效设备标识')
        self.process = subprocess.Popen([str(STATE / 'ipatool-auth')], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                        cwd=job, env=env, start_new_session=True, bufsize=1)

    def exchange(self, message: dict) -> dict:
        try:
            self.process.stdin.write(json.dumps(message) + '\n')
            self.process.stdin.flush()
            ready, _, _ = select.select([self.process.stdout], [], [], 180)
            if not ready:
                raise RuntimeError('Apple 认证请求超时；本次停止，请稍后使用新验证码重新登录。')
            line = self.process.stdout.readline(2 * 1024 * 1024)
            result = json.loads(line)
        except (BrokenPipeError, json.JSONDecodeError):
            raise RuntimeError('认证会话已结束；请重新运行脚本，不要继续使用旧验证码。') from None
        return result

    def prepare(self):
        result = self.exchange({'action':'prepare', 'guid':self.guid})
        if not result.get('ok') or not result.get('prepared'):
            raise RuntimeError(result.get('message', 'SAP握手检查失败'))

    def request(self, payload: dict) -> dict:
        print('正在使用同一会话验证 Apple 账户…', flush=True)
        message = {key: payload.get(key, '') for key in ('appleAccount', 'password', 'code')}
        message['guid'] = self.guid
        if payload.get('resolveDownload'):
            message.update(resolveDownload=True,appID=payload['appId'],versionID=payload['versionId'])
        result = self.exchange(message)
        if payload.get('diagnose'):
            trace = result.get('diagnostics', {})
            safe = {key: trace[key] for key in ('request', 'cookiesBefore', 'cookiesAfter', 'signerReused')
                    if key in trace and isinstance(trace[key], (int, bool))}
            print('[认证会话] ' + json.dumps(safe, ensure_ascii=False), flush=True)
        if not result.get('ok'):
            values = tuple(str(payload.get(key, '')) for key in ('appleAccount', 'password', 'code'))
            return {'type': 'error', 'code': result.get('code', 'APPLE_AUTH_FAILED'),
                    'message': redact(result.get('message', 'Apple认证失败'), values)}
        user = result.get('user', {})
        if not user.get('authHeaders', {}).get('X-Token') or not user.get('authHeaders', {}).get('X-Dsid'):
            raise RuntimeError('认证结果缺少有效令牌，未继续下载')
        return {'type': 'authenticated', 'user': user, 'downloadInfo': result.get('downloadInfo')}

    def close(self):
        try:
            self.process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            pass
        stop_process(self.process)
        self.process.stdout.close()


def download_interactive(node: Path, args) -> Path | bool | None:
    if getattr(args, 'login_only', False):
        app_id, version_id = '', ''
    else:
        app_id = validated_id(args.app_id or input('App ID：'))
        version_id = validated_id(args.version_id or input('版本 ID（数字构建编号）：'), version=True)
    account = input('Apple ID：').strip()
    if not account or len(account) > 254 or any(ord(c) < 32 for c in account):
        raise ValueError('Apple ID 格式无效')
    password = secret_input('Apple ID 密码（输入不显示）：')
    code = ''  # Ask for OTP only after Apple's authentication response requests it.
    jobs = STATE / 'jobs'; jobs.mkdir(exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='run-', dir=jobs) as temporary, contextlib.ExitStack() as stack:
        job = Path(temporary)
        staging = job / 'output'; staging.mkdir(mode=0o700)
        payload = {'appId':app_id, 'versionId':version_id, 'appleAccount':account,
                   'password':password, 'code':code, 'sessionKey':base64.b64encode(secrets.token_bytes(32)).decode(),
                   'staging':str(staging)}
        payload['diagnose'] = getattr(args, 'diagnose', False)
        payload['authEngine'] = getattr(args, 'auth_engine', 'native')
        payload['resolveDownload'] = False  # Preserve the established local IPA flow.
        if getattr(args, 'login_only', False):
            payload['action'] = 'login'
        auth = None
        if payload['authEngine'] == 'ipatool':
            auth = AppleAuthSession(node, job)
            stack.callback(auth.close)
        for attempt in range(3):
            if auth:
                authentication = auth.request(payload)
                if authentication['type'] == 'error':
                    result = authentication
                else:
                    result = run_worker(node, dict(payload, authorizedUser=authentication['user'], resolvedSong=authentication.get('downloadInfo')), job)
            else:
                result = run_worker(node, payload, job)
            if result['type'] == 'success':
                if result.get('login'):
                    return True
                source = Path(result['path']).resolve()
                if source.parent != staging.resolve() or not source.is_file():
                    raise RuntimeError('下载程序返回了异常文件位置')
                with zipfile.ZipFile(source) as archive:
                    names = archive.namelist()
                    if 'iTunesMetadata.plist' not in names or not any(re.fullmatch(r'Payload/[^/]+\.app/SC_Info/[^/]+\.sinf', n, re.I) for n in names):
                        raise RuntimeError('IPA 缺少预期的授权数据，未保存为成功结果')
                destination = Path(args.output).expanduser().resolve()
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Use app name + actual bundle version; reserve without overwriting.
                desired = package_filename(source,app_id,version_id)
                index=1
                while True:
                    name=str(destination/(desired if index==1 else f'{Path(desired).stem}_{index}.ipa'))
                    try:
                        fd=os.open(name,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                        break
                    except FileExistsError: index+=1
                os.close(fd)
                try:
                    shutil.move(str(source), name)
                    os.chmod(name, 0o600)
                except BaseException:
                    Path(name).unlink(missing_ok=True)
                    raise
                return Path(name)
            print('\n' + result.get('message', '下载失败'))
            if result.get('code') == 'LICENSE_REQUIRED':
                print('该账号尚未拥有此应用。请先在对应地区 App Store 手动获取/购买，再重试；脚本不会自动购买。')
                return None
            if result.get('code') == 'APPLE_AUTH_FAILED' and any(f'HTTP {status}' in result.get('message', '') for status in (204, 404, 429, 500, 503)):
                print('Apple 未返回有效认证结果，无法据此判断验证码是否正确。本次停止；请勿反复提交同一验证码。')
            if attempt >= 2 or result.get('code') not in ('NEEDS_2FA', 'AUTH_OR_2FA'):
                return None
            print('Apple 可能需要双重认证，也可能是密码不正确。')
            choice = input('输入 1 填验证码，2 重输密码，其他键退出：').strip()
            if choice == '1':
                payload['code'] = input_code()
                if not payload['code']:
                    return None
            elif choice == '2':
                payload['password'] = secret_input('重新输入密码：')
                payload['code'] = ''
            else:
                return None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description='交互式下载 Apple 账号有权获取的历史 IPA；凭据只在运行时输入。')
    parser.add_argument('--app-id', help='数字 App ID；省略时交互输入')
    parser.add_argument('--version-id', help='数字版本 ID；省略时交互输入')
    parser.add_argument('--output', default=str(ROOT / 'downloads/ipa'), help='IPA 保存目录')
    parser.add_argument('--node', help='Node.js 24 可执行文件路径')
    parser.add_argument('--setup', action='store_true', help='仅准备项目依赖并执行无账号检查')
    parser.add_argument('--check', action='store_true', help='检查环境/模块/签名框架；不登录 Apple')
    parser.add_argument('--prepare-auth', action='store_true', help='准备并验证SAP签名握手；不提交账号密码')
    parser.add_argument('--diagnose', action='store_true', help='显示脱敏的HTTP状态与响应类型，不输出凭据')
    parser.add_argument('--login-only', action='store_true', help='仅验证Apple登录，不请求安装包')
    parser.add_argument('--auth-engine', choices=['native','ipatool'], default='ipatool', help='默认使用新版ipatool SAP认证；native保留用于对照诊断')
    args = parser.parse_args()
    if args.app_id:
        validated_id(args.app_id)
    if args.version_id:
        validated_id(args.version_id, version=True)
    node = runtime_node(args.node)
    setup(node, args.auth_engine)
    bridge_hash = hashlib.sha256((SUPPORT / 'ipatool-auth.go').read_bytes() + (SUPPORT / 'setup_ipatool.py').read_bytes() + (SUPPORT / 'ipatool-download-info.go').read_bytes()).hexdigest()
    bridge_marker = STATE / 'IPATOOL-BRIDGE-SHA256'
    if args.auth_engine == 'ipatool' and (not (STATE / 'ipatool-auth').is_file() or not bridge_marker.exists() or bridge_marker.read_text().strip() != bridge_hash):
        print('准备新版 Apple 认证模块…', flush=True)
        subprocess.run([sys.executable, str(SUPPORT / 'setup_ipatool.py')],check=True)
    if args.prepare_auth:
        if args.auth_engine != 'ipatool':
            raise RuntimeError('--prepare-auth 用于默认ipatool认证')
        with tempfile.TemporaryDirectory(prefix='prepare-', dir=STATE) as temporary:
            auth = AppleAuthSession(node, Path(temporary))
            try:
                print('准备Apple SAP签名资源并验证握手，首次运行可能需要几分钟…', flush=True)
                auth.prepare()
            finally:
                auth.close()
        print('Apple SAP签名握手检查通过；没有提交Apple ID或密码。')
        return 0
    if args.check or args.setup:
        if args.auth_engine == 'ipatool':
            helper = subprocess.run([str(STATE / 'ipatool-auth')], input='{"action":"check"}\n', capture_output=True, text=True, timeout=10, check=True)
            if json.loads(helper.stdout).get('sessionProtocol') != 3:
                raise RuntimeError('认证桥接器版本不匹配')
            print('新版认证桥接器检查通过（支持连续验证码会话）。')
        if args.auth_engine == 'native':
            probe = subprocess.run([str(STATE / 'sap-signer'), '--probe'], input=b'', capture_output=True, timeout=10)
            if probe.returncode:
                raise RuntimeError('CommerceKit 框架检查失败：' + probe.stderr.decode(errors='replace')[:500])
            print(probe.stdout.decode().strip())
        with tempfile.TemporaryDirectory(prefix='check-', dir=STATE) as temporary:
            result = run_worker(node, {'action':'check'}, Path(temporary))
        if result['type'] != 'success':
            raise RuntimeError(result.get('message', '模块检查失败'))
        print(result['message'])
        print('检查通过。运行 python3 ios_download.py 开始交互式下载。')
        return 0
    if not sys.stdin.isatty():
        raise RuntimeError('请在本机终端运行；不接受通过重定向或聊天传入账号密码。')
    print('iOS 历史应用 · 本机下载\n退出时清理临时会话；下载的 IPA 可能包含你的 Apple ID 授权信息。')
    output = download_interactive(node, args)
    if output is True:
        print('Apple 账户登录成功；本次只验证登录，临时会话已清理。')
        return 0
    if output:
        print(f'\n下载并打包完成：{output}\n文件大小：{output.stat().st_size / 1024 / 1024:.1f} MB')
        return 0
    return 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print('\n已取消，临时会话已清理。', file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f'错误：{error}', file=sys.stderr)
        raise SystemExit(1)
