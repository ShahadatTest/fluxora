"""Anonymous, expiring synthetic workspaces for one hosted server process.

The opaque cookie is a bearer capability, not a production identity. Only its
SHA-256 digest is stored; client input is never used directly as a file path.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import threading
import time

COOKIE='__Host-fluxora-visitor'
TTL=24*60*60


class VisitorError(Exception):
    def __init__(self,code,message):self.code=code;self.message=message


def cookie_token(headers):
    values=headers.get_all('Cookie',[])
    if len(values)>1 or sum(map(len,values))>4096:
        raise VisitorError(400,'Invalid visitor cookie headers')
    raw=values[0] if values else ''
    # SimpleCookie overwrites duplicate keys. Refuse ambiguity before parsing.
    if sum(part.strip().split('=',1)[0]==COOKIE for part in raw.split(';'))>1:
        raise VisitorError(400,'Ambiguous visitor cookie')
    try:
        parsed=SimpleCookie();parsed.load(raw)
        value=parsed[COOKIE].value if COOKIE in parsed else ''
    except CookieError:value=''
    return value if re.fullmatch(r'[A-Za-z0-9_-]{43}',value) else ''


@dataclass
class Workspace:
    database: str
    csrf: str
    staged: dict = field(default_factory=dict)


class Visitors:
    def __init__(self,root,*,ttl=TTL,capacity=64,workspace_bytes=32*1024*1024,total_bytes=256*1024*1024,clock=time.time):
        self.root=Path(root).resolve();self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.registry=self.root/'registry.sqlite'
        self.ttl=ttl;self.capacity=capacity;self.workspace_bytes=workspace_bytes;self.total_bytes=total_bytes;self.clock=clock
        self.lock=threading.RLock();self.active={};self.cache={}
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS visitors (digest TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL)')

    def _db(self):
        # Connection context managers commit but do not close SQLite handles.
        @contextmanager
        def connection():
            db=sqlite3.connect(self.registry,timeout=5)
            try:
                with db:yield db
            finally:db.close()
        return connection()

    def pending_workspaces(self):
        with self.lock:return list(self.cache.values())

    def database_for_digest(self,digest):return self._folder(digest)/'state.sqlite'

    def workspace_digest(self,database):
        path=Path(database).resolve();digest=path.parent.name
        if self.database_for_digest(digest)!=path:raise ValueError('Use the selected visitor workspace.')
        return digest

    @contextmanager
    def lease_digest(self,digest):
        """Trusted integration only: resume a registered session without its cookie.

        This method is never exposed as a URL/form workspace selector.
        """
        with self.lock,self._db() as db:
            folder=self._folder(digest);now=self.clock();self._prune(db,now)
            row=db.execute('SELECT csrf,expires FROM visitors WHERE digest=?',(digest,)).fetchone()
            if not row or row[1]<=now or not (folder/'state.sqlite').is_file():
                raise VisitorError(409,'Visitor workspace expired')
            if self._bytes(folder)>=self.workspace_bytes or self._bytes(self.root)>=self.total_bytes:
                raise VisitorError(503,'Demo storage capacity reached')
            state=self.cache.setdefault(digest,Workspace(str(folder/'state.sqlite'),row[0]))
            self.active[digest]=self.active.get(digest,0)+1
        try:yield state
        finally:
            with self.lock:
                self.active[digest]-=1
                if not self.active[digest]:del self.active[digest]

    def _folder(self,digest):
        if not re.fullmatch('[a-f0-9]{64}',digest):raise RuntimeError('Invalid workspace identity')
        folder=self.root/digest
        if folder.is_symlink() or folder.resolve().parent!=self.root:raise RuntimeError('Invalid workspace boundary')
        return folder

    def _bytes(self,folder):
        return sum(p.stat().st_size for p in folder.rglob('*') if p.is_file() and not p.is_symlink())

    def _prune(self,db,now):
        for digest, in db.execute('SELECT digest FROM visitors WHERE expires<=?',(now,)).fetchall():
            if self.active.get(digest):continue
            folder=self._folder(digest)
            if folder.exists():shutil.rmtree(folder)
            db.execute('DELETE FROM visitors WHERE digest=?',(digest,));self.cache.pop(digest,None)
        # Recover directories orphaned by a process stopping between seed/insert.
        known={r[0] for r in db.execute('SELECT digest FROM visitors')}
        for folder in self.root.iterdir():
            if re.fullmatch('[a-f0-9]{64}',folder.name) and folder.name not in known:
                folder=self._folder(folder.name)
                if folder.is_dir() and now-folder.stat().st_mtime>=self.ttl:shutil.rmtree(folder)

    @contextmanager
    def lease(self,headers,method,*,example='atlas'):
        token=cookie_token(headers);digest=hashlib.sha256(token.encode()).hexdigest() if token else ''
        issued=None
        with self.lock,self._db() as db:
            now=self.clock();self._prune(db,now)
            row=db.execute('SELECT csrf,expires FROM visitors WHERE digest=?',(digest,)).fetchone() if digest else None
            if row and row[1]<=now:row=None
            if not row:
                if method!='GET':raise VisitorError(409,'Visitor workspace missing or expired; reload the page before submitting')
                if db.execute('SELECT count(*) FROM visitors').fetchone()[0]>=self.capacity:
                    raise VisitorError(503,'Visitor workspace capacity reached; try again after older demos expire')
                if self._bytes(self.root)>=self.total_bytes:
                    raise VisitorError(503,'Demo storage capacity reached')
                issued=secrets.token_urlsafe(32);digest=hashlib.sha256(issued.encode()).hexdigest()
                folder=self._folder(digest);folder.mkdir(mode=0o700)
                try:
                    from .demo_examples import seed
                    from .store import SQLiteStore
                    ledger=seed(example)
                    store=SQLiteStore(str(folder/'state.sqlite'))
                    try:store.save_ledger(ledger)
                    finally:store.close()
                    csrf=secrets.token_hex(32)
                    db.execute('INSERT INTO visitors VALUES (?,?,?)',(digest,csrf,now+self.ttl))
                except BaseException:
                    shutil.rmtree(folder);raise
                row=(csrf,now+self.ttl)
            folder=self._folder(digest)
            if not (folder/'state.sqlite').is_file():raise VisitorError(503,'Visitor workspace unavailable')
            if self._bytes(folder)>=self.workspace_bytes or self._bytes(self.root)>=self.total_bytes:
                raise VisitorError(503,'Demo storage capacity reached')
            db.commit()  # Admission must be durable before marking a lease active.
            state=self.cache.setdefault(digest,Workspace(str(folder/'state.sqlite'),row[0]))
            self.active[digest]=self.active.get(digest,0)+1
        try:yield state,issued
        finally:
            with self.lock:
                self.active[digest]-=1
                if not self.active[digest]:del self.active[digest]


def cookie_header(token):
    return f'{COOKIE}={token}; Path=/; Max-Age={TTL}; Secure; HttpOnly; SameSite=Lax'
