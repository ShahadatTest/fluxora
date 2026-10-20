"""Bounded synthetic demo behind an HTTPS proxy, with explicit access modes.

This is a restricted demo deployment, not multi-user production authentication.
Local CLI behaviour and financial/approval handlers remain unchanged.
"""
import base64
from collections import deque
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import threading
import time
from urllib.parse import urlsplit,parse_qs
from http.server import ThreadingHTTPServer

from . import web, agent_web
from .visitors import Visitors, VisitorError, cookie_header


def password_hash(password, salt=None):
    salt=salt or secrets.token_hex(16)
    digest=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1,dklen=32).hex()
    return 'scrypt$'+salt+'$'+digest


@dataclass(frozen=True)
class Config:
    domain: str
    username: str
    verifier: str
    database: str
    port: int = 8080
    workers: int = 8
    public_demo: bool = False
    visitor_capacity: int = 128
    public_ai: bool = False
    public_ai_daily_limit: int = 1000

    def __post_init__(self):
        if len(self.domain)>253 or '.' not in self.domain or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',part) for part in self.domain.split('.')):
            raise ValueError('Set one public DNS hostname without scheme, port or path.')
        if type(self.public_demo) is not bool:raise ValueError('Invalid public demo mode.')
        if type(self.visitor_capacity) is not int or not 1<=self.visitor_capacity<=256:raise ValueError('Visitor capacity must be between 1 and 256.')
        if type(self.public_ai) is not bool or (self.public_ai and not self.public_demo):raise ValueError('Public AI requires explicit public demo mode.')
        if type(self.public_ai_daily_limit) is not int or not 1<=self.public_ai_daily_limit<=1000:raise ValueError('Public AI daily limit must be between 1 and 1000.')
        if not self.public_demo:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',self.username):raise ValueError('Invalid owner username.')
            if not re.fullmatch(r'scrypt\$[a-f0-9]{32}\$[a-f0-9]{64}',self.verifier):raise ValueError('A valid owner password verifier is required.')
        if not 1024<=self.port<=65535 or not 1<=self.workers<=16:raise ValueError('Invalid hosted resource limits.')


def environment_config():
    mode=os.environ.get('FLUXORA_PUBLIC_DEMO','0')
    if mode not in {'0','1'}:raise ValueError('Set FLUXORA_PUBLIC_DEMO to 0 or 1.')
    ai=os.environ.get('FLUXORA_PUBLIC_AI','0')
    if ai not in {'0','1'}:raise ValueError('Set FLUXORA_PUBLIC_AI to 0 or 1.')
    daily=os.environ.get('FLUXORA_PUBLIC_AI_DAILY_LIMIT','1000')
    if not re.fullmatch('[0-9]{1,4}',daily):raise ValueError('Invalid public AI daily limit.')
    if ai=='1' and not all(os.environ.get(k,'').strip() for k in ['CASHFLOW_MODEL_API_KEY','CASHFLOW_MODEL_BASE_URL','CASHFLOW_MODEL_NAME']):raise ValueError('Public AI needs a complete private model configuration.')
    if mode=='1' and ai!='1' and os.environ.get('CASHFLOW_MODEL_API_KEY','').strip():
        from .telegram_bridge import configured
        private=configured()
        if not private or not private[0].use_model:
            raise ValueError('Public visitor demos require model-free operation; only an explicitly configured private owner Telegram model is allowed.')
    capacity=os.environ.get('FLUXORA_VISITOR_CAPACITY','128')
    if not re.fullmatch('[0-9]{1,3}',capacity):raise ValueError('Visitor capacity must be between 1 and 256.')
    return Config(os.environ.get('FLUXORA_DOMAIN',''),os.environ.get('FLUXORA_OWNER_USER',''),
        os.environ.get('FLUXORA_OWNER_HASH',''),os.environ.get('FLUXORA_DATABASE','/data/state.sqlite'),public_demo=mode=='1',visitor_capacity=int(capacity),public_ai=ai=='1',public_ai_daily_limit=int(daily))


class WindowLimits:
    def __init__(self):self.lock=threading.Lock();self.events={}
    def allow(self,key,cap,seconds=60):
        with self.lock:
            now=time.monotonic();queue=self.events.setdefault(key,deque())
            while queue and queue[0]<=now-seconds:queue.popleft()
            if len(queue)>=cap:return False
            queue.append(now);return True


class DemoServer(ThreadingHTTPServer):
    daemon_threads=False
    block_on_close=True
    request_queue_size=32
    def __init__(self,address,handler,config):
        self.config=config;self.limits=WindowLimits();self.auth_cache=None
        self.telegram=None
        self.public_ai_adapter=None
        if config.public_ai:
            from .public_ai import create
            self.public_ai_adapter=create(config.database,config.public_ai_daily_limit)
        self.visitors=Visitors(Path(config.database).resolve().parent/'visitors',capacity=config.visitor_capacity) if config.public_demo else None
        self.slots=threading.BoundedSemaphore(config.workers)
        super().__init__(address,handler)
    def process_request(self,request,address):
        if not self.slots.acquire(False):
            try:
                # Consume bounded request headers before replying: closing a
                # socket with unread input can reset the connection and hide
                # the intended 503, particularly on Windows.
                request.settimeout(.25);received=b''
                while b'\r\n\r\n' not in received and len(received)<16384:
                    chunk=request.recv(min(4096,16384-len(received)))
                    if not chunk:break
                    received+=chunk
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nRetry-After: 5\r\nConnection: close\r\n\r\n')
                request.shutdown(socket.SHUT_WR)
            except OSError:pass
            self.shutdown_request(request);return
        try:super().process_request(request,address)
        except BaseException:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()
    def handle_error(self,request,address):
        # Never print request headers, credentials, provider text or financial data.
        print('Hosted request failed; connection closed.',flush=True)


class Handler(web.Handler):
    server_version='FluxoraDemo/0.25.10'
    sys_version=''
    def version_string(self):return self.server_version
    def setup(self):super().setup();self.connection.settimeout(15)
    def send_header(self,key,value):
        if not hasattr(self,'_response_headers'):self._response_headers=set()
        # Workspace responses must not be reused across visitor sessions.
        if key.lower()=='cache-control' and self.server.config.public_demo:value='no-store'
        self._response_headers.add(key.lower());super().send_header(key,value)
    def end_headers(self):
        defaults={
            'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
            # no-referrer turns native form POST origins into "null" in browsers,
            # conflicting with our exact Origin check. Keep referrers private
            # across origins while allowing same-origin forms to identify us.
            'X-Frame-Options':'DENY', 'Referrer-Policy':'same-origin',
            'Permissions-Policy':'camera=(), microphone=(), geolocation=()',
            'Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        }
        for key,value in defaults.items():
            if key.lower() not in getattr(self,'_response_headers',set()):self.send_header(key,value)
        if getattr(self,'_visitor_cookie',None):self.send_header('Set-Cookie',cookie_header(self._visitor_cookie))
        super().end_headers()
    def _reply(self,code,message,challenge=False):
        body=json.dumps({'status':message}).encode()
        self.send_response(code);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close')
        if challenge:self.send_header('WWW-Authenticate','Basic realm="Fluxora owner demo", charset="UTF-8"')
        if code==429:self.send_header('Retry-After','60')
        self.end_headers();self.wfile.write(body);self.close_connection=True
    def _authorized(self):
        values=self.headers.get_all('Authorization',[])
        if len(values)!=1 or len(values[0])>512:return False
        raw=values[0];digest=hashlib.sha256(raw.encode()).digest()
        if self.server.auth_cache and hmac.compare_digest(digest,self.server.auth_cache):return True
        if not self.server.limits.allow('auth-attempt',20):return False
        try:
            scheme,encoded=raw.split(' ',1)
            if scheme.lower()!='basic':return False
            user,password=base64.b64decode(encoded,validate=True).decode('utf-8').split(':',1)
            if not hmac.compare_digest(user.encode(),self.server.config.username.encode()) or not 20<=len(password)<=200:return False
            salt=self.server.config.verifier.split('$')[1]
            if not hmac.compare_digest(password_hash(password,salt),self.server.config.verifier):return False
            self.server.auth_cache=digest;return True
        except (ValueError,UnicodeError):return False
    def _handle(self):
        self._response_headers=set();self._visitor_cookie=None;config=self.server.config
        if self.command=='GET' and self.path=='/healthz' and self.client_address[0] in {'127.0.0.1','::1'}:
            try:
                with sqlite3.connect(Path(config.database).resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as db:
                    db.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone()
            except sqlite3.Error:return self._reply(503,'Database unavailable')
            return self._reply(200,'ok')
        hosts=self.headers.get_all('Host',[])
        if len(hosts)!=1 or hosts[0].lower() not in {config.domain,config.domain+':443'}:return self._reply(403,'Unrecognised host')
        if not config.public_demo and not self._authorized():return self._reply(401,'Owner authentication required',True)
        if self.command not in {'GET','POST'}:return self._reply(405,'Use GET or POST')
        if len(self.path)>8192 or not self.path.startswith('/') or self.path.startswith('//'):return self._reply(400,'Invalid request target')
        lengths=self.headers.get_all('Content-Length',[])
        if self.headers.get_all('Transfer-Encoding') or len(lengths)>1 or (lengths and not re.fullmatch('[0-9]{1,10}',lengths[0])):
            return self._reply(400,'Invalid request framing')
        length=int(lengths[0]) if lengths else 0
        if length>web.MAX_REQUEST_BYTES:return self._reply(413,'Request too large')
        if self.command=='GET' and length:return self._reply(400,'GET body not supported')
        if self.headers.get('Content-Encoding','identity').lower()!='identity':return self._reply(415,'Compressed request bodies are not supported')
        if self.command=='POST':
            if self.headers.get_all('Origin',[])!=['https://'+config.domain]:return self._reply(403,'Same-origin form required')
            if not self.server.limits.allow('post',40):return self._reply(429,'Operation rate limit')
            if urlsplit(self.path).path in {'/agent/ask','/agent/verify','/agent/tasks/translate','/agent/tasks/qualify'} and not self.server.limits.allow('model',6):
                return self._reply(429,'Agent request rate limit')
        if not self.server.limits.allow('all',600):return self._reply(429,'Request rate limit')
        if self.path=='/healthz':return self._reply(200,'ok')
        parsed=urlsplit(self.path);path=parsed.path
        from .ui import REQUEST
        REQUEST.path=path;REQUEST.hosted=True;REQUEST.public_demo=config.public_demo
        REQUEST.public_ai=self.server.public_ai_adapter is not None
        REQUEST.public_ai_adapter=self.server.public_ai_adapter
        REQUEST.public_ai_qualification_path=(self.server.public_ai_adapter.qualification_path if self.server.public_ai_adapter else None)
        if config.public_ai and self.command=='POST' and path in {'/agent/verify','/agent/tasks/qualify','/agent/ask'}:
            return self._reply(403,'Public AI uses reviewed guided tasks only; operator checks are private')
        if config.public_ai and self.command=='GET' and path=='/agent':return self._redirect('/agent/tasks')
        if path.startswith('/settings/connections'):
            if self.command!='GET':return self._reply(403,'Hosted connection settings are locked; no operation was performed')
            if path!='/settings/connections':return self._reply(404,'Settings preview not found')
            if parsed.query or parsed.fragment:return self._reply(400,'Open the settings preview without query parameters')
            # A static preview, before visitor admission or any configuration
            # access. Disabled markup is informational; this server guard is
            # the authority boundary even for manually crafted requests.
            from .ui import REQUEST
            from .connection_preview import render
            REQUEST.path=path;REQUEST.hosted=True;REQUEST.public_demo=config.public_demo
            return self._send_html(200,render())
        if config.public_demo:
            parsed=urlsplit(self.path);path=parsed.path
            # Explicit, code-owned public routes only. Do not dispatch arbitrary
            # paths without a workspace or fall back to the legacy shared DB.
            from .site_web import assets,public_page
            from .demo_examples import EXAMPLES,render
            if self.command=='GET':
                if path=='/demo/examples':
                    query=parse_qs(parsed.query,keep_blank_values=True)
                    values=query.get('example',['atlas'])
                    if set(query)-{'example'} or len(values)!=1 or values[0] not in EXAMPLES:return self._reply(400,'Choose one displayed demo example')
                    return self._send_html(200,render(web,values[0]))
                public=path in {'/welcome','/product','/how-it-works','/trust','/about','/docs','/start'} or bool(re.fullmatch('/docs/[a-z0-9-]+',path))
                asset=path.startswith('/assets/') and path[len('/assets/'):] in assets
                if public or asset:return super()._handle()
                if path.startswith('/assets/') or path=='/favicon.ico' or web.match_route('GET',path)[0] is None:
                    return self._send_html(404,public_page('Not found','<h1>Page not found</h1><p><a href="/welcome">Product home</a></p>',path))
            example='atlas'
            if path in {'/app','/'}:
                query=parse_qs(parsed.query,keep_blank_values=True)
                values=query.get('demo_example',['atlas'])
                if len(values)!=1 or values[0] not in EXAMPLES:return self._reply(400,'Choose one displayed demo example')
                example=values[0]
            try:
                with self.server.visitors.lease(self.headers,self.command,example=example) as (state,issued):
                    self._visitor_cookie=issued
                    with web.workspace_scope(state):return self._dispatch_workspace()
            except VisitorError as error:
                if error.code==503 and self.command=='GET':
                    return self._send_html(503,public_page('Demo workspace busy','<h1>Workspace capacity is temporarily full.</h1><p>Existing workspaces are preserved. Try again after an older session expires, or explore the read-only examples now.</p><a class="btn primary" href="/demo/examples">Explore demo examples</a>',path))
                return self._reply(error.code,error.message)
            except (OSError,sqlite3.Error):return self._reply(503,'Visitor workspace unavailable')
        return self._dispatch_workspace()

    def _dispatch_workspace(self):
        if self.server.config.public_ai and self.command=='POST' and urlsplit(self.path).path=='/agent/tasks/translate':
            identity=hashlib.sha256(web.database_path().encode()).hexdigest()
            if not self.server.limits.allow('public-ai:'+identity,20,3600):return self._reply(429,'This visitor has reached the hourly AI allowance; direct reports remain available')
        with web.LOCK:
            # Bounded staging is hosted-only. Never evict a currently executing
            # interpretation; old unsubmitted previews can safely expire.
            now=time.monotonic()
            pending=web.staged()
            stores=[s.staged for s in self.server.visitors.pending_workspaces()] if self.server.visitors else [pending]
            for store in stores:
                for token,payload in list(store.items()):
                    stamp=payload.setdefault('_hosted_created',now)
                    if now-stamp>900 and not payload.get('parsing'):store.pop(token,None)
            if sum(map(len,stores))>=128 or len(pending)>=(16 if self.server.visitors else 128):
                return self._reply(503,'Too many pending previews; allow older previews to expire')
        from .ui import REQUEST
        REQUEST.hosted=True
        REQUEST.public_demo=self.server.config.public_demo
        return super()._handle()
    do_GET=_handle
    do_POST=_handle
    do_HEAD=_handle
    do_OPTIONS=_handle


def main():
    config=environment_config()
    web.DB_PATH=config.database
    Path(config.database).parent.mkdir(parents=True,exist_ok=True)
    if not Path(config.database).exists():
        from .cli import cmd_seed
        cmd_seed(config.database)
    agent_web.reset_live_adapter();agent_web.mount(web)
    server=DemoServer(('0.0.0.0',config.port),Handler,config)
    from .telegram_bridge import configured,TelegramBridge
    telegram=configured()
    if telegram:
        tg_config,api=telegram
        from .model_adapter import OpenAICompatibleAdapter
        adapter=OpenAICompatibleAdapter.from_env(max_retries=0,timeout_s=25,total_budget_s=30) if tg_config.use_model else None
        server.telegram=TelegramBridge(server,tg_config,api,adapter=adapter)
        server.telegram.start()
    def stop(*_):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    print(('Public isolated visitor demo' if config.public_demo else 'Protected synthetic owner demo')+' started; private network listener.',flush=True)
    try:server.serve_forever()
    finally:
        server.server_close()
        if server.telegram:server.telegram.close()


if __name__=='__main__':main()
