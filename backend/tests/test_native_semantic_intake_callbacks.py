"""Default-off native semantic inbox handoff callbacks; fake/temp only."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.test_assistant_native_lane import native_system
from tests.test_assistant_native_visibility_lane import case,request


@pytest.mark.parametrize('ready',[False,None])
def test_semantic_inbox_not_ready_keeps_normal_candidate_unclaimed(tmp_path,ready):
    lane,source,_,adapter,*_=native_system(tmp_path)
    lane.semantic_intake_ready=lambda:ready
    assert lane.collect()==[]
    assert source.state=='pending' and adapter.native_calls==[]


def test_semantic_inbox_not_ready_keeps_requested_hint_unclaimed(case):
    request(case);case.lane.semantic_intake_ready=lambda:False
    assert case.lane.collect()==[]
    row=case.ledger.get_hint(case.hid)
    assert row['state']=='pending' and row['attempts']==0 and case.adapter.native_calls==[]


@pytest.mark.parametrize('action,expected',[(action,'mention') for action in
    ('query','save','clarify','cancel')]+[(None,'reply'),('unknown','reply')])
def test_recorded_native_instruction_is_not_swallowed_by_an_older_pending_choice(tmp_path,action,expected):
    lane,source,*_=native_system(tmp_path)
    lane._reply_session=lambda candidate:{'choice_id':'existing'}
    lane.choice_controller=SimpleNamespace(semantic_choice_planner=lambda:None)
    lane.queue.update_control(lambda old:{**old,'desired_state':'running'})
    def interpret(candidate):
        candidate['content']='caller copy mutation'
        return action
    lane.semantic_candidate_action=interpret
    lane._mention=lambda claim:['mention']
    lane._reply=lambda claim,session=None:['reply']
    assert lane.collect()==[expected]
    assert source.candidate['content']!='caller copy mutation'


@pytest.mark.parametrize('action',['save','clarify'])
@pytest.mark.parametrize('problem',['other_sender','revoked'])
def test_unregistered_other_user_or_revoked_user_cannot_call_recorded_action(tmp_path,action,problem):
    lane,source,_,adapter,_,_,registry,migrated=native_system(tmp_path)
    lane._reply_session=lambda candidate:{'choice_id':'existing'}
    if problem=='other_sender':
        source.candidate['sender_profile_url']='https://www.douyin.com/user/other-A'
        source.candidate['sender_sec_uid']='other-A'
    else:registry.revoke_user(migrated['user_id'])
    def unauthorized(candidate):
        pytest.fail('Invalid user must be rejected before any recorded-action callback')
        return action
    lane.semantic_candidate_action=unauthorized
    assert lane.collect()==[]
    assert source.held==['native_identity_unverified'] and adapter.native_calls==[]


def test_plain_comment_keeps_choice_path_and_never_calls_AT_action(tmp_path):
    lane,source,*_=native_system(tmp_path,source_kind='native_reply_notice_candidate')
    lane.semantic_candidate_action=lambda candidate:pytest.fail('Plain COMMENT is not an independent AT')
    lane._reply=lambda claim,session=None:['existing-choice']
    lane._mention=lambda claim:pytest.fail('Plain COMMENT cannot become real AT')
    assert lane.collect()==['existing-choice']


@pytest.mark.parametrize('native_inbox',[None,object()])
def test_only_native_inbox_provider_receives_frozen_actual_candidate(tmp_path,native_inbox):
    lane,source,_,adapter,*_=native_system(tmp_path)
    class Provider:
        def __call__(self,*args,**kwargs):pass
    provider=Provider();provider.native_inbox=native_inbox
    lane.browser_lane.semantic_resolution_provider=provider
    lane.queue.update_control(lambda old:{**old,'desired_state':'running'})
    original=lane.browser_lane.prepare_native_detail;seen=[]
    def prepare(notice,detail,**kwargs):
        seen.append(deepcopy(kwargs.get('native_candidate')))
        assert callable(kwargs['semantic_checkpoint'])
        if native_inbox is None:assert 'native_candidate' not in kwargs
        else:
            assert kwargs['native_candidate']==source.candidate
            kwargs['native_candidate']['content']='cannot mutate actual candidate'
        # Exercise NativeLane forwarding without asking this no-model provider
        # fixture to manufacture a real semantic Resolution for BrowserLane.
        lane.browser_lane.semantic_resolution_provider=None
        try:return original(notice,detail)
        finally:lane.browser_lane.semantic_resolution_provider=provider
    lane.browser_lane.prepare_native_detail=prepare
    [row]=lane.collect()
    assert row['envelope']['comment_text']==source.candidate['content']
    assert source.candidate['content']!='cannot mutate actual candidate'
    assert seen==([None] if native_inbox is None else [source.candidate])
