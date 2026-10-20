"""Loopback HTTP failure suppression; no external provider calls."""
import time
import pytest
from tests.test_model_adapter import stub, _StubHandler, _adapter, _completion, CANARY_KEY


@pytest.mark.parametrize('http_status',[500,502,503,504])
def test_exhausted_server_error_opens_cooldown_then_explicit_verify_can_recover(stub,http_status):
    _StubHandler.responses=[(http_status,{'error':{'message':CANARY_KEY}}),(200,_completion(text='OK'))]
    adapter=_adapter(stub,max_retries=0)
    first=adapter.verify()
    assert first.state=='error' and first.retry_after_seconds>=29
    assert CANARY_KEY not in first.detail
    assert adapter.verify().state=='error' and len(_StubHandler.requests)==1
    adapter._cooldown_until=time.monotonic()-1
    assert adapter.verify().state=='verified' and len(_StubHandler.requests)==2


def test_transport_failure_opens_cooldown_and_does_not_retry_ambiguous_remote_call(monkeypatch):
    import urllib.error
    from cashflow_copilot.model_adapter import OpenAICompatibleAdapter
    calls=[]
    class Fails:
        def open(self,*args,**kwargs):
            calls.append(1)
            raise urllib.error.URLError(CANARY_KEY)
    monkeypatch.setattr('urllib.request.build_opener',lambda *args:Fails())
    adapter=OpenAICompatibleAdapter(api_key=CANARY_KEY,base_url='http://127.0.0.1:1',model='local')
    assert adapter.verify().retry_after_seconds>=29
    assert adapter.verify().state=='error' and len(calls)==1
    assert CANARY_KEY not in adapter.status().detail
