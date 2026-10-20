"""Local-only connection profiles. Never put credentials in financial SQLite.

No environment mutation, automatic provider fallback or automatic bot restart.
HTTP uses this manager's gate for a whole request; settings changes refuse a
busy bot instead of interrupting a model task or waiting on a cross-lock join.
"""
import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import urlsplit
from uuid import uuid4

from .errors import CashflowError
from .local_vault import WindowsVault
from .model_adapter import OpenAICompatibleAdapter,OpenRouterAdapter,_openrouter_url
from .telegram_api import TelegramAPI
from .telegram_bridge import TelegramBridge,TelegramConfig

PRESETS={
    'openrouter':('OpenRouter','https://openrouter.ai/api/v1','https://openrouter.ai/docs/api/reference/overview'),
    'gemini':('Gemini compatible','https://generativelanguage.googleapis.com/v1beta/openai','https://ai.google.dev/gemini-api/docs/openai'),
    'deepseek':('DeepSeek','https://api.deepseek.com','https://api-docs.deepseek.com/'),
    'groq':('Groq','https://api.groq.com/openai/v1','https://console.groq.com/docs/openai'),
    'mistral':('Mistral','https://api.mistral.ai/v1','https://docs.mistral.ai/resources/migration-guides'),
    'custom':('Custom OpenAI-compatible','',''),
}


def endpoint(value):
    if not isinstance(value,str) or not 1<=len(value)<=512 or any(ord(c)<33 for c in value):raise CashflowError('Use a valid API base URL.')
    try:
        p=urlsplit(value);port=p.port
        if p.username or p.password or p.query or p.fragment or not p.hostname:raise ValueError()
        if p.scheme!='https' and not (p.scheme=='http' and p.hostname in {'127.0.0.1','localhost','::1'}):raise ValueError()
        if p.path.endswith('/chat/completions'):raise ValueError()
    except ValueError:raise CashflowError('Use an HTTPS API base URL, or loopback HTTP. No credentials, query or completions suffix.') from None
    return value.rstrip('/')


def secret(value,required=True):
    if not isinstance(value,str) or len(value)>4096 or any(ord(c)<33 or ord(c)>126 for c in value):raise CashflowError('Use a valid secret in the password field.')
    if required and not value:raise CashflowError('Enter the API key/token in its password field.')
    return value


# Idle polling briefly takes the same lock for SQLite housekeeping. Allow that
# section to finish while retaining a bounded refusal for a real active request.
BOT_IDLE_WAIT_SECONDS=.25


def idle_bot(fn):
    def guarded(self,*args,**kwargs):
        bot=self.server.telegram
        if bot and not bot.lock.acquire(timeout=BOT_IDLE_WAIT_SECONDS):raise CashflowError('Bot request in progress. Wait for its result before changing connections.')
        try:return fn(self,*args,**kwargs)
        finally:
            if bot:bot.lock.release()
    return guarded


class Connections:
    def __init__(self,server,*,vault=None,adapter_factory=None,api_factory=None):
        self.server=server;self.gate=threading.RLock()
        self.vault=vault or WindowsVault(Path(server.workspace_database).parent/'.local-connections.dpapi')
        self.adapter_factory=adapter_factory or self._adapter
        self.api_factory=api_factory or TelegramAPI
        self.data={'profiles':{},'active':'environment','telegram':None}
        self.adapters={};self.env_adapter=None;self.off_adapter=OpenAICompatibleAdapter();self.load_error=False
        if self.vault.path.exists():
            try:
                raw=json.loads(self.vault.load())
                if raw.get('version')!=1 or not isinstance(raw.get('profiles'),dict) or len(raw['profiles'])>12:raise ValueError()
                for ident,profile in raw['profiles'].items():
                    if not re.fullmatch('[a-f0-9]{32}',ident) or not isinstance(profile,dict):raise ValueError()
                    self._validate(profile)
                tg=raw.get('telegram')
                if tg:self._validate_bot(tg)
                active=raw.get('active','off')
                if active not in {'off','environment'} and active not in raw['profiles']:raise ValueError()
                self.data=dict(profiles=raw['profiles'],active=active,telegram=tg)
            except Exception:self.load_error=True;self.data['active']='off'
    @staticmethod
    def _adapter(profile):
        kind=OpenRouterAdapter if _openrouter_url(profile['base_url']) else OpenAICompatibleAdapter
        adapter=kind(api_key=profile['key'] or 'local-no-key',base_url=profile['base_url'],model=profile['model'],timeout_s=25,max_retries=0,total_budget_s=30)
        adapter.local_configuration_id=profile['revision']
        return adapter
    def _validate(self,p):
        if p.get('provider') not in PRESETS or not isinstance(p.get('label'),str) or not 1<=len(p['label'])<=64:raise CashflowError('Choose a provider and a short profile label.')
        if not isinstance(p.get('model'),str) or not re.fullmatch(r'[A-Za-z0-9_./:@+\-]{1,160}',p['model']):raise CashflowError('Enter the exact model ID from your provider.')
        if type(p.get('no_key',False)) is not bool:raise CashflowError('Reload the current connection form.')
        endpoint(p.get('base_url'));secret(p.get('key'),required=not p.get('no_key',False))
        if p.get('no_key') and urlsplit(p['base_url']).hostname not in {'127.0.0.1','localhost','::1'}:raise CashflowError('Key-free mode is only available for loopback providers.')
        if not isinstance(p.get('remember'),bool) or not re.fullmatch('[a-f0-9]{32}',p.get('revision','')):raise CashflowError('Reload the current connection form.')
    def _validate_bot(self,p):
        if not re.fullmatch('[1-9][0-9]{0,15}',str(p['owner'])) or type(p.get('use_model')) is not bool:raise ValueError()
        TelegramConfig(int(p['owner']),p['name'],p['use_model']);secret(p['token'])
        # Shape validation only, never a network call.
        TelegramAPI(p['token'])
        if not isinstance(p.get('remember'),bool):raise ValueError()
    def _persist(self,candidate):
        if self.load_error:raise CashflowError('Saved settings could not be opened. Use Forget saved settings before replacing them; session-only mode is still available.')
        profiles={k:v for k,v in candidate['profiles'].items() if v['remember']}
        tg=candidate['telegram'];tg=tg if tg and tg['remember'] else None
        active=candidate['active']
        stored={'version':1,'profiles':profiles,'telegram':tg,'active':active if active in profiles or active in {'off','environment'} else 'off'}
        if profiles or tg:self.vault.save(json.dumps(stored).encode())
        else:self.vault.forget()
    def _commit(self,candidate):
        if self.load_error:
            if any(p['remember'] for p in candidate['profiles'].values()) or (candidate['telegram'] and candidate['telegram']['remember']):
                raise CashflowError('The saved vault is unavailable. Forget it explicitly before remembering replacements.')
        else:self._persist(candidate)
        self.data=candidate
    def redacted(self,value):
        for p in self.data['profiles'].values():
            if p['key']:value=str(value).replace(p['key'],'[redacted]')
        if self.data['telegram']:value=str(value).replace(self.data['telegram']['token'],'[redacted]')
        return value
    def public(self):
        rows=[]
        for ident,p in self.data['profiles'].items():
            adapter=self.adapters.get(ident)
            rows.append(dict(id=ident,label=self.redacted(p['label']),provider=p['provider'],base_url=self.redacted(p['base_url']),model=self.redacted(p['model']),remember=p['remember'],key_present=bool(p['key']),no_key=p.get('no_key',False),state=adapter.status().state if adapter else 'configured-but-unverified'))
        return rows
    def active_adapter(self):
        ident=self.data['active']
        if ident=='off':return self.off_adapter
        if ident=='environment':
            if self.env_adapter is None or not self.env_adapter.configuration_matches_env():
                self.env_adapter=OpenAICompatibleAdapter.from_env(timeout_s=25,max_retries=0,total_budget_s=30)
            return self.env_adapter
        if ident not in self.adapters:self.adapters[ident]=self.adapter_factory(self.data['profiles'][ident])
        return self.adapters[ident]
    def _bot_idle(self):
        bot=self.server.telegram
        if bot and not bot.lock.acquire(timeout=BOT_IDLE_WAIT_SECONDS):raise CashflowError('The bot is processing a request. Wait for its result, then change connections.')
        if bot:bot.lock.release()
    def _invalidate(self):
        self.server.visitors.state.staged.clear()
        bot=self.server.telegram
        if bot:
            with bot.lock,bot.db() as db:
                db.execute("UPDATE reviews SET status='revoked' WHERE status='pending' AND kind IN ('task','model_check')")
                if bot.config.use_model:bot.adapter=self.active_adapter()
    @idle_bot
    def save_profile(self,*,ident,label,provider,base_url,model,key,remember=False,no_key=False):
        self._bot_idle();candidate=copy.deepcopy(self.data)
        if ident and ident not in candidate['profiles']:raise CashflowError('Profile not found.')
        if not ident and len(candidate['profiles'])>=12:raise CashflowError('Review or remove an existing profile before adding another (maximum 12).')
        ident=ident or uuid4().hex;old=candidate['profiles'].get(ident,{})
        key=key or old.get('key','')
        p=dict(label=label.strip(),provider=provider,base_url=PRESETS[provider][1] if provider in PRESETS and provider!='custom' else base_url.strip(),model=model.strip(),key=key,no_key=no_key,remember=remember,revision=uuid4().hex)
        self._validate(p)
        sensitive=[p['key']]+[v['key'] for v in self.data['profiles'].values()]
        if self.data['telegram']:sensitive.append(self.data['telegram']['token'])
        if any(s and any(s in p[f] for f in ['label','base_url','model']) for s in sensitive):raise CashflowError('Keep credentials in password fields, not labels, URLs or model IDs.')
        candidate['profiles'][ident]=p;self._commit(candidate);self.adapters.pop(ident,None)
        if candidate['active']==ident:self._invalidate()
        return ident
    @idle_bot
    def activate(self,ident):
        self._bot_idle()
        if ident not in self.data['profiles'] and ident not in {'off','environment'}:raise CashflowError('Choose an existing profile.')
        candidate=copy.deepcopy(self.data);candidate['active']=ident
        if ident in candidate['profiles']:candidate['profiles'][ident]['revision']=uuid4().hex
        self._commit(candidate);self.adapters.pop(ident,None);self.env_adapter=None;self.off_adapter=OpenAICompatibleAdapter();self._invalidate()
    @idle_bot
    def remove(self,ident):
        self._bot_idle()
        if ident not in self.data['profiles']:raise CashflowError('Profile not found.')
        candidate=copy.deepcopy(self.data);candidate['profiles'].pop(ident)
        if candidate['active']==ident:candidate['active']='off'
        self._commit(candidate);self.adapters.pop(ident,None);self._invalidate()
    @idle_bot
    def verify(self,ident):
        if ident!=self.data['active']:raise CashflowError('Activate this profile, then test its connection.')
        return self.active_adapter().verify().state
    def save_bot(self,*,token,owner,name,use_model=False,remember=False):
        if self.server.telegram:raise CashflowError('Stop the current local bot before editing its configuration.')
        old=self.data['telegram'] or {};token=token or old.get('token','');owner=owner or old.get('owner','');name=name or old.get('name','')
        p=dict(token=token,owner=owner,name=name,use_model=use_model,remember=remember)
        try:self._validate_bot(p)
        except Exception:raise CashflowError('Enter a valid bot token, numeric private owner ID and optional bot username.') from None
        known=[token]+[row['key'] for row in self.data['profiles'].values()]
        if any(value and value in name for value in known):raise CashflowError('Keep credentials only in password fields.')
        candidate=copy.deepcopy(self.data);candidate['telegram']=p
        if not old or any(old.get(k)!=p.get(k) for k in ('token','owner','name','use_model')):
            path=Path(self.server.workspace_database).parent/'telegram-control.sqlite'
            if path.is_file():
                db=sqlite3.connect(path)
                try:
                    with db:
                        db.execute('DELETE FROM binding');db.execute('DELETE FROM pairs')
                        db.execute("UPDATE reviews SET status='revoked' WHERE status='pending'")
                        db.execute("UPDATE deliveries SET status='cancelled' WHERE status='queued'")
                        if old.get('token')!=token:
                            db.execute("DELETE FROM settings WHERE key='offset'")
                            db.execute('DELETE FROM updates')
                finally:db.close()
        # Revoke old authority before publishing changed credentials. A storage
        # failure can require relinking, but cannot leave a new bot an old link.
        self._commit(candidate)
    def test_bot(self):
        p=self.data['telegram']
        if not p:raise CashflowError('Save the local bot configuration first.')
        try:
            me=self.api_factory(p['token']).call('getMe',{})
            if not isinstance(me,dict) or me.get('is_bot') is not True:raise ValueError()
            if p['name'] and me.get('username','').casefold()!=p['name'].casefold():raise ValueError()
        except Exception:raise CashflowError('Bot identity check failed. Check the saved token/username; no raw Telegram response is shown.') from None
    def start_bot(self):
        if self.server.telegram:raise CashflowError('A local bot is already active.')
        p=self.data['telegram']
        if not p:raise CashflowError('Save the local bot configuration first.')
        config=TelegramConfig(int(p['owner']),p['name'],p['use_model'])
        bot=TelegramBridge(self.server,config,self.api_factory(p['token']),adapter=self.active_adapter() if p['use_model'] else None)
        self.server.telegram=bot;bot.start()
    def stop_bot(self):
        bot=self.server.telegram
        if not bot:return
        if not bot.lock.acquire(timeout=BOT_IDLE_WAIT_SECONDS):raise CashflowError('Bot request in progress. Wait for its result before stopping.')
        try:bot.stop.set()
        finally:bot.lock.release()
        bot.close();self.server.telegram=None
    def forget_bot(self):
        if self.server.telegram:raise CashflowError('Stop the local bot first.')
        candidate=copy.deepcopy(self.data);candidate['telegram']=None;self._commit(candidate)
    @idle_bot
    def forget_saved(self):
        self._bot_idle();self.vault.forget();self.load_error=False
        candidate=copy.deepcopy(self.data)
        for p in candidate['profiles'].values():p['remember']=False
        if candidate['telegram']:candidate['telegram']['remember']=False
        self.data=candidate
