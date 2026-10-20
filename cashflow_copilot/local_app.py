"""Persistent single-owner loopback app, with explicitly enabled Telegram.

Reuses the existing handlers, deterministic engine and Telegram control plane.
No visitor expiry, production login, automatic qualification or model call.
"""
from contextlib import contextmanager
import argparse
import hashlib
from pathlib import Path
import re
import sqlite3

from . import web,agent_web
from .demo_examples import seed,EXAMPLES
from .store import SQLiteStore
from .visitors import Workspace,VisitorError


class RunLock:
    """OS-released advisory lock; never remove another process's lock file."""
    def __init__(self,database):self.path=Path(str(database)+'.runlock');self.file=None
    def __enter__(self):
        try:
            self.file=self.path.open('a+b')
            if self.file.seek(0,2)==0:self.file.write(b'0');self.file.flush()
            self.file.seek(0)
            if __import__('os').name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            if self.file:self.file.close();self.file=None
            raise ValueError('This local database is already running. Reuse its browser window or stop that app first.') from None
        return self
    def __exit__(self,*_):
        if self.file:self.file.close();self.file=None


class LocalWorkspace:
    durable=True
    def __init__(self,database):
        self.database=Path(database).resolve()
        self.digest=hashlib.sha256(str(self.database).encode()).hexdigest()
        self.state=Workspace(str(self.database),web.CSRF_TOKEN)
    def workspace_digest(self,database):
        if Path(database).resolve()!=self.database:raise ValueError('Use the configured local workspace.')
        return self.digest
    def database_for_digest(self,digest):
        if digest!=self.digest:raise VisitorError(409,'Local workspace changed; link the current workspace.')
        return self.database
    @contextmanager
    def lease_digest(self,digest):
        if not self.database_for_digest(digest).is_file():raise VisitorError(409,'Local workspace unavailable')
        yield self.state


class Handler(web.Handler):
    def setup(self):super().setup();self.connection.settimeout(15)
    def send_header(self,key,value):
        if not hasattr(self,'_local_headers'):self._local_headers=set()
        if key.lower()=='cache-control':value='no-store'
        self._local_headers.add(key.lower());super().send_header(key,value)
    def end_headers(self):
        for key,value in [('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),('X-Frame-Options','DENY'),('Referrer-Policy','same-origin')]:
            if key.lower() not in getattr(self,'_local_headers',set()):self.send_header(key,value)
        super().end_headers()
    def reject(self,message):
        self.close_connection=True
        return self._send_html(403,'<!doctype html><title>Local request refused</title><p>'+message+'</p>')
    def _handle(self):
        self._local_headers=set()
        if self.command not in {'GET','POST'}:return self.reject('Use GET or POST.')
        expected='127.0.0.1:'+str(self.server.server_address[1])
        if self.headers.get_all('Host',[])!=[expected]:return self.reject('Open the displayed 127.0.0.1 address.')
        if self.command=='POST' and self.headers.get_all('Origin',[])!=['http://'+expected]:
            return self.reject('Submit this form from the local Fluxora page.')
        lengths=self.headers.get_all('Content-Length',[])
        if self.headers.get_all('Transfer-Encoding') or len(lengths)>1 or (lengths and not re.fullmatch('[0-9]{1,10}',lengths[0])):
            return self.reject('Invalid request framing.')
        if self.command=='GET' and lengths and int(lengths[0]):return self.reject('GET bodies are not supported.')
        if self.path=='/healthz' and self.command=='GET':
            data=b'{"status":"ok","mode":"local"}'
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)))
            self.end_headers();self.wfile.write(data);return
        from .ui import REQUEST
        with self.server.local_connections.gate:
            REQUEST.local_connections=self.server.local_connections
            try:
                with web.workspace_scope(self.server.visitors.state):return super()._handle()
            finally:del REQUEST.local_connections
    do_GET=_handle
    do_POST=_handle
    do_HEAD=_handle
    do_OPTIONS=_handle


def create_server(database,port=8765,*,example='atlas',telegram=False,api=None,telegram_config=None):
    database=Path(database).resolve();database.parent.mkdir(parents=True,exist_ok=True)
    # Bind before seeding so an occupied port does not create an unused ledger.
    server=web.ThreadingHTTPServer(('127.0.0.1',port),Handler)
    try:
        if not database.exists():
            store=SQLiteStore(str(database))
            try:store.save_ledger(seed(example))
            finally:store.close()
        with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as conn:
            if conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Database integrity check failed; retain it and restore a reviewed backup.')
        store=SQLiteStore(str(database))
        try:store.load_ledger()
        finally:store.close()
        server.workspace_database=str(database);server.visitors=LocalWorkspace(database);server.telegram=None
        from .local_connections import Connections
        from . import local_settings_web
        server.local_connections=Connections(server);local_settings_web.mount(web)
        agent_web.reset_live_adapter();agent_web.mount(web)
        if telegram:
            from .telegram_bridge import configured,TelegramBridge
            current=(telegram_config,api) if telegram_config is not None and api is not None else configured()
            if not current:raise ValueError('Telegram is not configured. Set its environment privately or omit --telegram.')
            config,transport=current
            from .model_adapter import OpenAICompatibleAdapter
            adapter=server.local_connections.active_adapter() if config.use_model else None
            server.telegram=TelegramBridge(server,config,transport,adapter=adapter)
        return server
    except BaseException:
        server.server_close();raise


def main(argv=None):
    parser=argparse.ArgumentParser(description='Persistent Fluxora on this computer only.')
    parser.add_argument('--db',default='.runtime/local/state.sqlite')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--example',choices=tuple(EXAMPLES),default='atlas')
    parser.add_argument('--telegram',action='store_true',help='Enable configured private bot; do not reuse a token polling on the VPS.')
    parser.add_argument('--open-browser',action='store_true')
    args=parser.parse_args(argv)
    if not 1024<=args.port<=65535:parser.error('Choose a port from 1024 to 65535.')
    database=Path(args.db).resolve();database.parent.mkdir(parents=True,exist_ok=True)
    try:
        with RunLock(database):
            server=create_server(database,args.port,example=args.example,telegram=args.telegram)
            url='http://127.0.0.1:'+str(args.port)
            print('Fluxora local: '+url+'/welcome',flush=True)
            print('Workspace: '+url+'/app | Docs: '+url+'/docs | Telegram: '+url+'/telegram',flush=True)
            print('Persistent synthetic database: '+str(database),flush=True)
            print('No model call on startup. Ctrl+C stops this app; data is retained.',flush=True)
            if server.telegram:server.telegram.start()
            if args.open_browser:
                import webbrowser
                webbrowser.open(url+'/welcome')
            try:server.serve_forever()
            except KeyboardInterrupt:pass
            finally:
                server.server_close()
                if server.telegram:server.telegram.close()
    except (OSError,ValueError,sqlite3.Error):
        # No raw configuration/provider exception or environment values.
        parser.exit(1,'Local startup failed. Check the port, database permissions/integrity and private optional configuration. No reset was performed.\n')


if __name__=='__main__':main()
