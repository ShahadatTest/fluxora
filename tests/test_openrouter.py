"""OpenRouter-shaped loopback HTTP stubs only: no live provider or billing."""
import copy
import json
import sqlite3
import shutil
import subprocess
import sys
from pathlib import Path
import threading
import time

import pytest

from cashflow_copilot import agent, agent_web
from cashflow_copilot.agent_requests import run_request
from cashflow_copilot.authz import owner_context_for
from cashflow_copilot.model_adapter import (
    AdapterError, OpenAICompatibleAdapter, OpenRouterAdapter,
    OPENROUTER_BASE_URL, OPENROUTER_MODEL, OPENROUTER_MAX_REQUEST_BYTES,
    MAX_CONTINUATION_BYTES,
)
from cashflow_copilot.store import SQLiteStore
from tests.test_model_adapter import stub, _StubHandler, _completion, CANARY_KEY
from tests.test_live_evaluation import plan
from tools.live_cases import CASES, case_ledger, evaluate
from tools.openrouter_ready import main as ready


def adapter(base):
    return OpenRouterAdapter(api_key=CANARY_KEY, base_url=base,
                             model=OPENROUTER_MODEL, max_retries=2)


def tool_response(name='compute_forecast', arguments=None, ident='call-forecast'):
    return _completion(tool_calls=[dict(id=ident, type='function',
        function=dict(name=name, arguments=json.dumps(arguments or {})))])


def configure(monkeypatch, key=CANARY_KEY):
    monkeypatch.setenv('CASHFLOW_MODEL_BASE_URL', OPENROUTER_BASE_URL)
    monkeypatch.setenv('CASHFLOW_MODEL_NAME', OPENROUTER_MODEL)
    if key is None:
        monkeypatch.delenv('CASHFLOW_MODEL_API_KEY', raising=False)
    else:
        monkeypatch.setenv('CASHFLOW_MODEL_API_KEY', key)
    agent_web.reset_live_adapter()


def test_environment_selects_openrouter_without_any_network(monkeypatch):
    configure(monkeypatch, key=None)
    monkeypatch.setattr(OpenAICompatibleAdapter, '_post', lambda *a, **k: pytest.fail('No startup call'))
    a = OpenAICompatibleAdapter.from_env()
    assert isinstance(a, OpenRouterAdapter) and a.status().state == 'disconnected'
    assert a.status().model == OPENROUTER_MODEL and a.verify().state == 'disconnected'
    assert 'openrouter' in a.provider_label and a.configuration_matches_env()


@pytest.mark.parametrize('base', [
    'https://openrouter.ai.attacker.invalid/api/v1',
    'https://attacker.invalid/openrouter.ai/api/v1',
    'https://openrouter.ai@attacker.invalid/api/v1',
    'http://openrouter.ai/api/v1', 'https://openrouter.ai/api/v2',
    'https://openrouter.ai:444/api/v1', 'https://openrouter.ai/api/v1?key=x',
    'https://openrouter.ai/api/v1#fragment',
])
def test_profile_detection_does_not_match_deceptive_urls(monkeypatch, base):
    configure(monkeypatch)
    monkeypatch.setenv('CASHFLOW_MODEL_BASE_URL', base)
    assert type(OpenAICompatibleAdapter.from_env()) is OpenAICompatibleAdapter


def test_local_readiness_check_without_key_is_explicitly_pending(monkeypatch, capsys):
    configure(monkeypatch, key=None)
    monkeypatch.setattr(OpenAICompatibleAdapter, '_post', lambda *a, **k: pytest.fail('Offline check'))
    assert ready() == 0
    report = json.loads(capsys.readouterr().out)
    assert report['openrouter_profile'] and not report['key_present']
    assert report['state'] == 'disconnected' and 'pending' in report['live_verification']


def test_readiness_with_key_stays_unverified_and_never_displays_key(monkeypatch, capsys):
    configure(monkeypatch)
    monkeypatch.setattr(OpenAICompatibleAdapter, '_post', lambda *a, **k: pytest.fail('Offline check'))
    assert ready() == 0
    out = capsys.readouterr().out
    assert CANARY_KEY not in out and json.loads(out)['state'] == 'configured-but-unverified'


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows PowerShell launcher')
@pytest.mark.parametrize('prompt', [False, True], ids=['no-key', 'mocked-hidden-prompt'])
def test_powershell_launcher_actual_offline_check_and_hidden_input(monkeypatch, prompt):
    import os
    from_env = dict(os.environ)
    for name in ('CASHFLOW_MODEL_API_KEY', 'CASHFLOW_MODEL_BASE_URL', 'CASHFLOW_MODEL_NAME'):
        from_env.pop(name, None)
    script = Path(__file__).resolve().parents[1]/'tools/start_openrouter.ps1'
    quote = lambda s: "'" + str(s).replace("'", "''") + "'"
    command = "$ErrorActionPreference='Stop'; "
    if prompt:
        command += ("function Read-Host { param($Prompt,[switch]$AsSecureString); "
                    "ConvertTo-SecureString 'dummy-hidden-input-only' -AsPlainText -Force }; ")
    command += '& ' + quote(script) + ' -Python ' + quote(sys.executable) + ' -CheckOnly'
    if prompt:
        command += " -PromptForKey; if ($env:CASHFLOW_MODEL_API_KEY -cne 'dummy-hidden-input-only') { throw 'Hidden input was not passed locally' }; Remove-Item Env:CASHFLOW_MODEL_API_KEY"
    completed = subprocess.run([shutil.which('pwsh') or 'powershell', '-NoProfile', '-Command', command],
        env=from_env, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    assert 'dummy-hidden-input-only' not in completed.stdout and 'dummy-hidden-input-only' not in completed.stderr
    report = json.loads(completed.stdout)
    assert report['key_present'] == prompt and report['openrouter_profile']
    assert report['state'] == ('configured-but-unverified' if prompt else 'disconnected')
    assert 'qwen/qwen3.7-flash' in report['provider_label']
    assert report['request_policy']['reasoning_max_tokens']==512
    assert report['request_policy']['max_tokens_per_response']==2048


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows PowerShell launcher')
@pytest.mark.parametrize('existing,override,expected',[
    ('openai/gpt-6-luna','', 'openai/gpt-6-luna'),
    ('openai/gpt-6-luna','deepseek/deepseek-v4-flash','deepseek/deepseek-v4-flash')])
def test_launcher_preserves_explicit_environment_or_accepts_model_option(existing,override,expected):
    import os
    env=dict(os.environ,CASHFLOW_MODEL_NAME=existing)
    env.pop('CASHFLOW_MODEL_API_KEY',None)
    script=Path(__file__).resolve().parents[1]/'tools/start_openrouter.ps1'
    command=[shutil.which('pwsh') or 'powershell','-NoProfile','-File',str(script),'-Python',sys.executable,'-CheckOnly']
    if override:command+=['-Model',override]
    result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert expected in json.loads(result.stdout)['provider_label']


def test_openrouter_http_request_limits_and_routing(stub):
    _StubHandler.responses = [(200, _completion('local stub answer'))]
    a = adapter(stub)
    assert a.chat([{'role': 'user', 'content': 'Synthetic test'}], []).text == 'local stub answer'
    req = _StubHandler.requests[0]; body = req['body']
    assert req['path'] == '/chat/completions'
    assert req['headers']['Authorization'] == 'Bearer ' + CANARY_KEY
    assert body['model'] == OPENROUTER_MODEL and body['stream'] is False
    assert body['max_tokens'] == 2048 and body['reasoning'] == {'max_tokens': 512}
    assert body['provider'] == dict(require_parameters=True, data_collection='deny', allow_fallbacks=False)
    assert body['tool_choice'] == 'auto' and 'models' not in body and CANARY_KEY not in json.dumps(body)


def test_verification_uses_bounded_nonreasoning_ping_without_financial_context(stub):
    _StubHandler.responses = [(200, _completion('OK'))]
    assert adapter(stub).verify().state == 'verified'
    body = _StubHandler.requests[0]['body']
    assert body['messages'] == [{'role': 'user', 'content': 'Reply OK.'}]
    assert body['max_tokens'] == 64 and body['reasoning'] == {'enabled': False}
    assert 'tools' not in body and 'tool_choice' not in body
    assert body['provider']['require_parameters']


def test_minimax_bounded_reasoning_profile_preserves_response_and_privacy_bounds(stub):
    _StubHandler.responses=[(200,_completion('local stub answer')),(200,_completion('OK'))]
    a=OpenRouterAdapter(api_key=CANARY_KEY,base_url=stub,model='minimax/minimax-m3',max_retries=0)
    assert a.chat([{'role':'user','content':'Synthetic test'}],[]).text=='local stub answer'
    assert a.verify().state=='verified'
    chat,ping=[r['body'] for r in _StubHandler.requests]
    assert chat['reasoning']=={'max_tokens':512}
    assert ping['reasoning']=={'enabled':False}
    assert chat['max_tokens']==4096 and ping['max_tokens']==64
    assert chat['provider']==dict(require_parameters=True,data_collection='deny',allow_fallbacks=False)
    assert a.request_policy()['reasoning_max_tokens']==512
    assert a.request_policy()['max_tokens_per_response']==4096


def test_verification_survives_page_render_and_invalidates_on_key_model_endpoint_restart(stub, monkeypatch):
    from cashflow_copilot import web
    configure(monkeypatch)
    # All verification HTTP is delegated to loopback; the application keeps the
    # canonical public configuration identity. This is not a real gateway call.
    monkeypatch.setattr(OpenRouterAdapter, '_post',
        lambda self, body, **kw: OpenAICompatibleAdapter(api_key=CANARY_KEY, base_url=stub,
            model=OPENROUTER_MODEL)._post(body, **kw))
    _StubHandler.responses = [(200, _completion('OK'))]
    a = agent_web.live_adapter(); assert a.verify().state == 'verified'
    assert agent_web.live_adapter() is a and 'verified' in agent_web.banner_status()
    monkeypatch.setattr(web, 'read_state', lambda fn: fn(case_ledger('breach')))
    html = agent_web._panel_html(web)
    assert 'configured-but-unverified' not in html and '2,048 output tokens' in html
    assert CANARY_KEY not in html and len(_StubHandler.requests) == 1
    for name, value in [('CASHFLOW_MODEL_NAME', 'qwen/another-reviewed-model'),
                        ('CASHFLOW_MODEL_API_KEY', 'dummy-changed-key'),
                        ('CASHFLOW_MODEL_BASE_URL', 'https://other.invalid/v1')]:
        monkeypatch.setenv(name, value)
        new = agent_web.live_adapter()
        assert new is not a and new.status().state == 'configured-but-unverified'
        a = new
    agent_web.reset_live_adapter()
    assert agent_web.live_adapter() is not a
    assert agent_web.live_adapter().status().state == 'configured-but-unverified'


@pytest.mark.parametrize('variant', ['details', 'reasoning', 'reasoning_content'])
def test_reasoning_continuation_round_trips_but_never_enters_answer_or_sqlite(stub, tmp_path, variant):
    l = case_ledger('breach'); before = copy.deepcopy(vars(l)); payload = tool_response()
    secret_marker = 'LOCAL-EPHEMERAL-CONTINUATION-ONLY'
    details = [dict(type='reasoning.text', text=secret_marker, signature='signed-stub', index=0),
               dict(type='reasoning.encrypted', data='opaque-test-data', index=1)]
    payload['choices'][0]['message'][variant if variant != 'details' else 'reasoning_details'] = details if variant == 'details' else secret_marker
    _StubHandler.responses = [(200, payload), (200, _completion('LOCAL STUB COMMENTARY, not verified'))]
    db = tmp_path/'history.sqlite'
    run = agent.run_agent_question(l, 'Compute forecast', adapter(stub), db_path=db, data_mode='minimized')
    assert run.outcome == 'completed' and run.tool_calls[0]['name'] == 'compute_forecast'
    sent = _StubHandler.requests[1]['body']['messages']
    assistant = next(m for m in sent if m['role'] == 'assistant')
    assert assistant['tool_calls'][0]['id'] == 'call-forecast'
    if variant == 'details': assert assistant['reasoning_details'] == details
    else: assert assistant['reasoning'] == secret_marker
    assert sent[-1]['role'] == 'tool' and sent[-1]['tool_call_id'] == 'call-forecast'
    assert secret_marker not in run.answer and secret_marker not in json.dumps(agent.list_agent_runs(db))
    assert secret_marker.encode() not in db.read_bytes()
    assert {k:v for k,v in vars(l).items() if k != 'audit'} == {k:v for k,v in before.items() if k != 'audit'}
    assert run.disclosure['request_policy']['max_tokens_per_response'] == 2048


@pytest.mark.parametrize('bad', ['not-list', [1], {'type': 'reasoning.text'}])
def test_malformed_reasoning_stops_before_tools(stub, bad):
    payload = tool_response(); payload['choices'][0]['message']['reasoning_details'] = bad
    _StubHandler.responses = [(200, payload)]
    run = agent.run_agent_question(case_ledger('breach'), 'Forecast', adapter(stub))
    assert run.outcome == 'provider_error' and run.tool_calls == []


@pytest.mark.parametrize('value', [CANARY_KEY, 'x' * MAX_CONTINUATION_BYTES], ids=['dummy-secret', 'oversize'])
def test_secret_or_oversize_reasoning_fails_closed(stub, value):
    payload = tool_response(); payload['choices'][0]['message']['reasoning'] = value
    _StubHandler.responses = [(200, payload)]
    with pytest.raises(AdapterError, match='local bounds') as failure:
        adapter(stub).chat([], [])
    assert CANARY_KEY not in str(failure.value)


@pytest.mark.parametrize('reason', ['length', 'error', 'content_filter'])
def test_incomplete_provider_result_cannot_create_even_a_draft(stub, reason):
    l = case_ledger('draft'); before = copy.deepcopy(vars(l)); calls = plan(next(c for c in CASES if c.id == 'draft'))
    p = tool_response(calls[0].name, calls[0].arguments); p['choices'][0]['finish_reason'] = reason
    _StubHandler.responses = [(200, p)]
    run = agent.run_agent_question(l, 'Draft the request', adapter(stub))
    assert run.outcome == 'provider_error' and run.tool_calls == [] and l.actions == {}
    assert vars(l) == before


def test_request_byte_bound_rejects_before_any_http(stub):
    with pytest.raises(AdapterError, match='request exceeds'):
        adapter(stub).chat([{'role': 'user', 'content': 'x' * OPENROUTER_MAX_REQUEST_BYTES}], [])
    assert not _StubHandler.requests


def test_http_200_error_body_never_displays_or_persists_dummy_key(stub, tmp_path):
    _StubHandler.responses = [(200, dict(error=dict(message='raw provider ' + CANARY_KEY),
        choices=_completion('forged answer')['choices']))]
    db = tmp_path/'error.sqlite'
    r = agent.run_agent_question(case_ledger('breach'), 'Forecast', adapter(stub), db_path=db)
    saved = json.dumps(agent.list_agent_runs(db))
    assert r.outcome == 'provider_error' and 'raw provider' not in saved and CANARY_KEY not in saved
    assert 'forged answer' not in r.answer


def test_http_retry_exhaustion_has_no_tool_side_effects(stub, monkeypatch):
    monkeypatch.setattr('cashflow_copilot.model_adapter.RETRY_BACKOFF_S', (.001, .001))
    _StubHandler.responses = [(429, {'error': {'message': CANARY_KEY}})] * 4
    a = adapter(stub); l = case_ledger('draft')
    r = agent.run_agent_question(l, 'Draft', a)
    assert r.outcome == 'provider_error' and not l.actions and not r.tool_calls
    assert len(_StubHandler.requests) == 3 and CANARY_KEY not in a.status().detail
    assert all(q['body']['max_tokens'] == 2048 for q in _StubHandler.requests)


def test_late_http_draft_reply_is_rejected_without_background_tool_work(stub):
    case = next(c for c in CASES if c.id == 'draft'); tc = plan(case)[0]
    _StubHandler.responses = [(200, tool_response(tc.name, tc.arguments))]; _StubHandler.sleep_s = .15
    l = case_ledger('draft'); r = agent.run_agent_question(l, case.question, adapter(stub), time_budget_s=.03)
    time.sleep(.2)
    assert r.outcome in {'timeout', 'provider_error'} and not l.actions and not r.tool_calls


def test_multistep_draft_is_saved_only_once_with_policy_and_survives_reload(stub, tmp_path):
    case = next(c for c in CASES if c.id == 'draft'); tc = plan(case)[0]
    _StubHandler.responses = [(200, tool_response('get_evidence', ident='read')),
        (200, tool_response(tc.name, tc.arguments, ident='draft')),
        (200, _completion('LOCAL STUB: draft only, owner review required'))]
    db = tmp_path/'draft.sqlite'; s = SQLiteStore(db); l = case_ledger('draft'); s.save_ledger(l); s.close()
    lock = threading.RLock(); a = adapter(stub); ctx = owner_context_for(l.business_id)
    done = run_request(db, 'a'*32, case.question, 'live', a, ctx=ctx, lock=lock, data_mode='minimized')
    replay = run_request(db, 'a'*32, case.question, 'live', adapter(stub), ctx=ctx, lock=lock, data_mode='minimized')
    assert done.status == 'completed' and replay.replayed and len(_StubHandler.requests) == 3
    s = SQLiteStore(db)
    try: restored = s.load_ledger()
    finally: s.close()
    assert len(restored.actions) == 1 and restored.actions['eval-draft'].state == 'draft'
    assert restored.receipts == l.receipts and restored.consent_map == l.consent_map
    assert replay.result.disclosure == done.result.disclosure
    assert agent.list_agent_runs(db)[0]['disclosure']['request_policy']['require_parameters']


@pytest.mark.parametrize('case', CASES, ids=lambda c:c.id)
def test_synthetic_reference_cases_through_openrouter_http_profile(stub, case):
    calls = plan(case)
    responses = []
    if calls:
        responses.append((200, _completion(tool_calls=[dict(id=c.id, type='function',
            function=dict(name=c.name, arguments=json.dumps(c.arguments))) for c in calls])))
    responses.append((200, _completion('Scripted HTTP stub commentary; manual review pending')))
    _StubHandler.responses = responses
    row = evaluate(lambda _: adapter(stub), [case], mode='scripted_test', data_mode='minimized')[0]
    assert row['automated_passed'], [c for c in row['checks'] if not c['passed']]
    assert not row['financial_prose_verified'] and row['manual_review_status'] == 'pending'
