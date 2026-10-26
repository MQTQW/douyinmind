from pathlib import Path
import hashlib
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.assistant import usage_routes as routes
from app.assistant.registry_routes import router as registry_router
from app.assistant.usage_budget import UsageBudget, MARKER_NAME


@pytest.fixture
def client(tmp_path, monkeypatch):
    path=tmp_path/'usage-budget.sqlite3'
    monkeypatch.setattr(routes,'BUDGET_PATH',path)
    app=FastAPI(); app.include_router(registry_router); app.include_router(routes.router)
    with TestClient(app,base_url='http://127.0.0.1:8000',client=('127.0.0.1',5001)) as c:
        c.headers.update({'Origin':'http://127.0.0.1:5173','Content-Type':'application/json'})
        yield c,path


def admin(c):
    response=c.post('/assistant/registry/admin-session',json={'confirm_local_admin':True})
    assert response.status_code==200
    return {'Authorization':'Bearer '+response.json()['admin_token']}


def test_get_uninitialized_never_creates_ledger_or_marker(client):
    c,path=client
    result=c.get('/assistant/usage')
    assert result.status_code==200 and result.json()['initialized'] is False
    assert not path.exists() and not (path.parent/MARKER_NAME).exists()


def test_readonly_usage_and_full_admin_policy_update_with_stale_rejection(client):
    c,path=client; budget=UsageBudget.prepare(path)
    budget.reserve('task','request',100); budget.settle('request',{'total_tokens':1140})
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    view=c.get('/assistant/usage').json()
    assert view['daily']['actual_tokens']==1140 and view['currency_cost'] is None
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    body={'per_task_tokens':2000,'daily_tokens':5000,'expected_revision':view['policy']['revision']}
    assert c.put('/assistant/usage/limits',json=body).status_code==401
    headers=admin(c)
    response=c.put('/assistant/usage/limits',headers=headers,json=body)
    assert response.status_code==200 and response.json()['policy']['daily_tokens']==5000
    stale=c.put('/assistant/usage/limits',headers=headers,json={**body,'daily_tokens':None})
    assert stale.status_code==409 and stale.json()['detail']['code']=='usage_policy_changed'
    assert budget.summary()['policy']['daily_tokens']==5000
    assert budget.summary()['daily']['actual_tokens']==1140


@pytest.mark.parametrize('change',[{'per_task_tokens':True},{'daily_tokens':-1},{'daily_tokens':2.5},{'expected_revision':True},{'path':'elsewhere'}])
def test_invalid_policy_does_not_change_existing_limits(client,change):
    c,path=client; budget=UsageBudget.prepare(path)
    headers=admin(c)
    response=c.put('/assistant/usage/limits',headers=headers,json={'per_task_tokens':None,'daily_tokens':None,'expected_revision':1,**change})
    assert response.status_code==422
    assert budget.summary()['policy']=={'per_task_tokens':None,'daily_tokens':None,'revision':1}


def test_missing_ledger_with_marker_is_not_fresh_allowance(client):
    c,path=client; UsageBudget.prepare(path); path.unlink()
    response=c.get('/assistant/usage')
    assert response.status_code==503 and response.json()['detail']['code']=='usage_budget_missing'
    assert not path.exists()


def test_dangling_marker_is_not_uninitialized(client):
    c,path=client; marker=path.parent/MARKER_NAME
    try: marker.symlink_to(path.parent/'nonexistent-marker-target')
    except OSError: pytest.skip('This Windows session cannot create a temporary symlink')
    response=c.get('/assistant/usage')
    assert response.status_code==503
    assert marker.is_symlink() and not path.exists()


def test_foreign_origin_cannot_read_or_update_local_caps(client):
    c,path=client; UsageBudget.prepare(path)
    response=c.get('/assistant/usage',headers={'Origin':'https://third-party.invalid'})
    assert response.status_code==403
