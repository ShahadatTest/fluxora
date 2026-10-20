import json,time,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest
from cashflow_copilot.model_adapter import OpenAICompatibleAdapter,AdapterError
from cashflow_copilot import agent_web,web
from tests.test_web import live_server,_post

class RateStub(BaseHTTPRequestHandler):
    count=0;hint='45';key='DUMMY-RATE-HEADER-SECRET'
    def do_POST(self):
        type(self).count+=1;self.rfile.read(int(self.headers['Content-Length']))
        if type(self).count==1:
            self.send_response(429);self.send_header('Retry-After',self.hint);data=json.dumps(dict(error=dict(message=self.key))).encode()
        else:
            self.send_response(200);data=json.dumps(dict(choices=[dict(message=dict(content='OK'),finish_reason='stop')])).encode()
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def log_message(self,*args):pass

@pytest.fixture
def rate_server():
    RateStub.count=0;RateStub.hint='45'
    server=ThreadingHTTPServer(('127.0.0.1',0),RateStub);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield 'http://127.0.0.1:'+str(server.server_address[1])
    server.shutdown();thread.join(timeout=5);server.server_close()

def test_retry_after_cooldown_suppresses_calls_until_expired_then_recovers(rate_server):
    a=OpenAICompatibleAdapter(api_key=RateStub.key,base_url=rate_server,model='stub')
    assert a.verify().state=='error' and a.status().retry_after_seconds>=44
    assert RateStub.count==1
    assert a.verify().state=='error' and RateStub.count==1
    assert RateStub.key not in json.dumps(vars(a.status()))
    a._cooldown_until=time.monotonic()-1
    assert a.verify().state=='verified' and RateStub.count==2 and a.status().retry_after_seconds==0

@pytest.mark.parametrize('hint',['NaN','-1','DUMMY-RATE-HEADER-SECRET','x'*101,''])
def test_bad_retry_headers_are_never_echoed_or_interpreted_as_credentials(hint):
    assert OpenAICompatibleAdapter._retry_after({'Retry-After':hint}) is None

def test_concurrent_provider_call_is_rejected_without_network(rate_server):
    a=OpenAICompatibleAdapter(api_key=RateStub.key,base_url=rate_server,model='stub')
    a._transport_lock.acquire()
    try:
        with pytest.raises(AdapterError,match='already running'):a._post({})
        assert RateStub.count==0
    finally:a._transport_lock.release()

def test_wrong_model_financial_claim_is_hidden_and_never_used_in_primary_or_history_summary():
    calls=[dict(name='compute_forecast',ok=True,result=dict(currencies={'USD':dict(min_balance='USD -400.00',min_balance_date='2027-01-08',closing='USD 50.00',first_breach_date='2027-01-07',worst_shortfall='USD 600.00')}))]
    primary=agent_web._deterministic_answer_html(web,calls)
    wrong=agent_web._answer_html(web,'The shortfall is USD 650.00 <script>bad()</script>')
    assert 'USD 600.00' in primary and 'USD 650.00' not in primary
    assert 'USD 650.00' not in '; '.join(agent_web._deterministic_lines(calls))
    assert "data-unverified-commentary" in wrong and '<details open' not in wrong
    assert '<script>' not in wrong and 'USD 650.00' in wrong

def test_retry_after_http_date_is_parsed_without_returning_raw_header():
    from datetime import datetime,timedelta,timezone
    from email.utils import format_datetime
    header=format_datetime(datetime.now(timezone.utc)+timedelta(seconds=120),usegmt=True)
    delay=OpenAICompatibleAdapter._retry_after({'Retry-After':header})
    assert 118<=delay<=121

def test_normal_http_primary_answer_precedes_closed_prose_and_long_question_can_wrap(live_server):
    base,_=live_server;agent_web.mount(web)
    status,html=_post(base,'/agent/ask',dict(csrf=web.CSRF_TOKEN,request_id='e'*32,business_id='demo-agency',question='Review source hash '+'a'*128,provider='mock',data_mode='minimized'))
    assert status==200
    assert "class='mut agent-question' style='overflow-wrap:anywhere'" in html
    assert 'main{overflow-wrap:anywhere;min-width:0}' in html
    assert html.index('Deterministic answer')<html.index('data-unverified-commentary')
    assert "<details class='card' data-unverified-commentary>" in html
