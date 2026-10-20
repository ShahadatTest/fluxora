"""SQLite persistence for the ledger.

Restarting from the same database file preserves recorded receipts,
applied events and actions, and the receipt idempotency keys prevent
duplicate execution after a restart.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from .ledger import Ledger
from .models import (
    Account,
    Action,
    Allocation,
    BusinessEvent,
    Document,
    Evidence,
    FxQuote,
    Outflow,
    OwnerConstraints,
    Receivable,
    Receipt,
)
from .money import parse_rate


def _d(value: str | None) -> date | None:
    if value is None:
        return None
    y, m, d = (int(p) for p in value.split("-"))
    return date(y, m, d)


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state_versions (id INTEGER PRIMARY KEY CHECK (id=1), version INTEGER NOT NULL);
INSERT OR IGNORE INTO state_versions VALUES (1,0);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, currency TEXT NOT NULL,
    opening_minor INTEGER NOT NULL, floor_minor INTEGER NOT NULL,
    evidence_id TEXT);
CREATE TABLE IF NOT EXISTS receivables (
    id TEXT PRIMARY KEY, currency TEXT NOT NULL, original_minor INTEGER NOT NULL,
    expected_date TEXT, evidence_id TEXT NOT NULL, allocations_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outflows (
    id TEXT PRIMARY KEY, currency TEXT NOT NULL, amount_minor INTEGER NOT NULL,
    date TEXT NOT NULL, protected INTEGER NOT NULL, evidence_id TEXT NOT NULL,
    consent_required INTEGER NOT NULL, max_deferrable INTEGER NOT NULL,
    status TEXT NOT NULL, paid_date TEXT);
CREATE TABLE IF NOT EXISTS receipts (
    business_id TEXT NOT NULL, id TEXT NOT NULL,
    receivable_id TEXT NOT NULL, amount_minor INTEGER NOT NULL,
    date_received TEXT NOT NULL, state TEXT NOT NULL,
    evidence_id TEXT NOT NULL, actor TEXT NOT NULL,
    PRIMARY KEY (business_id, id));
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, receivable_id TEXT NOT NULL,
    new_expected_date TEXT NOT NULL, evidence_id TEXT NOT NULL,
    observed_date TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applied_events (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, summary TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fx_quotes (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT, from_ccy TEXT NOT NULL, to_ccy TEXT NOT NULL,
    rate TEXT NOT NULL, as_of TEXT NOT NULL, source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, terms_json TEXT NOT NULL,
    actor TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL,
    approved_version INTEGER, counterparty_json TEXT, audit_json TEXT NOT NULL,
    business_id TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, event TEXT NOT NULL, actor TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outflow_terms (
    outflow_id TEXT PRIMARY KEY,
    original_date TEXT, original_amount INTEGER,
    recorded_splits_json TEXT, amendments_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recovery_events (
    id TEXT PRIMARY KEY, business_id TEXT NOT NULL, result_json TEXT NOT NULL);
"""


class SQLiteStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.executescript(SCHEMA)
        # Additive migrations for databases created before business-scoped
        # idempotency existed. CREATE TABLE IF NOT EXISTS leaves old tables
        # untouched, so add the new columns explicitly; the save path
        # rewrites every table on each save, using explicit column lists so
        # old (column-appended) and new (column-first) layouts both work.
        self._ensure_column("receipts", "business_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("actions", "business_id", "TEXT NOT NULL DEFAULT ''")

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        cols = {row[1] for row in
                self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            self.conn.commit()

    def close(self):
        self.conn.close()

    def state_version(self):
        """Generation outside replaceable ledger metadata; catches reset/restore ABA."""
        return self.conn.execute('SELECT version FROM state_versions WHERE id=1').fetchone()[0]

    # -- save ---------------------------------------------------------
    def save_ledger(self, ledger: Ledger, *, commit: bool = True) -> None:
        """Persist the whole ledger in a single commit.

        Callers must hold the process-wide lock (web.mutate_state) across
        the load -> mutate -> save cycle so concurrent retries cannot
        interleave validation and writes. On exception the connection is
        left uncommitted and nothing is saved.
        """
        c = self.conn
        c.execute('UPDATE state_versions SET version=version+1 WHERE id=1')
        c.execute("DELETE FROM meta")
        c.execute("DELETE FROM accounts")
        c.execute("DELETE FROM receivables")
        c.execute("DELETE FROM outflows")
        c.execute("DELETE FROM receipts")
        c.execute("DELETE FROM events")
        c.execute("DELETE FROM applied_events")
        c.execute("DELETE FROM evidence")
        c.execute("DELETE FROM documents")
        c.execute("DELETE FROM fx_quotes")
        c.execute("DELETE FROM actions")
        meta = {
            "business_id": ledger.business_id,
            "business_name": ledger.business_name,
            "base_currency": ledger.base_currency,
            "timezone": ledger.timezone,
            "as_of": ledger.as_of.isoformat(),
            "horizon_days": str(ledger.horizon_days),
            "max_advance_minor": "" if ledger.constraints.max_advance_minor is None
            else str(ledger.constraints.max_advance_minor),
            "allow_supplier_date_change": "1" if ledger.constraints.allow_supplier_date_change else "0",
            "allow_ops_deferral": "1" if ledger.constraints.allow_ops_deferral else "0",
            "advance_caps_minor": json.dumps(ledger.constraints.advance_caps_minor, sort_keys=True),
            "runtime_evidence_ids": json.dumps(sorted(ledger.runtime_evidence_ids)),
            "consent_map": json.dumps(ledger.consent_map),
            "demo_checks": json.dumps(ledger.demo_checks),
            "collection_date_sources": json.dumps(ledger.collection_date_sources, sort_keys=True),
            "receipt_reviews": json.dumps(ledger.receipt_reviews, sort_keys=True),
            "owner_operations": json.dumps(ledger.owner_operations, sort_keys=True),
            "receipt_inbox": json.dumps(ledger.receipt_inbox, sort_keys=True),
            "receipt_adjustments": json.dumps(ledger.receipt_adjustments),
            "receipt_review_archive": json.dumps(ledger.receipt_review_archive),
            "forecast_snapshots": json.dumps(ledger.forecast_snapshots),
            "invoice_due_sources": json.dumps(ledger.invoice_due_sources),
            "recurring_bills": json.dumps(ledger.recurring_bills),
            "recurring_occurrences": json.dumps(ledger.recurring_occurrences),
            "duplicate_reviews": json.dumps(ledger.duplicate_reviews),
            "split_receipts": json.dumps(ledger.split_receipts),
            "fact_checks": json.dumps(ledger.fact_checks),
            "item_details": json.dumps({
                'invoice': {r.id: dict(due_date=r.due_date.isoformat() if r.due_date else None,
                    counterparty=r.counterparty, invoice_reference=r.invoice_reference,identity_evidence_id=r.identity_evidence_id) for r in ledger.receivables.values()},
                'bill': {o.id: dict(counterparty=o.counterparty, invoice_reference=o.invoice_reference,identity_evidence_id=o.identity_evidence_id) for o in ledger.outflows.values()}}),
            "bill_payment_records": json.dumps({o.id: o.observed_payments for o in ledger.outflows.values() if o.observed_payments}),
            "bill_payment_corrections": json.dumps({o.id:dict(adjustments=o.payment_adjustments,legacy_payment=o.legacy_payment) for o in ledger.outflows.values() if o.payment_adjustments or o.legacy_payment}),
            "collection_holds": json.dumps({r.id:r.collection_hold for r in ledger.receivables.values() if r.collection_hold}),
        }
        for name in ['entry_records','split_adjustments','cash_movements','statement_batches','statement_reviews',
                     'payment_acknowledgements','payment_pair_reviews','date_corrections','instalment_proposals','scenarios','setup_receipts','payment_promises','unallocated_refunds','evidence_attachments','attachment_import_receipts','statement_balance_checks','credit_transfers','loans','agent_workflows']:
            meta[name]=json.dumps(getattr(ledger,name))
        from .treasury import STATE_FIELDS
        for name in STATE_FIELDS:
            meta[name] = json.dumps(getattr(ledger, name))
        meta['refunds']=json.dumps({'invoice':{r.id:r.refunds for r in ledger.receivables.values() if r.refunds},
            'bill':{o.id:o.refunds for o in ledger.outflows.values() if o.refunds}})
        meta['agreement_schedules']=json.dumps({o.id:o.agreement_schedules for o in ledger.outflows.values() if o.agreement_schedules})
        meta['credit_entries']=json.dumps({'invoice':{r.id:r.credit_entries for r in ledger.receivables.values() if r.credit_entries},
            'bill':{o.id:o.credit_entries for o in ledger.outflows.values() if o.credit_entries}})
        meta['face_adjustments']=json.dumps({'invoice':{r.id:r.face_adjustments for r in ledger.receivables.values() if r.face_adjustments},
            'bill':{o.id:o.face_adjustments for o in ledger.outflows.values() if o.face_adjustments}})
        c.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
        for a in ledger.accounts.values():
            c.execute("INSERT INTO accounts VALUES (?,?,?,?,?)",
                      (a.id, a.currency, a.opening_minor, a.floor_minor, a.evidence_id))
        for r in ledger.receivables.values():
            allocs = [dict(receipt_id=x.receipt_id, amount_minor=x.amount_minor,
                           date_received=x.date_received.isoformat(),
                           observed=x.observed) for x in r.allocations]
            c.execute("INSERT INTO receivables VALUES (?,?,?,?,?,?)",
                      (r.id, r.currency, r.original_minor,
                       r.expected_date.isoformat() if r.expected_date else None,
                       r.evidence_id, json.dumps(allocs)))
        for o in ledger.outflows.values():
            c.execute("INSERT INTO outflows VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (o.id, o.currency, o.amount_minor, o.date.isoformat(),
                       int(o.protected), o.evidence_id,
                       int(o.counterparty_consent_required_for_change),
                       o.maximum_deferrable_minor, o.status,
                       o.paid_date.isoformat() if o.paid_date else None))
        for r in ledger.receipts.values():
            # Explicit column list: idempotency is keyed by (business_id, id).
            c.execute(
                "INSERT INTO receipts (business_id, id, receivable_id, "
                "amount_minor, date_received, state, evidence_id, actor) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ledger.business_id, r.id, r.receivable_id, r.amount_minor,
                 r.date_received.isoformat(), r.state, r.evidence_id, r.actor))
        for e in ledger.events.values():
            c.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                      (e.id, e.type, e.receivable_id, e.new_expected_date.isoformat(),
                       e.evidence_id, e.observed_date.isoformat()))
        c.executemany("INSERT INTO applied_events VALUES (?)",
                      [(i,) for i in ledger.applied_event_ids])
        for e in ledger.evidence.values():
            c.execute("INSERT INTO evidence VALUES (?,?,?)", (e.id, e.kind, e.summary))
        for d in ledger.documents.values():
            c.execute("INSERT INTO documents VALUES (?,?,?,?)",
                      (d.id, d.kind, d.content, d.source))
        for q in ledger.fx_quotes:
            c.execute("INSERT INTO fx_quotes (from_ccy,to_ccy,rate,as_of,source) VALUES (?,?,?,?,?)",
                      (q.from_ccy, q.to_ccy, str(q.rate), q.as_of.isoformat(), q.source))
        for a in ledger.actions.values():
            c.execute(
                "INSERT INTO actions (id, kind, terms_json, actor, version, "
                "state, approved_version, counterparty_json, audit_json, "
                "business_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (a.id, a.kind, json.dumps(a.terms), a.actor, a.version,
                 a.state, a.approved_version,
                 json.dumps(a.counterparty_response),
                 json.dumps(a.audit), a.business_id or ledger.business_id))
        c.execute("DELETE FROM audit_log")
        c.executemany(
            "INSERT INTO audit_log (at, event, actor, detail) VALUES (?,?,?,?)",
            [(e["at"], e["event"], e["actor"], e["detail"]) for e in ledger.audit],
        )
        c.execute("DELETE FROM outflow_terms")
        for o in ledger.outflows.values():
            if o.original_date is not None or o.recorded_splits is not None or o.amendments:
                splits = ([[d.isoformat(), m] for d, m in o.recorded_splits]
                          if o.recorded_splits else None)
                c.execute(
                    "INSERT INTO outflow_terms VALUES (?,?,?,?,?)",
                    (o.id,
                     o.original_date.isoformat() if o.original_date else None,
                     o.original_amount_minor,
                     json.dumps(splits) if splits else None,
                     json.dumps(o.amendments)))
        if commit:
            self.conn.commit()

    # -- load ---------------------------------------------------------
    def load_ledger(self) -> Ledger:
        c = self.conn
        meta = dict(c.execute("SELECT key, value FROM meta").fetchall())
        if not meta:
            raise KeyError("database has no seeded ledger; run the seed command first")
        led = Ledger()
        led.business_id = meta["business_id"]
        led.business_name = meta["business_name"]
        led.base_currency = meta["base_currency"]
        led.timezone = meta["timezone"]
        led.as_of = _d(meta["as_of"])
        led.horizon_days = int(meta["horizon_days"])
        led.constraints = OwnerConstraints(
            max_advance_minor=int(meta["max_advance_minor"]) if meta["max_advance_minor"] else None,
            allow_supplier_date_change=meta["allow_supplier_date_change"] == "1",
            allow_ops_deferral=meta["allow_ops_deferral"] == "1",
            advance_caps_minor=json.loads(meta.get('advance_caps_minor', '{}')),
        )
        # Additive persistence: old databases simply have no entries.
        led.runtime_evidence_ids = set(json.loads(meta.get("runtime_evidence_ids", "[]")))
        led.consent_map = dict(json.loads(meta.get("consent_map", "{}")))
        for row in c.execute("SELECT * FROM accounts"):
            led.accounts[row[0]] = Account(row[0], row[1], row[2], row[3], row[4])
        for row in c.execute("SELECT * FROM receivables"):
            allocs = [Allocation(x["receipt_id"], x["amount_minor"],
                                 _d(x["date_received"]), x["observed"])
                      for x in json.loads(row[5])]
            led.receivables[row[0]] = Receivable(row[0], row[1], row[2], _d(row[3]),
                                                row[4], allocs)
        for row in c.execute("SELECT * FROM outflows"):
            led.outflows[row[0]] = Outflow(
                row[0], row[1], row[2], _d(row[3]), bool(row[4]), row[5],
                bool(row[6]), row[7], row[8], _d(row[9]))
        for row in c.execute(
                "SELECT business_id, id, receivable_id, amount_minor, "
                "date_received, state, evidence_id, actor FROM receipts"):
            row_business = row[0] or ""
            # Defensive scoping: only load receipt idempotency records that
            # belong to this ledger's business ("" = pre-scoping rows, which
            # are stamped on load). Anything else indicates cross-business
            # contamination and is not trusted.
            if row_business and row_business != led.business_id:
                continue
            led.receipts[row[1]] = Receipt(
                id=row[1], receivable_id=row[2], amount_minor=row[3],
                date_received=_d(row[4]), state=row[5], evidence_id=row[6],
                actor=row[7], business_id=led.business_id)
        for row in c.execute("SELECT * FROM events"):
            led.events[row[0]] = BusinessEvent(row[0], row[1], row[2], _d(row[3]),
                                              row[4], _d(row[5]))
        for (eid,) in c.execute("SELECT id FROM applied_events"):
            led.applied_event_ids.add(eid)
        for row in c.execute("SELECT * FROM evidence"):
            led.evidence[row[0]] = Evidence(row[0], row[1], row[2])
        for row in c.execute("SELECT * FROM documents"):
            led.documents[row[0]] = Document(row[0], row[1], row[2], row[3])
        for row in c.execute("SELECT from_ccy,to_ccy,rate,as_of,source FROM fx_quotes"):
            led.fx_quotes.append(FxQuote(row[0], row[1], parse_rate(row[2]),
                                        _d(row[3]), row[4]))
        for row in c.execute(
                "SELECT id, kind, terms_json, actor, version, state, "
                "approved_version, counterparty_json, audit_json, business_id "
                "FROM actions"):
            led.actions[row[0]] = Action(
                id=row[0], kind=row[1], terms=json.loads(row[2]), actor=row[3],
                version=row[4], state=row[5], approved_version=row[6],
                counterparty_response=json.loads(row[7]) if row[7] else None,
                audit=json.loads(row[8]),
                # Pre-scoping rows ("") belong to this ledger's business;
                # stamp them so transitions authorize against a real scope.
                business_id=row[9] or led.business_id)
        led.audit = [
            {"at": r[0], "event": r[1], "actor": r[2], "detail": r[3]}
            for r in c.execute("SELECT at, event, actor, detail FROM audit_log ORDER BY seq")
        ]
        for row in c.execute(
                "SELECT outflow_id, original_date, original_amount, "
                "recorded_splits_json, amendments_json FROM outflow_terms"):
            out = led.outflows.get(row[0])
            if out is None:
                continue
            out.original_date = _d(row[1])
            out.original_amount_minor = row[2]
            out.recorded_splits = (
                [(_d(d), m) for d, m in json.loads(row[3])] if row[3] else None)
            out.amendments = json.loads(row[4])
        # demo workflow checklist (additive; old databases simply lack it)
        led.demo_checks = json.loads(meta.get("demo_checks", "{}"))
        led.collection_date_sources = json.loads(meta.get('collection_date_sources', '{}'))
        led.receipt_reviews = json.loads(meta.get('receipt_reviews', '{}'))
        led.owner_operations = json.loads(meta.get('owner_operations', '{}'))
        led.receipt_inbox = json.loads(meta.get('receipt_inbox', '{}'))
        led.receipt_adjustments = json.loads(meta.get('receipt_adjustments', '{}'))
        led.receipt_review_archive = json.loads(meta.get('receipt_review_archive', '{}'))
        led.forecast_snapshots = json.loads(meta.get('forecast_snapshots', '{}'))
        led.invoice_due_sources = json.loads(meta.get('invoice_due_sources', '{}'))
        led.recurring_bills = json.loads(meta.get('recurring_bills', '{}'))
        led.recurring_occurrences = json.loads(meta.get('recurring_occurrences', '{}'))
        led.duplicate_reviews = json.loads(meta.get('duplicate_reviews', '{}'))
        led.split_receipts = json.loads(meta.get('split_receipts', '{}'))
        led.fact_checks = json.loads(meta.get('fact_checks', '{}'))
        for name in ['entry_records','split_adjustments','cash_movements','statement_batches','statement_reviews',
                     'payment_acknowledgements','payment_pair_reviews','date_corrections','instalment_proposals','scenarios','setup_receipts','payment_promises','unallocated_refunds','evidence_attachments','attachment_import_receipts','statement_balance_checks','credit_transfers','loans','agent_workflows']:
            setattr(led,name,json.loads(meta.get(name,'{}')))
        from .treasury import STATE_FIELDS
        for name in STATE_FIELDS:
            setattr(led, name, json.loads(meta.get(name, '{}')))
        for kind,collection in [('invoice',led.receivables),('bill',led.outflows)]:
            for ident,entries in json.loads(meta.get('credit_entries','{}')).get(kind,{}).items():
                if ident in collection:collection[ident].credit_entries=entries
        for kind,collection in [('invoice',led.receivables),('bill',led.outflows)]:
            for ident,changes in json.loads(meta.get('face_adjustments','{}')).get(kind,{}).items():
                if ident in collection:collection[ident].face_adjustments=changes
        for kind,collection in [('invoice',led.receivables),('bill',led.outflows)]:
            for ident,entries in json.loads(meta.get('refunds','{}')).get(kind,{}).items():
                if ident in collection:collection[ident].refunds=entries
        for ident,entries in json.loads(meta.get('agreement_schedules','{}')).items():
            if ident in led.outflows:led.outflows[ident].agreement_schedules=entries
        details = json.loads(meta.get('item_details', '{}'))
        for kind, collection in [('invoice', led.receivables), ('bill', led.outflows)]:
            for ident, values in details.get(kind, {}).items():
                if ident in collection:
                    item = collection[ident]
                    item.counterparty, item.invoice_reference = values.get('counterparty', ''), values.get('invoice_reference', '')
                    item.identity_evidence_id = values.get('identity_evidence_id','')
                    if kind == 'invoice': item.due_date = _d(values['due_date']) if values.get('due_date') else None
        for oid, payments in json.loads(meta.get('bill_payment_records', '{}')).items():
            if oid in led.outflows:
                led.outflows[oid].observed_payments = payments
        for oid,values in json.loads(meta.get('bill_payment_corrections','{}')).items():
            if oid in led.outflows:
                led.outflows[oid].payment_adjustments=values['adjustments']
                led.outflows[oid].legacy_payment=values.get('legacy_payment')
        for rid,hold in json.loads(meta.get('collection_holds','{}')).items():
            if rid in led.receivables:led.receivables[rid].collection_hold=hold
        return led
