"""One explicit failed read-only query retry: temporary ledgers, fake providers."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.queue import AssistantQueue, QueueError
from app.assistant.runtime import AssistantRuntime
from app.mention_agent.service import AgentService
from app.mention_agent.store import AgentStore, AgentStoreError
from tests.test_assistant_product_bridge import system, receive
from tests.test_assistant_legacy_extensions import QueryKnowledge, QueryModel


class Knowledge(QueryKnowledge):
    fail = True
    def query(self, question):
        if self.fail:
            raise RuntimeError('synthetic scoped query failure')
        return super().query(question)


class PaidModel(QueryModel):
    def __init__(self, tokens):
        super().__init__(); self.tokens=tokens
    def complete(self, messages, tools):
        assert not any(item['function']['name']=='save_current_video' for item in tools)
        return replace(super().complete(messages,tools),usage={'total_tokens':self.tokens})


def failed_case(tmp_path, *, public=True):
    b,a,e,r,m,q,h,c=system(tmp_path,public=public)
    task=receive(q,h,r,{**e,'comment_text':'@助手 请根据编程知识库说明Git。'})
    knowledge=Knowledge();models=[PaidModel(1140),PaidModel(100)]
    supplied=[]
    def model_factory():
        value=models[len(supplied)];supplied.append(value);return value
    bridge=LegacyBridge(r,q,c,agent_path=a.path,conversation=h,database=tmp_path/'never-open-business.db',
        model_factory=model_factory,knowledge_factory=lambda _:knowledge)
    runtime=AssistantRuntime(q,c,execute=bridge.execute,reconcile=bridge.reconcile,
        resume_adapter=bridge.operator_resume,retry_adapter=bridge.operator_retry_query)
    failed=runtime.execute_once()
    assert failed['state']=='needs_review' and failed['reason_code']=='knowledge_query_failed'
    assert failed['usage']['total_tokens']==1140 and len(supplied)==1 and len(models[0].requests)==1
    return SimpleNamespace(binding=b,agent=a,registry=r,migrated=m,queue=q,history=h,control=c,
        bridge=bridge,runtime=runtime,task=failed,knowledge=knowledge,models=models,supplied=supplied,root=tmp_path)


def reconcile(case):
    result=case.runtime.reconcile_task(case.task['id'])
    assert case.queue.public_task(result)['actions']['can_retry_query'] is True
    assert case.queue.public_task(result)['actions']['can_resume'] is False
    return result


def test_same_task_retry_preserves_failure_steps_paid_usage_and_no_import(tmp_path):
    case=failed_case(tmp_path);agent_id=case.task['agent_task_id']
    # A known persisted old measurement must survive, not be replaced by the
    # much shorter new attempt. No production telemetry is touched.
    import json
    with case.queue._tx() as db:
        details=case.task['details'];details['timings']['execution_ms']=500000
        db.execute('UPDATE assistant_tasks SET details=? WHERE id=?',(json.dumps(details),case.task['id']))
    case.task=case.queue.get_task(case.task['id'])
    old_steps=case.agent.list_steps(agent_id);old_ms=case.task['details']['timings']['execution_ms']
    assert case.queue.public_task(case.task)['actions']['can_retry_query'] is False
    reconcile(case);case.knowledge.fail=False
    armed=case.runtime.retry_query_task(case.task['id'])
    intent=case.queue.get_query_retry(case.task['id'])
    assert armed['state']=='queued' and intent['state']=='queued'
    assert case.agent.get_task(agent_id)['task_id']==agent_id
    # Ordinary handler replays cannot claim an armed paid attempt without its ID.
    assert case.agent.claim(agent_id) is None
    done=case.runtime.execute_once()
    assert done['id']==case.task['id'] and done['agent_task_id']==agent_id
    assert done['state']=='ready_to_publish' and done['usage']['total_tokens']==1340
    assert done['usage']['model_responses']==3
    assert done['details']['timings']['execution_ms']>=old_ms
    assert 'retrieval_ms' not in done['details']['timings'], 'old failed unmeasured query must not become a complete total'
    assert case.agent.list_steps(agent_id)[:len(old_steps)]==old_steps
    assert case.agent.get_task_ingestion(agent_id) is None
    assert case.agent.get_reply(agent_id)['send_token'] is None
    assert case.agent.get_query_retry(agent_id)['state']=='consumed'
    assert case.queue.get_query_retry(case.task['id'])['state']=='finished'
    assert case.runtime.retry_query_task(case.task['id'])['state']=='ready_to_publish'
    assert case.runtime.execute_once() is None and len(case.supplied)==2
    again=case.queue.request_action(case.task['id'],'retry_query')
    assert again['id']==intent['action_id'] and again['state']=='done'
    assert case.runtime.process_action_once() is None
    assert not (tmp_path/'never-open-business.db').exists()


def test_async_action_path_arms_without_running_model_on_action_lane(tmp_path):
    case=failed_case(tmp_path);reconcile(case)
    action=case.queue.request_action(case.task['id'],'retry_query')
    assert case.runtime.process_action_once()['state']=='done'
    assert len(case.supplied)==1 and case.queue.get_task(case.task['id'])['state']=='queued'
    assert case.queue.latest_action(case.task['id'])['id']==action['id']
    case.knowledge.fail=False
    assert case.runtime.execute_once()['usage']['total_tokens']==1340


@pytest.mark.parametrize('change',['revoke','library','envelope','publication','incomplete','reply','ingestion','propose'])
def test_scope_incomplete_model_or_side_effect_barriers_refuse_retry(tmp_path,change):
    case=failed_case(tmp_path);agent_id=case.task['agent_task_id']
    if change=='revoke':
        case.registry.revoke_grant(case.migrated['grant_id'])
    elif change in {'library','envelope','publication'}:
        with case.queue._tx() as db:
            if change=='publication':
                db.execute("UPDATE assistant_tasks SET publication_token='synthetic-retained-publication-token' WHERE id=?",(case.task['id'],))
            elif change=='library':
                import json
                value=case.task['binding'].copy();value['knowledge_base_id']=8
                db.execute('UPDATE assistant_tasks SET binding=? WHERE id=?',(json.dumps(value),case.task['id']))
            else:
                import json
                value=case.task['envelope'].copy();value['comment_text']='另一个问题'
                db.execute('UPDATE assistant_tasks SET envelope=? WHERE id=?',(json.dumps(value),case.task['id']))
    else:
        with case.agent._tx() as db:
            if change=='incomplete':
                db.execute("DELETE FROM agent_steps WHERE task_id=? AND kind='model_response'",(agent_id,))
            elif change=='reply':
                import json
                proposal={'kind':'answer','text':'合成已预约正文','source_ids':['资料1'],'sources':[]}
                db.execute("INSERT INTO agent_replies VALUES('synthetic-reply',?,?,?,'reserved',0,'synthetic-send-token',NULL,NULL)",(agent_id,json.dumps(proposal),json.dumps(case.task['envelope'])))
            elif change=='ingestion':
                case.agent._create_ingestion_tables(db)
                db.execute("INSERT INTO agent_task_ingestions VALUES(?,'synthetic-linked-job',0)",(agent_id,))
            else:
                db.execute("INSERT INTO agent_steps(task_id,kind,metadata,created_at) VALUES(?,'decision','{}',0)",(agent_id,))
    with pytest.raises((QueueError,ValueError,AgentStoreError)):
        case.runtime.retry_query_task(case.task['id'])
    assert len(case.supplied)==1
    assert case.queue.get_task(case.task['id'])['state']=='needs_review'


def test_private_query_can_retry_without_granting_public_reply(tmp_path):
    case=failed_case(tmp_path,public=False);reconcile(case);case.knowledge.fail=False
    case.runtime.retry_query_task(case.task['id'])
    done=case.runtime.execute_once()
    assert done['state']=='completed_private' and case.agent.get_reply(done['agent_task_id'])['send_token'] is None


def test_concurrent_request_and_same_action_replay_only_create_one_intent(tmp_path):
    case=failed_case(tmp_path);reconcile(case)
    def request(_):
        return AssistantQueue(case.queue.path).request_action(case.task['id'],'retry_query')['id']
    with ThreadPoolExecutor(max_workers=2) as workers:
        ids=list(workers.map(request,range(2)))
    assert ids[0]==ids[1]
    assert case.runtime.process_action_once()['state']=='done'
    assert case.runtime.process_action_once() is None
    case.knowledge.fail=False
    assert case.runtime.execute_once()['usage']['total_tokens']==1340
    assert len(case.supplied)==2


def test_second_agent_intent_and_ordinary_resume_cannot_rearm_paid_work(tmp_path):
    case=failed_case(tmp_path);reconcile(case)
    with pytest.raises(QueueError):case.runtime.resume_task(case.task['id'])
    case.runtime.retry_query_task(case.task['id'])
    with pytest.raises(AgentStoreError,match='query_retry_already_used'):
        case.agent.arm_failed_query_retry(case.task['agent_task_id'],'a'*32,
            binding=case.task['binding'],envelope=case.task['envelope'])
    assert len(case.supplied)==1


def test_crash_after_product_claim_is_held_without_a_second_model_attempt(tmp_path):
    case=failed_case(tmp_path);case.runtime.retry_query_task(case.task['id'])
    claimed=case.queue.claim_next('execute',run_id='dead-run')
    assert claimed['_query_retry_action_id']==case.queue.get_query_retry(case.task['id'])['action_id']
    # Simulate claim plus agent admission, then process loss before model request.
    old=case.agent.claim(case.task['agent_task_id'],query_retry_action_id=claimed['_query_retry_action_id'])
    assert old is not None
    fresh=AssistantQueue(case.queue.path);fresh.recover_interrupted(current_run_id='new-run')
    assert fresh.get_query_retry(case.task['id'])['state']=='held'
    assert fresh.claim_next('execute') is None
    assert case.runtime.retry_query_task(case.task['id'])['state']=='needs_review'
    assert case.agent.claim(case.task['agent_task_id'],query_retry_action_id=claimed['_query_retry_action_id']) is None
    assert len(case.supplied)==1


def test_arm_gap_failure_is_held_and_replay_does_not_run_model(tmp_path,monkeypatch):
    case=failed_case(tmp_path)
    monkeypatch.setattr(case.queue,'activate_query_retry',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('synthetic failure after agent arm')))
    with pytest.raises(RuntimeError):case.runtime.retry_query_task(case.task['id'])
    assert case.agent.get_query_retry(case.task['agent_task_id'])['state']=='armed'
    assert case.queue.get_query_retry(case.task['id'])['state']=='held'
    assert case.agent.claim(case.task['agent_task_id']) is None
    assert case.runtime.retry_query_task(case.task['id'])['state']=='needs_review'
    assert len(case.supplied)==1


def test_old_owned_schema_gains_only_lazy_retry_tables_and_retains_old_rows(tmp_path):
    case=failed_case(tmp_path);old_steps=case.agent.list_steps(case.task['agent_task_id'])
    with case.agent._tx() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_query_retries'").fetchone()
    with case.queue._connection() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='assistant_query_retries'").fetchone()
    # Open pre-retry owner schema without any retry migration/bootstrap rewrite.
    AgentStore(case.agent.path);AssistantQueue(case.queue.path)
    case.runtime.retry_query_task(case.task['id'])
    assert case.agent.list_steps(case.task['agent_task_id'])==old_steps
    assert case.queue.get_task(case.task['id'])['usage']['total_tokens']==1140


def test_revocation_after_arming_blocks_model_but_does_not_refund_retry_fence(tmp_path):
    case=failed_case(tmp_path);case.runtime.retry_query_task(case.task['id'])
    case.registry.revoke_grant(case.migrated['grant_id'])
    assert case.runtime.execute_once()['reason_code']=='binding_changed'
    assert len(case.supplied)==1
    assert case.queue.get_query_retry(case.task['id'])['state']=='finished'


def test_paused_task_cannot_arm_new_paid_retry(tmp_path):
    case=failed_case(tmp_path);case.control.desired='paused'
    with pytest.raises(QueueError,match='query_retry_requires_running'):
        case.runtime.retry_query_task(case.task['id'])
    assert case.queue.get_query_retry(case.task['id']) is None and len(case.supplied)==1


def test_forged_action_id_or_token_cannot_arm_an_agent_retry(tmp_path):
    case=failed_case(tmp_path);reconcile(case)
    with pytest.raises(ValueError):case.bridge.operator_retry_query(case.queue.get_task(case.task['id']),'a'*32)
    assert case.agent.get_query_retry(case.task['agent_task_id']) is None
    action=case.queue.request_action(case.task['id'],'retry_query')
    reserved=case.queue.take_query_retry_action(case.task['id'],action['id'])
    with pytest.raises(QueueError,match='query_retry_action_not_current'):
        case.runtime.retry_query_task(case.task['id'],_action={**reserved,'_query_retry_token':'wrong'})
    assert case.agent.get_query_retry(case.task['agent_task_id']) is None
    assert case.queue.get_query_retry(case.task['id'])['state']=='reserved'


def test_retry_parameter_cannot_enqueue_a_new_comment_or_model_attempt(tmp_path):
    case=failed_case(tmp_path)
    model=PaidModel(100)
    new_envelope={**case.task['envelope'],'command_id':'different-retry-command',
                  'reference_id':'different-retry-reference'}
    before=case.agent.list_tasks()
    result=AgentService(case.bridge.agent,case.bridge._binding(case.task),model=model,
        knowledge=case.knowledge,database=tmp_path/'never-open-business.db').process(
            new_envelope,query_retry_action_id='a'*32)
    assert model.requests==[]
    assert case.agent.list_tasks()==before
    assert result['task_id'] is None and result['created'] is False


def test_stale_product_execution_token_cannot_start_retry_model(tmp_path):
    case=failed_case(tmp_path);case.runtime.retry_query_task(case.task['id'])
    claimed=case.queue.claim_next('execute')
    result=case.bridge.execute({**claimed,'execution_token':'stale-token'})
    assert result['reason_code']=='query_retry_scope_changed'
    assert len(case.supplied)==1 and case.agent.get_query_retry(case.task['agent_task_id'])['state']=='armed'
