"""A proven-unsent original lookup failure can resume once, without another model."""
from datetime import datetime, timezone

import pytest

from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.runtime import AssistantRuntime
from app.assistant.queue import QueueError
from app.mention_agent.publisher import ReplyPreflightError
from tests.test_assistant_product_bridge import system, receive
from tests.test_assistant_legacy_extensions import QueryKnowledge, QueryModel
from tests.test_mention_agent_import_end_to_end import Adapter


class HeldOriginal(Adapter):
    def __init__(self, *, bad_recheck=False):
        super().__init__()
        self.prepares=0;self.clears=0;self.bad_recheck=bad_recheck
    def check_intervention(self):
        pass
    def prepare_reply(self,target,text):
        self.prepares+=1
        if self.prepares==1:
            raise ReplyPreflightError('original_notification_not_unique')
        proof=super().prepare_reply(target,text)
        if self.bad_recheck:proof['video_id']='7999999000000000099'
        return proof
    def clear_prepared_reply(self,target,text):
        self.clears+=1


def held_case(tmp_path, *, bad_recheck=False):
    binding,agent,env,registry,migrated,queue,history,control=system(tmp_path)
    control.snapshot=lambda:{'desired_state':control.desired,'run_id':'run-test','receiver_state':'ready'}
    task=receive(queue,history,registry,{**env,'comment_text':'@助手 请根据编程知识库说明Git。'})
    model=QueryModel();adapter=HeldOriginal(bad_recheck=bad_recheck)
    bridge=LegacyBridge(registry,queue,control,agent_path=tmp_path/'agent.sqlite3',conversation=history,
        database=tmp_path/'never-open-business.db',model_factory=lambda:model,
        knowledge_factory=lambda _:QueryKnowledge(),adapter=adapter)
    runtime=AssistantRuntime(queue,control,execute=bridge.execute,publish=bridge.publish,
        reconcile=bridge.reconcile,resume_adapter=bridge.operator_resume)
    ready=runtime.execute_once()
    failed=runtime.publish_once()
    assert failed['state']=='needs_review' and failed['reason_code']=='original_notification_not_unique'
    assert adapter.submits==0 and agent.get_reply(ready['agent_task_id'])['send_token'] is None
    return agent,queue,model,adapter,bridge,runtime,ready


def test_unsent_same_text_failure_is_rechecked_and_resumed_without_provider_or_import_replay(tmp_path):
    agent,queue,model,adapter,bridge,runtime,ready=held_case(tmp_path)
    snapshot=runtime.reconcile_task(ready['id'])
    assert snapshot['reason_code']=='preflight_failed'
    assert snapshot['details']['steps'][-1]['metadata']['code']=='original_notification_not_unique'
    assert agent.get_task(ready['agent_task_id'])['reason_code']=='original_notification_not_unique'
    assert queue.public_task(snapshot)['actions']['can_resume'] is True
    resumed=runtime.resume_task(ready['id'])
    assert resumed['state']=='ready_to_publish' and adapter.clears==1
    assert len(model.requests)==2 and adapter.submits==0
    done=runtime.publish_once()
    assert done['state']=='b_view_visible' and adapter.submits==1
    assert len(model.requests)==2 and len(agent.list_reply_hold_releases(ready['agent_task_id']))==1
    assert runtime.publish_once() is None
    assert queue.public_task(done)['actions']['can_resume'] is False


def test_fresh_but_wrong_video_proof_cannot_release_or_send_held_reply(tmp_path):
    agent,queue,model,adapter,bridge,runtime,ready=held_case(tmp_path,bad_recheck=True)
    runtime.reconcile_task(ready['id'])
    with pytest.raises(QueueError,match='operator_resume_unavailable'):
        runtime.resume_task(ready['id'])
    assert agent.get_reply(ready['agent_task_id'])['send_token'] is None
    assert agent.list_reply_hold_releases(ready['agent_task_id'])==[]
    assert adapter.submits==0 and len(model.requests)==2
