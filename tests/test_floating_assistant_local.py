"""Private local handoff uses existing model qualification/disclosure gates."""
import copy
from cashflow_copilot import task_qualification
from tests.test_local_connections import settings,add
from tests.test_web import _load


def test_private_handoff_preserves_exact_escaped_question_and_original_gate(settings,monkeypatch):
    app,c=settings;add(c);ident=app.local_connections.public()[0]['id'];c.post('/settings/connections/activate',id=ident)
    adapter=app.local_connections.active_adapter()
    def forbidden(*args,**kwargs):raise AssertionError('Handoff cannot call model or verify automatically')
    monkeypatch.setattr(adapter,'chat',forbidden);monkeypatch.setattr(adapter,'verify',forbidden)
    before=copy.deepcopy(vars(_load(app.workspace_database)))
    code,body,_=c.post('/assistant/continue',question='Prepare a draft <script>alert(1)</script> for review')
    assert code==200 and b'&lt;script&gt;' in body and b'<script>alert(1)' not in body
    assert b'action=\'/agent/tasks/start\'' in body and b'formaction=\'/agent/tasks/preview\'' in body and b'Preview before sending</button>' in body
    assert b"<details class='task-options'><summary>Options</summary>" in body
    assert b"<button type='submit' disabled>" in body and b'configuration needs the current model check' in body
    assert b'provider billing and retention apply' in body and b'Final approval stays separate' in body
    assert vars(_load(app.workspace_database))==before
    monkeypatch.setattr(task_qualification,'status',lambda *a:'qualified')
    code,body,_=c.post('/assistant/continue',question='Show the baseline cash forecast. Read only.')
    assert code==200 and b"<button type='submit' disabled>" not in body and b'>Send</button>' in body
    assert b'name=\'business_id\'' in body and b'name=\'request_id\'' in body
    assert vars(_load(app.workspace_database))==before


def test_handoff_rejects_csrf_origin_extra_fields_queries_and_empty_question(settings):
    app,c=settings;c.request('/demo')
    assert c.request('/assistant/continue',dict(csrf='wrong',question='forecast'))[0]==403
    assert c.request('/assistant/continue',dict(csrf=c.csrf,question='forecast'),extra={'Origin':'https://evil.example'})[0]==403
    for fields in [dict(question=''),dict(question=' '),dict(question='x'*2001),dict(question='forecast',role='owner')]:
        assert c.post('/assistant/continue',**fields)[0]==400
    assert c.post('/assistant/continue?key=dummy',question='forecast')[0]==400
