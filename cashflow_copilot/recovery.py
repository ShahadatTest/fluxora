"""Local SQLite recovery copies with checksummed manifests and reviewed restore.

Only files created in this database's recovery directory are selectable.
Checksums detect accidental corruption, not a malicious local OS operator.
"""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from datetime import datetime, timezone

from .authz import require_access
from .errors import CashflowError
from .owner_workflows import review_revision
from .planning import _hash
from .store import SQLiteStore
from .agent import AGENT_RUNS_SCHEMA


def recovery_directory(db):
    path = Path(db).resolve()
    return path.parent / (path.name + '.backups')


def _readonly(path):
    obj = SQLiteStore.__new__(SQLiteStore)
    obj.path = Path(path)
    obj.conn = sqlite3.connect(obj.path.resolve().as_uri() + '?mode=ro', uri=True)
    return obj


def create_backup(db, label, *, ctx):
    source = SQLiteStore(db)
    try:
        ledger = source.load_ledger()
        require_access(ctx, ledger)
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 200:
            raise CashflowError('Backup label needs 1–200 characters.')
        folder = recovery_directory(db); folder.mkdir(parents=True, exist_ok=True)
        key = uuid4().hex
        target = folder / (key + '.sqlite')
        copied = sqlite3.connect(str(target))
        try: source.conn.backup(copied)
        finally: copied.close()
        metadata = dict(id=key, format_version=1, app_version='0.17.1', business_id=ledger.business_id,
            business_name=ledger.business_name, as_of=ledger.as_of.isoformat(), label=label.strip(),
            created_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
            sha256=hashlib.sha256(target.read_bytes()).hexdigest(), bytes=target.stat().st_size)
        (folder / (key + '.json')).write_text(json.dumps(metadata, indent=2), encoding='utf-8')
        return metadata
    finally: source.close()


def list_backups(db, *, ctx):
    current = SQLiteStore(db)
    try: require_access(ctx, current.load_ledger())
    finally: current.close()
    folder = recovery_directory(db)
    if not folder.exists(): return []
    result = []
    for path in folder.glob('*.json'):
        if re.fullmatch('[a-f0-9]{32}', path.stem):
            try:
                item = json.loads(path.read_text(encoding='utf-8'))
                if item.get('id') == path.stem: result.append(item)
            except (ValueError, OSError): continue
    return sorted(result, key=lambda m: m.get('created_at', ''), reverse=True)


def checked_backup(db, backup_id):
    if not isinstance(backup_id, str) or not re.fullmatch('[a-f0-9]{32}', backup_id):
        raise CashflowError('Choose a backup from the local inventory; filesystem paths are not accepted.')
    folder = recovery_directory(db).resolve()
    path, manifest = folder / (backup_id + '.sqlite'), folder / (backup_id + '.json')
    if path.resolve().parent != folder or manifest.resolve().parent != folder:
        raise CashflowError('Backup paths must remain inside the local recovery directory.')
    try:
        info = json.loads(manifest.read_text(encoding='utf-8'))
        if info['format_version'] != 1 or info['app_version'] not in {'0.6.0', '0.7.0','0.8.0','0.9.0','0.10.0','0.11.0','0.12.0','0.13.0','0.14.0','0.15.0','0.16.0','0.17.0','0.17.1'} or info['id'] != backup_id:
            raise CashflowError('Unsupported backup format/app version. No state was changed.')
        if path.stat().st_size > 64 * 1024 * 1024 or path.stat().st_size != info['bytes']:
            raise CashflowError('Backup size is invalid or exceeds the 64 MiB restore limit.')
        if hashlib.sha256(path.read_bytes()).hexdigest() != info['sha256']:
            raise CashflowError('Backup checksum failed; no state was changed.')
        store = _readonly(path)
        try:
            if store.conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise CashflowError('SQLite integrity check failed.')
            ledger = store.load_ledger()
            if ledger.business_id != info['business_id'] or ledger.as_of.isoformat() != info['as_of']:
                raise CashflowError('Backup manifest and stored business/date do not agree.')
        finally: store.close()
    except (OSError, ValueError, KeyError, sqlite3.Error):
        raise CashflowError('This local backup is incomplete or incompatible. No state was changed.')
    return path, info, ledger


def _summary(ledger):
    from .treasury import account_cash, restricted_cash
    return dict(business_id=ledger.business_id, business_name=ledger.business_name, as_of=ledger.as_of.isoformat(),
        receipts=len(ledger.receipts), actions=len(ledger.actions), corrections=len(ledger.receipt_adjustments),
        split_payments=len(ledger.split_receipts), source_checks=len(ledger.fact_checks),
        source_adjustments=sum(len(r.face_adjustments) for r in list(ledger.receivables.values())+list(ledger.outflows.values())),
        whole_payment_corrections=len(ledger.split_adjustments),other_cash_movements=len(ledger.cash_movements),
        payment_promises=len(ledger.payment_promises),unallocated_refunds=len(ledger.unallocated_refunds),
        statement_balance_checks=len(ledger.statement_balance_checks),credit_transfers=len(ledger.credit_transfers),loans=len(ledger.loans),
        party_identities=len(ledger.party_register),party_links=len(ledger.party_links),cash_accounts=len(ledger.accounts),
        forecast_account_routes=len(ledger.forecast_routes),
        internal_transfers=len(ledger.internal_transfers),cash_restrictions=len(ledger.cash_restrictions),
        evidence_attachments=len(ledger.evidence_attachments),active_attachment_bytes=sum(a['bytes'] for a in ledger.evidence_attachments.values() if a['active']),
        refunds=sum(len(i.refunds) for i in list(ledger.receivables.values())+list(ledger.outflows.values())),
        date_corrections=len(ledger.date_corrections),instalment_proposals=len(ledger.instalment_proposals),saved_scenarios=len(ledger.scenarios),
        statement_batches=len(ledger.statement_batches),statement_reviews=len(ledger.statement_reviews),statement_mapping_profiles=len(ledger.statement_profiles),
        bill_payments=sum(len(o.observed_payments) for o in ledger.outflows.values()),
        forecasts=len(ledger.forecast_snapshots),
        payment_corrections=sum(len(o.payment_adjustments) for o in ledger.outflows.values()),
        active_collection_holds=sum(bool(r.collection_hold and r.collection_hold['active']) for r in ledger.receivables.values()),
        recurring_templates=len(ledger.recurring_bills),recurring_occurrences=len(ledger.recurring_occurrences),
        duplicate_reviews=len(ledger.duplicate_reviews),due_dates=sum(r.due_date is not None for r in ledger.receivables.values()),
        cash={a.currency: ledger.actual_cash(a.currency) for a in ledger.accounts.values()},
        account_balances={a.id:dict(currency=a.currency,book_minor=account_cash(ledger,a.id),restricted_minor=restricted_cash(ledger,account_id=a.id)) for a in ledger.accounts.values()},
        states={aid:a.state for aid,a in ledger.actions.items()})


def restore_preview(db, backup_id, *, ctx):
    store = SQLiteStore(db)
    try:
        current = store.load_ledger(); require_access(ctx, current)
        path, info, restored = checked_backup(db, backup_id)
        # Local UI owner can explicitly replace the selected synthetic business.
        # Model/viewer contexts have no restore capability.
        revision = _hash([review_revision(current), backup_id, info['sha256']])
        return dict(backup=info, current=_summary(current), restored=_summary(restored), revision=revision,
            cross_business=current.business_id != restored.business_id,
            removed_receipt_ids=sorted(set(current.receipts)-set(restored.receipts)),
            removed_action_ids=sorted(set(current.actions)-set(restored.actions)),
            warning='Restoring rewinds financial state and idempotency history to this backup. Later transactions are discarded '
                'from active state; retries of discarded transaction IDs may execute again. A new pre-restore recovery copy retains the current state.')
    finally: store.close()


def restore_backup(db, backup_id, revision, target_business_confirmation, *, ctx):
    store = SQLiteStore(db)
    try:
        current = store.load_ledger(); require_access(ctx, current)
        operation_id = _hash(['restore', backup_id, revision])
        done = store.conn.execute('SELECT business_id,result_json FROM recovery_events WHERE id=?', (operation_id,)).fetchone()
        if done:
            if done[0] != current.business_id:
                raise CashflowError('This restore replay belongs to a different active business.')
            return dict(json.loads(done[1]), duplicate=True)
        preview = restore_preview(db, backup_id, ctx=ctx)
        if revision != preview['revision']:
            raise CashflowError('Active state changed after preview. Review the restore again.')
        if target_business_confirmation != preview['restored']['business_id']:
            raise CashflowError('Type the exact target business ID shown in the restore preview.')
        path, info, restored = checked_backup(db, backup_id)
        # Capture rollback BEFORE any mutation, retaining all later entries.
        rollback = create_backup(db, 'Automatic before restore ' + backup_id, ctx=ctx)
        backup = _readonly(path)
        try:
            tables = {r[0] for r in backup.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            agent_rows = list(backup.conn.execute('SELECT run_id,business_id,question,provider_label,tool_calls_json,answer_summary,created_at,finished_at FROM agent_runs')) if 'agent_runs' in tables else []
            request_rows=list(backup.conn.execute('SELECT business_id,request_id,run_id,question,provider,status,instance_id,created_at,finished_at FROM agent_requests')) if 'agent_requests' in tables else []
            run_metadata_rows=list(backup.conn.execute('SELECT run_id,business_id,disclosure_json,usage_json FROM agent_run_metadata')) if 'agent_run_metadata' in tables else []
            request_policy_rows=list(backup.conn.execute('SELECT business_id,request_id,data_mode FROM agent_request_policies')) if 'agent_request_policies' in tables else []
        finally: backup.close()
        restored.log_audit('backup_restored', ctx.actor, f'Local owner explicitly restored {backup_id}; previous business {current.business_id}; rollback {rollback["id"]}.')
        result = dict(backup_id=backup_id, business_id=restored.business_id, rollback_backup_id=rollback['id'])
        # Schema setup precedes transaction; recovery receipts live outside rewound ledger tables.
        from .agent_requests import REQUEST_SCHEMA
        store.conn.executescript(AGENT_RUNS_SCHEMA+REQUEST_SCHEMA)
        try:
            store.conn.execute('BEGIN IMMEDIATE')
            store.save_ledger(restored, commit=False)
            store.conn.execute('DELETE FROM agent_runs')
            store.conn.executemany('INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?,?)', agent_rows)
            store.conn.execute('DELETE FROM agent_requests')
            store.conn.executemany('INSERT INTO agent_requests VALUES (?,?,?,?,?,?,?,?,?)',request_rows)
            store.conn.execute('DELETE FROM agent_run_metadata')
            store.conn.executemany('INSERT INTO agent_run_metadata VALUES (?,?,?,?)',run_metadata_rows)
            store.conn.execute('DELETE FROM agent_request_policies')
            store.conn.executemany('INSERT INTO agent_request_policies VALUES (?,?,?)',request_policy_rows)
            store.conn.execute('INSERT INTO recovery_events VALUES (?,?,?)', (operation_id, restored.business_id, json.dumps(result)))
            store.conn.commit()
        except Exception:
            store.conn.rollback()
            raise
        return dict(result, duplicate=False)
    finally: store.close()
