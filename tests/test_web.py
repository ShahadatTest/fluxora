"""Phase 2 web-app tests: real HTTP against a live test server.

Covers the four views, the 10-step sandbox workflow, concurrent retries at
the transactional boundary, the import pipeline, and CSRF enforcement.
"""

import re
import threading
import urllib.parse
import urllib.request
import urllib.error

import pytest

from cashflow_copilot import web
from cashflow_copilot.cli import cmd_seed
from cashflow_copilot.store import SQLiteStore


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    db = str(tmp_path / "web.sqlite")
    monkeypatch.setattr(web, "DB_PATH", db)
    cmd_seed(db)
    server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    yield base, db
    server.shutdown()
    thread.join(timeout=5)


def _get(base, path):
    with urllib.request.urlopen(base + path) as r:
        return r.status, r.read().decode("utf-8")


def _post(base, path, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(base + path, data=data)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


@pytest.fixture
def csrf(live_server):
    base, _ = live_server
    _, html = _get(base, "/demo")
    m = re.search(r"name='csrf' value='([0-9a-f]+)'", html)
    assert m, "CSRF token not found on /demo"
    return m.group(1)


def _load(db):
    store = SQLiteStore(db)
    try:
        return store.load_ledger()
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def test_all_views_return_200_with_honest_labels(live_server):
    base, _ = live_server
    for path in ("/", "/ledger", "/plans", "/actions", "/demo"):
        status, html = _get(base, path)
        assert status == 200, path
        assert "AI not connected" in html, path
        assert "SYNTHETIC DEMO" in html, path
    _, export = _get(base, "/audit/export")
    assert "synthetic_demo" in export


def test_csrf_is_enforced(live_server):
    base, _ = live_server
    status, _ = _post(base, "/demo/check", {"csrf": "wrong", "check": "inspected"})
    assert status == 403


def test_invalid_transition_rejected_server_side(live_server, csrf):
    base, _ = live_server
    s, _ = _post(base, "/actions/create",
                 {"csrf": csrf, "action_id": "act-x", "kind": "request_early_payment"})
    assert s == 200
    s, body = _post(base, "/actions/act-x/reconcile", {"csrf": csrf})
    assert s == 400
    assert "cannot move action" in body


# ---------------------------------------------------------------------------
# Full 10-step sandbox workflow over HTTP
# ---------------------------------------------------------------------------

def test_full_demo_workflow(live_server, csrf):
    from datetime import date
    base, db = live_server
    F = lambda **kw: dict(csrf=csrf, **kw)

    s, _ = _post(base, "/demo/reset", F())
    assert s == 200
    s, _ = _post(base, "/demo/apply-delay", F())
    assert s == 200
    # Re-applying the delay is idempotent, not an error.
    s, _ = _post(base, "/demo/apply-delay", F())
    assert s == 200
    s, _ = _post(base, "/demo/check", F(check="inspected"))
    assert s == 200
    s, _ = _post(base, "/demo/check", F(check="compared"))
    assert s == 200

    s, _ = _post(base, "/actions/create", F(
        action_id="act-a", kind="request_early_payment",
        receivable_id="inv-client-001", amount_major="5000.00",
        date="2026-10-22"))
    assert s == 200
    s, _ = _post(base, "/actions/act-a/approve", F())
    assert s == 200
    s, _ = _post(base, "/actions/act-a/send", F())
    assert s == 200
    s, _ = _post(base, "/actions/act-a/respond", F(accepted="true"))
    assert s == 200
    s, _ = _post(base, "/actions/act-a/observe", F(
        receipt_id="rcpt-w1", amount_major="5000.00",
        date_received="2026-10-22"))
    assert s == 200
    # Duplicate retry of the same receipt: idempotent no-op.
    s, _ = _post(base, "/actions/act-a/observe", F(
        receipt_id="rcpt-w1", amount_major="5000.00",
        date_received="2026-10-22"))
    assert s == 200
    s, _ = _post(base, "/actions/act-a/reconcile", F())
    assert s == 200

    # Supplier extension: acceptance is an explicit runtime event that amends
    # the recorded payable while retaining the original terms.
    s, _ = _post(base, "/actions/create", F(
        action_id="act-s", kind="request_supplier_extension",
        outflow_id="supplier-001", date="2026-11-06"))
    assert s == 200
    for step in ("approve", "send"):
        s, _ = _post(base, f"/actions/act-s/{step}", F())
        assert s == 200, step
    s, _ = _post(base, "/actions/act-s/respond", F(accepted="true"))
    assert s == 200

    s, _ = _post(base, "/demo/verify-persistence", F())
    assert s == 200

    led = _load(db)
    assert led.receivables["inv-client-001"].remaining_minor == 300_000
    assert len(led.receipts) == 1  # the duplicate retry created nothing
    assert led.actions["act-a"].state == "reconciled"
    out = led.outflows["supplier-001"]
    assert out.date == date(2026, 11, 6)
    assert out.original_date == date(2026, 10, 25)  # original retained
    assert len(out.amendments) == 1
    consent = led.consent_evidence_id("supplier-001")
    assert consent in led.runtime_evidence_ids
    s, html = _get(base, "/demo")
    assert html.count("class='done'") == 10


# ---------------------------------------------------------------------------
# Concurrency at the transactional boundary
# ---------------------------------------------------------------------------

def test_concurrent_identical_receipts_single_effect(live_server):
    from datetime import date
    from cashflow_copilot.models import Receipt
    base, db = live_server
    # Apply the delay first so the scenario is realistic (not required).
    results = []

    def worker():
        def do(ledger):
            return ledger.record_receipt(Receipt(
                id="rcpt-race", receivable_id="inv-client-001",
                amount_minor=100_000, date_received=date(2026, 10, 22),
                state="observed", evidence_id="ev-client-invoice",
                actor="owner"), actor="owner")
        try:
            results.append(web.mutate_state(do))
        except Exception as e:  # noqa: BLE001
            results.append(e)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    changed = [r for r in results if isinstance(r, dict) and r["changed"]]
    dups = [r for r in results if isinstance(r, dict) and r.get("duplicate")]
    assert len(changed) == 1, results
    assert len(dups) == 9, results
    led = _load(db)
    assert len(led.receipts) == 1
    assert led.receivables["inv-client-001"].remaining_minor == 700_000


def test_concurrent_conflicting_receipt_ids_one_winner(live_server):
    from datetime import date
    from cashflow_copilot.errors import IdempotencyConflict
    from cashflow_copilot.models import Receipt
    base, db = live_server
    outcomes = []

    def worker(amount):
        def do(ledger):
            return ledger.record_receipt(Receipt(
                id="rcpt-clash", receivable_id="inv-client-001",
                amount_minor=amount, date_received=date(2026, 10, 22),
                state="observed", evidence_id="ev-client-invoice",
                actor="owner"), actor="owner")
        try:
            outcomes.append(web.mutate_state(do))
        except IdempotencyConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=worker, args=(a,))
               for a in (100_000, 200_000, 300_000)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    wins = [o for o in outcomes if isinstance(o, dict) and o["changed"]]
    assert len(wins) == 1, outcomes
    assert outcomes.count("conflict") == 2, outcomes
    led = _load(db)
    assert len(led.receipts) == 1


# ---------------------------------------------------------------------------
# Import pipeline
# ---------------------------------------------------------------------------

def _multipart(fields: dict, files: dict):
    boundary = "BOUNDARY12345"
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{k}"\r\n\r\n{v}\r\n').encode()
    for k, (filename, content, ctype) in files.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{k}"; filename="{filename}"\r\n'
                 f"Content-Type: {ctype}\r\n\r\n").encode() + content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _mpost(base, path, fields, files):
    body, ctype = _multipart(fields, files)
    req = urllib.request.Request(base + path, data=body,
                                 headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_csv_import_flow_with_confirmation(live_server, csrf):
    base, db = live_server
    csv_text = ("record_type,reference,amount,currency,date,counterparty\n"
                "invoice,inv-new-1,2500.00,USD,2026-11-10,Client X\n"
                "bill,bill-new-1,300.00,USD,2026-11-12,Supplier Y\n"
                "invoice,inv-client-001,100.00,USD,2026-11-10,Client X\n")
    s, html = _mpost(base, "/ledger/import/csv", {"csrf": csrf}, {
        "file": ("tx.csv", csv_text.encode(), "text/csv")})
    assert s == 200, html
    token = re.search(r'name="token" value="([0-9a-f]+)"', html).group(1)
    mapping = {"csrf": csrf, "token": token, "col_0": "record_type",
               "col_1": "reference", "col_2": "amount_major",
               "col_3": "currency", "col_4": "date", "col_5": "counterparty"}
    s, html = _post(base, "/ledger/import/csv/preview", mapping)
    assert s == 200, html
    assert "already exists" in html  # duplicate warning for inv-client-001
    token2 = re.search(r'name="token" value="([0-9a-f]+)"', html).group(1)
    # Nothing has touched the ledger yet.
    assert "inv-new-1" not in _load(db).receivables
    s, _ = _post(base, "/ledger/import/confirm",
                 {"csrf": csrf, "token": token2})
    assert s == 200
    led = _load(db)
    assert led.receivables["inv-new-1"].original_minor == 250_000
    assert led.outflows["bill-new-1"].amount_minor == 30_000
    # The duplicate reference was skipped, not merged.
    assert led.receivables["inv-client-001"].original_minor == 800_000


def test_csv_import_rejects_wrong_extension(live_server, csrf):
    base, _ = live_server
    s, body = _mpost(base, "/ledger/import/csv", {"csrf": csrf}, {
        "file": ("tx.exe", b"junk", "application/octet-stream")})
    assert s == 400
    assert "invalid file type" in body


def test_fixture_import_rejects_invalid(live_server, csrf):
    base, db = live_server
    before = _load(db).business_name
    s, body = _mpost(base, "/ledger/import/fixture", {"csrf": csrf}, {
        "file": ("bad.json", b'{"nope": true}', "application/json")})
    assert s == 400
    assert "Fixture rejected" in body
    assert _load(db).business_name == before  # unchanged


def test_document_upload_without_text_is_unsupported(live_server, csrf):
    base, db = live_server
    fake_pdf = (b"%PDF-1.4\n1 0 obj<</Length 10>>stream\n"
                b"x\x9c\xcb\x00dummy-compressed-data\nendstream\nendobj\n")
    s, html = _mpost(base, "/ledger/import/document", {"csrf": csrf}, {
        "file": ("scan.pdf", fake_pdf, "application/pdf")})
    assert s == 200
    assert "unsupported" in html.lower()
    assert not any(e.id.startswith("ev-doc-") for e in _load(db).evidence.values())


def test_document_upload_stores_hash_not_content(live_server, csrf):
    base, db = live_server
    text = b"Invoice INV-9 from Client X\nAmount: USD 1,200.00\nDate: 2026-11-15\n"
    s, html = _mpost(base, "/ledger/import/document", {"csrf": csrf}, {
        "file": ("inv9.txt", text, "text/plain")})
    assert s == 200, html
    token = re.search(r'name="token" value="([0-9a-f]+)"', html).group(1)
    s, _ = _post(base, "/ledger/import/document/confirm", {
        "csrf": csrf, "token": token, "doc_kind": "invoice",
        "summary": "Client X invoice INV-9, USD 1,200"})
    assert s == 200
    led = _load(db)
    ev = next(e for e in led.evidence.values() if e.id.startswith("ev-doc-"))
    assert "content not retained" in ev.summary
    assert "Invoice INV-9 from Client X" not in ev.summary  # not the raw text
