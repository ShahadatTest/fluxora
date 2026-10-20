"""Phase 3 workstream B: model-adapter tests.

- The MockProviderAdapter is deterministic and labelled
  "mocked provider — deterministic demo, not a live model" on every
  surface (test names say "mock" explicitly).
- The OpenAICompatibleAdapter's HTTP behaviour (request shape, retries,
  timeouts, malformed responses, key redaction) is tested against a local
  stub HTTP server. No live model is ever called: the suite expects the
  "disconnected" state because no credential is available.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cashflow_copilot.model_adapter import (
    ENV_API_KEY,
    MOCK_PROVIDER_LABEL,
    AdapterError,
    AdapterResult,
    MockProviderAdapter,
    OpenAICompatibleAdapter,
    ToolCall,
)

CANARY_KEY = "sk-test-canary-9f8e7d6c5b4a"


# ---------------------------------------------------------------------------
# Local stub provider
# ---------------------------------------------------------------------------

class _StubHandler(BaseHTTPRequestHandler):
    responses: list = []   # [(status, body_dict_or_str), ...]
    requests: list = []    # captured request dicts
    sleep_s: float = 0.0

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = None
        _StubHandler.requests.append({
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
            "body": parsed,
        })
        if _StubHandler.sleep_s:
            time.sleep(_StubHandler.sleep_s)
        if _StubHandler.responses:
            status, payload = _StubHandler.responses.pop(0)
        else:
            status, payload = 200, {}
        data = (payload.encode("utf-8") if isinstance(payload, str)
                else json.dumps(payload).encode("utf-8"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub():
    _StubHandler.responses = []
    _StubHandler.requests = []
    _StubHandler.sleep_s = 0.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    thread.join(timeout=5)


def _completion(text="", tool_calls=None):
    msg = {"role": "assistant", "content": text if text else None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 1,
        "model": "test-model",
        "choices": [{"index": 0, "message": msg,
                     "finish_reason": "tool_calls" if tool_calls else "stop"}],
    }


def _adapter(stub_base, **kw):
    params = {"api_key": CANARY_KEY, "base_url": stub_base,
              "model": "test-model", "timeout_s": 5.0,
              "total_budget_s": 30.0}
    params.update(kw)
    return OpenAICompatibleAdapter(**params)


# ---------------------------------------------------------------------------
# Disconnected state (no credential available — the expected live state)
# ---------------------------------------------------------------------------

def test_live_adapter_disconnected_without_key(monkeypatch):
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    adapter = OpenAICompatibleAdapter.from_env()
    assert adapter.is_configured is False
    status = adapter.status()
    assert status.state == "disconnected"
    assert status.provider_label.startswith("live:")
    with pytest.raises(AdapterError, match="disconnected"):
        adapter.chat([{"role": "user", "content": "hi"}], [])
    # verify() without a key is a no-op, not a network call.
    assert adapter.verify().state == "disconnected"


def test_live_adapter_configured_but_unverified_with_key(monkeypatch):
    monkeypatch.setenv(ENV_API_KEY, CANARY_KEY)
    adapter = OpenAICompatibleAdapter.from_env()
    assert adapter.is_configured is True
    assert adapter.status().state == "configured-but-unverified"


def test_live_adapter_repr_and_status_redact_key():
    adapter = OpenAICompatibleAdapter(api_key=CANARY_KEY, model="m")
    assert CANARY_KEY not in repr(adapter)
    assert CANARY_KEY not in str(adapter)
    assert CANARY_KEY not in adapter.status().detail


# ---------------------------------------------------------------------------
# Wire format against the local stub
# ---------------------------------------------------------------------------

def test_openai_chat_request_shape_and_tool_call_parsing(stub):
    tc = [{"id": "call_1", "type": "function",
           "function": {"name": "get_evidence", "arguments": "{}"}}]
    _StubHandler.responses = [(200, _completion(tool_calls=tc))]
    adapter = _adapter(stub)
    tools = [{"type": "function",
              "function": {"name": "get_evidence", "description": "d",
                           "parameters": {"type": "object"}}}]
    result = adapter.chat([{"role": "user", "content": "hi"}], tools)
    assert len(_StubHandler.requests) == 1
    req = _StubHandler.requests[0]
    assert req["path"] == "/chat/completions"
    body = req["body"]
    assert body["model"] == "test-model"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "get_evidence"
    assert body["tool_choice"] == "auto"
    # The key travels in the Authorization header, never in the JSON body.
    assert req["headers"]["Authorization"] == f"Bearer {CANARY_KEY}"
    assert CANARY_KEY not in json.dumps(body)
    assert result.tool_calls == [
        ToolCall(id="call_1", name="get_evidence", arguments={})]
    assert result.text == ""
    assert result.provider_label == "live:test-model"
    # A successful real call moves the adapter to "verified".
    assert adapter.status().state == "verified"


def test_tool_result_message_shape():
    # Documents the message shape the runner feeds back to the model.
    msg = {"role": "tool", "tool_call_id": "call_1",
           "content": json.dumps({"ok": True})}
    assert set(msg) == {"role", "tool_call_id", "content"}


def test_malformed_provider_json_gives_clean_error(stub):
    _StubHandler.responses = [(200, "this is not json{{{")]
    adapter = _adapter(stub)
    with pytest.raises(AdapterError, match="malformed provider response"):
        adapter.chat([{"role": "user", "content": "hi"}], [])
    assert adapter.status().state == "error"
    assert CANARY_KEY not in adapter.status().detail


def test_malformed_tool_arguments_json_gives_clean_error(stub):
    tc = [{"id": "c1", "type": "function",
           "function": {"name": "x", "arguments": "{not json"}}]
    _StubHandler.responses = [(200, _completion(tool_calls=tc))]
    adapter = _adapter(stub)
    with pytest.raises(AdapterError, match="malformed tool arguments"):
        adapter.chat([{"role": "user", "content": "hi"}], [])


def test_missing_choices_gives_clean_error(stub):
    _StubHandler.responses = [(200, {"oops": True})]
    adapter = _adapter(stub)
    with pytest.raises(AdapterError, match="malformed provider response"):
        adapter.chat([{"role": "user", "content": "hi"}], [])


def test_timeout_is_bounded(stub):
    _StubHandler.sleep_s = 5.0
    adapter = _adapter(stub, timeout_s=1.0, total_budget_s=10.0)
    start = time.monotonic()
    with pytest.raises(AdapterError, match="failed"):
        adapter.chat([{"role": "user", "content": "hi"}], [])
    elapsed = time.monotonic() - start
    assert elapsed < 8.0, f"call was not bounded: {elapsed:.1f}s"


def test_429_then_success_retries(stub):
    _StubHandler.responses = [
        (429, {"error": {"message": "rate limited", "type": "rate_limit"}}),
        (200, _completion(text="ok")),
    ]
    adapter = _adapter(stub)
    result = adapter.chat([{"role": "user", "content": "hi"}], [])
    assert result.text == "ok"
    assert len(_StubHandler.requests) == 2


def test_429_repeatedly_gives_up_within_budget(stub):
    _StubHandler.responses = [
        (429, {"error": {"message": "slow down"}})] * 10
    adapter = _adapter(stub, max_retries=2, timeout_s=2.0,
                       total_budget_s=25.0)
    start = time.monotonic()
    with pytest.raises(AdapterError, match="429"):
        adapter.chat([{"role": "user", "content": "hi"}], [])
    elapsed = time.monotonic() - start
    # initial attempt + 2 retries, then give up inside the total budget.
    assert len(_StubHandler.requests) == 3
    assert elapsed < 25.0, f"retry loop exceeded budget: {elapsed:.1f}s"


def test_401_is_not_retried(stub):
    _StubHandler.responses = [(401, {"error": {"message": "bad key"}})]
    adapter = _adapter(stub)
    with pytest.raises(AdapterError, match="401.*authorization") as failure:
        adapter.chat([{"role": "user", "content": "hi"}], [])
    assert failure.value.__suppress_context__
    assert len(_StubHandler.requests) == 1


def test_500_is_retried_then_succeeds(stub):
    _StubHandler.responses = [
        (500, {"error": {"message": "server blew up"}}),
        (200, _completion(text="recovered")),
    ]
    adapter = _adapter(stub)
    assert adapter.chat([{"role": "user", "content": "hi"}], []).text == \
        "recovered"
    assert len(_StubHandler.requests) == 2


def test_verify_error_state_keeps_message_without_key(stub):
    _StubHandler.responses = [
        (401, {"error": {"message": "invalid api key", "type": "auth"}})]
    adapter = _adapter(stub)
    status = adapter.verify()
    assert status.state == "error"
    assert "invalid api key" in status.detail
    assert CANARY_KEY not in status.detail


def test_verify_success_marks_verified(stub):
    _StubHandler.responses = [(200, _completion(text="pong"))]
    adapter = _adapter(stub)
    assert adapter.verify().state == "verified"


# ---------------------------------------------------------------------------
# Mock provider: deterministic, labelled everywhere
# ---------------------------------------------------------------------------

MOCK_QUESTIONS = [
    "Why will we breach our cash floor?",
    "What if the client pays seven days later?",
    "Which missing fact could change this decision?",
    "Compare the available plans.",
    "Draft a partial-payment request for this plan.",
]

EXPECTED_MOCK_SCRIPTS = {
    "Why will we breach our cash floor?": ["get_evidence", "compute_forecast"],
    "What if the client pays seven days later?": ["get_evidence",
                                                  "compute_forecast"],
    "Which missing fact could change this decision?": ["missing_facts",
                                                       "get_constraints"],
    "Compare the available plans.": ["compare_plans"],
    "Draft a partial-payment request for this plan.": ["compare_plans",
                                                       "draft_action"],
}


def test_mock_provider_scripts_the_five_questions():
    adapter = MockProviderAdapter()
    for question, expected in EXPECTED_MOCK_SCRIPTS.items():
        result = adapter.chat([{"role": "user", "content": question}], [])
        assert [c.name for c in result.tool_calls] == expected, question
        assert result.provider_label == adapter.provider_label
        assert MOCK_PROVIDER_LABEL in result.provider_label


def test_mock_provider_unknown_question_is_honest():
    adapter = MockProviderAdapter()
    result = adapter.chat([{"role": "user", "content": "Tell me a joke"}], [])
    assert result.tool_calls == []
    assert MOCK_PROVIDER_LABEL in result.text
    assert "supported scripted demo questions" in result.text


def test_mock_provider_labels_every_surface():
    adapter = MockProviderAdapter()
    assert MOCK_PROVIDER_LABEL in adapter.provider_label
    status = adapter.status()
    assert MOCK_PROVIDER_LABEL in status.detail
    assert MOCK_PROVIDER_LABEL in status.provider_label
    assert MOCK_PROVIDER_LABEL in adapter.verify().provider_label
    result = AdapterResult(provider_label=adapter.provider_label)
    assert MOCK_PROVIDER_LABEL in result.provider_label


def test_mock_final_answers_carry_the_mock_label():
    from cashflow_copilot.model_adapter import _finalize
    for key in ("breach_why", "delay_7", "missing_facts", "compare_plans",
                "draft_partial"):
        text = _finalize(key, {})
        assert MOCK_PROVIDER_LABEL in text, key

