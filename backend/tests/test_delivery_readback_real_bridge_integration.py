"""Real temporary stores through the Bridge and owner scheduler, with fake UI."""
from types import SimpleNamespace
import threading

import pytest

from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.runtime import AssistantRuntime
from tests.test_reply_readback_ledger import setup_unknown, setup_queue, rows


@pytest.mark.parametrize('weak',[False,True])
def test_read_only_confirmation_uses_real_credential_and_cannot_republish(tmp_path,monkeypatch,weak):
    case,env,agent,reply,observation=setup_unknown(tmp_path,weak=weak)
    queue,task=setup_queue(tmp_path,case,env,reply)
    before=rows(agent.path,('sqlite_master','agent_tasks','agent_replies'))
    bridge=LegacyBridge.__new__(LegacyBridge)
    bridge.agent=agent;bridge.queue=queue
    bridge.adapter=SimpleNamespace(check_intervention=lambda:None)
    bridge.browser_owner_thread_id=threading.get_ident()
    bridge.should_stop=lambda:False
    bridge.control=SimpleNamespace(snapshot=queue.get_control)
    bridge.admission_lock=threading.RLock()
    bridge._find=lambda task:agent.get_task(reply['task_id'])
    bridge._authorized=lambda task:True
    observed=[]
    def observe(adapter,target,text):
        observed.append((target,text))
        assert text==reply['text'] and target==reply['target']
        return observation
    import app.mention_receiver.choice_browser as module
    monkeypatch.setattr(module,'observe_existing_choice_reply',observe,raising=False)
    def forbidden(*args):
        pytest.fail('readback entered model/import/publish')
    runtime=AssistantRuntime(queue,bridge.control,run_id='owner',execute=forbidden,publish=forbidden,
        delivery_readback_adapter=bridge.observe_existing_delivery,
        recorded_readback_adapter=bridge.get_recorded_delivery)
    result=runtime.reconcile_delivery_once()
    assert result['state']==result['reply_state']=='b_view_visible'
    assert len(observed)==1
    assert rows(agent.path,('sqlite_master','agent_tasks','agent_replies'))==before
    assert agent.get_reply(reply['task_id'])['state']=='delivery_unknown'
    assert runtime.reconcile_delivery_once() is None
    assert len(observed)==1
    assert queue.claim_next('execute') is None and queue.claim_next('publish') is None
    assert result['execution_attempts']==result['publish_attempts']==1
