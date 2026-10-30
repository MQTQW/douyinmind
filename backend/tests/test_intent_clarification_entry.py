"""Normal opt-in configuration only: fake control, no browser/model/subprocess."""
from copy import deepcopy

import pytest

from app.assistant import __main__ as entry
from app.assistant.clarification_sessions import ClarificationSessionStore,ClarificationError,DATABASE_NAME
from tests.test_intent_clarification_sessions import sample


@pytest.fixture
def configured(tmp_path,monkeypatch):
    state=tmp_path/'assistant';state.mkdir();calls=[]
    class Queue:
        def __init__(self,path,**kwargs):self.path=path;calls.append('queue')
        def get_control(self):return {'state':'stopped','run_id':None,'pid':None}
    class Control:
        def __init__(self,queue,state_dir=None):pass
        def snapshot(self):return {'state':'stopped','run_id':None,'pid':None}
        def start(self):calls.append('start')
        def finish(self,*args):raise AssertionError('configuration must not control an owner')
    monkeypatch.setattr(entry,'STATE',state)
    monkeypatch.setattr(entry,'STORAGE',tmp_path)
    monkeypatch.setattr(entry,'RECEIVER_STATE',tmp_path/'receiver-not-opened')
    monkeypatch.setattr(entry,'AssistantQueue',Queue)
    monkeypatch.setattr(entry,'AssistantControl',Control)
    monkeypatch.setattr(entry,'start_guard',lambda:calls.append('guard'))
    monkeypatch.setattr(entry,'run_registered',lambda *a,**k:pytest.fail('configuration never starts a helper'))
    return state,calls


def test_normal_cli_enable_is_guarded_configuration_not_start_and_epoch_is_once(configured,tmp_path):
    state,calls=configured
    args=['--state-dir',str(state),'--enable-intent-clarification']
    assert entry.main(args)==0 and calls==['queue','guard']
    store=ClarificationSessionStore(state/DATABASE_NAME);epoch=store.activated_at
    assert entry.main(args)==0 and ClarificationSessionStore(store.path).activated_at==epoch
    assert calls==['queue','guard','queue','guard']
    # Existing rows cannot become future admissions just because the feature
    # was enabled. No source/queue/task/barrier migration is invoked.
    fake=sample(tmp_path/'synthetic-other')
    task,source=deepcopy(fake.task),deepcopy(fake.source)
    task['created_at']=epoch-1;source['first_seen']=epoch-1
    with pytest.raises(ClarificationError):
        store.arm(task,source,reason_code='semantic_intent_unclear',semantic_plan_id='semantic-'+'d'*64)
    assert store.for_root(task['id']) is None


def test_failed_existing_start_guard_cannot_create_opt_in_state(configured,monkeypatch):
    state,calls=configured
    def blocked():calls.append('guard');raise ValueError('synthetic guard rejection')
    monkeypatch.setattr(entry,'start_guard',blocked)
    assert entry.main(['--state-dir',str(state),'--enable-intent-clarification'])==1
    assert calls==['queue','guard'] and not (state/DATABASE_NAME).exists()


def test_default_normal_start_does_not_enable_or_create_clarification(configured):
    state,calls=configured
    assert entry.main(['--state-dir',str(state)])==0
    assert calls==['queue','guard','start'] and not (state/DATABASE_NAME).exists()


@pytest.mark.parametrize('extra',[
    ['--run-registered'],['--review-native-source','native:'+'a'*64],
    ['--inspect-assistant-reply','1'*32],['--candidate-sha256','b'*64],
])
def test_enable_cannot_be_combined_with_owner_recovery_or_send_work(configured,extra):
    state,calls=configured
    with pytest.raises(SystemExit) as result:
        entry.main(['--state-dir',str(state),'--enable-intent-clarification',*extra])
    assert result.value.code==2 and calls==[] and not (state/DATABASE_NAME).exists()
