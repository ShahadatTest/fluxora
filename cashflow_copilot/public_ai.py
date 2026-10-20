"""Opt-in public task translation with a durable UTC-day attempt budget.

Only code-owned guided schemas may reach this adapter from public routes.
Counters store no questions, visitor identities, model responses or credentials.
"""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import time

from .model_adapter import AdapterError, OpenAICompatibleAdapter


class DailyBudget:
    def __init__(self, path, limit=1000, clock=time.time):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Public AI daily limit must be between 1 and 1000.')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limit, self.clock = limit, clock
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS attempts (day TEXT PRIMARY KEY, count INTEGER NOT NULL)')

    def reserve(self):
        day = datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT OR IGNORE INTO attempts VALUES (?,0)', (day,))
                count = db.execute('SELECT count FROM attempts WHERE day=?', (day,)).fetchone()[0]
                if count >= self.limit:
                    raise AdapterError('Public AI daily call allowance reached. Direct reports remain available.')
                db.execute('UPDATE attempts SET count=count+1 WHERE day=?', (day,))
        except sqlite3.Error:
            raise AdapterError('Public AI usage counter unavailable. Direct reports remain available.') from None


class PublicTaskAdapter:
    def __init__(self, adapter, budget, qualification_path):
        self.adapter = adapter
        self.budget = budget
        self.qualification_path = Path(qualification_path)
        # Only a fingerprint leaves memory. A key/configuration change
        # invalidates qualification without persisting credentials.
        identity = '\0'.join((adapter._api_key or '', adapter._base_url, adapter._model or '',
                               hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
        self.local_configuration_id = hashlib.sha256(identity.encode()).hexdigest()
        original = adapter._post_locked

        def bounded(payload, *, deadline=None):
            if deadline is not None and time.monotonic() >= deadline:
                raise AdapterError('model provider deadline budget exhausted')
            budget.reserve()
            return original(payload, deadline=deadline)

        adapter._post_locked = bounded

    @property
    def provider_label(self):
        return self.adapter.provider_label

    @property
    def is_configured(self):
        return self.adapter.is_configured

    def status(self):
        return self.adapter.status()

    def request_policy(self):
        return dict(self.adapter.request_policy(), public_daily_attempt_limit=self.budget.limit,
                    public_retries=0, public_timeout_seconds=25, public_total_budget_seconds=30)

    def chat(self, messages, tools, *, deadline=None):
        return self.adapter.chat(messages, tools, deadline=deadline)

    def verify(self):
        return self.adapter.verify()


def create(database, daily_limit=1000):
    root = Path(database).resolve().parent / 'public-ai'
    adapter = OpenAICompatibleAdapter.from_env(max_retries=0, timeout_s=25, total_budget_s=30)
    if not adapter.is_configured or not adapter.status().model:
        raise ValueError('Public AI needs a complete private model configuration.')
    return PublicTaskAdapter(adapter, DailyBudget(root / 'usage.sqlite', daily_limit), root / 'qualification.json')
