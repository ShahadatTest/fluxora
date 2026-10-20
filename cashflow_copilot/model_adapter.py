"""Model provider adapters for the constrained financial agent.

Two implementations behind one abstract interface:

- ``OpenAICompatibleAdapter``: a real server-side adapter for any
  OpenAI-compatible ``/v1/chat/completions`` endpoint. Configuration comes
  from the environment by default (``CASHFLOW_MODEL_API_KEY``,
  ``CASHFLOW_MODEL_BASE_URL``, ``CASHFLOW_MODEL_NAME``), or explicit local
  server-side profile configuration. Locally authored
  errors discard provider error bodies; configuration displays redact the key.
- ``OpenRouterAdapter``: the same protocol with bounded output, routing
  preferences and ephemeral reasoning continuation for multi-step tool use.
- ``MockProviderAdapter``: a deterministic, clearly-labelled mock used for
  the disconnected demo. EVERY surface that touches it labels it
  "mocked provider — deterministic demo, not a live model". It must never
  be presented as a live model.

Connection states: "disconnected" (no key), "configured-but-unverified"
(key present, ``verify()`` not yet run), "verified" (``verify()`` succeeded
with a real call), "error" (the last call failed; the stored message never
contains the key).

Chat-completions wire format implemented here and exercised by local HTTP stubs:
POST {base_url}/chat/completions with
{"model", "messages", "tools": [{"type": "function", "function":
{"name", "description", "parameters"}}], "tool_choice": "auto"};
responses carry choices[0].message {content, tool_calls: [{id, type,
function: {name, arguments (JSON string)}}]}; tool results are sent back as
{"role": "tool", "tool_call_id": ..., "content": ...}; errors arrive as
{"error": {"message", "type", "code"}} with HTTP 401/429/5xx.
"""

from __future__ import annotations

import json
import copy
import os
import time
import math
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.request
import urllib.parse
from dataclasses import dataclass, field

from .errors import CashflowError

# ---------------------------------------------------------------------------
# Default environment configuration; the local profile manager uses constructors.
# ---------------------------------------------------------------------------

ENV_API_KEY = "CASHFLOW_MODEL_API_KEY"
ENV_BASE_URL = "CASHFLOW_MODEL_BASE_URL"
ENV_MODEL_NAME = "CASHFLOW_MODEL_NAME"

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_TOTAL_BUDGET_S = 90.0
RETRY_BACKOFF_S = (1.0, 2.0)  # deterministic backoff between retries
MAX_RESPONSE_BYTES = 1_048_576
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Historical reference model used by the original wire-contract fixtures.
# Runtime model selection is always CASHFLOW_MODEL_NAME, never this constant.
OPENROUTER_MODEL = "qwen/qwen3.8-flash"
OPENROUTER_MAX_TOKENS = 2048
OPENROUTER_REASONING_TOKENS = 512
OPENROUTER_MAX_REQUEST_BYTES = 262_144
MAX_CONTINUATION_BYTES = 65_536


def _openrouter_url(value: str) -> bool:
    """Select provider policy only for the exact public gateway URL."""
    try:
        parsed = urllib.parse.urlsplit(value)
        return (parsed.scheme == 'https' and parsed.hostname == 'openrouter.ai'
                and parsed.port in (None, 443) and parsed.path.rstrip('/') == '/api/v1'
                and not (parsed.username or parsed.password or parsed.query or parsed.fragment))
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

# Required label: shown on every UI, API, audit and test surface that uses
# the mock. Never present mock output as a live model.
MOCK_PROVIDER_LABEL = "mocked provider — deterministic demo, not a live model"

# Connection states
DISCONNECTED = "disconnected"
CONFIGURED_UNVERIFIED = "configured-but-unverified"
VERIFIED = "verified"
ERROR = "error"


class AdapterError(CashflowError):
    """A model-provider call failed cleanly (no traceback or key leaked)."""


# ---------------------------------------------------------------------------
# Interface types
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class AdapterResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    provider_label: str = ""
    usage: dict | None = None
    # Transport continuation only, never displayed or stored with run history.
    reasoning_details: list | None = field(default=None, repr=False)
    reasoning: str | None = field(default=None, repr=False)


@dataclass
class AdapterStatus:
    state: str  # disconnected | configured-but-unverified | verified | error
    provider_label: str
    model: str | None
    base_url: str
    detail: str = ""  # human-readable note; NEVER contains the API key
    retry_after_seconds: int = 0


class ModelAdapter:
    """Abstract model interface used by the agent runner."""

    provider_label: str = "unknown"

    def chat(self, messages: list[dict], tools: list[dict], *,
             deadline: float | None = None) -> AdapterResult:
        raise NotImplementedError

    def verify(self) -> AdapterStatus:
        raise NotImplementedError

    def status(self) -> AdapterStatus:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Live adapter: OpenAI-compatible chat completions (stdlib only)
# ---------------------------------------------------------------------------

class OpenAICompatibleAdapter(ModelAdapter):
    """Server-side adapter for an OpenAI-compatible chat-completions endpoint.

    The adapter keeps the API key in process memory. Its own errors and
    configuration displays do not include raw provider error bodies or keys.
    """

    def __init__(self, api_key: str | None = None,
                 base_url: str | None = None,
                 model: str | None = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 total_budget_s: float = DEFAULT_TOTAL_BUDGET_S):
        self._api_key = api_key or None
        if self._api_key is not None and not self._api_key.strip():
            self._api_key = None
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._model = model
        self._timeout_s = float(timeout_s)
        self._max_retries = int(max_retries)
        self._total_budget_s = float(total_budget_s)
        self._state = (DISCONNECTED if self._api_key is None
                       else CONFIGURED_UNVERIFIED)
        self._last_error = ""
        self._transport_lock = threading.Lock()
        self._cooldown_until = 0.0
        self._cooldown_failure = 'model provider returned HTTP 429: rate limit reached; local cooldown active'

    @classmethod
    def from_env(cls, **overrides) -> "OpenAICompatibleAdapter":
        """Build from the environment. Overrides are for tests only."""
        params = {
            "api_key": os.environ.get(ENV_API_KEY),
            "base_url": os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL),
            "model": os.environ.get(ENV_MODEL_NAME),
        }
        params.update(overrides)
        adapter_type = cls
        if cls is OpenAICompatibleAdapter and _openrouter_url(params['base_url']):
            adapter_type = OpenRouterAdapter
        return adapter_type(**params)

    @property
    def provider_label(self) -> str:
        return f"live:{self._public_text(self._model or 'unknown-model')}"

    def _public_text(self,value):
        value=str(value)
        if self._api_key:value=value.replace(self._api_key,'[redacted]')
        return ''.join(c for c in value if c.isprintable())[:200]

    def _public_url(self):
        try:
            p=urllib.parse.urlsplit(self._base_url)
            if p.username or p.password or p.query or p.fragment:return '[invalid provider URL; review environment configuration]'
            return self._public_text(self._base_url)
        except ValueError:return '[invalid provider URL; review environment configuration]'

    @property
    def is_configured(self) -> bool:
        return self._api_key is not None

    def configuration_matches_env(self) -> bool:
        other = OpenAICompatibleAdapter.from_env()
        return type(self) is type(other) and (self._api_key, self._base_url, self._model) == (
            other._api_key, other._base_url, other._model)

    def request_policy(self) -> dict:
        """Public request settings; never credentials or provider reasoning."""
        return {}

    def status(self) -> AdapterStatus:
        detail = ""
        if self._state == DISCONNECTED:
            detail = (f"No {ENV_API_KEY} is set. The agent panel offers the "
                      f"labelled mock demo instead ({MOCK_PROVIDER_LABEL}).")
        elif self._state == CONFIGURED_UNVERIFIED:
            detail = 'A server-side credential is configured; verify() has not run yet.'
        elif self._state == ERROR:
            detail = self._last_error
        return AdapterStatus(
            state=self._state,
            provider_label=self.provider_label,
            model=self._public_text(self._model) if self._model else None,
            base_url=self._public_url(),
            detail=detail,
            retry_after_seconds=max(0,math.ceil(self._cooldown_until-time.monotonic())),
        )

    def __repr__(self) -> str:  # key is never shown
        return (
            f"OpenAICompatibleAdapter(model={self._public_text(self._model)!r}, "
            f"base_url={self._public_url()!r}, api_key='***', state={self._state!r})"
        )

    __str__ = __repr__

    # -- wire protocol ---------------------------------------------------

    def _request_body(self, messages: list[dict], tools: list[dict]) -> dict:
        return {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }

    def _verification_body(self) -> dict:
        return {'model': self._model,
                'messages': [{'role': 'user', 'content': 'ping'}],
                'max_tokens': 1}

    def _post(self, payload: dict, *, deadline: float | None = None) -> dict:
        # One in-flight HTTP request per active configuration. Do not queue
        # unreviewed work or block a second run past its remaining deadline.
        if not self._transport_lock.acquire(blocking=False):
            raise AdapterError('model provider request already running; wait for it to finish before asking again')
        try:
            if time.monotonic()<self._cooldown_until:
                raise AdapterError(self._cooldown_failure)
            return self._post_locked(payload,deadline=deadline)
        finally:self._transport_lock.release()

    def _post_locked(self, payload: dict, *, deadline: float | None = None) -> dict:
        """POST one payload with bounded retries on 429/5xx.

        Raises AdapterError with a clean message (never the key).
        """
        if self._api_key is None:
            raise AdapterError(
                f"model provider is disconnected: {ENV_API_KEY} is not set")
        url = f"{self._base_url}/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        if isinstance(self, OpenRouterAdapter) and len(data) > OPENROUTER_MAX_REQUEST_BYTES:
            raise AdapterError('model request exceeds the configured byte limit; reduce context')
        # The key is attached to the request object only (unredirected, so it
        # is never forwarded on redirects) and never to any string that
        # could end up in logs, errors, or the `headers` literal below.
        deadline = min(deadline if deadline is not None else float('inf'),
                       time.monotonic() + self._total_budget_s)
        parsed = urllib.parse.urlsplit(url)
        if (parsed.username or parsed.password or parsed.query or parsed.fragment
                or not parsed.hostname
                or (parsed.scheme != 'https' and not (
                    parsed.scheme == 'http' and parsed.hostname in
                    {'127.0.0.1', 'localhost', '::1'}))):
            raise AdapterError('invalid provider URL: use HTTPS or loopback HTTP without credentials or query')
        opener = urllib.request.build_opener(_NoRedirect())
        attempt = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterError('model provider deadline budget exhausted')
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
                method="POST")
            req.add_unredirected_header(
                "Authorization", f"Bearer {self._api_key}")
            try:
                with opener.open(req, timeout=min(self._timeout_s, remaining)) as resp:
                    chunks = []
                    size = 0
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise AdapterError('model provider deadline budget exhausted')
                        # read1 performs at most one underlying read. Reapply the
                        # remaining budget so a trickling body cannot reset it.
                        sock = getattr(getattr(resp.fp, 'raw', None), '_sock', None)
                        if sock is not None:
                            sock.settimeout(min(self._timeout_s, remaining))
                        chunk = resp.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise AdapterError('provider response exceeds byte limit')
                        if not chunk:
                            break
                        chunks.append(chunk)
                    raw = b''.join(chunks).decode('utf-8')
            except urllib.error.HTTPError as exc:
                exc.close()  # Never read, echo or persist a provider error body.
                if self._maybe_retry(exc, attempt, deadline):
                    attempt += 1
                    continue
                raise self._http_error(exc) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                # Transport failure or timeout: bounded, no retry (a timed-out
                # completion may still have executed server-side).
                self._cooldown_until=max(self._cooldown_until,time.monotonic()+30)
                self._cooldown_failure='model provider call failed: transport error or timeout'
                raise AdapterError(self._cooldown_failure) from None
            except UnicodeDecodeError:
                raise AdapterError('malformed provider response: invalid UTF-8') from None
            if time.monotonic() >= deadline:
                raise AdapterError('model provider deadline budget exhausted')
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                raise AdapterError('malformed provider response (not JSON)') from None

    def _maybe_retry(self, exc: urllib.error.HTTPError, attempt: int,
                     deadline: float) -> bool:
        """Sleep the backoff and return True when this error is retryable and
        the total budget still allows another bounded attempt."""
        retryable = exc.code == 429 or 500 <= exc.code < 600
        backoff = (RETRY_BACKOFF_S[attempt]
                   if attempt < len(RETRY_BACKOFF_S)
                   else RETRY_BACKOFF_S[-1])
        hint=self._retry_after(exc.headers) if exc.code in {429,503} else None
        if hint is not None:backoff=max(backoff,hint)
        # Long server hints cannot be waited out inside a short agent run.
        # Keep the hint as shared status; a user retries explicitly after it.
        if not retryable or attempt >= self._max_retries or backoff>8 or time.monotonic()+backoff>=deadline:
            if retryable:
                self._cooldown_until=max(self._cooldown_until,time.monotonic()+max(hint or 0,30))
                self._cooldown_failure=str(self._http_error(exc))+'; local cooldown active'
            return False
        time.sleep(backoff)
        # Enforce the total budget before starting the next attempt.
        return time.monotonic() < deadline

    @staticmethod
    def _retry_after(headers):
        """Return only a bounded numeric delay, never raw untrusted headers."""
        raw=headers.get('Retry-After','') if headers else ''
        if not isinstance(raw,str) or not 1<=len(raw)<=100:return None
        try:
            delay=float(raw)
        except ValueError:
            try:
                stamp=parsedate_to_datetime(raw)
                if stamp.tzinfo is None:return None
                delay=(stamp-datetime.now(timezone.utc)).total_seconds()
            except (ValueError,TypeError,OverflowError):return None
        if not math.isfinite(delay) or delay<0:return None
        # Reject unreasonable hints rather than storing arbitrary provider text.
        return min(delay,86400)

    def _http_error(self, exc: urllib.error.HTTPError) -> AdapterError:
        # Only locally authored messages cross the provider error boundary.
        message = {401: 'invalid api key or provider authorization denied',
                   403: 'provider authorization denied',
                   429: 'rate limit reached'}.get(exc.code, 'request rejected')
        return AdapterError(
            f"model provider returned HTTP {exc.code}: {message}")

    def _parse_completion(self, payload: dict) -> AdapterResult:
        from .agent_disclosure import parse_usage
        try:
            choices = payload.get("choices")
            if not choices:
                raise KeyError("choices")
            message = choices[0].get("message") or {}
            if not isinstance(message, dict):
                raise TypeError()
        except (AttributeError, KeyError, IndexError, TypeError):
            raise AdapterError(
                "malformed provider response: missing choices[0].message")
        if choices[0].get('finish_reason') in {'length', 'error', 'content_filter'}:
            raise AdapterError('provider did not complete its response; no partial tool request accepted')
        if 'error' in payload:
            raise AdapterError('provider reported a failed response')
        text = message.get("content") or ""
        if not isinstance(text, str):
            raise AdapterError(
                "malformed provider response: message.content is not a string")
        tool_calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise AdapterError(
                "malformed provider response: tool_calls is not a list")
        for tc in raw_calls:
            if not isinstance(tc, dict) or not isinstance(tc.get('function'), dict):
                raise AdapterError('malformed provider response: invalid tool call')
            fn = (tc or {}).get("function") or {}
            name = fn.get("name") or ""
            if not isinstance(name, str) or not isinstance(tc.get('id'), str):
                raise AdapterError('malformed provider response: invalid tool identity')
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw)
            except (json.JSONDecodeError, TypeError):
                raise AdapterError('malformed tool arguments JSON') from None
            if not isinstance(args, dict):
                raise AdapterError(
                    "malformed tool arguments: "
                    "expected a JSON object")
            tool_calls.append(ToolCall(id=tc.get("id") or "",
                                       name=name, arguments=args))
        return AdapterResult(
            text=text,
            tool_calls=tool_calls,
            finish_reason=choices[0].get("finish_reason"),
            provider_label=self.provider_label,
            usage=parse_usage(payload),
        )

    # -- public interface -------------------------------------------------

    def chat(self, messages: list[dict], tools: list[dict], *,
             deadline: float | None = None) -> AdapterResult:
        if self._api_key is None:
            self._state = DISCONNECTED
            raise AdapterError(
                f"model provider is disconnected: {ENV_API_KEY} is not set")
        if not self._model:
            self._state = ERROR
            self._last_error = f'{ENV_MODEL_NAME} is required for verification and chat'
            raise AdapterError(
                f"model provider has no model configured: {ENV_MODEL_NAME} "
                "is not set")
        try:
            payload = self._post(self._request_body(messages, tools), deadline=deadline)
            result = self._parse_completion(payload)
        except AdapterError as exc:
            self._state = ERROR
            self._last_error = str(exc)  # message never contains the key
            raise
        self._state = VERIFIED
        self._last_error = ""
        return result

    def verify(self) -> AdapterStatus:
        """Run a minimal real call to verify the configured credentials."""
        if self._api_key is None:
            self._state = DISCONNECTED
            self._last_error = ""
            return self.status()
        if not self._model:
            self._state = ERROR
            self._last_error = f'{ENV_MODEL_NAME} is required for verification and chat'
            return self.status()
        try:
            payload = self._post(self._verification_body())
            self._parse_completion(payload)
        except AdapterError as exc:
            self._state = ERROR
            self._last_error = str(exc)
            return self.status()
        self._state = VERIFIED
        self._last_error = ""
        return self.status()


class OpenRouterAdapter(OpenAICompatibleAdapter):
    """OpenRouter profile. Direct construction also supports loopback stubs.

    Normal application selection is by the canonical gateway URL, from the
    same three environment variables. No automatic model substitution.
    """

    @property
    def provider_label(self) -> str:
        return f'live:openrouter:{self._public_text(self._model or "unknown-model")}'

    def request_policy(self) -> dict:
        policy=dict(protocol='OpenRouter Chat Completions',
                    max_tokens_per_response=OPENROUTER_MAX_TOKENS,
                    reasoning_max_tokens=OPENROUTER_REASONING_TOKENS,
                    max_request_bytes=OPENROUTER_MAX_REQUEST_BYTES,
                    require_parameters=True, data_collection='deny',
                    allow_provider_fallbacks=False,
                    automatic_model_fallback=False)
        effort={'openai/gpt-6-luna':'none','deepseek/deepseek-v4-flash':'none',
                'z-ai/glm-5.3-flash':'low'}.get(self._model)
        if effort:
            policy.pop('reasoning_max_tokens');policy['reasoning_effort']=effort
        if self._model=='minimax/minimax-m3':
            # A 2,048-token total truncated the account-funding evaluation.
            # Retain bounded reasoning; disabling it degraded account analysis.
            policy['max_tokens_per_response']=4096
        return policy

    def _request_body(self, messages: list[dict], tools: list[dict]) -> dict:
        body = super()._request_body(messages, tools)
        body.update(max_tokens=self.request_policy()['max_tokens_per_response'], stream=False,
                    reasoning={'max_tokens': OPENROUTER_REASONING_TOKENS},
                    provider={'require_parameters': True, 'data_collection': 'deny',
                              'allow_fallbacks': False})
        effort=self.request_policy().get('reasoning_effort')
        if effort:body['reasoning']={'effort':effort}
        return body

    def _verification_body(self) -> dict:
        body = self._request_body([{'role': 'user', 'content': 'Reply OK.'}], [])
        body.pop('tools'); body.pop('tool_choice')
        # Verification tests connectivity, not reasoning or tool competence.
        body.update(max_tokens=64, reasoning={'enabled': False})
        # Mandatory-reasoning candidates must not be verified with reasoning
        # explicitly disabled. Keep the ping bounded and free of ledger data.
        effort=self.request_policy().get('reasoning_effort')
        if effort:
            body['reasoning']={'effort':effort}
            if effort!='none':body['max_tokens']=256
        return body

    def _parse_completion(self, payload: dict) -> AdapterResult:
        result = super()._parse_completion(payload)
        if not result.tool_calls:
            return result
        message = payload['choices'][0]['message']
        details = message.get('reasoning_details')
        reasoning = message.get('reasoning', message.get('reasoning_content'))
        if details is not None:
            if not isinstance(details, list) or any(not isinstance(v, dict) for v in details):
                raise AdapterError('malformed provider reasoning continuation')
            continuation = {'reasoning_details': details}
        elif reasoning is not None:
            if not isinstance(reasoning, str):
                raise AdapterError('malformed provider reasoning continuation')
            continuation = {'reasoning': reasoning}
        else:
            return result
        serialized = json.dumps(continuation)
        if (len(serialized.encode('utf-8')) > MAX_CONTINUATION_BYTES
                or (self._api_key and self._api_key in serialized)):
            raise AdapterError('provider reasoning continuation rejected by local bounds')
        # Preserve complete signed/encrypted blocks and order, without using
        # their contents as financial evidence or additional tool authority.
        result.reasoning_details = copy.deepcopy(continuation.get('reasoning_details'))
        result.reasoning = continuation.get('reasoning')
        return result


# ---------------------------------------------------------------------------
# Mock provider: deterministic, scripted, always labelled
# ---------------------------------------------------------------------------

# The five demo questions the mock answers, matched by keyword. Anything else
# gets an honest "I can only answer the scripted demo questions" reply.
_MOCK_SCRIPTS: dict[str, list[tuple[str, dict]]] = {
    "workflow_snapshot": [
        ("read_workspace", {"view":"weekly_cashflow"}),
        ("prepare_workflow", {"job_id":"mock-weekly-review","label":"Weekly cash review",
            "steps_json":'[{"operation":"save_forecast","arguments":{"label":"Weekly cash review"}}]'}),
    ],
    "breach_why": [
        ("get_evidence", {}),
        ("compute_forecast", {}),
    ],
    "delay_7": [
        ("get_evidence", {}),
        ("compute_forecast", {"scenario": {"shift_receivable_days": 7}}),
    ],
    "missing_facts": [
        ("missing_facts", {}),
        ("get_constraints", {}),
    ],
    "compare_plans": [
        ("compare_plans", {}),
    ],
    "draft_partial": [
        ("compare_plans", {}),
        ("draft_action", {
            "action_id": "act-agent-partial-1",
            "kind": "request_early_payment",
            "receivable_id": "inv-client-001",
            "amount_minor": 500_000,
            "currency": "USD",
            "date": "2026-10-22",
        }),
    ],
}


def _match_question(question: str) -> str | None:
    q = question.lower()
    if 'workflow' in q and ('forecast' in q or 'weekly' in q):return 'workflow_snapshot'
    if "draft" in q and ("partial" in q or "payment" in q):
        return "draft_partial"
    if "compare" in q and "plan" in q:
        return "compare_plans"
    if "missing fact" in q:
        return "missing_facts"
    if ("seven days later" in q or "7 days later" in q
            or "seven days" in q):
        return "delay_7"
    if "why" in q and "breach" in q:
        return "breach_why"
    return None


class MockProviderAdapter(ModelAdapter):
    """Deterministic scripted provider for the disconnected demo.

    It returns fixed tool-call sequences for the five demo questions and
    formats its final answers from the actual tool results (engine output),
    never from fixture literals. Every result is labelled with
    MOCK_PROVIDER_LABEL.
    """

    provider_label = f"mock ({MOCK_PROVIDER_LABEL})"

    def status(self) -> AdapterStatus:
        return AdapterStatus(
            state="mock",
            provider_label=self.provider_label,
            model=None,
            base_url="",
            detail=(f"{MOCK_PROVIDER_LABEL}. Use it only for the scripted "
                    "demo questions."),
        )

    def verify(self) -> AdapterStatus:
        return self.status()

    def chat(self, messages: list[dict], tools: list[dict], *,
             deadline: float | None = None) -> AdapterResult:
        question = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                question = str(msg.get("content") or "")
                break
        key = _match_question(question)
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if key is not None and tool_msgs:
            results = {}
            for m in tool_msgs:
                try:
                    results[m.get("tool_call_id")] = json.loads(
                        m.get("content") or "{}")
                except (json.JSONDecodeError, ValueError):
                    results[m.get("tool_call_id")] = {}
            return AdapterResult(
                text=_finalize(key, results),
                finish_reason="stop",
                provider_label=self.provider_label,
            )
        if key is not None:
            calls = [
                ToolCall(id=f"mock-call-{i + 1}", name=name, arguments=args)
                for i, (name, args) in enumerate(_MOCK_SCRIPTS[key])
            ]
            return AdapterResult(
                text="",
                tool_calls=calls,
                finish_reason="tool_calls",
                provider_label=self.provider_label,
            )
        return AdapterResult(
            text=(f"[{MOCK_PROVIDER_LABEL}] I can only answer the supported "
                  "scripted demo questions in this disconnected demo: why "
                  "the cash floor will be breached, a seven-day-later "
                  "scenario, which missing fact could change the decision, "
                  "a plan comparison, and drafting a partial-payment "
                  "request, and preparing a forecast-review workflow."),
            finish_reason="stop",
            provider_label=self.provider_label,
        )


# ---------------------------------------------------------------------------
# Mock final-answer formatters (built from tool results, not fixtures)
# ---------------------------------------------------------------------------

def _tool_result(results: dict, tool_name: str) -> dict:
    for call_id, payload in results.items():
        if isinstance(payload, dict) and payload.get("tool") == tool_name:
            inner = payload.get("result")
            return inner if isinstance(inner, dict) else {}
    return {}


def _finalize(key: str, results: dict) -> str:
    builders = {
        "workflow_snapshot": lambda results: (
            "Workflow prepared/read using the labelled deterministic mock.\n"
            + json.dumps(_tool_result(results,'prepare_workflow'),indent=2)
            + "\nReview the workflow queue for actual status. Approval belongs to the owner; no new operation executes merely from this answer."),
        "breach_why": _answer_breach_why,
        "delay_7": _answer_delay_7,
        "missing_facts": _answer_missing_facts,
        "compare_plans": _answer_compare_plans,
        "draft_partial": _answer_draft_partial,
    }
    header = f"[{MOCK_PROVIDER_LABEL}]\n\n"
    return header + builders[key](results)


def _cause_lines(items: list) -> str:
    return "\n".join(
        f"- {c['kind']} {c['id']}: {c['amount']} on {c['date']} "
        f"(source: {c['evidence_id']})"
        for c in items)


def _answer_breach_why(results: dict) -> str:
    fc = _tool_result(results, "compute_forecast")
    usd = (fc.get("currencies") or {}).get("USD", {})
    causes = usd.get("breach_causes") or []
    priors = usd.get("prior_outflows") or []
    assumptions = "\n".join(
        f"- {a}" for a in (fc.get("assumptions") or [])[:3])
    breach_line=(f"- First projected breach: {usd.get('first_breach_date')} (worst shortfall {usd.get('worst_shortfall')}).\n"
        if usd.get('first_breach_date') else '- No USD floor breach is projected within this window under the recorded assumptions.\n')
    other_currencies=''.join(f"- {ccy} separately: minimum {r.get('min_balance')}; first projected breach {r.get('first_breach_date') or 'none in window'}; closing {r.get('closing')}.\n" for ccy,r in (fc.get('currencies') or {}).items() if ccy!='USD')
    usd_lines=(f"- Pooled USD floor: {usd.get('floor')}; this does not prove funding in a particular account.\n"
        f"- Forecast minimum USD balance: {usd.get('min_balance')} on {usd.get('min_balance_date')}.\n"
        f"{breach_line}" if usd else '- No USD forecast returned; no USD figure or breach is inferred.\n')
    return (
        "Cash-floor review — deterministic mock summary:\n\n"
        "Deterministic forecast (conditional on recorded terms):\n"
        f"{usd_lines}{other_currencies}"
        "Outflows draining cash before the breach:\n"
        f"{_cause_lines(priors) or '- (none before the breach date)'}\n"
        "Cash movements landing on the breach date:\n"
        f"{_cause_lines(causes) or '- (no dated causes returned)'}\n\n"
        "Assumptions (not facts):\n"
        f"{assumptions or '- none'}\n\n"
        "Proposed changes: none yet — ask me to compare the available plans.\n"
        "Observed payments: review the source-linked deterministic results separately. Expected collection is not observed money."
    )


def _answer_delay_7(results: dict) -> str:
    fc = _tool_result(results, "compute_forecast")
    currencies=fc.get('currencies') or {}
    lines=[]
    for ccy,curve in currencies.items():
        breach=(f"first projected breach {curve['first_breach_date']} (worst shortfall {curve.get('worst_shortfall')})"
            if curve.get('first_breach_date') else 'no projected floor breach within this window')
        lines.append(f"- {ccy} separately: minimum {curve.get('min_balance')} on {curve.get('min_balance_date')}; {breach}; closing {curve.get('closing')}.")
    scenario = fc.get("scenario_description") or fc.get("scenario") or {}
    if isinstance(scenario, dict):
        scenario = scenario.get("description") or json.dumps(scenario)
    return (
        "Seven-day collection-delay scenario — deterministic mock summary:\n\n"
        "Conditional calculation:\n"
        f"- Scenario applied: {scenario}\n"
        + ('\n'.join(lines) if lines else '- No currency forecast returned; no balance or breach is inferred.') + '\n\n'
        "Assumptions: the seven-day shift is a scenario assumption, not a "
        "new payment promise or observed receipt. No recorded date, claim, cash, approval or payroll obligation was changed.\n\n"
        "Review the separate deterministic tool results and source IDs. A closing balance can be positive while an earlier daily cash floor is breached; use the actual minima and dates above."
    )


def _answer_missing_facts(results: dict) -> str:
    mf = _tool_result(results, "missing_facts")
    lines = []
    for c in (mf.get("clarifications") or []) + (mf.get("sensitivity") or []):
        lines.append(
            f"- {c.get('question')}\n  Why it matters: {c.get('why_it_matters')}")
    return (
        "Missing facts that could change the decision:\n\n"
        + ("\n".join(lines) if lines else "- None flagged by the engine.")
        + "\n\nThese are clarification requests, not invented facts: each "
          "names why the answer would change feasibility."
    )


def _answer_compare_plans(results: dict) -> str:
    cp = _tool_result(results, "compare_plans")
    base = cp.get("baseline") or {}
    parts = ["Plan comparison — computed by the deterministic engine:\n"]
    busd = (base.get("currencies") or {}).get(cp.get('base_currency', 'USD'), {})
    parts.append(
        f"Current forecast (no plan): minimum {busd.get('min_balance')} on "
        f"{busd.get('min_balance_date')}; first breach "
        f"{busd.get('first_breach_date')}.")
    plans = cp.get("plans") or []
    if cp.get('no_feasible_plan') or not plans:
        parts.append(
            "\nNo feasible plan within the allowed actions — every candidate "
            "is INFEASIBLE:")
    for p in plans:
        verdict = ("CONDITIONALLY SUFFICIENT — permissions/payment pending"
            if p.get('financially_sufficient') else "INFEASIBLE / INCOMPLETE")
        parts.append(
            f"\n{p.get('name')} — {verdict}\n"
            f"  min {p.get('min_balance')}, closing {p.get('closing')}, "
            f"altered obligations {p.get('altered_obligations')}\n"
            f"  reasons: {'; '.join(p.get('reasons') or [])}")
        for label in p.get("conditional_labels") or []:
            parts.append(f"  conditional: {label[:160]}")
    notes = cp.get("notes") or []
    if notes:
        parts.append("\nNotes:\n" + "\n".join(f"- {n}" for n in notes))
    parts.append(
        "\nA proposed plan is not cash: projected receipts are conditional "
        "until observed and recorded.")
    return "\n".join(parts)


def _answer_draft_partial(results: dict) -> str:
    da = _tool_result(results, "draft_action")
    if not da or da.get("state") != "draft":
        return (
            "I could not create the draft partial-payment request. "
            f"Tool result: {json.dumps(da)[:400]}")
    terms = da.get("terms") or {}
    return (
        "Draft created — for owner review only:\n\n"
        "Recorded facts:\n"
        f"- Draft action {da.get('action_id')} (kind: {da.get('kind')}, "
        f"state: draft).\n"
        f"- Terms: request {terms.get('amount')} on {terms.get('date')} "
        f"against {terms.get('receivable_id')}.\n\n"
        "Proposed change vs commitment: this is a DRAFT. It changes no "
        "balances, is not approved, has not been sent, and moves no money. "
        "The owner must approve it explicitly before anything else happens.\n\n"
        "Next actions: review the draft on the Action History page; approve "
        "only if the terms are correct."
    )
