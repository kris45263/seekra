"""Seekra 自己的 ChatGPT OAuth 注册；不读取 Codex 凭据或使用 API key。"""
import base64
from contextlib import contextmanager
import ctypes
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import uuid4
import webbrowser

import httpx
import jwt


ISSUER = 'https://auth.openai.com'
AUTHORIZE = ISSUER + '/api/accounts/authorize'
TOKEN = ISSUER + '/api/accounts/oauth/token'
JWKS = ISSUER + '/.well-known/jwks.json'
RESOURCE = 'https://api.openai.com/v1'
PLAN_SCOPE = 'chatgpt.tokens.use.direct'
SCOPES = 'openid profile email offline_access resource.invoke ' + PLAN_SCOPE


def _dpapi(data, decrypt=False):
    """Windows 凭据只能由同一用户解密，禁止弹出系统保护提示。"""
    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_uint32), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    function.restype = ctypes.c_int
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise OSError('无法使用当前 Windows 用户保护或读取 ChatGPT 凭据。')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(result.data)


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('凭据目录或文件不能是符号链接。')
    fd, name = tempfile.mkstemp(prefix='.auth-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a+b') as file:
        if file.tell() == 0:
            file.write(b'0'); file.flush()
        file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('该 ChatGPT 账号正在登录或刷新，请稍后重试。') from None
        try:
            yield
        finally:
            file.seek(0)
            if os.name == 'nt':
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


class ChatGPTAuth:
    def __init__(self, directory=Path('.seekra/chatgpt'), profile='default'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', profile):
            raise ValueError('ChatGPT 账号标签需为 1–40 位字母、数字、下划线或连字符。')
        self.directory = Path(directory)
        self.profile = profile
        self.path = self.directory / (profile + '.credentials')

    def read(self):
        if not self.path.exists():
            return {}
        if self.path.is_symlink():
            raise ValueError('凭据文件不能是符号链接。')
        raw = self.path.read_bytes()
        if os.name == 'nt':
            if not raw.startswith(b'DPAPI1\n'):
                raise ValueError('ChatGPT 凭据保护格式不匹配，请重新登录。')
            raw = _dpapi(raw[7:], decrypt=True)
        elif self.path.stat().st_mode & 0o077:
            raise ValueError('ChatGPT 凭据文件需要仅所有者可访问的权限。')
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise ValueError('ChatGPT 凭据文件不可读，请重新登录。') from None

    def write(self, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        _atomic_write(self.path, b'DPAPI1\n' + _dpapi(raw) if os.name == 'nt' else raw)

    def prepare(self, redirect_uri):
        with _lock(self.directory / 'host.lock'):
            host_path = self.directory / 'host.json'
            if host_path.exists():
                host = json.loads(host_path.read_text(encoding='utf-8'))['ext_agent_host_id']
            else:
                host = 'urn:uuid:' + str(uuid4())
                _atomic_write(host_path, json.dumps({'ext_agent_host_id':host}).encode())
        account = self.read()
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        client_id = account.get('client_id', 'dynamic_agent_client')
        query = {'client_id':client_id, 'ext_agent_host_id':host, 'response_type':'code',
                 'redirect_uri':redirect_uri, 'scope':SCOPES, 'resource':RESOURCE,
                 'state':state, 'nonce':nonce, 'code_challenge_method':'S256',
                 'code_challenge':base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')}
        if client_id == 'dynamic_agent_client':
            query['agent_name_hint'] = 'Seekra'
        elif account.get('id_token'):
            query['id_token_hint'] = account['id_token']
        return {'url':AUTHORIZE + '?' + urlencode(query), 'state':state, 'nonce':nonce,
                'verifier':verifier, 'redirect_uri':redirect_uri, 'client_id':client_id,
                'host':host, 'account':account}

    def _identity(self, token, client_id, client, nonce=None):
        try:
            header = jwt.get_unverified_header(token)
            response = client.get(JWKS, timeout=30)
            response.raise_for_status()
            key = next(k for k in response.json()['keys'] if k.get('kid') == header.get('kid'))
            identity = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=['RS256'],
                                  issuer=ISSUER, audience=client_id, leeway=5,
                                  options={'require':['iss','aud','sub','exp','iat']})
            if not isinstance(identity['sub'], str) or not identity['sub']:
                raise ValueError()
            if nonce is not None and not hmac.compare_digest(str(identity.get('nonce', '')), nonce):
                raise ValueError()
            return identity
        except (jwt.PyJWTError, ValueError, KeyError, StopIteration, TypeError):
            raise ValueError('ChatGPT 身份签名或 issuer/audience/nonce 校验失败。') from None

    def _tokens(self, client, form):
        response = client.post(TOKEN, data=form, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f'ChatGPT 授权或刷新失败（HTTP {response.status_code}），请重新登录。')
        try:
            data = response.json()
            if (not isinstance(data, dict) or data.get('token_type', '').lower() != 'bearer' or
                any(not isinstance(data.get(k), str) or not data[k] for k in ('access_token','refresh_token')) or
                type(data.get('expires_in')) is not int or data['expires_in'] <= 0):
                raise ValueError()
            return data
        except (ValueError, TypeError, AttributeError):
            raise ValueError('ChatGPT 令牌响应格式无效。') from None

    def _merge(self, old, tokens):
        scopes = tokens.get('scope')
        scopes = scopes.split() if isinstance(scopes, str) else old.get('scopes', [])
        if PLAN_SCOPE not in scopes:
            raise ValueError('本次登录未授予 ChatGPT 套餐使用权限，请在授权页允许后重试。')
        return {**old, **{k:tokens[k] for k in ('access_token','refresh_token','id_token') if k in tokens},
                'scopes':scopes, 'expires_at':time.time()+tokens['expires_in'],
                'earliest_refresh_at':tokens.get('earliest_refresh_at', 0)}

    def complete(self, pending, callback, client):
        if not hmac.compare_digest(callback.get('state', ''), pending['state']):
            raise ValueError('登录返回的 state 不匹配。')
        if callback.get('error'):
            raise ValueError('ChatGPT 登录未获授权，未交换或保存令牌。')
        client_id = callback.get('client_id', pending['client_id'])
        if not client_id or client_id == 'dynamic_agent_client' or (
                pending['client_id'] != 'dynamic_agent_client' and client_id != pending['client_id']):
            raise ValueError('登录返回的 client_id 缺失或不匹配。')
        if not callback.get('code'):
            raise ValueError('登录返回缺少授权码。')
        account = pending['account']
        # 初次交换失败仍保留已签发注册，后续使用同一 client_id 重授权。
        if not account:
            self.write({'client_id':client_id, 'ext_agent_host_id':pending['host']})
        tokens = self._tokens(client, {'grant_type':'authorization_code', 'client_id':client_id,
            'code':callback['code'], 'code_verifier':pending['verifier'],
            'redirect_uri':pending['redirect_uri'], 'resource':RESOURCE})
        identity = self._identity(tokens.get('id_token'), client_id, client, pending['nonce'])
        if account.get('subject') and identity['sub'] != account['subject']:
            raise ValueError('登录账号与该标签原有账号不同，请使用新的 --chatgpt-profile 标签。')
        saved = self._merge({**account, 'client_id':client_id, 'ext_agent_host_id':pending['host'],
                             'subject':identity['sub'], 'email':identity.get('email'), 'issuer':ISSUER}, tokens)
        self.write(saved)

    def access_token(self, client):
        with _lock(self.path.with_suffix('.lock')):
            account = self.read()
            if not account.get('access_token') or PLAN_SCOPE not in account.get('scopes', []):
                raise ValueError('尚未授权 ChatGPT 套餐，请先运行 seekra --chatgpt-login。')
            if account.get('expires_at', 0) > time.time()+60:
                return account['access_token']
            earliest = account.get('earliest_refresh_at', 0)
            if isinstance(earliest, (int, float)) and earliest > time.time():
                raise RuntimeError('ChatGPT 令牌尚未到可刷新时间，请稍后重试。')
            tokens = self._tokens(client, {'grant_type':'refresh_token', 'client_id':account['client_id'],
                                           'refresh_token':account['refresh_token'], 'resource':RESOURCE})
            if tokens.get('id_token'):
                identity = self._identity(tokens['id_token'], account['client_id'], client)
                if identity['sub'] != account['subject']:
                    raise ValueError('刷新返回的账号与原账号不同。')
            saved = self._merge(account, tokens)
            self.write(saved)
            return saved['access_token']

    def login(self, client, *, open_browser=webbrowser.open, timeout=180):
        with _lock(self.path.with_suffix('.lock')):
            received = {}
            pending = None
            class Handler(BaseHTTPRequestHandler):
                def do_GET(handler):
                    parsed = urlparse(handler.path)
                    values = parse_qs(parsed.query, keep_blank_values=True)
                    valid = (parsed.path == '/auth/callback' and
                             handler.headers.get('Host') == f'127.0.0.1:{server.server_port}' and
                             all(len(v) == 1 for v in values.values()) and
                             hmac.compare_digest(values.get('state', [''])[0], pending['state']))
                    handler.send_response(200 if valid else 400)
                    handler.send_header('Content-Type', 'text/plain; charset=utf-8')
                    handler.send_header('Cache-Control', 'no-store')
                    handler.end_headers()
                    handler.wfile.write(('登录返回已收到，请回到 Seekra 查看验证结果。' if valid else '无效登录返回。').encode())
                    if valid:
                        received.update({k:v[0] for k,v in values.items()})
                def log_message(self, *args):
                    pass
            with HTTPServer(('127.0.0.1', 0), Handler) as server:
                server.timeout = .25
                pending = self.prepare(f'http://127.0.0.1:{server.server_port}/auth/callback')
                if not open_browser(pending['url']):
                    raise RuntimeError('无法打开系统浏览器，请配置默认浏览器后重试。')
                deadline = time.monotonic()+timeout
                while not received and time.monotonic() < deadline:
                    server.handle_request()
                if not received:
                    raise RuntimeError('ChatGPT 登录等待超时；可以重新运行登录命令。')
                self.complete(pending, received, client)
