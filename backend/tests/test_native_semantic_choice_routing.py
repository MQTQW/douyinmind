"""Native AT may try a known parent, but never loses an independent query."""
from types import SimpleNamespace

import pytest

from app.assistant.browser_lane import BrowserLane
from app.assistant.native_lane import NativeLane
from app.mention_agent.contracts import Binding
from tests.test_assistant_native_lane import NativeCapture,SourceStore,native_candidate
from tests.test_semantic_library_choice import semantic,TEXT
from tests.test_assistant_choice_controller import choice_detail,case


def lane_for(s,*,text,source_kind='native_notice_candidate',details=False):
    binding=Binding.from_dict(s.session['root_binding'])
    adapter=NativeCapture(binding);adapter.keys=[]
    adapter.page.keyboard=SimpleNamespace(press=adapter.keys.append)
    adapter.last_choice_ask_proof=None
    def read(session,allowed,*,keep_open=False,candidate_text=None):
        assert keep_open is True and allowed==[] and candidate_text==text
        return [choice_detail(s.case,session,text=text,target=8)] if details else []
    adapter.read_choice_replies=read
    adapter.observe_current_choice_replies=lambda *a,**kw:[choice_detail(s.case,s.session,text=text,target=8)]
    base=BrowserLane(adapter,s.case.registry,s.case.queue,s.case.history)
    store=SourceStore(native_candidate(binding,content=text,source_kind=source_kind,
        native_comment_id='8999999000000000444'))
    native=NativeLane(base,SimpleNamespace(),store);native.run_id='semantic-native-owner'
    native.choice_controller=s.case.controller
    native._reply_session=lambda candidate:s.session
    s.case.queue.update_control(lambda old:{**old,'desired_state':'running','state':'running','run_id':native.run_id})
    return native,store,adapter


def test_independent_AT_with_no_actual_old_parent_stays_a_new_actual_mention(semantic):
    s=semantic;text='@助手 Git 暂存区有什么作用'
    lane,store,adapter=lane_for(s,text=text)
    before=s.case.store.get(s.session['choice_id'])
    rows=lane.collect()
    assert len(rows)==1 and rows[0]['kind']=='query'
    assert rows[0]['envelope']['comment_text']==text
    assert rows[0]['envelope']['evidence']['real_mention'] is True
    assert s.model.calls==0 and store.held==[]
    assert s.case.store.get(s.session['choice_id'])==before
    assert len(adapter.native_calls)==1


def test_unparented_plain_reply_never_becomes_a_new_mention_or_model(semantic):
    s=semantic
    lane,store,_=lane_for(s,text=TEXT,source_kind='native_reply_notice_candidate')
    lane._mention=lambda claim:pytest.fail('A reply with no @ cannot become a new mention')
    assert lane.collect()==[] and s.model.calls==0
    assert store.held==['native_reply_not_matched']


def test_model_already_interpreted_actual_choice_cannot_fallthrough_to_second_model(semantic):
    s=semantic;s.model.action='query';s.model.target_state='unspecified';s.model.grant=None
    lane,store,_=lane_for(s,text=TEXT,details=True)
    lane._mention=lambda claim:pytest.fail('Paid/parent-proved choice cannot fall through')
    assert lane.collect()==[] and s.model.calls==1
    assert store.held==['native_reply_not_matched']
    assert s.case.store.get(s.session['choice_id'])['selection_envelope'] is None
