"""Paused-owner first draft: fake UI/bundle, owned TEMP ledgers, no effects API."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import threading
from types import SimpleNamespace

import pytest

from app.assistant import operator_draft_owner as owner_module
from app.assistant.operator_draft_owner import QueryDraftOwnerError, finalize_query_draft
from app.assistant.operator_draft_service import operator_snapshot_digest
from app.assistant.semantic_plan_store import decision_metadata
from app.mention_agent.followup_evidence import make_followup_envelope
from app.mention_agent.store import AgentStore
from tests.test_semantic_command_resolution import case as semantic_case
from tests.test_thread_followup_owner_review import case as followup_case
from tests.test_native_recovery_owner import case as recovery_case
from tests.test_assistant_native_recovery_mailbox import make as mailbox


QUEUE='1'*32
RUN='2'*32
CANDIDATE='4'*64
DRAFT_SOURCE='assistant-draft:'+QUEUE+':'+CANDIDATE
SOURCE_TEXT='合成原文讲解资料整理的方法和核对步骤。'


def forbidden(*args,**kwargs):
    pytest.fail('operator finalization attempted model/query/import/send/budget mutation')


@pytest.fixture
def draft(followup_case,tmp_path,monkeypatch):
    c=followup_case
    env=make_followup_envelope(c.detail,c.binding)
    resolution=c.c.router(env,source_checkpoint=lambda:True)
    binding=resolution.binding
    env=make_followup_envelope(c.detail,binding)
    receipt=c.c.store.get_recorded_decision(env,binding=binding)
    metadata=decision_metadata(receipt)
    agent=AgentStore(tmp_path/'owner-agent.sqlite3')
    agent.put_binding(binding.to_dict())
    task=agent.enqueue(binding.binding_id,env)
    claim=agent.claim(task['task_id'])
    ref={'source_id':'资料1','platform_item_id':env['video_id'],'chunk_id':'synthetic:1',
        'text_sha256':hashlib.sha256(SOURCE_TEXT.encode()).hexdigest()}
    requests=[]
    for number in range(1,5):
        key='live-'+format(number,'048x');requests.append(key)
        c.c.budget.reserve(metadata['budget_scope_id'],key,100)
        c.c.budget.settle(key,{'total_tokens':10,'prompt_tokens':6,'completion_tokens':4})
        agent.record_step(task['task_id'],claim['claim_token'],'model_request',{'round':number,'budget_request_id':key})
        agent.record_step(task['task_id'],claim['claim_token'],'model_response',{'round':number,'budget_request_id':key,
            'usage':{'total_tokens':10,'prompt_tokens':6,'completion_tokens':4}})
        name='query_knowledge_base' if number==1 else 'propose_reply'
        agent.record_step(task['task_id'],claim['claim_token'],'tool_call',{'round':number,'tool_name':name})
        result=({'round':number,'tool_name':name,'status':'ok','knowledge_base_id':binding.knowledge_base_id,
            'evidence_refs':[ref]} if number==1 else
            {'round':number,'tool_name':name,'status':'error','code':'invalid_public_reply'})
        agent.record_step(task['task_id'],claim['claim_token'],'tool_result',result)
    agent.needs_review(task['task_id'],claim['claim_token'],'model_round_budget_exceeded')
    prefix=agent.freeze_reply_prefix('assistant:'+QUEUE,'少爷')
    state={'run_id':RUN,'pid':os.getpid(),'process_alive':True,'state':'paused',
        'desired_state':'paused','worker_state':'idle','manual_verification':{'active':False}}
    queue_task={'id':QUEUE,'agent_task_id':task['task_id'],'binding':binding.to_dict(),
        'binding_id':binding.binding_id,'envelope':env,'kind':'query','state':'needs_review',
        'reason_code':'model_round_budget_exceeded','publication_token':None,'publish_attempts':0,
        'user_id':resolution.user_id,'device_id':resolution.device_id,
        'knowledge_base_id':binding.knowledge_base_id,'authorization':resolution.authorization}
    events=[];live=[True];detail=deepcopy(c.detail)
    def authorized(observed):
        return live[0] and c.c.registry.authorize_snapshot(observed['authorization'],action='query')==binding
    def read(*args,**kwargs):
        assert kwargs=={'keep_open':True}
        events.append('read')
        return deepcopy(detail)
    bridge=SimpleNamespace(agent=agent,usage_budget=c.c.budget,semantic_store=c.c.store,
        control=SimpleNamespace(snapshot=lambda:deepcopy(state)),
        queue=SimpleNamespace(get_task=lambda key:deepcopy(queue_task) if key==QUEUE else None),
        admission_lock=threading.RLock(),browser_owner_thread_id=threading.get_ident(),should_stop=lambda:c.c.stopped[0],
        _binding=lambda value:binding,_authorized=authorized,
        _semantic_receipt=lambda value:c.c.store.get_recorded_decision(value['envelope'],binding=binding),
        _resolution=lambda value:c.c.router.resolver.resolve_verified_intent(value['envelope'],
            c.c.store.get_recorded_decision(value['envelope'],binding=binding)),
        adapter=SimpleNamespace(read_followup_reply=read,prepare_reply=forbidden,submit_reply=forbidden,
            save_reply_evidence=forbidden),model_factory=forbidden,knowledge_factory=forbidden)
    snapshot=agent.operator_query_draft_snapshot(task['task_id'])
    evidence=[{**ref,'title':'合成资料','url':'https://www.douyin.com/video/'+env['video_id'],'text':SOURCE_TEXT}]
    proposal={'kind':'answer','text':'原文介绍了资料整理的方法和核对步骤。','source_ids':['资料1'],
        'sources':[{key:evidence[0][key] for key in ('source_id','platform_item_id','chunk_id','title','url','text_sha256')}]}
    bundle={'task_id':QUEUE,'agent_task_id':task['task_id'],'run_id':RUN,
        'snapshot_digest':operator_snapshot_digest(queue_task,snapshot,queue_task['authorization'],binding,
            'assistant:'+QUEUE,prefix,RUN,binding.receiver_profile_url),
        'reply_style_key':'assistant:'+QUEUE,'prefix':prefix,'effective_max_chars':320-len(prefix),
        'evidence':evidence,'evidence_digest':owner_module._digest(evidence),'candidate_sha256':CANDIDATE,'proposal':proposal}
    hooks={'identity':None,'fetch':None};counts={'identity':0,'fetch':0}
    def identity():
        counts['identity']+=1;events.append('identity')
        if hooks['identity']:hooks['identity'](counts['identity'])
    def fetch(key):
        assert key==QUEUE
        counts['fetch']+=1
        if hooks['fetch']:hooks['fetch'](counts['fetch'])
        return deepcopy(bundle)
    # Setup above records fake historical fees only. Finalization cannot mutate
    # accounting or call another model, retrieval, import or browser sender.
    monkeypatch.setattr(c.c.model,'complete',forbidden)
    monkeypatch.setattr(c.c.budget,'reserve',forbidden)
    monkeypatch.setattr(c.c.budget,'settle',forbidden)
    return SimpleNamespace(c=c,bridge=bridge,agent=agent,task=queue_task,state=state,detail=detail,
        bundle=bundle,snapshot=snapshot,prefix=prefix,requests=requests,scope=metadata['budget_scope_id'],
        semantic_request=metadata['request_key'],events=events,live=live,hooks=hooks,counts=counts,
        identity=identity,fetch=fetch,finalize=lambda:finalize_query_draft(bridge,QUEUE,
            expected_candidate_sha256=CANDIDATE,reverify_identity=identity,fetch_bundle=fetch))


def unchanged(draft):
    assert draft.agent.get_reply(draft.task['agent_task_id']) is None
    assert draft.agent.operator_query_draft_snapshot(draft.task['agent_task_id'])==draft.snapshot


def test_temp_ledgers_success_creates_first_ready_only_and_preserves_all_old_records(draft):
    before_budget=draft.c.c.budget.path.read_bytes();before_semantic=draft.c.c.store.path.read_bytes()
    before_task=deepcopy(draft.task)
    result=draft.finalize()
    assert result=={'source_id':DRAFT_SOURCE,'status':'operator_query_draft_finalized','dispatchable':False}
    assert draft.counts=={'identity':2,'fetch':2} and draft.events==['identity','read','identity']
    reply=draft.agent.get_reply(draft.task['agent_task_id'])
    assert reply['state']=='ready' and reply['send_token'] is None
    assert reply['text']==draft.prefix+draft.bundle['proposal']['text']
    assert draft.task==before_task and draft.task['state']=='needs_review'
    assert draft.c.c.budget.path.read_bytes()==before_budget and draft.c.c.store.path.read_bytes()==before_semantic
    with draft.agent._tx() as db:
        audit=json.loads(db.execute('SELECT value FROM agent_meta WHERE key=?',
            ('operator_query_draft_v1:'+draft.task['agent_task_id'],)).fetchone()[0])
        count=db.execute('SELECT COUNT(*) FROM agent_steps WHERE task_id=?',(draft.task['agent_task_id'],)).fetchone()[0]
    assert count==16 and audit['provenance']=='operator_authored_grounded_answer_not_recorded_model_draft'
    with pytest.raises(Exception):draft.finalize()
    assert draft.counts['fetch']==2


@pytest.mark.parametrize('change',['pid','running','worker','manual','publication','publication_empty',
    'publication_attempts','publication_attempts_bool','kind','reason','permission','stop'])
def test_owner_scope_and_current_permission_refusals_never_read_or_commit(draft,change):
    if change=='pid':draft.state['pid']+=1
    elif change=='running':draft.state['desired_state']='running'
    elif change=='worker':draft.state['worker_state']='processing'
    elif change=='manual':draft.state['manual_verification']['active']=True
    elif change=='publication':draft.task['publication_token']='reserved-send'
    elif change=='publication_empty':draft.task['publication_token']=''
    elif change=='publication_attempts':draft.task['publish_attempts']=1
    elif change=='publication_attempts_bool':draft.task['publish_attempts']=False
    elif change=='kind':draft.task['kind']='save'
    elif change=='reason':draft.task['reason_code']='model_budget_unknown'
    elif change=='permission':draft.live[0]=False
    else:draft.c.c.stopped[0]=True
    with pytest.raises(Exception):draft.finalize()
    assert draft.events==[] and draft.counts['fetch']==0
    unchanged(draft)


@pytest.mark.parametrize('change',['snapshot','candidate_hash','prefix','source_hash','source_text','cross_video','unrecorded_source','oversize'])
def test_bundle_snapshot_frozen_prefix_and_recorded_source_hash_are_required(draft,change):
    if change=='snapshot':draft.bundle['snapshot_digest']='0'*64
    elif change=='candidate_hash':draft.bundle['candidate_sha256']='0'*64
    elif change=='prefix':draft.bundle['prefix']='其他称呼，'
    elif change=='source_hash':draft.bundle['evidence'][0]['text_sha256']='0'*64
    elif change=='source_text':draft.bundle['evidence'][0]['text']+='被替换'
    elif change=='cross_video':draft.bundle['evidence'][0]['platform_item_id']='7999999000000000099'
    elif change=='unrecorded_source':draft.bundle['evidence'][0]['source_id']='资料2'
    else:draft.bundle['proposal']['text']='文'*321
    with pytest.raises(Exception):draft.finalize()
    assert 'read' not in draft.events
    unchanged(draft)


def test_double_changed_request_and_proposal_before_first_fetch_cannot_replace_reviewed_candidate(draft):
    # A service could reconstruct a different, individually valid candidate
    # after both persisted text fields changed. The owner intent retains the
    # original registered digest, rather than trusting this new first fetch.
    altered={'request':{'text':'原文还提到了核对步骤。'},
        'proposal':{**draft.bundle['proposal'],'text':'原文还提到了核对步骤。'}}
    draft.bundle['proposal']=altered['proposal']
    draft.bundle['candidate_sha256']=owner_module._digest(altered)
    assert draft.bundle['candidate_sha256']!=CANDIDATE
    with pytest.raises(QueryDraftOwnerError,match='snapshot_changed'):draft.finalize()
    assert draft.counts['fetch']==1 and 'read' not in draft.events
    unchanged(draft)


@pytest.mark.parametrize('state',['reserved','unknown','settled'])
def test_extra_same_scope_budget_request_is_not_ignored(draft,state):
    with draft.bridge.usage_budget._db(write=True) as db:
        original=dict(db.execute('SELECT * FROM usage_budget_requests WHERE request_key=?',(draft.requests[0],)).fetchone())
        original.update(request_key='live-'+'e'*48,state=state)
        if state!='settled':
            original.update(total_tokens=None,input_tokens=None,output_tokens=None,outcome='unknown')
        columns=list(original)
        db.execute('INSERT INTO usage_budget_requests('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',
            [original[key] for key in columns])
    with pytest.raises(QueryDraftOwnerError,match='budget_unverified'):draft.finalize()
    assert 'read' not in draft.events
    unchanged(draft)


@pytest.mark.parametrize('which,column,value',[('agent','input_tokens',5),('agent','output_tokens',5),
    ('semantic','input_tokens',11),('semantic','total_tokens',21),('agent','state','unknown')])
def test_budget_usage_components_and_known_settled_outcome_must_match_steps(draft,which,column,value):
    key=draft.requests[0] if which=='agent' else draft.semantic_request
    with draft.bridge.usage_budget._db(write=True) as db:
        if column=='state':
            db.execute('UPDATE usage_budget_requests SET state=?,total_tokens=NULL,input_tokens=NULL,output_tokens=NULL WHERE request_key=?',(value,key))
        else:db.execute('UPDATE usage_budget_requests SET '+column+'=? WHERE request_key=?',(value,key))
    with pytest.raises(QueryDraftOwnerError,match='budget_unverified'):draft.finalize()
    unchanged(draft)


@pytest.mark.parametrize('change',['old','wrong_sender','wrong_video','bad_prior','bool_unique'])
def test_fresh_ui_is_readonly_and_must_reverify_exact_parent_and_identity(draft,change):
    proof=draft.detail['evidence']
    if change=='old':proof['observed_at_utc']=(datetime.now(timezone.utc)-timedelta(seconds=121)).isoformat()
    elif change=='wrong_sender':draft.detail['sender_profile_url']='https://www.douyin.com/user/other-A'
    elif change=='wrong_video':draft.detail['video_id']='7999999000000000099'
    elif change=='bad_prior':proof['prior_reply_text']+='changed'
    else:proof['unique_count']=True
    with pytest.raises(Exception):draft.finalize()
    assert draft.events==['identity','read']
    unchanged(draft)


@pytest.mark.parametrize('change',['permission','stop','queue','bundle','budget'])
def test_after_ui_recheck_detects_changes_before_any_permit(draft,change):
    def alter(number):
        if number!=2:return
        if change=='permission':draft.live[0]=False
        elif change=='stop':draft.c.c.stopped[0]=True
        elif change=='queue':draft.task['reason_code']='changed'
        elif change=='bundle':draft.bundle['proposal']['text']='另一份操作员正文'
        else:
            with draft.bridge.usage_budget._db(write=True) as db:
                db.execute('UPDATE usage_budget_requests SET input_tokens=5 WHERE request_key=?',(draft.requests[0],))
    draft.hooks['identity']=alter
    with pytest.raises(Exception):draft.finalize()
    assert 'read' in draft.events
    unchanged(draft)


def test_later_actual_registry_revocation_is_rechecked(draft):
    def revoke(number):
        if number==2:
            draft.c.c.registry.set_local_owner_access(draft.task['user_id'],draft.task['device_id'],
                query_all_local=False,public_reply_allowed=False,verified_by='synthetic-revocation')
    draft.hooks['identity']=revoke
    with pytest.raises(Exception):draft.finalize()
    unchanged(draft)


def test_native_owner_entry_keeps_pause_and_never_dispatches(recovery_case):
    c=recovery_case;c.state['manual_verification']={'active':False};events=[]
    c.lane._recovery_identity=lambda:events.append('identity')
    def finalize(task_id,*,expected_candidate_sha256,reverify_identity):
        assert task_id==QUEUE and c.flags[0].is_set() and c.flags[1].is_set()
        assert expected_candidate_sha256==CANDIDATE
        reverify_identity();events.append('finalized')
        return {'source_id':DRAFT_SOURCE,'status':'operator_query_draft_finalized','dispatchable':False}
    c.owner.draft_finalizer=finalize
    result=c.owner('finalize_query_draft',DRAFT_SOURCE)
    assert result['ok'] is True and result['dispatchable'] is False and result['queued_for_fresh_ui'] is False
    assert c.state['desired_state']=='paused' and not c.flags[0].is_set() and not c.flags[1].is_set()
    assert events==['identity','identity','finalized']


@pytest.mark.parametrize('active',['queued','waiting_desktop','executing','waiting_ingestion','ready_to_publish',
    'publishing','waiting_external','waiting_library','needs_reconcile'])
def test_native_owner_refuses_other_active_work_before_finalizer(recovery_case,active):
    c=recovery_case;c.state['manual_verification']={'active':False};c.counts[active]=1
    c.owner.draft_finalizer=forbidden;c.lane._recovery_identity=forbidden
    assert c.owner('finalize_query_draft',DRAFT_SOURCE)['ok'] is False


@pytest.mark.parametrize('block',['manual','worker','pending','hold','wrong_namespace'])
def test_native_owner_existing_human_worker_and_namespace_gates_also_cover_finalize(recovery_case,block):
    c=recovery_case;c.state['manual_verification']={'active':False}
    if block=='manual':c.state['manual_verification']['active']=True
    elif block=='worker':c.flags[2].set()
    elif block=='pending':c.pending[0]=True
    elif block=='hold':c.flags[0].set()
    c.owner.draft_finalizer=forbidden;c.lane._recovery_identity=forbidden
    source='assistant-task:'+QUEUE if block=='wrong_namespace' else DRAFT_SOURCE
    assert c.owner('finalize_query_draft',source)['ok'] is False


def test_mailbox_finalize_has_task_namespace_once_only_and_fixed_no_effects_result(tmp_path):
    box,_,_=mailbox(tmp_path)
    source=DRAFT_SOURCE
    with pytest.raises(Exception):box.submit('native:'+'f'*64,'finalize_query_draft')
    box.submit(source,'finalize_query_draft');calls=[]
    def handler(operation,observed):
        calls.append((operation,observed))
        return {'ok':True,'reviewed':True,'queued_for_fresh_ui':False,'dispatchable':False}
    result=box.process_one(handler)
    assert result['status']=='done' and result['no_model'] and result['no_import'] and result['no_send']
    assert result['queued_for_fresh_ui'] is False and result['dispatchable'] is False
    assert calls==[('finalize_query_draft',source)] and box.process_one(handler) is None


def test_legacy_wrapper_only_emits_fixed_safe_diagnostic_on_failure(monkeypatch):
    from app.assistant.legacy_bridge import LegacyBridge
    bridge=SimpleNamespace()
    def fail(*args,**kwargs):raise RuntimeError('private-draft https://synthetic.invalid/secret')
    monkeypatch.setattr(owner_module,'finalize_query_draft',fail)
    with pytest.raises(RuntimeError):
        LegacyBridge.finalize_operator_query_draft(bridge,QUEUE,expected_candidate_sha256=CANDIDATE,reverify_identity=forbidden)
    assert bridge.last_operator_draft_diagnostic=={'stage':'operator_query_draft_finalize',
        'code':'operator_query_draft_unconfirmed','exception_type':'local_error'}


def test_pure_mock_orchestration_validates_before_single_commit(monkeypatch):
    import app.assistant.legacy_bridge as bridge_module
    import app.mention_agent.store as store_module
    events=[];task={'agent_task_id':'3'*32,'authorization':{},'envelope':{'video_id':'7999999000000000001',
        'comment_text':'帮我总结下'}}
    binding=SimpleNamespace(receiver_profile_url='https://www.douyin.com/user/mock-B')
    control={'run_id':RUN};ref={'source_id':'资料1','platform_item_id':task['envelope']['video_id'],
        'chunk_id':'mock:1','text_sha256':hashlib.sha256('合成原文'.encode()).hexdigest()}
    snapshot={'recorded_refs':[ref]};prefix='';proposal={'kind':'answer','text':'合成正文','source_ids':['资料1'],'sources':[]}
    evidence=[{**ref,'text':'合成原文'}]
    bundle={'task_id':QUEUE,'agent_task_id':task['agent_task_id'],'run_id':RUN,'snapshot_digest':'a'*64,
        'reply_style_key':'assistant:'+QUEUE,'prefix':prefix,'effective_max_chars':320,
        'evidence':evidence,'proposal':proposal,'evidence_digest':'b'*64,'candidate_sha256':'c'*64}
    agent=SimpleNamespace(operator_query_draft_snapshot=lambda key:deepcopy(snapshot),_reply_prefix=lambda db,key:prefix,
        _commit_operator_query_draft=lambda permit:events.append('commit'))
    bridge=SimpleNamespace(admission_lock=threading.RLock(),agent=agent,
        _resolution=lambda observed:SimpleNamespace(action='query',library={'name':'合成库'}))
    @contextmanager
    def readonly(value):yield None
    monkeypatch.setattr(bridge_module,'_followup_owned_ro',readonly)
    monkeypatch.setattr(owner_module,'_scope',lambda *args:(deepcopy(task),binding,deepcopy(control)))
    monkeypatch.setattr(owner_module,'_budget',lambda *args:events.append('budget') or 'd'*64)
    monkeypatch.setattr(owner_module,'_fresh_ui',lambda *args:events.append('read') or 'e'*64)
    monkeypatch.setattr('app.assistant.operator_draft_service.operator_snapshot_digest',lambda *args:'a'*64)
    monkeypatch.setattr('app.assistant.operator_draft_service.validate_operator_proposal',lambda *args,**kwargs:proposal)
    monkeypatch.setattr(store_module,'_operator_query_draft_permit',lambda *args:events.append('permit') or object())
    result=finalize_query_draft(bridge,QUEUE,expected_candidate_sha256='c'*64,reverify_identity=lambda:events.append('identity'),
        fetch_bundle=lambda key:deepcopy(bundle))
    assert result['status']=='operator_query_draft_finalized' and result['dispatchable'] is False
    assert events==['identity','budget','read','identity','budget','permit','commit']


def test_real_queue_reconcile_and_publish_resume_never_reexecute_failed_query(tmp_path,monkeypatch):
    from app.assistant.legacy_bridge import LegacyBridge
    from app.assistant.runtime import AssistantRuntime
    from app.mention_agent.model import ModelTurn
    from tests.test_conversation_followup_bridge import prepared,planned_child,child_detail,AllKnowledge
    values=prepared(tmp_path)
    _,agent,_,registry,_,queue,history,_,_,anchor,backend,adapter=values
    env,queued,budget,semantic,planner,_=planned_child(tmp_path,values)
    state={'run_id':RUN,'pid':os.getpid(),'process_alive':True,'state':'running',
        'desired_state':'running','worker_state':'idle','manual_verification':{'active':False}}
    control=SimpleNamespace(snapshot=lambda:deepcopy(state))
    class RecordedKnowledge(AllKnowledge):
        def query(self,question):
            self.last=super().query(question)
            return deepcopy(self.last)
    knowledge=RecordedKnowledge()
    class RejectedModel:
        calls=0
        def complete(self,messages,tools):
            self.calls+=1
            name,args=(('query_knowledge_base',{'question':env['comment_text']}) if self.calls==1 else
                ('propose_reply',{'text':'文'*321,'source_ids':['资料1']}))
            return ModelTurn({'role':'assistant','content':None,'tool_calls':[{'id':'failed-'+str(self.calls),
                'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]},
                {'prompt_tokens':4,'completion_tokens':3,'total_tokens':7},'tool_calls')
    model=RejectedModel()
    bridge=LegacyBridge(registry,queue,control,agent_path=agent.path,conversation=history,
        database=tmp_path/'never-open-business.db',import_client=backend,adapter=adapter,
        semantic_store=semantic,usage_budget=budget,model_factory=lambda:model,knowledge_factory=lambda _:knowledge)
    runtime=AssistantRuntime(queue,control,execute=bridge.execute,publish=bridge.publish,
        reconcile=bridge.reconcile,resume_adapter=bridge.operator_resume)
    failed=runtime.execute_once()
    assert failed['id']==queued['id'] and failed['state']=='needs_review'
    assert failed['kind']=='query' and failed['reason_code']=='model_round_budget_exceeded'
    assert model.calls==4 and planner.calls==1 and len(knowledge.questions)==1 and backend.posts==1
    assert adapter.submits==1  # only the original save acknowledgement
    agent_id=failed['agent_task_id'];snapshot=agent.operator_query_draft_snapshot(agent_id)
    old_steps=agent.list_steps(agent_id);old_usage=deepcopy(failed['usage'])
    old_events=queue.list_events(failed['id']);before_history=history.path.read_bytes()
    before_budget=budget.path.read_bytes();before_semantic=semantic.path.read_bytes()
    refs=snapshot['recorded_refs'];raw=knowledge.last['evidence'][0]
    assert len(refs)==1 and hashlib.sha256(raw['text'].encode()).hexdigest()==refs[0]['text_sha256']
    # All subsequent retrieval/model/execution entrypoints fail immediately.
    monkeypatch.setattr(model,'complete',forbidden)
    monkeypatch.setattr(planner,'complete',forbidden)
    monkeypatch.setattr(knowledge,'query',forbidden)
    bridge.model_factory=bridge.knowledge_factory=forbidden
    runtime.execute_adapter=forbidden
    adapter.read_followup_reply=lambda *args,**kwargs:child_detail(anchor)
    state.update(state='paused',desired_state='paused')
    prefix=agent.reply_prefix('assistant:'+failed['id'])
    evidence=[{**refs[0],**{key:raw[key] for key in ('text','title','url')}}]
    proposal={'kind':'answer','text':'视频介绍如何整理文件。','source_ids':['资料1'],
        'sources':[{key:evidence[0][key] for key in
            ('source_id','platform_item_id','chunk_id','title','url','text_sha256')}]}
    binding=bridge._binding(failed)
    bundle={'task_id':failed['id'],'agent_task_id':agent_id,'run_id':RUN,
        'snapshot_digest':operator_snapshot_digest(failed,snapshot,failed['authorization'],binding,
            'assistant:'+failed['id'],prefix,RUN,binding.receiver_profile_url),
        'reply_style_key':'assistant:'+failed['id'],'prefix':prefix,'effective_max_chars':320-len(prefix),
        'evidence':evidence,'evidence_digest':owner_module._digest(evidence),'candidate_sha256':'7'*64,'proposal':proposal}
    result=finalize_query_draft(bridge,failed['id'],expected_candidate_sha256='7'*64,reverify_identity=lambda:None,
        fetch_bundle=lambda key:deepcopy(bundle))
    assert result['dispatchable'] is False and queue.get_task(failed['id'])['state']=='needs_review'
    assert agent.get_reply(agent_id)['sources'][0]['text_sha256']==refs[0]['text_sha256']
    assert agent.get_task(agent_id)['reason_code']=='model_round_budget_exceeded'
    assert agent.list_steps(agent_id)==old_steps and adapter.submits==1
    assert history.path.read_bytes()==before_history
    reconciled=runtime.reconcile_task(failed['id'])
    assert reconciled['state']=='needs_review' and reconciled['reconciliation']['safe_to_publish'] is True
    assert reconciled['reconciliation']['safe_to_execute'] is False
    assert reconciled['reconciliation']['model_started'] is True
    assert 'text_sha256' not in reconciled['details']['sources'][0]
    assert reconciled['usage']==old_usage
    assert queue._resume_phase(reconciled,__import__('time').time())=='publish'
    resumed=runtime.resume_task(failed['id'])
    assert resumed['state']=='ready_to_publish' and resumed['kind']=='query'
    assert history.path.read_bytes()==before_history
    assert any(event['kind']=='operator_resume' and event['metadata']['phase']=='publish'
        for event in queue.list_events(failed['id']))
    assert any(event['metadata'].get('reason_code')=='model_round_budget_exceeded'
        for event in queue.list_events(failed['id']))
    assert runtime.execute_once() is None  # paused, so no execution claim
    state.update(state='running',desired_state='running')
    assert runtime.execute_once() is None  # still no queued execution to claim
    published=runtime.publish_once()
    assert published['state']=='b_view_visible' and published['id']==failed['id']
    assert 'text_sha256' not in published['details']['sources'][0]
    assert agent.get_reply(agent_id)['sources'][0]['text_sha256']==refs[0]['text_sha256']
    assert adapter.submits==2 and runtime.publish_once() is None
    assert published['usage']==old_usage and agent.list_steps(agent_id)==old_steps
    assert queue.list_events(failed['id'])[:len(old_events)]==old_events
    assert budget.path.read_bytes()==before_budget and semantic.path.read_bytes()==before_semantic
    assert model.calls==4 and planner.calls==1 and len(knowledge.questions)==1 and backend.posts==1
