"""Persisted local synthetic sandbox web UI.

- Python stdlib only (http.server + sqlite3): no web framework dependency.
- All financial calculations come from the deterministic engine
  (forecast.py / plans.py / ledger.py). Nothing is computed in the browser;
  the browser receives server-rendered HTML and server-generated SVG charts.
- Single-user local sandbox. The server binds to 127.0.0.1 only.
- State lives in one SQLite file. Every mutating request runs inside a
  process-wide lock as load -> mutate (engine-enforced boundaries) -> save
  in a single commit, so concurrent retries cannot create duplicate effects.
- Trusted context validation happens at the engine mutation boundary
  (authz.require_access, before any idempotency short-circuit). The UI has
  no login; each mutating handler builds a server-side owner RequestContext
  from the loaded ledger — actor/role/business are never taken from client
  input.
- Simulated counterparties, sandbox outbox and the "AI not connected"
  status are labelled honestly on every page.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
import sys
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlencode, quote

from . import actions as actions_mod
from . import agent_web
from . import authz as authz_mod
from .authz import RequestContext
from .errors import CashflowError
from .fixture import validate_fixture
from .forecast import forecast
from .fx import display_convert
from .ledger import Ledger
from .models import Document, Evidence, Receipt
from .money import format_amount, minor_digits
from .planning import stress_test, plan_draft_preview, create_plan_drafts
from .adaptive import advance_cap, generate_proposals, overlapping_actions, validate_current_request
from .attention import attention_items, stale_plan
from .plans import (
    build_advance_plan,
    build_plan_a,
    build_plan_b_full,
    build_plan_b_supplier_only,
    evaluate_plan,
)
from .store import SQLiteStore

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_REQUEST_BYTES = 2 * 1024 * 1024  # reject unreasonable uploads
MAX_UPLOAD_BYTES = 1 * 1024 * 1024
ALLOWED_IMPORT_EXTENSIONS = {".csv", ".json", ".txt"}
ALLOWED_DOC_EXTENSIONS = {".txt", ".pdf", ".csv"}

APP_TITLE = "Fluxora"
SYNTHETIC_BANNER = (
    "SYNTHETIC DEMO — all businesses, people, amounts and messages are "
    "invented. No real money or business-counterparty messages. Optional "
    "owner Telegram notifications are real messages about synthetic records."
)

CSRF_TOKEN = secrets.token_hex(16)
LOCK = threading.RLock()
DB_PATH = "data/state.sqlite"

# Staged imports awaiting user confirmation: token -> dict.
_STAGED: dict[str, dict] = {}
_WORKSPACE = threading.local()


def database_path():
    state=getattr(_WORKSPACE, 'state', None)
    return state.database if state is not None else DB_PATH


def staged():
    state=getattr(_WORKSPACE, 'state', None)
    return state.staged if state is not None else _STAGED


def csrf_token():
    state=getattr(_WORKSPACE, 'state', None)
    return state.csrf if state is not None else CSRF_TOKEN


@contextmanager
def workspace_scope(state):
    """Bind all existing handlers to one server-selected visitor workspace."""
    previous=getattr(_WORKSPACE, 'state', None)
    _WORKSPACE.state=state
    try:yield
    finally:
        if previous is None:del _WORKSPACE.state
        else:_WORKSPACE.state=previous


# ---------------------------------------------------------------------------
# Store helpers (transactional boundary)
# ---------------------------------------------------------------------------

def _open_store():
    return SQLiteStore(database_path())


def read_state(fn):
    """Run fn(ledger) under the lock; read-only."""
    with LOCK:
        store = _open_store()
        try:
            ledger = store.load_ledger()
        finally:
            store.close()
        return fn(ledger)


def mutate_state(fn):
    """Run fn(ledger) under the lock and persist in one commit.

    fn returns (result, response_hint). On exception nothing is saved.
    """
    with LOCK:
        store = _open_store()
        try:
            ledger = store.load_ledger()
            result = fn(ledger)
            store.save_ledger(ledger)
        finally:
            store.close()
        return result


def fresh_seed_state(ledger: Ledger) -> bool:
    return (not ledger.applied_event_ids and not ledger.receipts
            and not ledger.actions and not ledger.runtime_evidence_ids
            and not ledger.audit)


def _owner_ctx(ledger: Ledger) -> RequestContext:
    """Build the server-side owner context for a UI-driven mutation.

    The demo UI has no login; the local operator is the owner of the
    ledger's business. The context is constructed here from the loaded
    ledger — NEVER from client-supplied form values or headers — so a
    client cannot forge a different actor, role, or business.
    """
    return RequestContext(actor="owner", role="owner",
                          business_id=ledger.business_id)


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

class Request:
    def __init__(self, method, path, query, headers, body):
        self.method = method
        self.path = path
        self.query = query          # dict[str, list[str]]
        self.headers = headers
        self.body = body            # bytes
        self.form: dict[str, list[str]] = {}
        self.files: dict[str, dict] = {}  # name -> {filename, content_type, data}
        ctype = headers.get("Content-Type", "")
        if method == "POST" and body:
            if "application/x-www-form-urlencoded" in ctype:
                self.form = parse_qs(body.decode("utf-8", "replace"))
            elif "multipart/form-data" in ctype:
                self.form, self.files = parse_multipart(body, ctype)

    def field(self, name: str, default: str = "") -> str:
        vals = self.form.get(name)
        return vals[0] if vals else default


def parse_multipart(body: bytes, content_type: str):
    """Minimal multipart/form-data parser (file uploads + text fields)."""
    form: dict[str, list[str]] = {}
    files: dict[str, dict] = {}
    m = re.search(r"boundary=([^;]+)", content_type)
    if not m:
        return form, files
    boundary = ("--" + m.group(1).strip().strip('"')).encode("latin-1")
    for part in body.split(boundary):
        if not part or part in (b"--", b"--\r\n"):
            continue
        if b"\r\n\r\n" not in part:
            continue
        head, data = part.split(b"\r\n\r\n", 1)
        if data.endswith(b"\r\n"):
            data = data[:-2]
        head_s = head.decode("latin-1", "replace")
        name_m = re.search(r'name="([^"]+)"', head_s)
        if not name_m:
            continue
        name = name_m.group(1)
        file_m = re.search(r'filename="([^"]*)"', head_s)
        if file_m and file_m.group(1):
            ct_m = re.search(r"Content-Type:\s*([^\r\n]+)", head_s)
            files[name] = {
                "filename": file_m.group(1),
                "content_type": (ct_m.group(1).strip() if ct_m else
                                 "application/octet-stream"),
                "data": data,
            }
        else:
            form.setdefault(name, []).append(data.decode("utf-8", "replace"))
    return form, files


ROUTES: dict[tuple[str, str], object] = {}


def route(method: str, path: str):
    def deco(fn):
        ROUTES[(method, path)] = fn
        return fn
    return deco


def match_route(method: str, path: str):
    if (method, path) in ROUTES:
        return ROUTES[(method, path)], {}
    for (m, pattern), fn in ROUTES.items():
        if m != method:
            continue
        # Single path parameter: /actions/<id>/approve
        regex = "^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", pattern) + "$"
        mm = re.match(regex, path)
        if mm:
            return fn, mm.groupdict()
    return None, {}


class Handler(BaseHTTPRequestHandler):
    server_version = "CashflowCopilot/0.25.10"

    def log_message(self, fmt, *args):  # keep the console quiet-ish
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_REQUEST_BYTES:
            raise PayloadTooLarge("request exceeds 2 MiB")
        return self.rfile.read(length) if length else b""

    def _handle(self):
        parsed = urlparse(self.path)
        from .ui import REQUEST
        REQUEST.path = parsed.path
        REQUEST.hosted = hasattr(self.server,'config')
        REQUEST.public_demo = bool(getattr(getattr(self.server,'config',None),'public_demo',False))
        try:
            body = self._read_body()
        except PayloadTooLarge as exc:
            return self._send_html(413, page("Request too large",
                                            f"<p class='err'>{esc(str(exc))}</p>"))
        req = Request(self.command, parsed.path, parse_qs(parsed.query),
                      self.headers, body)
        fn, params = match_route(self.command, parsed.path)
        if fn is None:
            return self._send_html(404, page("Not found",
                                            "<p class='err'>Unknown path.</p>"))
        if self.command == "POST" and req.field("csrf") != csrf_token():
            return self._send_html(403, page("Forbidden",
                                            "<p class='err'>Invalid CSRF token.</p>"))
        try:
            return fn(self, req, **params)
        except CashflowError as exc:
            return self._send_html(400, page("Action refused",
                                            f"<p class='err'>{esc(str(exc))}</p>"
                                            "<p><a class='btn' href='/app'>Return to workspace</a> "
                                            "<a href='/docs/troubleshooting'>Troubleshooting guide</a></p>"))
        except KeyError as exc:
            return self._send_html(404, page("Not found",
                                            f"<p class='err'>Unknown record: "
                                            f"{esc(str(exc))}</p>"))
        except PayloadTooLarge as exc:
            return self._send_html(413, page("Request too large",
                                            f"<p class='err'>{esc(str(exc))}</p>"))
        except Exception:  # never leak a traceback to the browser
            return self._send_html(500, page("Server error",
                                            "<p class='err'>The request did not finish normally. "
                                            "Completion is not confirmed. Check saved records and "
                                            "action history before trying again.</p>"
                                            "<p><a class='btn' href='/actions'>Action history</a> "
                                            "<a href='/records'>Saved records</a> "
                                            "<a href='/docs/troubleshooting'>Troubleshooting guide</a></p>"))

    do_GET = _handle
    do_POST = _handle

    def _send_html(self, code: int, body: str):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json_download(self, filename: str, payload: dict):
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()


class PayloadTooLarge(Exception):
    pass


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def esc(value) -> str:
    return html.escape(str(value), quote=True)


def money(minor: int, ccy: str) -> str:
    return esc(format_amount(int(minor), ccy))


def iso(d) -> str:
    return d.isoformat() if d is not None else "—"


CSS = """
:root{--bg:#f7f7f4;--card:#fff;--ink:#1d1d1b;--mut:#6b6b66;--line:#e3e1da;
--acc:#0b5fff;--ok:#0a7a3d;--warn:#a05a00;--bad:#b3261e;--sim:#6a4fb3}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:0 14px 40px}
header.top{background:#101014;color:#fff;padding:10px 0}
header.top .wrap{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding-bottom:10px}
.brand{font-weight:700;font-size:18px;margin-right:auto}
.badge{display:inline-block;font-size:12px;padding:2px 10px;border-radius:999px;
font-weight:600;letter-spacing:.02em}
.badge.sim{background:#2a2440;color:#d9ccff;border:1px solid var(--sim)}
.badge.ai{background:#3a2f10;color:#ffd97a;border:1px solid #8a6d1f}
nav.tabs{display:flex;gap:4px;flex-wrap:wrap;background:#101014;padding:0 0 10px}
nav.tabs .wrap{display:flex;gap:4px;flex-wrap:wrap;padding-bottom:0}
nav.tabs a{color:#ddd;text-decoration:none;padding:8px 14px;border-radius:8px 8px 0 0}
nav.tabs a.on{background:var(--bg);color:var(--ink);font-weight:600}
.banner{background:#fff8e6;border-bottom:2px solid #e8c547;color:#5c430a;
font-size:13px;padding:8px 0}
main{overflow-wrap:anywhere;min-width:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:16px;margin:14px 0;overflow-wrap:anywhere}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
.kv{display:flex;justify-content:space-between;gap:10px;padding:6px 0;
border-bottom:1px dashed var(--line);font-size:14px}
.kv:last-child{border-bottom:0}
.kv b{font-variant-numeric:tabular-nums;white-space:nowrap}
.kv .status-line{white-space:normal;font-weight:400;min-width:0}
.kv .status-line .pill{white-space:nowrap}
.kv>span{min-width:0}
.kv.stack{flex-direction:column;align-items:flex-start;gap:2px}
.amt{white-space:nowrap}
.mut{color:var(--mut);font-size:13px}
.err{color:var(--bad);font-weight:600}
.ok{color:var(--ok);font-weight:600}
.warn{color:var(--warn);font-weight:600}
table{width:100%;border-collapse:collapse;font-size:13px;display:block;
overflow-x:auto;white-space:nowrap}
table.wrap-t{white-space:normal}
.weekly-cash-scroll{max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}
.weekly-cash-table{display:table;min-width:980px;white-space:nowrap;overflow-wrap:normal}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);
font-variant-numeric:tabular-nums}
th{background:#f0eee8;position:sticky;top:0}
tr.breach td{background:#fdeceb}
tr:target td{background:#fff3dd}
.pill{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;
font-weight:600;background:#eef;border:1px solid var(--line)}
.pill.ok{background:#e7f6ec;border-color:#bfe3cc;color:var(--ok)}
.pill.bad{background:#fdeceb;border-color:#f3c1bd;color:var(--bad)}
.pill.warn{background:#fff3dd;border-color:#ecd9a8;color:var(--warn)}
.pill.sim{background:#efeafd;border-color:#cfc2f2;color:var(--sim)}
form.inline{display:inline}
button,.btn{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid #c9c7bd;
background:#fff;cursor:pointer}
button.primary,.btn.primary{background:var(--acc);border-color:var(--acc);color:#fff}
button.danger{border-color:var(--bad);color:var(--bad)}
.btn{display:inline-block;max-width:100%;text-decoration:none}
.grid>div{min-width:0}
input,select,textarea{font:inherit;padding:7px 9px;border:1px solid #c9c7bd;border-radius:8px;
max-width:100%}
label{display:block;font-size:13px;font-weight:600;margin:8px 0 2px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:end}
.row>div{min-width:0;max-width:100%}
.row select{width:100%;min-width:0}
.steps{list-style:none;margin:0;padding:0;counter-reset:s}
.steps li{padding:10px 10px 10px 44px;border:1px solid var(--line);border-radius:10px;
margin:8px 0;position:relative;background:#fff}
.steps li:before{counter-increment:s;content:counter(s);position:absolute;left:12px;top:10px;
width:24px;height:24px;border-radius:50%;background:#e9e7df;color:#555;display:flex;
align-items:center;justify-content:center;font-size:13px;font-weight:700}
.steps li.done{border-color:#bfe3cc;background:#f4fbf6}
.steps li.done:before{content:"✓";background:var(--ok);color:#fff}
pre.dump{background:#101014;color:#d7f5d7;padding:12px;border-radius:8px;overflow:auto;
font-size:12px}
footer{color:var(--mut);font-size:12px;padding:20px 14px;text-align:center}
@media(max-width:640px){.kv{font-size:13px}body{font-size:15px}
nav.tabs .wrap{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch}
nav.tabs a{white-space:nowrap;padding:8px 10px}
header.top .wrap{gap:6px}.brand{font-size:16px}
.kv b{white-space:normal;overflow-wrap:anywhere;text-align:right}
.kv.stack b{text-align:left}
.card{padding:12px;margin:10px 0}}
"""


def page(title: str, body: str, active: str = "") -> str:
    from .ui import app_page
    return app_page(title, body, active, agent_web.banner_status(), CSS)


def csrf_field() -> str:
    return f"<input type='hidden' name='csrf' value='{csrf_token()}'>"


def post_button(action: str, label: str, cls: str = "",
                fields: dict | None = None, confirm: str = "") -> str:
    hidden = "".join(
        f"<input type='hidden' name='{esc(k)}' value='{esc(v)}'>"
        for k, v in (fields or {}).items())
    js = f"onsubmit=\"return confirm('{esc(confirm)}')\"" if confirm else ""
    return (f"<form class='inline' method='post' action='{esc(action)}' {js}>"
            f"{csrf_field()}{hidden}"
            f"<button class='{esc(cls)}' type='submit'>{esc(label)}</button></form>")


def evidence_link(ledger: Ledger, evid: str | None) -> str:
    if not evid:
        return "<span class='mut'>none</span>"
    ev = ledger.evidence.get(evid)
    label = f"{evid}: {ev.summary}" if ev else f"{evid} (unknown)"
    runtime = " <span class='pill sim'>runtime event</span>" \
        if evid in ledger.runtime_evidence_ids else ""
    originals=sum(a['active'] and a['evidence_id']==evid for a in ledger.evidence_attachments.values())
    files=f" <a href='/attachments?evidence_id={quote(evid,safe='')}'>Original files ({originals})</a>" if originals else ''
    return f"<a href='/ledger#evidence-{quote(evid, safe='')}' title='{esc(label)}'>{esc(evid)}</a>{runtime}{files}"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# SVG forecast chart (server-generated; table alongside for accessibility)
# ---------------------------------------------------------------------------

def forecast_svg(cf, width: int = 900, height: int = 260) -> str:
    days = cf.days
    n = len(days)
    vals = [d.intraday_low_minor for d in days] + [d.closing_minor for d in days]
    lo = min(vals + [cf.floor_minor])
    hi = max(vals + [cf.floor_minor])
    span = max(hi - lo, 1)
    pad_l, pad_r, pad_t, pad_b = 100, 16, 28, 28
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b

    def x(i: int) -> float:
        return pad_l + iw * i / max(n - 1, 1)

    def y(v: int) -> float:
        return pad_t + ih * (1 - (v - lo) / span)

    # breach shading
    rects = []
    for i, d in enumerate(days):
        if d.floor_breached:
            rects.append(
                f"<rect x='{x(i) - iw / n / 2:.1f}' y='{pad_t}' "
                f"width='{iw / n + 1:.1f}' height='{ih}' fill='#fdeceb'/>")
    # floor line
    fy = y(cf.floor_minor)
    # balance line (closing)
    pts = " ".join(f"{x(i):.1f},{y(d.closing_minor):.1f}" for i, d in enumerate(days))
    # intraday low line (dashed)
    low_pts = " ".join(f"{x(i):.1f},{y(d.intraday_low_minor):.1f}"
                       for i, d in enumerate(days))
    # first breach marker
    marker = ""
    if cf.first_breach_date is not None:
        bi = next(i for i, d in enumerate(days) if d.date == cf.first_breach_date)
        label_x=x(bi)-8 if bi>n/2 else x(bi)+8
        anchor='end' if bi>n/2 else 'start'
        marker = (f"<circle cx='{x(bi):.1f}' cy='{y(days[bi].intraday_low_minor):.1f}' "
                  f"r='5' fill='#b3261e'/><text x='{label_x:.1f}' text-anchor='{anchor}' "
                  f"y='{y(days[bi].intraday_low_minor) - 8:.1f}' font-size='11' "
                  f"fill='#b3261e'>first breach {cf.first_breach_date.isoformat()}</text>")
    ticks = ""
    for i in range(0, n, 5):
        ticks += (f"<text x='{x(i):.1f}' y='{height - 8}' font-size='10' "
                  f"text-anchor='middle' fill='#6b6b66'>"
                  f"{days[i].date.strftime('%b %d')}</text>")
    grid = ''
    for value in sorted({lo,(lo+hi)//2,hi}):
        grid += (f"<line x1='{pad_l}' y1='{y(value):.1f}' x2='{width-pad_r}' y2='{y(value):.1f}' stroke='#e6eee8'/>"
                 f"<text x='{pad_l-12}' y='{y(value)+4:.1f}' text-anchor='end' font-size='11' fill='#768578'>{money(value,cf.currency)}</text>")
    return (
        f"<svg viewBox='0 0 {width} {height}' width='100%' role='img' "
        f"aria-label='{len(days)}-day {esc(cf.currency)} forecast chart'>"
        f"{grid}{''.join(rects)}"
        f"<line x1='{pad_l}' y1='{fy:.1f}' x2='{width - pad_r}' y2='{fy:.1f}' "
        f"stroke='#a05a00' stroke-dasharray='6 4' stroke-width='1.5'/>"
        f"<text x='{width - pad_r - 4:.1f}' y='{fy - 6:.1f}' font-size='11' "
        f"text-anchor='end' fill='#a05a00'>floor</text>"
        f"<polyline points='{low_pts}' fill='none' stroke='#b3261e' "
        f"stroke-width='1.5' stroke-dasharray='3 3'/>"
        f"<polyline points='{pts}' fill='none' stroke='#217358' stroke-width='2.5'/>"
        f"{marker}{ticks}"
        f"<text x='{pad_l}' y='{pad_t - 2}' font-size='11' fill='#6b6b66'>"
        f"{esc(cf.currency)} · solid: closing · dashed: intraday low · red: floor breach</text>"
        f"</svg>")


def forecast_table(cf) -> str:
    rows = []
    for d in cf.days:
        cls = " class='breach'" if d.floor_breached else ""
        rows.append(
            f"<tr{cls}><td>{d.date.isoformat()}</td>"
            f"<td>{money(d.opening_minor, cf.currency)}</td>"
            f"<td>{money(d.inflow_minor, cf.currency)}</td>"
            f"<td>{money(d.outflow_minor, cf.currency)}</td>"
            f"<td>{money(d.intraday_low_minor, cf.currency)}</td>"
            f"<td>{money(d.closing_minor, cf.currency)}</td>"
            f"<td>{'yes' if d.floor_breached else 'no'}</td></tr>")
    return (
        "<table aria-label='Daily forecast table'><thead><tr>"
        "<th>Date</th><th>Opening</th><th>Inflow</th><th>Outflow</th>"
        "<th>Intraday low</th><th>Closing</th><th>Floor breached</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def fx_status_html(ledger: Ledger) -> str:
    """Display-only conversion; never makes funds available."""
    parts = []
    for q in ledger.fx_quotes:
        amount_minor = sum(a.opening_minor for a in ledger.accounts.values() if a.currency == q.from_ccy)
        conv = display_convert(amount_minor, q.from_ccy, q.to_ccy,
                               ledger.fx_quotes, ledger.as_of)
        pill = {"ok": "ok", "stale": "warn", "missing": "bad",
                "same_currency": "ok"}.get(conv.status, "warn")
        shown = (f"{q.to_ccy} {conv.value:,}" if conv.value is not None else "—")
        parts.append(
            f"<div class='kv'><span>{esc(q.from_ccy)} → {esc(q.to_ccy)} "
            f"@ {esc(str(q.rate))} <span class='pill {pill}'>{esc(conv.status)}</span>"
            f"<br><span class='mut'>{esc(conv.note)}; source {esc(q.source)}</span></span>"
            f"<b>{esc(shown)} <span class='mut'>(display only)</span></b></div>")
    parts.append("<p class='mut'>Display conversion only: it is never an executed "
                 "FX transaction and can never cover a cash gap or be used for "
                 "payments.</p>")
    return "".join(parts)


def gap_causes_html(ledger: Ledger, fc) -> str:
    """Which dated items cause the gap, each linked to its source record."""
    usd = fc.currencies.get(ledger.base_currency)
    if usd is None or usd.first_breach_date is None:
        return "<p class='ok'>No floor breach in the current forecast.</p>"
    items = []
    for eid, evt in ledger.events.items():
        if evt.id in ledger.applied_event_ids and evt.type == "client_delay":
            rec = ledger.receivables[evt.receivable_id]
            items.append(
                f"<div class='kv'><span>Client delay event "
                f"<span class='pill sim'>simulated</span><br><span class='mut'>"
                f"{esc(rec.id)} expected {iso(evt.new_expected_date)} "
                f"(was the dated inflow covering the gap)</span><br>"
                f"Evidence: {evidence_link(ledger, evt.evidence_id)}</span>"
                f"<b>{money(rec.original_minor, rec.currency)} delayed</b></div>")
    for out in ledger.outflows.values():
        if out.status == "scheduled" and out.date >= ledger.as_of:
            prot = " <span class='pill warn'>protected</span>" if out.protected else ""
            items.append(
                f"<div class='kv'><span>{esc(out.id)} due {iso(out.date)}{prot}"
                f"<br>Evidence: {evidence_link(ledger, out.evidence_id)}</span>"
                f"<b>{money(out.amount_minor, out.currency)}</b></div>")
    return "".join(items)


@route("GET", "/")
def overview(handler: Handler, req: Request):
    def build(ledger: Ledger):
        fc = forecast(ledger, ledger.as_of, ledger.horizon_days)
        usd = fc.currencies.get(ledger.base_currency)
        eur = fc.currencies.get("EUR")
        delay_applied = "evt-delay-001" in ledger.applied_event_ids

        cash_rows = "".join(
            f"<div class='kv'><span>Actual cash on hand — {esc(a.currency)} "
            f"<span class='mut'>(observed receipts minus paid outflows; "
            f"drafts and plans never touch this)</span></span>"
            f"<b>{money(ledger.actual_cash(a.currency), a.currency)}</b></div>"
            for a in {a.currency: a for a in ledger.accounts.values()}.values())

        from .treasury import treasury_report
        treasury = treasury_report(ledger, ctx=_owner_ctx(ledger))
        cash_rows += "<p><a href='/cash-accounts'>Review individual cash accounts / observed transfers</a> · <a href='/cash-availability'>Review restricted cash</a></p>"
        for ccy, values in treasury['currencies'].items():
            cash_rows += f"<div class='kv stack'><span>{esc(ccy)} restricted → available for decisions</span><b>{money(values['restricted_minor'],ccy)} → {money(values['available_minor'],ccy)}</b></div>"
        cash_rows += '<p>Forecast decision balances exclude active restrictions once. Cash-floor buffers and scheduled payroll are separate. Same-currency forecasts pool accounts; confirm account-level funding separately.</p>'

        prot_rows = "".join(
            f"<div class='kv'><span>{esc(o.id)} — {iso(o.date)} "
            f"<span class='pill warn'>protected</span><br>"
            f"<span class='mut'>Evidence: {evidence_link(ledger, o.evidence_id)}</span>"
            f"</span><b>{money(o.amount_minor, o.currency)}</b></div>"
            for o in ledger.outflows.values() if o.protected)

        if usd is not None:
            summary = (
                f"<div class='grid'>"
                f"<div class='kv stack'><span>Lowest projected balance</span>"
                f"<b>{money(usd.min_balance_minor, usd.currency)} "
                f"on {usd.min_balance_date.isoformat()}</b></div>"
                f"<div class='kv stack'><span>Cash floor</span>"
                f"<b>{money(usd.floor_minor, usd.currency)}</b></div>"
                f"<div class='kv stack'><span>First floor-breach date</span>"
                f"<b>{usd.first_breach_date.isoformat() if usd.first_breach_date else 'none'}</b></div>"
                f"<div class='kv stack'><span>Worst shortfall vs floor</span>"
                f"<b>{money(usd.worst_shortfall_minor, usd.currency)}</b></div>"
                f"<div class='kv stack'><span>Closing balance (day 30)</span>"
                f"<b>{money(usd.closing_minor, usd.currency)}</b></div>"
                f"<div class='kv stack'><span>EUR (separate currency)</span>"
                f"<b>{money(eur.min_balance_minor, 'EUR') if eur else '—'} min</b></div>"
                f"</div>"
                f"<h3>30-day forecast — {esc(usd.currency)}</h3>"
                f"{forecast_svg(usd)}"
                f"<details><summary>Accessible data table (all 30 days)</summary>"
                f"{forecast_table(usd)}</details>")
        else:
            summary = "<p class='mut'>No base-currency account.</p>"

        assumptions = "".join(f"<li>{esc(a)}</li>" for a in fc.assumptions)
        clarifs = "".join(
            f"<li><b>{esc(c['code'])}</b> — {esc(c['question'])} "
            f"<span class='mut'>{esc(c['why_it_matters'])}</span></li>"
            for c in fc.clarifications) or "<li>None.</li>"

        delay_box = ("<p><span class='pill sim'>client-delay event applied</span> "
                     "The invoice is now expected 5 November 2026.</p>"
                     if delay_applied else
                     post_button("/demo/apply-delay", "Apply client-delay event",
                                 cls="primary",
                                 confirm="Apply the simulated client-delay event?"))
        if 'evt-delay-001' not in ledger.events:
            delay_box = "<p>Imported synthetic ledger. See Today's Priorities for work based on these records.</p>"
        attention = attention_items(ledger, ctx=_owner_ctx(ledger))

        body = f"""
<h1>Overview</h1>
<div class="card"><h2>Today's Priorities — {len(attention)} items</h2>
<p>Evidence, missing facts and pending workflow steps, ordered by urgency as of the ledger snapshot.</p>
<a class="btn primary" href="/today">Review what needs attention</a></div>
<p><a class="btn" href="/stress">Test a payment delay</a>
<a class="btn" href="/plans">Compare plans and review draft requests</a></p>
<div class="card"><div class="kv"><span>As-of date</span><b>{ledger.as_of.isoformat()}</b></div>
<div class="kv"><span>Business</span><b>{esc(ledger.business_name)} (synthetic)</b></div>
<div class="kv"><span>Demo workflow</span><span class="status-line">{delay_box}</span></div></div>
<div class="card"><h2>Actual cash by currency</h2>{cash_rows}</div>
<div class="card"><h2>Upcoming protected obligations</h2>{prot_rows or "<p class='mut'>None.</p>"}</div>
<div class="card"><h2>Forecast vs cash floor</h2>{summary}</div>
<div class="card"><h2>What causes the gap</h2>{gap_causes_html(ledger, fc)}</div>
<div class="card"><h2>Display-only currency conversion</h2>{fx_status_html(ledger)}</div>
<div class="card"><h2>Assumptions</h2><ul>{assumptions}</ul>
<h2>Open clarifications</h2><ul>{clarifs}</ul></div>
"""
        return page("Overview", body, active="/")
    return handler._send_html(200, read_state(build))


# ---------------------------------------------------------------------------
# Evidence & Ledger
# ---------------------------------------------------------------------------

def _ledger_tables_html(ledger: Ledger) -> str:
    rec_rows = []
    for r in ledger.receivables.values():
        allocs = "".join(
            f"<div class='mut'>{esc(a.receipt_id)}: {money(a.amount_minor, r.currency)} "
            f"on {iso(a.date_received)} {'(observed)' if a.observed else '(projected)'}</div>"
            for a in r.allocations)
        rec_rows.append(
            f"<tr><td>{esc(r.id)}</td><td>{esc(r.currency)}</td>"
            f"<td>{money(r.original_minor, r.currency)}<br><span class='mut'>Effective total {money(r.effective_total_minor,r.currency)}; credit requiring review {money(r.credit_balance_minor,r.currency)}</span></td>"
            f"<td>{money(r.remaining_minor, r.currency)}</td>"
            f"<td>{iso(r.expected_date)}</td>"
            f"<td>{evidence_link(ledger, r.evidence_id)}{allocs}"
            f"<br><a href='/facts?invoice_id={quote(r.id, safe='')}'>Review expected date</a>"
            f"<br><a href='/invoice-adjustments#record-{esc(r.id)}'>Review source amount / credit note</a>"
            f"<br>Contractual due: {iso(r.due_date)} · <a href='/records?kind=invoice&amp;item_id={quote(r.id,safe='')}'>Review due/party/reference</a>"
            f"{('<br>Date source: ' + evidence_link(ledger, ledger.collection_date_sources[r.id])) if r.id in ledger.collection_date_sources else ''}</td></tr>")
    out_rows = []
    for o in ledger.outflows.values():
        prot = " <span class='pill warn'>protected</span>" if o.protected else ""
        consent = " <span class='pill'>consent required</span>" \
            if o.counterparty_consent_required_for_change else ""
        paid = (f"<span class='pill ok'>paid {iso(o.paid_date)}</span>"
                if o.status == "paid" else "<span class='pill'>scheduled</span>")
        pay_btn = ""
        if o.status == "scheduled":
            pay_btn = f"<a href='/payments?bill_id={quote(o.id, safe='')}'>Record observed payment</a>"
        out_rows.append(
            f"<tr><td>{esc(o.id)}{prot}{consent}</td><td>{esc(o.currency)}</td>"
            f"<td>{money(o.amount_minor, o.currency)}<br><span class='mut'>Effective total {money(o.effective_total_minor,o.currency)}; settled including noncash credit {money(o.paid_minor,o.currency)}; remaining {money(o.remaining_minor,o.currency)}; overpaid requiring review {money(o.overpaid_minor,o.currency)}</span><br><a href='/bill-adjustments#record-{esc(o.id)}'>Review source amount</a></td><td>{iso(o.date)}</td>"
            f"<td>{paid}</td><td>{evidence_link(ledger, o.evidence_id)}</td>"
            f"<td>{pay_btn}</td></tr>")
    rcpt_rows = []
    for rc in ledger.receipts.values():
        from .settlement import effective_receipt, split_parent
        current = effective_receipt(ledger, rc.id)
        parent = split_parent(ledger,rc.id)
        correction = (f"<br>Effective: {esc(current.receivable_id)} {money(current.amount_minor, ledger.receivables[current.receivable_id].currency)} on {iso(current.date_received)}"
                      if current != rc else "")
        rcpt_rows.append(
            f"<tr><td>{esc(rc.id)} <span class='pill'>idempotency key</span></td>"
            f"<td>{esc(rc.receivable_id)}</td>"
            f"<td>{money(rc.amount_minor, ledger.receivables[rc.receivable_id].currency)}</td>"
            f"<td>{iso(rc.date_received)}</td><td>{'allocation of existing cash — '+esc(parent['id']) if parent else esc(rc.state)}</td>"
            f"<td>{evidence_link(ledger, rc.evidence_id)}{correction}<br><a href='/corrections?receipt_id={quote(rc.id,safe='')}'>Review/correct original</a></td></tr>")
    evt_rows = []
    for e in ledger.events.values():
        applied = ("<span class='pill ok'>applied</span>"
                   if e.id in ledger.applied_event_ids else "<span class='pill'>recorded</span>")
        evt_rows.append(
            f"<tr><td>{esc(e.id)}</td><td>{esc(e.type)}</td>"
            f"<td>{esc(e.receivable_id)} → {iso(e.new_expected_date)}</td>"
            f"<td>{applied}</td><td>{evidence_link(ledger, e.evidence_id)}</td></tr>")
    ev_rows = "".join(
        f"<tr id='evidence-{esc(e.id)}'><td>{esc(e.id)}"
        f"{' <span class=\'pill sim\'>runtime event</span>' if e.id in ledger.runtime_evidence_ids else ''}"
        f"</td><td>{esc(e.kind)}</td><td>{esc(e.summary)}</td></tr>"
        for e in ledger.evidence.values())
    doc_rows = "".join(
        f"<tr><td>{esc(d.id)}</td><td>{esc(d.kind)}</td>"
        f"<td>{esc(d.source)}</td>"
        f"<td class='mut'>content not stored (sha256 {esc(d.content[:16])}…)</td></tr>"
        for d in ledger.documents.values()) or \
        "<tr><td colspan='4' class='mut'>No documents uploaded yet.</td></tr>"
    return f"""
<h2>Invoices (receivables)</h2>
<table><thead><tr><th>ID</th><th>Ccy</th><th>Original</th><th>Remaining</th>
<th>Expected</th><th>Evidence / allocations</th></tr></thead><tbody>
{"".join(rec_rows)}</tbody></table>
<h2>Bills (outflows)</h2>
<table><thead><tr><th>ID</th><th>Ccy</th><th>Amount</th><th>Date</th><th>Status</th>
<th>Evidence</th><th></th></tr></thead><tbody>{"".join(out_rows)}</tbody></table>
<h2>Receipts</h2>
<table><thead><tr><th>Receipt ID</th><th>Invoice</th><th>Amount</th><th>Date</th>
<th>State</th><th>Evidence</th></tr></thead><tbody>
{"".join(rcpt_rows) or "<tr><td colspan='6' class='mut'>No receipts recorded yet.</td></tr>"}
</tbody></table>
<h2>Events</h2>
<table><thead><tr><th>ID</th><th>Type</th><th>Effect</th><th>Status</th>
<th>Evidence</th></tr></thead><tbody>{"".join(evt_rows)}</tbody></table>
<h2>Evidence register</h2>
<table><thead><tr><th>ID</th><th>Kind</th><th>Summary</th></tr></thead><tbody>
{ev_rows}</tbody></table>
<h2>Uploaded documents (private)</h2>
<table><thead><tr><th>ID</th><th>Kind</th><th>Source</th><th>Storage</th></tr></thead>
<tbody>{doc_rows}</tbody></table>
<p class="mut">Standard document imports retain content hashes and extracted fields. Optional <a href='/attachments'>Evidence Attachments</a> retain original bytes locally after separate owner confirmation; raw bytes are excluded from audit/financial exports and model tools.</p>"""


def _import_forms_html() -> str:
    return f"""
<div class="card"><h2>Import</h2>
<div class="grid">
<div><h3>Structured fixture (JSON)</h3>
<p class="mut">Validate a fixture file, preview it, then confirm to replace the ledger.</p>
<form method="post" action="/ledger/import/fixture" enctype="multipart/form-data">
{csrf_field()}<label>Fixture JSON file <input type="file" name="file" accept=".json" required></label>
<button class="primary" type="submit">Upload &amp; preview</button></form></div>
<div><h3>Transactions (CSV)</h3>
<p class="mut">Upload a CSV, map its columns, preview the records, then confirm.</p>
<form method="post" action="/ledger/import/csv" enctype="multipart/form-data">
{csrf_field()}<label>CSV file <input type="file" name="file" accept=".csv" required></label>
<button class="primary" type="submit">Upload &amp; map columns</button></form></div>
<div><h3>Invoice document (text / PDF)</h3>
<p class="mut">Best-effort text extraction; every field is shown for confirmation.
Missing dates or amounts are never invented.</p>
<form method="post" action="/ledger/import/document" enctype="multipart/form-data">
{csrf_field()}<label>Document file <input type="file" name="file" accept=".txt,.pdf,.csv" required></label>
<button class="primary" type="submit">Upload &amp; extract</button></form></div>
</div></div>"""


@route("GET", "/ledger")
def ledger_page(handler: Handler, req: Request):
    def build(ledger: Ledger):
        body = (f"<h1>Evidence &amp; Ledger</h1>"
                "<p><a class='btn' href='/facts'>Resolve collection facts</a> <a class='btn' href='/receipts'>Review existing receipts</a> <a class='btn' href='/daily'>Review daily cash snapshot</a></p>"
                f"<div class='card'>{_ledger_tables_html(ledger)}</div>"
                f"{_import_forms_html()}")
        return page("Evidence & Ledger", body, active="/ledger")
    return handler._send_html(200, read_state(build))


@route("POST", "/ledger/mark-paid")
def ledger_mark_paid(handler: Handler, req: Request):
    outflow_id = req.field("outflow_id")
    paid_date = date.fromisoformat(req.field("paid_date"))
    evidence_id = req.field("evidence_id")

    def do(ledger: Ledger):
        ctx = _owner_ctx(ledger)
        return ledger.mark_paid(outflow_id, paid_date, "owner", evidence_id,
                               ctx=ctx,acknowledge_duplicates=req.field('acknowledge_duplicates')=='yes')
    mutate_state(do)
    return handler._redirect("/ledger")


# ---------------------------------------------------------------------------
# Import pipeline: upload -> preview/mapping -> explicit confirmation
# ---------------------------------------------------------------------------

def _stage(payload: dict) -> str:
    token = secrets.token_hex(12)
    with LOCK:
        if getattr(_WORKSPACE,'state',None) is not None:
            import time
            payload.setdefault('_hosted_created',time.monotonic())
        staged()[token] = payload
    return token


def _pop_staged(token: str) -> dict | None:
    with LOCK:
        return staged().pop(token, None)


def _ext_of(filename: str) -> str:
    name = filename.lower()
    return "." + name.rsplit(".", 1)[1] if "." in name else ""


@route("POST", "/ledger/import/fixture")
def import_fixture_upload(handler: Handler, req: Request):
    up = req.files.get("file")
    if not up:
        raise CashflowError("no file uploaded")
    if _ext_of(up["filename"]) != ".json":
        raise CashflowError(f"invalid file type {up['filename']!r}: expected .json")
    if len(up["data"]) > MAX_UPLOAD_BYTES:
        raise CashflowError("file exceeds the 1 MiB upload limit")
    try:
        data = json.loads(up["data"].decode("utf-8"))
    except Exception:
        raise CashflowError("file is not valid JSON")
    try:
        validate_fixture(data)
    except CashflowError as exc:
        problems = getattr(exc, "problems", [str(exc)])
        items = "".join(f"<li>{esc(p)}</li>" for p in problems)
        return handler._send_html(400, page(
            "Fixture invalid",
            f"<h1>Fixture rejected</h1><div class='card'><p class='err'>"
            f"The uploaded fixture failed validation. Nothing was imported.</p>"
            f"<ul>{items}</ul></div>", active="/ledger"))
    token = _stage({"kind": "fixture", "data": data,
                    "filename": up["filename"]})
    biz = data["business"]
    body = f"""
<h1>Preview fixture import</h1>
<div class="card"><h2>{esc(biz['name'])} <span class="pill sim">synthetic</span></h2>
<div class="kv"><span>As-of</span><b>{esc(biz['as_of'])}</b></div>
<div class="kv"><span>Accounts</span><b>{len(data['accounts'])}</b></div>
<div class="kv"><span>Receivables</span><b>{len(data['receivables'])}</b></div>
<div class="kv"><span>Outflows</span><b>{len(data['outflows'])}</b></div>
<div class="kv"><span>Events</span><b>{len(data['events'])}</b></div>
<div class="kv"><span>Evidence items</span><b>{len(data['evidence'])}</b></div>
<p class="warn">Confirming replaces the entire current ledger (receipts, actions,
approvals and history will be discarded).</p>
<form method="post" action="/ledger/import/confirm">{csrf_field()}
<input type="hidden" name="token" value="{token}">
<button class="primary" type="submit">Confirm import — replace ledger</button>
<a class="btn" href="/ledger">Cancel</a></form></div>"""
    return handler._send_html(200, page("Preview fixture import", body, active="/ledger"))


def _parse_csv_text(text: str) -> tuple[list[str], list[list[str]]]:
    import csv as csv_mod
    import io
    reader = csv_mod.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise CashflowError("CSV is empty")
    return rows[0], rows[1:]


CSV_FIELDS = ["record_type", "reference", "amount_major", "currency",
              "date", "counterparty", "note", "due_date", "invoice_reference"]


@route("POST", "/ledger/import/csv")
def import_csv_upload(handler: Handler, req: Request):
    up = req.files.get("file")
    if not up:
        raise CashflowError("no file uploaded")
    if _ext_of(up["filename"]) != ".csv":
        raise CashflowError(f"invalid file type {up['filename']!r}: expected .csv")
    if len(up["data"]) > MAX_UPLOAD_BYTES:
        raise CashflowError("file exceeds the 1 MiB upload limit")
    try:
        text = up["data"].decode("utf-8-sig")
    except Exception:
        raise CashflowError("CSV must be UTF-8 encoded")
    headers, rows = _parse_csv_text(text)
    if len(rows) > 500:
        raise CashflowError("CSV has more than 500 data rows; split the file")
    token = _stage({"kind": "csv", "headers": headers, "rows": rows[:50],
                    "total_rows": len(rows), "filename": up["filename"]})
    # Per-column mapping selects.
    selects = []
    for i, h in enumerate(headers):
        hl = h.strip().lower()
        guess = hl if hl in CSV_FIELDS else ""
        if not guess:
            for f in CSV_FIELDS:
                if f in hl or hl in f:
                    guess = f
                    break
        options = "".join(
            f"<option value='{f}'{' selected' if f == guess else ''}>{f}</option>"
            for f in [""] + CSV_FIELDS)
        selects.append(
            f"<div><label>CSV column “{esc(h)}” → <select name='col_{i}'>{options}"
            f"</select></label></div>")
    sample = "".join(
        "<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r[:len(headers)]) + "</tr>"
        for r in rows[:5])
    body = f"""
<h1>Map CSV columns</h1>
<div class="card"><p class="mut">File: {esc(up['filename'])} — {len(rows)} data rows
(first 50 staged for preview). Map each CSV column to a ledger field, then
preview the records before anything touches the ledger.</p>
<form method="post" action="/ledger/import/csv/preview">{csrf_field()}
<input type="hidden" name="token" value="{token}">
<div class="row">{"".join(selects)}</div>
<button class="primary" type="submit">Preview mapped records</button>
<a class="btn" href="/ledger">Cancel</a></form>
<h3>Sample rows</h3><table><thead><tr>
{"".join(f"<th>{esc(h)}</th>" for h in headers)}</tr></thead>
<tbody>{sample}</tbody></table></div>"""
    return handler._send_html(200, page("Map CSV columns", body, active="/ledger"))


def _parse_major_to_minor(text: str, currency: str) -> int:
    from decimal import Decimal, InvalidOperation
    try:
        d = Decimal(text.strip())
    except (InvalidOperation, ValueError):
        raise CashflowError(f"amount {text!r} is not a valid decimal number")
    digits = minor_digits(currency)
    scaled = d * (Decimal(10) ** digits)
    if scaled != scaled.to_integral_value():
        raise CashflowError(
            f"amount {text!r} has more precision than {currency} allows")
    return int(scaled)


@route("POST", "/ledger/import/csv/preview")
def import_csv_preview(handler: Handler, req: Request):
    staged = _pop_staged(req.field("token"))
    if not staged or staged["kind"] != "csv":
        raise CashflowError("import session expired; upload the CSV again")
    headers, rows = staged["headers"], staged["rows"]
    mapping: dict[int, str] = {}
    for i in range(len(headers)):
        f = req.field(f"col_{i}")
        if f:
            if f not in CSV_FIELDS:
                raise CashflowError(f"unknown field {f!r}")
            mapping[i] = f

    def build(ledger: Ledger):
        records, problems = [], []
        seen_ids: set[str] = set()
        for ri, row in enumerate(rows, start=2):
            cells = {mapping[i]: row[i].strip() for i in mapping
                     if i < len(row)}
            rtype = cells.get("record_type", "").lower()
            if rtype not in ("invoice", "bill", "receipt"):
                problems.append(
                    f"row {ri}: record_type must be invoice, bill or receipt "
                    f"(got {cells.get('record_type', '')!r}) — row skipped")
                continue
            ref = cells.get("reference") or f"csv-{ri}"
            if ref in seen_ids:
                problems.append(f"row {ri}: duplicate reference {ref!r} within "
                                f"the file — row skipped")
                continue
            seen_ids.add(ref)
            ccy = (cells.get("currency") or "").upper()
            if not re.fullmatch(r"[A-Z]{3}", ccy):
                problems.append(f"row {ri}: currency {ccy!r} is not a valid "
                                f"ISO code — row skipped")
                continue
            try:
                amount_minor = _parse_major_to_minor(
                    cells.get("amount_major", ""), ccy)
            except CashflowError as exc:
                problems.append(f"row {ri}: {exc} — row skipped")
                continue
            if amount_minor <= 0:
                problems.append(f"row {ri}: amount must be positive — row skipped")
                continue
            dat = cells.get("date") or ""
            try:
                parsed = date.fromisoformat(dat) if dat else None
            except ValueError:
                problems.append(f"row {ri}: date {dat!r} is not ISO YYYY-MM-DD "
                                f"— recorded as missing, never invented")
                parsed = None
            # Duplicate warnings against existing ledger records.
            dup = None
            if rtype == "invoice" and ref in ledger.receivables:
                dup = f"receivable {ref} already exists"
            elif rtype == "bill" and ref in ledger.outflows:
                dup = f"outflow {ref} already exists"
            elif rtype == "receipt" and ref in ledger.receipts:
                dup = f"receipt {ref} already recorded (idempotency key)"
            will_skip = parsed is None and rtype != "invoice"
            due = cells.get('due_date', '')
            try: due = date.fromisoformat(due).isoformat() if due else None
            except ValueError:
                problems.append(f'row {ri}: invalid contractual due_date — row skipped, never inferred from expected collection')
                due = None; will_skip = True
            if len(cells.get('counterparty',''))>200 or len(cells.get('invoice_reference',''))>200:
                problems.append(f'row {ri}: party/reference exceeds 200 characters — row skipped'); will_skip=True
            if rtype == 'receipt':
                invoice_id = cells.get('counterparty', '')
                invoice = ledger.receivables.get(invoice_id)
                if invoice is None or invoice.currency != ccy:
                    will_skip = True
                    problems.append(f'row {ri}: receipt needs an explicit existing invoice reference in counterparty and its same currency — row skipped')
            records.append({
                "row": ri, "record_type": rtype, "reference": ref,
                "amount_minor": amount_minor, "currency": ccy,
                "date": parsed.isoformat() if parsed else None,
                "date_missing": parsed is None,
                "will_skip": will_skip,
                "counterparty": cells.get("counterparty", ""),
                "note": cells.get("note", ""),
                "duplicate_warning": dup,
                "due_date": due if rtype=='invoice' else None,
                "invoice_reference": cells.get('invoice_reference',''),
            })
        from .operations import csv_duplicate_warnings
        from .owner_workflows import review_revision
        return csv_duplicate_warnings(ledger,records), problems, review_revision(ledger)

    records, problems, revision = read_state(build)
    token = _stage({"kind": "csv-confirm", "records": records, "revision": revision})
    rec_rows = []
    for r in records:
        date_cell = esc(r["date"] or "")
        if r["will_skip"]:
            date_cell += ' <span class="pill bad">missing or invalid required date/reference/currency — row will be skipped, never invented</span>'
        elif r["date_missing"]:
            date_cell += ' <span class="pill warn">missing — stays unknown (invoice)</span>'
        if r["duplicate_warning"]:
            dup_cell = '<span class="pill warn">' + esc(r["duplicate_warning"]) + "</span>"
        else:
            dup_cell = "<span class='mut'>—</span>"
        if r['possible_duplicates']:
            dup_cell += '<div class="warn">Possible different-ID duplicate: '+esc('; '.join(w['id']+': '+w['reason'] for w in r['possible_duplicates']))+'</div>'
            dup_cell += f"<label><input form='csv-confirm-form' type='checkbox' name='skip_row_{r['row']}' value='yes'> Skip this row before creation</label>"
        rec_rows.append(
            f"<tr><td>{r['row']}</td><td>{esc(r['record_type'])}</td>"
            f"<td>{esc(r['reference'])}</td>"
            f"<td>{money(r['amount_minor'], r['currency'])}</td>"
            f"<td>{date_cell}</td>"
            f"<td>{esc(r['counterparty'])}</td>"
            f"<td>{dup_cell}</td></tr>")
    rec_rows = "".join(rec_rows)
    prob_list = "".join(f"<li class='warn'>{esc(p)}</li>" for p in problems)
    body = f"""
<h1>Confirm CSV import</h1>
<div class="card"><p class="mut">{len(records)} records parsed from
{esc(staged['filename'])}. Nothing has touched the ledger yet — confirm to
create them with source <span class="pill">owner_csv_import</span>.</p>
<p>For receipts, counterparty is the exact existing invoice ID. Currency must match that invoice. Missing/unknown references are never guessed. For invoices, date is expected collection; optional due_date is contractual and stays unknown when absent. Optional invoice_reference is an external source reference.</p>
<p>Potential invoice/bill duplicates compare existing records and other preview rows. Suggestions are not proof. Skip a flagged row explicitly or acknowledge the source checks for rows you keep. Nothing is merged/deleted automatically.</p>
{"<ul>" + prob_list + "</ul>" if prob_list else ""}
<table><thead><tr><th>Row</th><th>Type</th><th>Reference</th><th>Amount</th>
<th>Date</th><th>Counterparty</th><th>Duplicate check</th></tr></thead>
<tbody>{rec_rows}</tbody></table>
<form id="csv-confirm-form" method="post" action="/ledger/import/confirm">{csrf_field()}
<input type="hidden" name="token" value="{token}">
{'<label><input type="checkbox" name="acknowledge_duplicates" value="yes"> I compared sources for flagged rows I am keeping and confirm they are distinct records.</label>' if any(r['possible_duplicates'] for r in records) else ''}
<button class="primary" type="submit">Confirm reviewed import — skipped rows omitted</button>
<a class="btn" href="/ledger">Cancel</a></form></div>"""
    return handler._send_html(200, page("Confirm CSV import", body, active="/ledger"))


def _extract_text_from_pdf(data: bytes) -> str:
    """Best-effort text extraction from uncompressed PDF content streams.

    Handles literal strings in Tj/TJ operators. Compressed or encrypted PDFs
    yield nothing — reported as unsupported, never guessed at.
    """
    texts: list[str] = []
    # Find stream ... endstream blocks and look for (text) Tj / [(text)] TJ.
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        block = m.group(1)
        if block[:2] == b"x\x9c" or block.strip().startswith(b"e\n"):
            continue  # deflated/encrypted — skip honestly
        for tm in re.finditer(rb"\((?:[^()\\]|\\.)*\)\s*Tj", block):
            raw = tm.group(0)
            s = raw[:raw.rfind(b")") + 1]
            try:
                txt = s[1:-1].decode("latin-1")
                txt = re.sub(r"\\(.)", r"\1", txt)
                if txt.strip():
                    texts.append(txt.strip())
            except Exception:
                continue
    return " ".join(texts)


@route("POST", "/ledger/import/document")
def import_document_upload(handler: Handler, req: Request):
    up = req.files.get("file")
    if not up:
        raise CashflowError("no file uploaded")
    ext = _ext_of(up["filename"])
    if ext not in ALLOWED_DOC_EXTENSIONS:
        raise CashflowError(
            f"invalid file type {up['filename']!r}: expected one of "
            f"{sorted(ALLOWED_DOC_EXTENSIONS)}")
    if len(up["data"]) > MAX_UPLOAD_BYTES:
        raise CashflowError("file exceeds the 1 MiB upload limit")
    if ext == ".pdf":
        text = _extract_text_from_pdf(up["data"])
        method = "pdf text extraction (best effort)"
    else:
        try:
            text = up["data"].decode("utf-8-sig")
        except Exception:
            raise CashflowError("text files must be UTF-8 encoded")
        method = "plain text"
    digest = hashlib.sha256(up["data"]).hexdigest()
    if not text.strip():
        return handler._send_html(200, page(
            "Document not readable",
            f"<h1>Document upload — unsupported</h1><div class='card'>"
            f"<p class='warn'>No extractable text was found in "
            f"{esc(up['filename'])} ({esc(method)}). Nothing was stored and "
            f"no fields were invented. Try a .txt or .csv export instead.</p>"
            f"<p><a class='btn' href='/ledger'>Back to ledger</a></p></div>",
            active="/ledger"))
    # Naive field spotting — every field is shown for confirmation.
    amounts = re.findall(r"(?:USD|EUR|GBP)\s*([\d,]+\.\d{2})", text)
    dates = re.findall(r"\b(20\d\d-[01]\d-[0-3]\d)\b", text)
    token = _stage({"kind": "document", "filename": up["filename"],
                    "digest": digest, "text": text[:4000],
                    "amounts": amounts[:10], "dates": dates[:10]})
    amt_list = "".join(f"<li>{esc(a)}</li>" for a in amounts[:10]) or \
        "<li class='warn'>none found — will stay missing, never invented</li>"
    date_list = "".join(f"<li>{esc(d)}</li>" for d in dates[:10]) or \
        "<li class='warn'>none found — will stay missing, never invented</li>"
    preview = esc(text[:1200])
    body = f"""
<h1>Confirm document import</h1>
<div class="card"><p class="mut">File: {esc(up['filename'])} — extracted via
{esc(method)}. The full content is <b>not</b> stored; only a sha256 hash
(<span class="mut">{digest[:16]}…</span>) and the fields you confirm below are
kept, with source <span class="pill">owner_document_upload</span>.</p>
<h3>Spotted amounts</h3><ul>{amt_list}</ul>
<h3>Spotted dates (ISO)</h3><ul>{date_list}</ul>
<h3>Extracted text (preview)</h3><pre class="dump">{preview}</pre>
<form method="post" action="/ledger/import/document/confirm">{csrf_field()}
<input type="hidden" name="token" value="{token}">
<div class="row">
<div><label>Record as <select name="doc_kind">
<option value="invoice">invoice evidence</option>
<option value="counterparty_message">counterparty message</option>
<option value="other">other evidence</option></select></label></div>
<div><label>Summary (your words) <input name="summary" size="40" required
placeholder="e.g. Client invoice INV-2026-042, USD 8,000"></label></div>
</div>
<button class="primary" type="submit">Confirm — store evidence (not the content)</button>
<a class="btn" href="/ledger">Cancel</a></form></div>"""
    return handler._send_html(200, page("Confirm document import", body, active="/ledger"))


@route("POST", "/ledger/import/document/confirm")
def import_document_confirm(handler: Handler, req: Request):
    staged = _pop_staged(req.field("token"))
    if not staged or staged["kind"] != "document":
        raise CashflowError("import session expired; upload the document again")
    summary = req.field("summary").strip()
    if not summary:
        raise CashflowError("a summary is required")
    doc_kind = req.field("doc_kind")

    def do(ledger: Ledger):
        ctx = _owner_ctx(ledger)
        authz_mod.require_access(ctx, ledger)  # direct ledger mutation below
        eid = f"ev-doc-{staged['digest'][:12]}"
        if eid in ledger.evidence:
            return {"duplicate": True, "evidence_id": eid}
        # The document text is scanned at the boundary (heuristic) but is
        # never stored; only the hash and the confirmed fields are kept.
        probe = Document(id=f"doc-{staged['digest'][:12]}", kind="upload",
                         content=staged["text"], source="owner_upload")
        actions_mod.require_trustworthy([probe])
        ledger.evidence[eid] = Evidence(
            id=eid, kind=f"owner_document_{doc_kind}",
            summary=f"{summary} [source file {staged['filename']}, "
                    f"sha256 {staged['digest'][:16]}…; content not retained]")
        ledger.log_audit("document_imported", "owner",
                         f"{eid}: {summary[:80]}")
        return {"duplicate": False, "evidence_id": eid}
    mutate_state(do)
    return handler._redirect("/ledger")


@route("POST", "/ledger/import/confirm")
def import_confirm(handler: Handler, req: Request):
    staged = _pop_staged(req.field("token"))
    if not staged:
        raise CashflowError("import session expired; upload the file again")

    def do(ledger: Ledger):
        ctx = _owner_ctx(ledger)
        authz_mod.require_access(ctx, ledger)  # direct ledger mutation below
        kind = staged["kind"]
        if kind == "fixture":
            fresh = Ledger.from_fixture(staged["data"])
            from .recovery import create_backup
            create_backup(database_path(), 'Automatic before fixture replacement', ctx=ctx)
            # Replace ledger contents in place.
            ledger.__dict__.clear()
            ledger.__dict__.update(fresh.__dict__)
            ledger.log_audit("fixture_imported", "owner",
                             f"ledger replaced from {staged['filename']}")
            return {"kind": "fixture"}
        if kind == "csv-confirm":
            from .owner_workflows import review_revision
            if staged.get('revision') and staged['revision'] != review_revision(ledger):
                raise CashflowError('Ledger changed after CSV preview. Upload and review again before import.')
            allowed={'csrf','token','acknowledge_duplicates'} | {f"skip_row_{r['row']}" for r in staged['records']}
            if set(req.form)-allowed:raise CashflowError('Unexpected CSV confirmation fields; review the import form.')
            for row in staged['records']:
                if row.get('possible_duplicates') and not row['duplicate_warning'] and not row['will_skip'] and req.field(f"skip_row_{row['row']}")!='yes' and req.field('acknowledge_duplicates')!='yes':
                    raise CashflowError('Explicitly skip possible duplicate rows or acknowledge the source review before keeping them.')
            created = {"invoice": 0, "bill": 0, "receipt": 0}
            skipped = 0
            for r in staged["records"]:
                if r["will_skip"] or req.field(f"skip_row_{r['row']}")=='yes':
                    skipped += 1
                    continue
                ref, ccy = r["reference"], r["currency"]
                evid = f"ev-csv-{ref}"
                if evid not in ledger.evidence:
                    ledger.evidence[evid] = Evidence(
                        evid, "owner_csv_import",
                        f"CSV import: {r['record_type']} {ref} "
                        f"({r['counterparty'] or 'no counterparty'})".strip())
                if r["record_type"] == "invoice":
                    if ref in ledger.receivables:
                        skipped += 1
                        continue
                    from .models import Receivable
                    ledger.receivables[ref] = Receivable(
                        id=ref, currency=ccy, original_minor=r["amount_minor"],
                        expected_date=date.fromisoformat(r["date"]) if r["date"] else None,
                        evidence_id=evid,due_date=date.fromisoformat(r['due_date']) if r.get('due_date') else None,
                        counterparty=r['counterparty'],invoice_reference=r.get('invoice_reference',''))
                    created["invoice"] += 1
                elif r["record_type"] == "bill":
                    if ref in ledger.outflows:
                        skipped += 1
                        continue
                    from .models import Outflow
                    ledger.outflows[ref] = Outflow(
                        id=ref, currency=ccy, amount_minor=r["amount_minor"],
                        date=date.fromisoformat(r["date"]) if r["date"] else ledger.as_of,
                        protected=False, evidence_id=evid,counterparty=r['counterparty'],invoice_reference=r.get('invoice_reference',''))
                    created["bill"] += 1
                else:  # receipt
                    if ref in ledger.receipts:
                        skipped += 1
                        continue
                    invoice = ledger.receivables.get(r['counterparty'])
                    if invoice is None or invoice.currency != ccy:
                        raise CashflowError('Receipt invoice/currency changed after preview. Upload and review again.')
                    ledger.record_receipt(Receipt(
                        id=ref, receivable_id=invoice.id,
                        amount_minor=r["amount_minor"],
                        date_received=date.fromisoformat(r["date"]) if r["date"] else ledger.as_of,
                        state="observed", evidence_id=evid, actor="owner"),
                        actor="owner", ctx=ctx,acknowledge_duplicates=req.field('acknowledge_duplicates')=='yes')
                    created["receipt"] += 1
            ledger.log_audit("csv_imported", "owner",
                             f"created={created} skipped_duplicates={skipped}")
            return {"kind": "csv", "created": created, "skipped": skipped}
        raise CashflowError(f"unknown staged import kind {kind!r}")
    mutate_state(do)
    return handler._redirect("/ledger")


# ---------------------------------------------------------------------------
# Plan Comparison
# ---------------------------------------------------------------------------


@route('GET', '/stress')
def stress_page(handler: Handler, req: Request):
    def build(ledger):
        invoices = [r for r in ledger.receivables.values() if r.remaining_minor > 0]
        from .settlement import collection_held
        invoices = [r for r in invoices if not collection_held(r)]
        rid = req.query.get('receivable_id', [invoices[0].id if invoices else ''])[0]
        delay_text = req.query.get('delay_days', ['7'])[0]
        if not re.fullmatch(r'[0-9]{1,2}', delay_text):
            raise CashflowError('Delay must be a whole number from 0 to 30 days.')
        delay = int(delay_text)
        options = ''.join(
            f"<option value='{esc(r.id)}' {'selected' if r.id == rid else ''}>"
            f"{esc(r.id)} — {money(r.remaining_minor, r.currency)}"
            f"{' (expected date missing)' if r.expected_date is None else ''}</option>"
            for r in invoices)
        form = ("<div class='card'><h2>Choose a hypothetical delay</h2>"
            "<form method='get' action='/stress'><div class='row'>"
            "<div><label>Invoice<select name='receivable_id' required>"
            f"{options}</select></label></div><div><label>Delay in days"
            f"<input type='number' name='delay_days' min='0' max='30' step='1' value='{delay}' required>"
            "</label></div><div><button class='primary'>Run stress test</button></div>"
            "</div></form></div>")
        intro = ("<h1>Cash-flow Stress Test</h1>"
            "<p><span class='pill sim'>Hypothetical scenario — deterministic calculation</span></p>"
            "<p>Shift one invoice's expected collection date. Recorded evidence, actual cash, "
            "payment dates and approvals stay unchanged. This is not a payment prediction.</p>")
        if not invoices:
            return page('Stress Test', intro + "<div class='card'>No eligible outstanding invoice is available. Held collections remain excluded. <a href='/holds'>Review holds</a>.</div>", active='/stress')
        selected = ledger.receivables.get(rid)
        if selected is not None and selected.expected_date is None and 0 <= delay <= 30:
            return page('Stress Test', intro + form +
                "<div class='card'><h2>Expected collection date missing</h2>"
                "<p>Confirm the expected date in the source data before testing a delay. "
                "No date has been assumed.</p><a href='/ledger'>Review evidence</a></div>", active='/stress')
        result = stress_test(ledger, rid, delay, ctx=_owner_ctx(ledger))
        presets = ' '.join(
            f"<a class='btn' href='/stress?{esc(urlencode({'receivable_id': rid, 'delay_days': n}))}'>"
            f"{'No added delay' if n == 0 else str(n) + ' days later'}</a>" for n in (0, 7, 14))
        evidence = evidence_link(ledger, result.evidence_id)
        horizon_end = result.scenario.currencies[result.currency].days[-1].date
        outside = ("<p class='warn'>The assumed collection is outside the forecast window. "
                   "It contributes no expected inflow inside this window.</p>"
                   if result.assumed_date > horizon_end else '')
        summary = ("<div class='card'><h2>Scenario assumptions</h2>"
            f"<div class='kv stack'><span>Selected invoice and remaining amount</span><b>{esc(rid)} — "
            f"{money(result.remaining_minor, result.currency)}</b></div>"
            f"<div class='kv'><span>Recorded expected date</span><b>{result.recorded_date}</b></div>"
            f"<div class='kv'><span>Assumed expected date (+{delay} days)</span><b>{result.assumed_date}</b></div>"
            f"<p class='mut'>Evidence: {evidence}. As of {ledger.as_of}; "
            f"forecast through {horizon_end}. Amounts and observed receipts are unchanged.</p>{outside}"
            f"<div class='row'>{presets}</div></div>")
        cards = []
        for ccy, scenario in result.scenario.currencies.items():
            baseline = result.baseline.currencies[ccy]
            columns = [
                ('Minimum projected balance', money(baseline.min_balance_minor, ccy), money(scenario.min_balance_minor, ccy)),
                ('First floor-breach date', iso(baseline.first_breach_date), iso(scenario.first_breach_date)),
                ('Worst shortfall vs floor', money(baseline.worst_shortfall_minor, ccy), money(scenario.worst_shortfall_minor, ccy)),
                ('Closing projected balance', money(baseline.closing_minor, ccy), money(scenario.closing_minor, ccy)),
            ]
            rows = ''.join(f'<tr><th>{label}</th><td>{old}</td><td>{new}</td></tr>' for label, old, new in columns)
            cards.append(f"<div class='card'><h2>{esc(ccy)} — baseline vs scenario</h2>"
                f"<p>Cash floor: {money(scenario.floor_minor, ccy)}. "
                "Currencies remain separate; display-only FX funds no obligation.</p>"
                "<div class='grid'>"
                f"<div class='kv stack'><span>Scenario minimum</span><b>{money(scenario.min_balance_minor, ccy)}</b></div>"
                f"<div class='kv stack'><span>Scenario first breach</span><b>{iso(scenario.first_breach_date)}</b></div>"
                f"<div class='kv stack'><span>Scenario shortfall vs floor</span><b>{money(scenario.worst_shortfall_minor, ccy)}</b></div></div>"
                "<table><tr><th>Measure</th><th>Recorded terms forecast</th><th>Hypothetical delay</th></tr>"
                f"{rows}</table><div class='grid'>"
                f"<div><h3>Recorded terms forecast</h3>{forecast_svg(baseline)}</div>"
                f"<div><h3>Hypothetical delay forecast</h3>{forecast_svg(scenario)}</div></div>"
                f"<details><summary>Daily scenario data — {esc(ccy)}</summary>{forecast_table(scenario)}</details></div>")
        assumptions = ''.join(f'<li>{esc(a)}</li>' for a in result.scenario.assumptions)
        clarifications = ''.join(f"<li>{esc(c['question'])}</li>" for c in result.scenario.clarifications)
        return page('Stress Test', intro + form + summary + ''.join(cards) +
            f"<div class='card'><h2>Assumptions and missing facts</h2><ul>{assumptions}{clarifications}</ul>"
            "<p>These results use one selected delay. Other invoices retain their recorded dates. "
            "Protected outflows are never rescheduled by this test.</p>"
            "<a href='/plans'>Compare response plans under the recorded ledger</a> "
            "(the stress scenario is not applied to those plans).</div>", active='/stress')
    return handler._send_html(200, read_state(build))


def _plan_draft_link(ledger, plan_id):
    try:
        preview = plan_draft_preview(ledger, plan_id, ctx=_owner_ctx(ledger))
    except CashflowError as exc:
        return f"<p class='mut'>Draft requests unavailable: {esc(str(exc))}</p>"
    return (f"<p><a class='btn primary' href='/plans/{esc(plan_id)}/draft'>"
            f"Review {len(preview['drafts'])} draft request(s)</a></p>"
            "<p class='mut'>Review terms first. Draft creation does not make this plan feasible "
            "or establish approval, consent or payment.</p>")


@route('GET', '/plans/<plan_id>/draft')
def plan_review_page(handler: Handler, req: Request, plan_id: str):
    def build(ledger):
        preview = plan_draft_preview(ledger, plan_id, ctx=_owner_ctx(ledger))
        cards = []
        for i, spec in enumerate(preview['drafts'], 1):
            target = spec.get('receivable_id') or spec.get('outflow_id')
            cards.append(f"<div class='card'><h2>Draft {i}: {esc(ACTION_KINDS[spec['kind']])}</h2>"
                f"<div class='kv stack'><span>Target invoice / bill</span><b>{esc(target)}</b></div>"
                f"<div class='kv'><span>Proposed amount</span><b>{money(spec['amount_minor'], spec['currency'])}</b></div>"
                f"<div class='kv'><span>Proposed date</span><b>{spec['date']}</b></div>"
                "<p>State after creation: <span class='pill'>DRAFT</span></p></div>")
        reasons = ''.join(f'<li>{esc(r)}</li>' for r in preview['reasons'])
        assumptions = ''.join(f'<li>{esc(a)}</li>' for a in preview['assumptions'])
        sources = ', '.join(evidence_link(ledger, eid) for eid in preview['evidence_ids'])
        status = 'Conditionally feasible under plan assumptions' if preview['feasible'] else 'Not feasible under current evidence / constraints'
        if preview.get('arithmetic_only'):
            status = ('Conditionally sufficient calculation — permissions/payment pending' if preview['feasible']
                else 'Insufficient or incomplete under the current assumptions')
        body = ("<h1>Review plan draft requests</h1>"
            f"<p>{esc(preview['name'])}</p><div class='card'><h2>{esc(status)}</h2>"
            f"<ul>{reasons}</ul><h3>Assumptions</h3><ul>{assumptions}</ul>"
            f"<p>Source evidence: {sources}</p>"
            "<p class='warn'>Creating draft requests does not resolve the feasibility conditions. "
            "Supplier acceptance must still be recorded separately. Payroll remains protected.</p></div>"
            + ''.join(cards) + "<div class='card'><h2>Create drafts for owner review</h2>"
            "<p>Check the targets, amounts and dates above. After creation you can edit "
            "draft terms in Action History, then approve each action separately. "
            "No request is sent and no cash or recorded payment schedule changes here.</p>"
            f"<form method='post' action='/plans/{esc(plan_id)}/draft'>{csrf_field()}"
            f"<input type='hidden' name='revision' value='{preview['revision']}'>"
            f"<button class='primary' type='submit'>Create {len(preview['drafts'])} draft request(s)</button></form>"
            "<p><a href='/plans'>Back to Plan Comparison</a></p></div>")
        return page('Review plan drafts', body, active='/plans')
    return handler._send_html(200, read_state(build))


@route('POST', '/plans/<plan_id>/draft')
def plan_create_drafts(handler: Handler, req: Request, plan_id: str):
    if set(req.form) - {'csrf', 'revision'}:
        raise CashflowError('Draft terms come from the reviewed server plan. Reopen review to change the plan.')
    result = mutate_state(lambda ledger: create_plan_drafts(
        ledger, plan_id, req.field('revision'), ctx=_owner_ctx(ledger)))
    return handler._redirect('/actions#' + result['action_ids'][0])

def _plan_detail_html(ledger: Ledger, plan, result) -> str:
    if result.feasible:
        feas = "<span class='pill ok'>feasible under hypothetical terms</span>"
    else:
        feas = "<span class='pill bad'>infeasible</span>"
    changes = []
    for pr in plan.projected_receipts:
        rec = ledger.receivables.get(pr.receivable_id)
        ccy = rec.currency if rec else ledger.base_currency
        changes.append(
            f"<div class='kv'><span>Projected receipt "
            f"<span class='pill sim'>conditional — not cash</span><br>"
            f"<span class='mut'>{esc(pr.label)}</span></span>"
            f"<b>{money(pr.amount_minor, ccy)} on {pr.date.isoformat()}</b></div>")
    for amd in plan.amendments:
        out = ledger.outflows.get(amd.outflow_id)
        if out is None:
            continue
        if amd.new_date is not None:
            what = f"move to {amd.new_date.isoformat()}"
        else:
            what = "split: " + ", ".join(
                f"{money(m, out.currency)} on {d.isoformat()}"
                for d, m in (amd.splits or []))
        consent_state = ""
        if out.counterparty_consent_required_for_change:
            if amd.consent_evidence_id and amd.consent_evidence_id in ledger.runtime_evidence_ids:
                consent_state = (f"<br>Consent: <span class='pill ok'>explicit acceptance "
                                 f"recorded</span> {esc(amd.consent_evidence_id)}")
            else:
                consent_state = ("<br>Consent: <span class='pill warn'>missing — "
                                 "no explicit acceptance event</span>")
        if amd.approval_evidence_id:
            consent_state += (f"<br>Approval evidence: "
                              f"{evidence_link(ledger, amd.approval_evidence_id)}")
        changes.append(
            f"<div class='kv'><span>Amend {esc(amd.outflow_id)} "
            f"(currently {iso(out.date)})<br><span class='mut'>{esc(what)}</span>"
            f"{consent_state}</span>"
            f"<b>{money(out.amount_minor, out.currency)}</b></div>")
    if not changes:
        changes.append("<p class='mut'>No dated changes — recorded terms kept.</p>")

    permissions = []
    for amd in plan.amendments:
        out = ledger.outflows.get(amd.outflow_id)
        if out is None:
            continue
        if out.counterparty_consent_required_for_change:
            permissions.append(
                "Counterparty acceptance via an explicit runtime event "
                "(owner approval alone is insufficient)")
        elif out.maximum_deferrable_minor:
            permissions.append(
                f"Owner approval (up to {money(out.maximum_deferrable_minor, out.currency)} "
                f"deferrable)")
    if plan.projected_receipts:
        permissions.append("Client agreement to the advance (simulated counterparty)")
    perm_list = "".join(f"<li>{esc(p)}</li>" for p in permissions) or "<li>None.</li>"

    metrics = (
        f"<div class='kv'><span>Altered obligations</span><b>{result.altered_obligations}</b></div>"
        f"<div class='kv'><span>Direct cost</span><b>{money(result.direct_cost_minor, ledger.base_currency)}</b></div>"
        f"<div class='kv'><span>Total date shift (days)</span><b>{result.total_date_shift_days}</b></div>"
        f"<div class='kv'><span>Conditional minimum balance</span>"
        f"<b>{money(result.min_balance_minor, ledger.base_currency) if result.forecast else '—'}</b></div>"
        f"<div class='kv'><span>Conditional closing balance</span>"
        f"<b>{money(result.closing_minor, ledger.base_currency) if result.forecast else '—'}</b></div>")

    failure_notes = {
        "plan-a": ("Fails if the client does not pay the USD 5,000 advance by "
                   "22 October 2026, or pays less. The forecast is conditional on "
                   "collection — actual cash is unchanged until a receipt is recorded."),
        "plan-b-supplier-only": ("Fails without the supplier's explicit acceptance, "
                   "and is insufficient even with it: the minimum stays at USD 2,500, "
                   "below the USD 3,000 floor."),
        "plan-b": ("Fails if the supplier rejects the move, or the owner does not "
                   "approve the USD 500 ops deferral. Recorded terms change only "
                   "through the Action History workflow."),
        "plan-advance-capped": ("Insufficient by design: a USD 4,000 advance alone "
                   "leaves the minimum at USD 2,000, missing the floor by USD 1,000."),
    }
    failure = failure_notes.get(plan.id, "See assumptions.")
    reasons = "".join(f"<li>{esc(r)}</li>" for r in result.reasons)
    assumptions = "".join(f"<li>{esc(a)}</li>" for a in plan.conditional_labels)
    if result.forecast:
        assumptions += "".join(f"<li>{esc(a)}</li>" for a in result.forecast.assumptions)

    return f"""
<div class="card"><h2>{esc(plan.name)} {feas}</h2>
<p class="mut">{esc(plan.description)}</p>
<h3>What the plan changes</h3>{"".join(changes)}
<h3>Required actions &amp; permissions</h3><ul>{perm_list}</ul>
<h3>Cost, disruption &amp; conditional balances</h3>{metrics}
<h3>Evaluation reasons</h3><ul>{reasons}</ul>
<h3>Assumptions</h3><ul>{assumptions}</ul>
<h3>When this plan fails</h3><p>{esc(failure)}</p>
{_plan_draft_link(ledger, plan.id)}</div>"""


@route("GET", "/plans/examples")
def example_plans_page(handler: Handler, req: Request):
    from .models import OwnerConstraints

    def build(ledger: Ledger):
        if not {'supplier-001', 'ops-001'} <= ledger.outflows.keys() or 'inv-client-001' not in ledger.receivables:
            return page('Fixture examples', "<h1>Fixture examples unavailable</h1><p>These examples require the original synthetic fixture. <a href='/plans'>Plan from your current records</a>.</p>", active='/plans')
        consent_id = ledger.consent_evidence_id("supplier-001")
        recorded = forecast(ledger, ledger.as_of, ledger.horizon_days)
        r_usd = recorded.currencies.get(ledger.base_currency)
        plans = [
            ("Recorded forecast (current terms)", None),
            ("Plan A", build_plan_a()),
            ("Plan B variants", build_plan_b_supplier_only(consent_id)),
            ("Plan B full", build_plan_b_full(consent_id)),
            ("Insufficient response", build_advance_plan(
                400_000,
                # show the capped advance under the infeasible constraint set
            )),
        ]
        cards = []
        if r_usd is not None:
            cards.append(f"""
<div class="card"><h2>Recorded forecast — current terms
<span class="pill">actual schedule</span></h2>
<div class="kv"><span>Lowest projected balance</span>
<b>{money(r_usd.min_balance_minor, r_usd.currency)} on {r_usd.min_balance_date.isoformat()}</b></div>
<div class="kv"><span>First floor-breach date</span>
<b>{r_usd.first_breach_date.isoformat() if r_usd.first_breach_date else "none"}</b></div>
<div class="kv"><span>Worst shortfall vs floor</span>
<b>{money(r_usd.worst_shortfall_minor, r_usd.currency)}</b></div>
<div class="kv"><span>Closing balance</span>
<b>{money(r_usd.closing_minor, r_usd.currency)}</b></div>
<p class="mut">This is the forecast under recorded terms — what is actually
scheduled. Plans below are hypothetical until executed through Action History.</p></div>""")
        infeasible_constraints = OwnerConstraints(
            max_advance_minor=400_000, allow_supplier_date_change=False,
            allow_ops_deferral=False)
        for label, plan in plans[1:]:
            if plan.id == "plan-advance-capped":
                result = evaluate_plan(ledger, plan, infeasible_constraints)
            else:
                result = evaluate_plan(ledger, plan, ledger.constraints)
            cards.append(_plan_detail_html(ledger, plan, result))
        consent_note = (
            f"<p><span class='pill ok'>explicit acceptance recorded</span> "
            f"{esc(consent_id)} — supplier date changes are authorized by this event.</p>"
            if consent_id else
            "<p><span class='pill warn'>no supplier acceptance recorded</span> — "
            "Plan B variants are infeasible until the counterparty accepts via the "
            "Action History workflow. Consent is never pre-granted.</p>")
        body = f"""
<h1>Plan Comparison</h1>
<div class="card"><h2>Supplier consent status</h2>{consent_note}
<p class="mut">Feasibility under hypothetical terms is not authorization to
execute. A proposed supplier extension is shown conditionally; recorded terms
change only through the Action History workflow (explicit acceptance event).</p></div>
{"".join(cards)}"""
        return page("Plan Comparison", body, active="/plans")
    return handler._send_html(200, read_state(build))


@route('GET', '/plans')
def plans_page(handler: Handler, req: Request):
    def build(ledger):
        proposals = generate_proposals(ledger, ctx=_owner_ctx(ledger))
        current = forecast(ledger, ledger.as_of, ledger.horizon_days)
        cards = []
        for proposal in proposals:
            verdict = ('Conditionally sufficient — permissions and payment still pending'
                if proposal.financially_sufficient else 'Insufficient or incomplete — no feasible plan from this strategy')
            changes = ''.join(f"<li>{esc(ACTION_KINDS[s['kind']])}: <b>{esc(s.get('receivable_id') or s.get('outflow_id'))}</b> — "
                f"{money(s['amount_minor'], s['currency'])} on {esc(s['date'])}</li>" for s in proposal.drafts)
            balances = ''.join(f"<div class='kv stack'><span>{esc(ccy)}: conditional minimum / remaining shortfall</span>"
                f"<b>{money(curve.min_balance_minor, ccy)} / {money(curve.worst_shortfall_minor, ccy)}</b></div>"
                for ccy, curve in proposal.forecast.currencies.items())
            notes = ''.join(f'<li>{esc(n)}</li>' for n in proposal.blockers + proposal.notes)
            permissions = ''.join(f'<li>{esc(n)}</li>' for n in proposal.pending_permissions)
            sources = ', '.join(evidence_link(ledger, eid) for eid in proposal.evidence_ids)
            conflicts = overlapping_actions(ledger, proposal.drafts)
            if conflicts:
                link = '<p>Review existing requests first: ' + ', '.join(
                    f"<a href='/actions#{esc(aid)}'>{esc(aid)}</a>" for aid in conflicts) + '. No overlapping batch will be created.</p>'
            elif proposal.drafts and not proposal.blockers:
                link = (f"<p><a class='btn primary' href='/plans/{proposal.id}/draft'>"
                    f"Review {len(proposal.drafts)} draft request(s)</a></p>")
            else:
                link = '<p>No new draft available; confirm missing inputs or review the remaining shortfall.</p>'
            daily = ''.join(f"<h3>{esc(ccy)} — hypothetical daily balances</h3>{forecast_table(curve)}"
                for ccy, curve in proposal.forecast.currencies.items())
            cards.append(f"<div class='card' id='{proposal.id}'><h2>{esc(proposal.name)}</h2>"
                f"<p class='{'ok' if proposal.financially_sufficient else 'warn'}'>{esc(verdict)}</p>"
                f"<ul>{changes or '<li>No eligible change.</li>'}</ul>{balances}"
                f"<ul>{notes}</ul><h3>Required next steps</h3><ul>{permissions or '<li>No new request proposed.</li>'}</ul>"
                f"<p>Source evidence: {sources or 'No source evidence available.'}</p>{link}"
                f"<details><summary>Inspect deterministic daily calculations</summary>{daily}</details></div>")
        baseline = ''.join(f"<div class='kv stack'><span>{esc(ccy)}: current minimum / first breach / shortfall</span>"
            f"<b>{money(c.min_balance_minor, ccy)} / {iso(c.first_breach_date)} / {money(c.worst_shortfall_minor, ccy)}</b></div>"
            for ccy, c in current.currencies.items())
        checked = lambda v: 'checked' if v else ''
        cap_inputs = []
        for ccy in sorted({a.currency for a in ledger.accounts.values()}):
            cap = advance_cap(ledger, ccy)
            value = format_amount(cap, ccy).split(' ', 1)[1].replace(',', '') if cap is not None else ''
            cap_inputs.append(f"<label>Maximum early collection — {esc(ccy)}<input name='advance_cap_{esc(ccy)}' inputmode='decimal' value='{value}' placeholder='blank = outstanding balance'></label>")
        policy = ("<div class='card'><h2>Owner planning constraints</h2>"
            "<p>Enter money amounts in the displayed currency (for example, 5000.00 USD). "
            "Each limit caps the total early collections in a proposed plan for that currency. "
            "Blank means the outstanding balances are the limit; zero disables advances. No currencies are pooled or converted.</p>"
            f"<form method='post' action='/plans/constraints'>{csrf_field()}"
            + ''.join(cap_inputs) +
            f"<label><input type='checkbox' name='allow_supplier_date_change' value='yes' {checked(ledger.constraints.allow_supplier_date_change)}> Consider supplier extension requests (acceptance still required)</label>"
            f"<label><input type='checkbox' name='allow_ops_deferral' value='yes' {checked(ledger.constraints.allow_ops_deferral)}> Consider owner-designated expense deferrals</label>"
            "<button type='submit'>Save owner constraints and recalculate</button></form></div>")
        return page('Plan Comparison', "<h1>Plan Comparison</h1>"
            f"<p>Plans from the current records, as of <b>{ledger.as_of.isoformat()}</b>. "
            "Every figure comes from the deterministic forecast.</p>"
            "<div class='card'><h2>Recorded forecast — current terms</h2>" + baseline + "</div>" + policy +
            "<div class='card'><h2>How these proposals work</h2>"
            "<p>Three bounded strategies: earlier collection, eligible bill deferral, and a combination. "
            "Collection is requested no later than the day before a breach. Bill deferral is limited to 14 days "
            "and the day after a recorded expected collection inside the forecast window. "
            "Previously amended/split bills need manual review. Protected payments stay fixed.</p>"
            "<p>These are heuristic proposals, not an exhaustive optimizer. A conditionally sufficient calculation "
            "does not make a supplier extension authorized. All collections remain uncertain until observed. "
            "Draft creation changes no financial records.</p></div>" + ''.join(cards) +
            "<p><a href='/today'>Review Today's Priorities</a> · <a href='/plans/examples'>Original fixture examples</a></p>", active='/plans')
    return handler._send_html(200, read_state(build))


@route('POST', '/plans/constraints')
def planning_constraints(handler: Handler, req: Request):
    def change(ledger):
        authz_mod.require_access(_owner_ctx(ledger), ledger)
        currencies = {a.currency for a in ledger.accounts.values()}
        if set(req.form) - {'csrf', 'allow_supplier_date_change', 'allow_ops_deferral'} - {'advance_cap_' + c for c in currencies}:
            raise CashflowError('Unknown owner constraint field.')
        caps = {}
        for ccy in currencies:
            raw = req.field('advance_cap_' + ccy).strip()
            if raw:
                if not re.fullmatch(r'[0-9]{1,12}(\.[0-9]{1,3})?', raw):
                    raise CashflowError('Advance limit must be a nonnegative money amount, or blank.')
                caps[ccy] = _parse_major_to_minor(raw, ccy)
        ledger.constraints.advance_caps_minor = caps
        ledger.constraints.max_advance_minor = caps.get(ledger.base_currency)
        ledger.constraints.allow_supplier_date_change = req.field('allow_supplier_date_change') == 'yes'
        ledger.constraints.allow_ops_deferral = req.field('allow_ops_deferral') == 'yes'
        ledger.log_audit('planning_constraints_updated', 'owner', 'Explicit owner planning policy updated; plans require review again.')
    mutate_state(change)
    return handler._redirect('/plans')


@route('GET', '/today')
def today_page(handler: Handler, req: Request):
    def build(ledger):
        items = attention_items(ledger, ctx=_owner_ctx(ledger))
        cards = []
        for item in items:
            amount = money(item['amount_minor'], item['currency']) if item['amount_minor'] is not None and item['currency'] else ''
            sources = ', '.join(evidence_link(ledger, eid) for eid in item['evidence_ids'])
            cards.append(f"<div class='card'><h2>{esc(item['title'])} <span class='pill {'bad' if item['urgency'] == 'critical' else 'warn'}'>{esc(item['urgency'])}</span></h2>"
                f"<div class='kv stack'><span>{esc(item['target'])} · {esc(item['date'] or 'Date to confirm')}</span><b>{amount}</b></div>"
                f"<p>{esc(item['why'])}</p><p><b>Next step:</b> {esc(item['next_step'])}</p>"
                f"<p>Evidence: {sources or 'Derived from the ledger record/workflow; confirm missing source evidence in the ledger.'}</p>"
                f"<a class='btn' href='{esc(item['href'])}'>Review related records</a></div>")
        return page("Today's Priorities", "<h1>Today's Priorities</h1>"
            f"<p><b>{len(items)} items</b> · Ledger as-of date: <b>{ledger.as_of.isoformat()}</b>. "
            "Today means this snapshot date, not the computer clock. No monitoring or external reminder is running.</p>"
            "<p>Urgent cash gaps and missing facts appear first, followed by approvals, responses and reconciliation. "
            "Items are recalculated from current records on every reload; resolving the underlying fact/state updates this list.</p>" +
            (''.join(cards) or "<div class='card'><p>No items flagged by these rules. This is not a guarantee that future cash is sufficient.</p></div>"), active='/today')
    return handler._send_html(200, read_state(build))


# ---------------------------------------------------------------------------
# Action History
# ---------------------------------------------------------------------------

ACTION_KINDS = {
    "request_early_payment": "Request early partial payment (client)",
    "request_supplier_extension": "Request supplier payment date change",
    "defer_ops_expense": "Defer owner-designated ops expense",
}

STATE_PILL = {
    "draft": "pill", "owner_approved": "pill warn", "sent": "pill",
    "counterparty_accepted": "pill ok", "counterparty_rejected": "pill bad",
    "payment_observed": "pill sim", "reconciled": "pill ok",
    "cancelled": "pill",
}


def _action_card_html(ledger: Ledger, action) -> str:
    pill = STATE_PILL.get(action.state, "pill")
    terms = "".join(
        f"<div class='kv'><span>{esc(k)}</span><b>{esc(v)}</b></div>"
        for k, v in action.terms.items())
    audit_rows = "".join(
        f"<tr><td>{esc(e['at'])}</td><td>{esc(e['event'])}</td>"
        f"<td>{esc(e['actor'])}</td><td>{esc(e.get('detail', ''))}</td>"
        f"<td>{esc(e.get('state', ''))}</td></tr>"
        for e in action.audit)
    ctr = ""
    if (action.state == 'draft' or (action.state == 'owner_approved' and action.kind != 'defer_ops_expense')) and stale_plan(ledger, action):
        ctr = "<p class='warn'>Ledger changed since this plan review. Recalculate and review current facts before proceeding. <a href='/plans'>Compare current plans</a>.</p>"
    if action.counterparty_response:
        r = action.counterparty_response
        verdict = "accepted" if r["accepted"] else "rejected"
        ctr += (f"<div class='kv'><span>Counterparty response "
               f"<span class='pill sim'>simulated</span></span>"
               f"<b>{verdict} — {esc(r['evidence_id'])}</b></div>")

    aid = action.id
    buttons = []
    if action.state == "draft":
        buttons.append(post_button(f'/actions/{aid}/cancel', 'Discard draft (owner)',
            confirm='Discard this unapproved draft? Its audit history will be kept.'))
        buttons.append(post_button(f"/actions/{aid}/approve", "Approve (owner)",
                                   cls="primary"))
        buttons.append(
            f"<details><summary>Change terms (invalidates approval)</summary>"
            f"<form method='post' action='/actions/{aid}/update-terms'>{csrf_field()}"
            f"<label>Amount (major units, e.g. 5000.00) "
            f"<input name='amount_major' placeholder='optional'></label>"
            f"<label>Date (YYYY-MM-DD) "
            f"<input name='date' placeholder='optional'></label>"
            f"<button type='submit'>Update terms</button></form></details>")
    if action.state == "owner_approved":
        if action.kind == 'defer_ops_expense':
            buttons.append('<p>The owner-approved expense schedule is recorded. No counterparty send or incoming receipt is needed.</p>')
        else:
            buttons.append(post_button(f"/actions/{aid}/send",
                                       "Send via sandbox outbox", cls="primary"))
    if action.state == "sent":
        buttons.append(post_button(f"/actions/{aid}/retry", "Retry reminder "
                                     "(no duplicate action)"))
        buttons.append(post_button(f"/actions/{aid}/respond",
                                   "Simulate: counterparty ACCEPTS",
                                   cls="primary",
                                   fields={"accepted": "true"}))
        buttons.append(post_button(f"/actions/{aid}/respond",
                                   "Simulate: counterparty REJECTS",
                                   fields={"accepted": "false"}))
    if (action.state == "counterparty_accepted"
            and action.kind == "request_early_payment"):
        buttons.append(
            f"<details open><summary>Record observed payment (simulated sandbox receipt)</summary>"
            f"<form method='post' action='/actions/{aid}/observe'>{csrf_field()}"
            f"<div class='row'>"
            f"<div><label>Receipt ID (unique) <input name='receipt_id' required "
            f"value='rcpt-{esc(aid)}'></label></div>"
            f"<div><label>Amount (major units) <input name='amount_major' required "
            f"placeholder='5000.00'></label></div>"
            f"<div><label>Date received <input name='date_received' required "
            f"value='{ledger.as_of.isoformat()}'></label></div>"
            f"</div><label><input type='checkbox' name='acknowledge_duplicates' value='yes'> I checked possible duplicate observations and confirm this is distinct; see Payment Warnings.</label><button class='primary' type='submit'>Record receipt</button></form></details>")
    if action.state == "payment_observed":
        buttons.append(post_button(f"/actions/{aid}/reconcile", "Reconcile",
                                   cls="primary"))
    if (action.state == 'counterparty_accepted'
            and action.kind == 'request_supplier_extension'):
        buttons.append('<p>Supplier acceptance records a date change, not a cash receipt.</p>')
    return f"""
<div class="card" id="{esc(aid)}"><h2>{esc(action.kind)} <span class="{pill}">{esc(action.state)}</span></h2>
<div class="kv"><span>Action ID</span><b>{esc(aid)}</b></div>
<div class="kv"><span>Terms version</span><b>v{action.version}
{"(approved v" + str(action.approved_version) + ")" if action.approved_version else "(not approved)"}</b></div>
<h3>Terms</h3>{terms}{ctr}
<div class="row">{"".join(buttons)}</div>
<details><summary>Audit trail ({len(action.audit)} events)</summary>
<table><thead><tr><th>At (UTC)</th><th>Event</th><th>Actor</th><th>Detail</th><th>State</th></tr></thead>
<tbody>{audit_rows}</tbody></table></details></div>"""


def _new_action_form_html() -> str:
    kind_opts = "".join(
        f"<option value='{k}'>{esc(v)}</option>" for k, v in ACTION_KINDS.items())
    return f"""
<div class="card"><h2>New draft action</h2>
<form method="post" action="/actions/create">{csrf_field()}
<div class="row">
<div><label>Action ID <input name="action_id" required
placeholder="act-advance-1" pattern="[A-Za-z0-9_-]+"></label></div>
<div><label>Kind <select name="kind">{kind_opts}</select></label></div>
</div>
<div class="row">
<div><label>Target invoice (for payment requests)
<input name="receivable_id" value="inv-client-001"></label></div>
<div><label>Target bill (for supplier / ops actions)
<input name="outflow_id" placeholder="supplier-001 / ops-001"></label></div>
</div>
<div class="row">
<div><label>Amount (major units) <input name="amount_major"
placeholder="5000.00"></label></div>
<div><label>Date (YYYY-MM-DD) <input name="date"
placeholder="2026-10-22"></label></div>
</div>
<p class="mut">Drafts never alter balances. Approval pins the terms version;
changing terms later invalidates the approval.</p>
<button class="primary" type="submit">Create draft</button></form></div>"""


@route("GET", "/actions")
def actions_page(handler: Handler, req: Request):
    def build(ledger: Ledger):
        cards = "".join(_action_card_html(ledger, a)
                        for a in ledger.actions.values())
        body = (f"<h1>Action History</h1>"
                f"<p class='mut'>Explicit persisted states: draft → owner_approved → "
                f"sent → counterparty_accepted / counterparty_rejected → "
                f"payment_observed → reconciled. Invalid transitions are rejected "
                f"server-side. The outbox is a labelled local sandbox — nothing is "
                f"really sent.</p>"
                f"{_new_action_form_html()}"
                f"{cards or '<div class=card><p class=mut>No actions yet.</p></div>'}")
        return page("Action History", body, active="/actions")
    return handler._send_html(200, read_state(build))


def _get_action(ledger: Ledger, action_id: str):
    action = ledger.actions.get(action_id)
    if action is None:
        raise KeyError(f"unknown action {action_id!r}")
    return action


@route("POST", "/actions/create")
def action_create(handler: Handler, req: Request):
    action_id = req.field("action_id").strip()
    kind = req.field("kind")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", action_id):
        raise CashflowError("action id must be alphanumeric/dash/underscore")
    if kind not in ACTION_KINDS:
        raise CashflowError(f"unknown action kind {kind!r}")
    terms: dict = {"kind": kind}
    if req.field("receivable_id").strip():
        terms["receivable_id"] = req.field("receivable_id").strip()
    if req.field("outflow_id").strip():
        terms["outflow_id"] = req.field("outflow_id").strip()
    if req.field("date").strip():
        try:
            terms["date"] = date.fromisoformat(req.field("date").strip()).isoformat()
        except ValueError:
            raise CashflowError("date must be ISO YYYY-MM-DD")

    def do(ledger: Ledger):
        ctx = _owner_ctx(ledger)
        if action_id in ledger.actions:
            raise CashflowError(f"action {action_id!r} already exists")
        if req.field('amount_major').strip():
            target = ledger.receivables.get(terms.get('receivable_id')) or ledger.outflows.get(terms.get('outflow_id'))
            currency = target.currency if target else ledger.base_currency
            terms['amount_minor'] = _parse_major_to_minor(req.field('amount_major'), currency)
            terms['currency'] = currency
        elif kind == 'request_supplier_extension':
            target = ledger.outflows.get(terms.get('outflow_id'))
            if target:
                terms['amount_minor'] = target.amount_minor
                terms['currency'] = target.currency
        action = actions_mod.create_action(
            action_id, kind, terms, actor="owner", ctx=ctx,
            business_id=ledger.business_id)
        ledger.actions[action_id] = action
        ledger.log_audit("action_created", "owner", f"{action_id} kind={kind}")
        return action_id
    mutate_state(do)
    return handler._redirect("/actions")


@route("POST", "/actions/<action_id>/approve")
def action_approve(handler: Handler, req: Request, action_id: str):
    def do(ledger: Ledger):
        ctx = _owner_ctx(ledger)
        action = _get_action(ledger, action_id)
        validate_current_request(ledger, action)
        actions_mod.approve(action, actor="owner", ctx=ctx)
        ledger.log_audit("action_approved", "owner",
                         f"{action_id} terms v{action.version}")
        # An owner-approved ops deferral amends the RECORDED payable terms
        # (the fixture permits deferral "after approval"); the original terms
        # and the approval evidence are retained on the outflow.
        if action.kind == "defer_ops_expense" and action.state == "owner_approved":
            out_id = action.terms.get("outflow_id", "ops-001")
            amount_minor = int(action.terms.get("amount_minor", 50_000))
            new_date = date.fromisoformat(action.terms.get("date", "2026-11-06"))
            out = ledger.outflows[out_id]
            kept = out.amount_minor - amount_minor
            approval_id = f'ev-owner-approval-{action.id}-v{action.version}'
            ledger.record_runtime_evidence(Evidence(approval_id, 'owner_approval',
                f'Owner explicitly approved action {action.id} version {action.version}.'), actor='owner', ctx=ctx)
            ledger.apply_recorded_amendment(
                out_id, actor="owner", evidence_id=approval_id,
                kind="owner_approved_deferral",
                splits=[(out.date, kept), (new_date, amount_minor)],
                detail=f"action {action_id} approved", ctx=ctx)
        return action_id
    mutate_state(do)
    return handler._redirect("/actions")


@route('POST', '/actions/<action_id>/cancel')
def action_cancel(handler: Handler, req: Request, action_id: str):
    def cancel(ledger):
        action = _get_action(ledger, action_id)
        actions_mod.cancel_draft(action, actor='owner', ctx=_owner_ctx(ledger))
        ledger.log_audit('draft_cancelled', 'owner', action_id)
    mutate_state(cancel)
    return handler._redirect('/today')


@route("POST", "/actions/<action_id>/update-terms")
def action_update_terms(handler: Handler, req: Request, action_id: str):
    def do(ledger: Ledger):
        ctx = _owner_ctx(ledger)
        action = _get_action(ledger, action_id)
        if action.state != 'draft':
            raise CashflowError('Only an unapproved draft can be edited in this sandbox UI.')
        terms = dict(action.terms)
        if req.field("amount_major").strip():
            currency = terms.get('currency', ledger.base_currency)
            terms["amount_minor"] = _parse_major_to_minor(
                req.field("amount_major"), currency)
            terms["currency"] = currency
            if 'amount' in terms:
                terms['amount'] = format_amount(terms['amount_minor'], currency)
        if req.field("date").strip():
            try:
                terms["date"] = date.fromisoformat(
                    req.field("date").strip()).isoformat()
            except ValueError:
                raise CashflowError("date must be ISO YYYY-MM-DD")
        actions_mod.update_terms(action, terms, actor="owner", ctx=ctx)
        ledger.log_audit("action_terms_updated", "owner",
                         f"{action_id} now v{action.version}; approval invalidated")
        return action_id
    mutate_state(do)
    return handler._redirect("/actions")


@route("POST", "/actions/<action_id>/send")
def action_send(handler: Handler, req: Request, action_id: str):
    def do(ledger: Ledger):
        ctx = _owner_ctx(ledger)
        action = _get_action(ledger, action_id)
        if action.kind != 'defer_ops_expense':
            validate_current_request(ledger, action)
        result = actions_mod.send(action, actor="owner", ctx=ctx)
        ledger.log_audit("action_sent_to_sandbox_outbox", "owner", action_id)
        return result
    mutate_state(do)
    return handler._redirect("/actions")


@route("POST", "/actions/<action_id>/retry")
def action_retry(handler: Handler, req: Request, action_id: str):
    def do(ledger: Ledger):
        ctx = _owner_ctx(ledger)
        action = _get_action(ledger, action_id)
        result = actions_mod.retry_reminder(action, actor="owner", ctx=ctx)
        ledger.log_audit("action_reminder_retry", "owner", action_id)
        return result
    mutate_state(do)
    return handler._redirect("/actions")


@route("POST", "/actions/<action_id>/respond")
def action_respond(handler: Handler, req: Request, action_id: str):
    accepted = req.field("accepted") == "true"

    def do(ledger: Ledger):
        ctx = _owner_ctx(ledger)
        action = _get_action(ledger, action_id)
        # The explicit acceptance event is created first so the evidence id
        # exists before the action references it.
        evidence_id = f"ev-sim-{action_id}-{'accept' if accepted else 'reject'}"
        if accepted and action.kind == "request_supplier_extension":
            out_id = action.terms.get("outflow_id", "supplier-001")
            new_date = date.fromisoformat(
                action.terms.get("date", "2026-11-06"))
            evidence_id = ledger.record_counterparty_acceptance(
                out_id, new_date, action_id, actor="sandbox_counterparty",
                ctx=ctx)
            # An accepted supplier proposal amends the RECORDED payable,
            # retaining the original terms and the acceptance evidence.
            ledger.apply_recorded_amendment(
                out_id, actor="sandbox_counterparty", evidence_id=evidence_id,
                kind="counterparty_accepted_move", new_date=new_date,
                detail=f"action {action_id} accepted", ctx=ctx)
        elif evidence_id not in ledger.evidence:
            ledger.record_runtime_evidence(Evidence(
                id=evidence_id, kind="synthetic_counterparty_message",
                summary=(f"Simulated sandbox response to action {action_id}: "
                         f"{'accepted' if accepted else 'rejected'}. "
                         f"Not a real commitment.")),
                actor="sandbox_counterparty", ctx=ctx)
        result = actions_mod.counterparty_respond(
            action, accepted, evidence_id, actor="sandbox_counterparty", ctx=ctx)
        ledger.log_audit("counterparty_response_simulated", "sandbox_counterparty",
                         f"{action_id} accepted={accepted}")
        return result
    mutate_state(do)
    return handler._redirect("/actions")


@route("POST", "/actions/<action_id>/observe")
def action_observe(handler: Handler, req: Request, action_id: str):
    receipt_id = req.field("receipt_id").strip()
    try:
        date_received = date.fromisoformat(req.field("date_received"))
    except ValueError:
        raise CashflowError("date_received must be ISO YYYY-MM-DD")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", receipt_id):
        raise CashflowError("receipt id must be alphanumeric/dash/underscore")

    def do(ledger: Ledger):
        ctx = _owner_ctx(ledger)
        action = _get_action(ledger, action_id)
        if action.kind != 'request_early_payment':
            raise CashflowError('only payment-request actions can record an incoming receipt')
        receivable_id = action.terms.get('receivable_id')
        if not receivable_id or receivable_id not in ledger.receivables:
            raise CashflowError('payment request requires an explicit valid invoice')
        target = ledger.receivables[receivable_id]
        amount_minor = _parse_major_to_minor(req.field('amount_major'), target.currency)
        receipt = Receipt(id=receipt_id, receivable_id=receivable_id,
                          amount_minor=amount_minor, date_received=date_received,
                          state="observed", evidence_id=target.evidence_id,
                          actor="owner")
        # Mutation boundary: caller authentication + business authorization
        # happen first (inside record_receipt), then trustworthy documents +
        # a valid approval for new executions.
        result = ledger.record_receipt(receipt, actor="owner",
                                       authorizing_action=action, ctx=ctx,acknowledge_duplicates=req.field('acknowledge_duplicates')=='yes')
        if result["changed"]:
            actions_mod.observe_payment(action, receipt_id, actor="owner", ctx=ctx)
            ledger.log_audit("payment_observed", "owner",
                             f"{action_id} receipt={receipt_id}")
        return result
    mutate_state(do)
    return handler._redirect("/actions")


@route("POST", "/actions/<action_id>/reconcile")
def action_reconcile(handler: Handler, req: Request, action_id: str):
    def do(ledger: Ledger):
        ctx = _owner_ctx(ledger)
        action = _get_action(ledger, action_id)
        actions_mod.reconcile(action, actor="owner", ctx=ctx)
        ledger.log_audit("action_reconciled", "owner", action_id)
        return action_id
    mutate_state(do)
    return handler._redirect("/actions")


# ---------------------------------------------------------------------------
# Labelled sandbox demo workflow (10 steps)
# ---------------------------------------------------------------------------

def _demo_steps(ledger: Ledger) -> list[dict]:
    checks = ledger.demo_checks
    beyond = {"owner_approved", "sent", "counterparty_accepted",
              "counterparty_rejected", "payment_observed", "reconciled"}
    states = {a.state for a in ledger.actions.values()}
    steps = [
        {"title": "Reset to the original fixture",
         "desc": "Seed a clean synthetic ledger.",
         "done": bool(checks.get("reset_done")) or fresh_seed_state(ledger),
         "widget": post_button("/demo/reset", "Reset demo", cls="danger",
                               confirm="Discard all demo state and reseed?")},
        {"title": "Apply the client-delay event",
         "desc": "The simulated client now expects to pay in full on 5 November 2026.",
         "done": "evt-delay-001" in ledger.applied_event_ids,
         "widget": post_button("/demo/apply-delay", "Apply delay event", cls="primary")},
        {"title": "Inspect the gap and its evidence",
         "desc": "See the floor breach, its causes and source records.",
         "done": bool(checks.get("inspected")),
         "widget": ("<a class='btn' href='/'>Open overview</a> "
                    + post_button("/demo/check", "Mark inspected",
                                  fields={"check": "inspected"}))},
        {"title": "Compare Plan A, Plan B and an insufficient response",
         "desc": "Recorded forecast vs proposed plans, with failure conditions.",
         "done": bool(checks.get("compared")),
         "widget": ("<a class='btn' href='/plans'>Open plan comparison</a> "
                    + post_button("/demo/check", "Mark compared",
                                  fields={"check": "compared"}))},
        {"title": "Approve a plan",
         "desc": "Create a draft action from a plan and approve it as owner.",
         "done": bool(states & beyond),
         "widget": ("<a class='btn' href='/actions'>Open action history</a> "
                     "<span class='mut'>create → approve</span>")},
        {"title": "Simulate sending the request",
         "desc": "Place the approved action in the labelled sandbox outbox.",
         "done": bool(states & (beyond - {"owner_approved"})),
         "widget": "<span class='mut'>use the action card on /actions</span>"},
        {"title": "Simulate the counterparty response",
         "desc": "Accept or reject. A supplier acceptance is an explicit runtime "
                 "event that authorizes the recorded date change.",
         "done": bool(states & {"counterparty_accepted", "counterparty_rejected",
                                "payment_observed", "reconciled"}),
         "widget": "<span class='mut'>use the action card on /actions</span>"},
        {"title": "Record a unique simulated payment",
         "desc": "One observed receipt, allocated to the invoice (idempotent).",
         "done": any(r.state == "observed" for r in ledger.receipts.values()),
         "widget": "<span class='mut'>use the action card on /actions</span>"},
        {"title": "Reconcile and reforecast",
         "desc": "Reconcile the action; the forecast recomputes from recorded terms.",
         "done": "reconciled" in states,
         "widget": "<span class='mut'>use the action card on /actions</span>"},
        {"title": "Refresh and restart without losing state",
         "desc": "Reload the page, or restart the server: SQLite persistence keeps "
                 "receipts, approvals and idempotency keys.",
         "done": bool(checks.get("persistence_verified")),
         "widget": post_button("/demo/verify-persistence", "Verify persistence now")},
    ]
    return steps


@route("GET", "/demo")
def demo_page(handler: Handler, req: Request):
    def build(ledger: Ledger):
        steps = _demo_steps(ledger)
        items = "".join(
            f"<li class='{'done' if s['done'] else ''}'><b>{esc(s['title'])}</b><br>"
            f"<span class='mut'>{esc(s['desc'])}</span><br>{s['widget']}</li>"
            for s in steps)
        done = sum(1 for s in steps if s["done"])
        body = f"""
<h1>Demo Workflow <span class="pill sim">sandbox</span></h1>
<p class="mut">{done} of {len(steps)} steps complete. Every step is labelled
simulated; nothing here contacts real people or moves real money.</p>
<ol class="steps">{items}</ol>"""
        return page("Demo Workflow", body, active="/demo")
    return handler._send_html(200, read_state(build))


@route("POST", "/demo/reset")
def demo_reset(handler: Handler, req: Request):
    from .cli import cmd_seed

    def do(ledger: Ledger):
        return None
    with LOCK:
        cmd_seed(database_path())
        store = _open_store()
        try:
            ledger = store.load_ledger()
            authz_mod.require_access(_owner_ctx(ledger), ledger)
            ledger.demo_checks["reset_done"] = True
            ledger.log_audit("demo_reset", "owner", "reseeded from fixture")
            store.save_ledger(ledger)
        finally:
            store.close()
    return handler._redirect("/demo")


@route("POST", "/demo/apply-delay")
def demo_apply_delay(handler: Handler, req: Request):
    def do(ledger: Ledger):
        return ledger.apply_event("evt-delay-001", ctx=_owner_ctx(ledger))
    mutate_state(do)
    back = req.headers.get("Referer", "/demo")
    path = urlparse(back).path if back.startswith("http") else back
    return handler._redirect(path if path in ("/", "/demo", "/plans") else "/demo")


@route("POST", "/demo/check")
def demo_check(handler: Handler, req: Request):
    check = req.field("check")
    if check not in ("inspected", "compared"):
        raise CashflowError("unknown checklist item")

    def do(ledger: Ledger):
        ctx = _owner_ctx(ledger)
        authz_mod.require_access(ctx, ledger)  # direct ledger mutation below
        ledger.demo_checks[check] = True
        ledger.log_audit("demo_checklist", "owner", check)
        return check
    mutate_state(do)
    return handler._redirect("/demo")


@route("POST", "/demo/verify-persistence")
def demo_verify_persistence(handler: Handler, req: Request):
    def snapshot(ledger: Ledger):
        return {
            "receipts": sorted(ledger.receipts),
            "applied_events": sorted(ledger.applied_event_ids),
            "actions": {k: v.state for k, v in ledger.actions.items()},
            "runtime_evidence": sorted(ledger.runtime_evidence_ids),
            "audit_len": len(ledger.audit),
        }
    before = read_state(snapshot)
    # Fresh connection, as after a server restart.
    after = read_state(snapshot)
    ok = before == after

    def do(ledger: Ledger):
        ctx = _owner_ctx(ledger)
        authz_mod.require_access(ctx, ledger)  # direct ledger mutation below
        ledger.demo_checks["persistence_verified"] = bool(ok)
        ledger.log_audit("persistence_verified", "owner",
                         f"restart-simulation reload match={ok}")
        return ok
    mutate_state(do)
    if not ok:
        raise CashflowError("persistence verification failed: state mismatch")
    return handler._redirect("/demo")


# ---------------------------------------------------------------------------
# Audit export (no credentials, no private document contents)
# ---------------------------------------------------------------------------

def _audit_export(ledger: Ledger) -> dict:
    recorded = forecast(ledger, ledger.as_of, ledger.horizon_days)
    summaries = {}
    for ccy, cf in recorded.currencies.items():
        summaries[ccy] = {
            "min_balance_minor": cf.min_balance_minor,
            "min_balance_date": cf.min_balance_date.isoformat(),
            "first_breach_date": (cf.first_breach_date.isoformat()
                                  if cf.first_breach_date else None),
            "worst_shortfall_minor": cf.worst_shortfall_minor,
            "closing_minor": cf.closing_minor,
            "floor_minor": cf.floor_minor,
        }
    return {
        "exported_at_utc": utcnow(),
        "synthetic_demo": True,
        "ai_connected": agent_web.live_adapter().status().state == 'verified',
        "provider_state": agent_web.live_adapter().status().state,
        "business": {"id": ledger.business_id, "name": ledger.business_name,
                     "base_currency": ledger.base_currency,
                     "as_of": ledger.as_of.isoformat(),
                     "horizon_days": ledger.horizon_days},
        "scenario_inputs": {
            "evidence": [{"id": e.id, "kind": e.kind, "summary": e.summary,
                          "runtime_event": e.id in ledger.runtime_evidence_ids}
                         for e in ledger.evidence.values()],
            "applied_events": sorted(ledger.applied_event_ids),
            "runtime_consent_map": ledger.consent_map,
            # Document contents are never stored and never exported.
            "documents": [{"id": d.id, "kind": d.kind, "source": d.source,
                           "content_sha256": d.content}
                          for d in ledger.documents.values()],
        },
        "approvals": [
            {"id": a.id, "kind": a.kind, "terms": a.terms, "actor": a.actor,
             "version": a.version, "state": a.state,
             "approved_version": a.approved_version,
             "counterparty_response": a.counterparty_response,
             "audit": a.audit}
            for a in ledger.actions.values()],
        "changes": {
            "owner_operations": ledger.owner_operations,
            "receipt_reviews": ledger.receipt_reviews,
            "receipt_adjustments": ledger.receipt_adjustments,
            "archived_receipt_reviews": ledger.receipt_review_archive,
            "bill_payment_observations": {o.id: o.observed_payments for o in ledger.outflows.values() if o.observed_payments},
            "bill_payment_corrections": {o.id:o.payment_adjustments for o in ledger.outflows.values() if o.payment_adjustments},
            "legacy_payment_originals": {o.id:o.legacy_payment for o in ledger.outflows.values() if o.legacy_payment},
            "collection_holds": {r.id:r.collection_hold for r in ledger.receivables.values() if r.collection_hold},
            "forecast_snapshots": ledger.forecast_snapshots,
            "invoice_due_sources": ledger.invoice_due_sources,
            "recurring_templates": ledger.recurring_bills,
            "recurring_occurrences": ledger.recurring_occurrences,
            "duplicate_reviews": ledger.duplicate_reviews,
            "record_details": {
                'invoices': {r.id:dict(due_date=r.due_date.isoformat() if r.due_date else None,counterparty=r.counterparty,invoice_reference=r.invoice_reference,identity_evidence_id=r.identity_evidence_id) for r in ledger.receivables.values()},
                'bills': {o.id:dict(counterparty=o.counterparty,invoice_reference=o.invoice_reference,identity_evidence_id=o.identity_evidence_id) for o in ledger.outflows.values()}},
            "receipt_inbox": ledger.receipt_inbox,
            "split_receipts": ledger.split_receipts,
            "fact_checks": ledger.fact_checks,
            "entry_records":ledger.entry_records,
            "source_amount_adjustments":{'invoices':{r.id:r.face_adjustments for r in ledger.receivables.values() if r.face_adjustments},'bills':{o.id:o.face_adjustments for o in ledger.outflows.values() if o.face_adjustments}},
            "whole_payment_corrections":ledger.split_adjustments,
            "other_cash_movements":ledger.cash_movements,
            "statement_batches":ledger.statement_batches,
            "statement_reviews":ledger.statement_reviews,
            "payment_acknowledgements":ledger.payment_acknowledgements,
            "payment_pair_reviews":ledger.payment_pair_reviews,
            "payment_promises":ledger.payment_promises,
            "unallocated_refunds":ledger.unallocated_refunds,
            "statement_balance_checks":ledger.statement_balance_checks,
            "credit_transfers":ledger.credit_transfers,
            "loan_register":ledger.loans,
            "evidence_attachments":{k:{f:v for f,v in a.items() if f!='payload_base64'} for k,a in ledger.evidence_attachments.items()},
            "date_corrections":ledger.date_corrections,
            "instalment_proposals":ledger.instalment_proposals,
            "agreement_schedules":{o.id:o.agreement_schedules for o in ledger.outflows.values() if o.agreement_schedules},
            "refunds":{'invoices':{r.id:r.refunds for r in ledger.receivables.values() if r.refunds},'bills':{o.id:o.refunds for o in ledger.outflows.values() if o.refunds}},
            "saved_scenarios":ledger.scenarios,
            "collection_date_sources": ledger.collection_date_sources,
            "ledger_audit": ledger.audit,
            "receipts": [{"id": r.id, "receivable_id": r.receivable_id,
                          "amount_minor": r.amount_minor,
                          "date_received": r.date_received.isoformat(),
                          "state": r.state, "evidence_id": r.evidence_id,
                          "actor": r.actor}
                         for r in ledger.receipts.values()],
            "outflow_amendments": [
                {"outflow_id": o.id,
                 "original_date": o.original_date.isoformat() if o.original_date else None,
                 "original_amount_minor": o.original_amount_minor,
                 "current_date": o.date.isoformat(),
                 "amendments": o.amendments}
                for o in ledger.outflows.values() if o.amendments],
            "paid_outflows": [
                {"outflow_id": o.id, "paid_date": o.paid_date.isoformat()}
                for o in ledger.outflows.values() if o.status == "paid"],
        },
        "results": {
            "recorded_forecast": summaries,
            "assumptions": recorded.assumptions,
            "clarifications": recorded.clarifications,
        },
        "excluded": ["connection credentials (environment/session memory or separate opt-in encrypted local vault)",
                     "raw uploaded document files (hashes and structured fields retained)",
                     "full agent runs (saved separately when present)"],
    }


@route("GET", "/audit/export")
def audit_export(handler: Handler, req: Request):
    payload = read_state(_audit_export)
    return handler._send_json_download("cashflow-audit-export.json", payload)


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def run_server(db_path: str = DB_PATH, port: int = 8765) -> None:
    agent_web.reset_live_adapter()
    global DB_PATH
    DB_PATH = db_path
    # Seed on first run so the demo works from a clean start.
    import os
    if not os.path.exists(db_path):
        from .cli import cmd_seed
        cmd_seed(db_path)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    agent_web.mount(sys.modules[__name__])
    print(f"Cashflow Action Copilot (synthetic sandbox demo) on "
          f"http://127.0.0.1:{port}/  [{agent_web.banner_status()}]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


from . import owner_web
owner_web.mount(sys.modules[__name__])
from . import accounting_web
accounting_web.mount(sys.modules[__name__])
from . import operations_web
operations_web.mount(sys.modules[__name__])
from . import controls_web
controls_web.mount(sys.modules[__name__])
from . import business_web
business_web.mount(sys.modules[__name__])
from . import completion_web
completion_web.mount(sys.modules[__name__])
from . import extension_web
extension_web.mount(sys.modules[__name__])
from . import workbench_web
workbench_web.mount(sys.modules[__name__])
from . import obligations_web
obligations_web.mount(sys.modules[__name__])
from . import review
review.mount(sys.modules[__name__])
from . import treasury_web
treasury_web.mount(sys.modules[__name__])
from . import funding_web
funding_web.mount(sys.modules[__name__])

from . import statement_mapping_web
statement_mapping_web.mount(sys.modules[__name__])

from . import site_web
site_web.mount(sys.modules[__name__])
from . import telegram_web
telegram_web.mount(sys.modules[__name__])
from . import floating_assistant
floating_assistant.mount(sys.modules[__name__])

from . import getting_started
getting_started.mount(sys.modules[__name__])
