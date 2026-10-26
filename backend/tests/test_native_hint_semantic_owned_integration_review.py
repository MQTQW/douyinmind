"""Real owned hint/source/registry/meaning stores + actual BrowserLane, fake UI."""
import sys
from types import SimpleNamespace

import pytest

from app.assistant.native_events import project_ui_review_hints,project_semantic_messages
from app.assistant.native_semantic_inbox import NativeSemanticInbox
from app.assistant.semantic_plan_store import SemanticPlanStore
from app.assistant.semantic_routing import SemanticIntentInterpreter
from app.assistant.usage_budget import UsageBudget
from app.mention_receiver.browser import ReceiverDetailError
from tests.test_assistant_native_visibility_lane import case,request
from tests.test_assistant_native_receiver import B,page
from tests.test_semantic_planning_store import turn


def setup_understood(case,tmp_path):
    text='@6 能不能帮我把这条也收进长视频那个库里呀？以后好找。'
    raw=case.raw('1010','20110',text,unknown=True)
    projected=case.project(page([raw]),9)
    case.source.record_page(case.source.prepare_channel(9,'601'),projected,
        next_cursor=('0','0'),has_more=False,allow_live=True)
    kwargs={'receiver_uid':B,'receiver_sec_uid':case.binding.receiver_profile_url.rsplit('/',1)[-1],
        'profile_allowed':lambda profile:profile==case.binding.sender_profile_url}
    [case.hint]=project_ui_review_hints(page([raw]),9,**kwargs)
    [case.hid]=case.ledger.record_hints([case.hint],page_fingerprint=projected['fingerprint'])
    case.registry.catalog_reader=lambda:[{'id':7,'name':'测试长视频'},{'id':8,'name':'另一个库'}]
    grant=case.registry.snapshot()['users'][0]['devices'][0]['libraries'][0]
    meaning_clock=[100.5]
    inbox=NativeSemanticInbox.prepare(tmp_path/'native-meaning.sqlite3',B,case.binding.receiver_profile_url,
        source_authorizer=case.source.native_meaning_checkpoint,clock=lambda:meaning_clock[0])
    inbox.enable_new_messages();meaning_clock[0]=102.0
    [message]=project_semantic_messages(page([raw]),9,**kwargs)
    assert len(case.hint)==22 and len(message)==19
    inbox.record_messages([message],page_fingerprint=projected['fingerprint'])
    semantic=SemanticPlanStore.prepare(tmp_path/'semantic-plans.sqlite3')
    budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3')
    class Model:
        calls=0
        def complete(self,messages,tools):
            self.calls+=1
            assert messages[-1]['content']==text and len(tools)==1
            return turn({'action':'save','target_state':'one','grant_id':grant['grant_id']})
    model=Model();provider=SemanticIntentInterpreter(case.registry,semantic,model,budget)
    provider.configure_native(inbox,case.source.native_meaning_checkpoint,
        source_store=case.source,hint_store=case.ledger)
    case.lane.browser_lane.semantic_resolution_provider=provider
    case.lane.semantic_intake_ready=provider.native_intake_ready
    case.lane.semantic_candidate_action=provider.native_candidate_action
    request(case)
    case.queue.update_control(lambda old:{**old,'state':'running','desired_state':'running'})
    assert case.lane.collect()==[] and case.adapter.native_calls==[] and model.calls==0
    assert provider.process_native_once()['state']=='understood' and model.calls==1
    return SimpleNamespace(model=model,provider=provider,semantic=semantic,budget=budget,text=text,inbox=inbox)


def test_oral_API_meaning_then_actual_hint_BrowserLane_UI_bind_uses_one_model(case,tmp_path):
    s=setup_understood(case,tmp_path)
    exceptions=[]
    previous=sys.gettrace()
    def diagnostic(frame,event,value):
        if (event=='exception' and frame.f_code.co_name in {'_prepare_mention_ui','_collect_visibility_hint'}
                and frame.f_globals.get('__name__')=='app.assistant.native_lane'):
            exceptions.append({'type':value[0].__name__,'code':getattr(value[1],'code',None),
                'function':frame.f_code.co_name,'line':frame.f_lineno})
        return diagnostic
    try:
        sys.settrace(diagnostic)
        rows=case.lane.collect()
    finally:sys.settrace(previous)
    assert len(rows)==1,{'hold_code':case.ledger.get_hint(case.hid)['hold_code'],'exceptions':exceptions}
    row=rows[0]
    assert row['kind']=='save' and row['binding'].knowledge_base_id==7
    assert row['envelope']['comment_text']==s.text and row['envelope']['native_comment_id'] is None
    assert s.semantic.get_recorded_decision(row['envelope'],binding=row['binding']) is not None
    task=case.queue.enqueue(**row);case.lane.admitted([task])
    assert case.lane.authority_current(task) is True and s.provider.native_authority_current(task) is True
    assert case.lane.collect()==[] and s.model.calls==1
    assert s.budget.summary()['all_time']['request_count']==1
    assert case.agent.list_tasks()==[]  # No answer model, importer or publisher.


def test_explicit_failed_hint_recovery_once_reuses_original_meaning_and_has_one_source(case,tmp_path):
    s=setup_understood(case,tmp_path)
    def failed_UI():raise ReceiverDetailError('synthetic original lookup failed')
    case.adapter.before_return=failed_UI
    assert case.lane.collect()==[]
    held=case.ledger.get_hint(case.hid)
    assert held['state']=='held' and held['hold_code']=='native_visibility_ui_unverified' and held['attempts']==1
    assert case.source.get_source(held['source_id']) is None and s.model.calls==1
    assert not any(case.queue.counts().values()) and case.agent.list_tasks()==[]
    with case.ledger._connection() as db:
        old_token=db.execute('SELECT claim_token FROM hints WHERE hint_id=?',(case.hid,)).fetchone()[0]
    original_plan=s.provider.store.get_native_decision(held['source_id']).plan_id
    case.adapter.before_return=lambda:None
    case.queue.update_control(lambda old:{**old,'state':'paused','desired_state':'paused'})
    reads=len(case.adapter.native_calls)
    result=case.lane.recover_visibility_hint(case.hid)
    assert result=={'source_id':case.hid,'status':'visibility_hint_recovery_requested','dispatchable':False}
    pending=case.ledger.get_hint(case.hid)
    assert pending['state']=='pending' and pending['attempts']==1 and pending['first_seen']==held['first_seen']
    assert len(case.adapter.native_calls)==reads and s.model.calls==1
    assert case.source.get_source(held['source_id']) is None
    assert case.ledger.check_claim(case.hid,old_token)['valid'] is False
    with pytest.raises(Exception):case.lane.recover_visibility_hint(case.hid)
    def late_old_claim():
        assert case.ledger.get_hint(case.hid)['attempts']==2
        assert case.ledger.check_claim(case.hid,old_token)['valid'] is False
        with pytest.raises(Exception):case.ledger.hold(case.hid,old_token,'native_visibility_ui_unverified')
    case.adapter.before_return=late_old_claim
    case.queue.update_control(lambda old:{**old,'state':'running','desired_state':'running'})
    [row]=case.lane.collect()
    assert row['kind']=='save' and row['envelope']['comment_text']==s.text
    task=case.queue.enqueue(**row);case.lane.admitted([task])
    assert case.ledger.get_hint(case.hid)['state']=='admitted' and case.ledger.get_hint(case.hid)['attempts']==2
    assert s.provider.store.get_native_decision(held['source_id']).plan_id==original_plan
    assert case.lane.authority_current(task) is True and s.provider.native_authority_current(task) is True
    assert case.lane.collect()==[] and s.model.calls==1
    assert s.budget.summary()['all_time']['request_count']==1 and case.agent.list_tasks()==[]
    with case.source._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM sources WHERE source_id=?',(held['source_id'],)).fetchone()[0]==1
    with case.ledger._connection() as db:
        audit={row[0]:row[1] for row in db.execute('SELECT kind,COUNT(*) FROM audit WHERE hint_id=? GROUP BY kind',(case.hid,))}
    assert audit['explicit_review_requested']==1 and audit['explicit_recovery_requested']==1 and audit['explicit_recovery_claimed']==1
    assert sum(case.queue.counts().values())==1
    case.queue.update_control(lambda old:{**old,'state':'paused','desired_state':'paused'})
    with pytest.raises(Exception):case.lane.recover_visibility_hint(case.hid)
    assert case.ledger.get_hint(case.hid)['attempts']==2
