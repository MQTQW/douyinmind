"""Real public DOM parser offline; intercepted browser, no external accounts."""
from copy import deepcopy
import hashlib
import time

import pytest

from app.mention_agent.contracts import Binding,validate_envelope
from app.mention_agent.followup_evidence import make_followup_envelope
from app.mention_receiver.followup_browser import read_followup_reply,followup_reply_state,prepare_followup_reply
from app.mention_receiver.browser import ReceiverDetailError,ReceiverPageError,ReceiverInterventionRequired
from tests.test_library_choice_weak_receipt_dom import browser,surface,complete,adapter,TEXT
from tests.test_library_choice_browser_dom import A,B,OTHER,VID

QUESTION='这个视频讲了什么事情';ANSWER='视频讲述了主人公的经历，并解释了事件发生的背景。'


def setup(surface,monkeypatch,*,linkless=False):
    state=complete(surface)
    surface.page.locator('#selection .FduGc_lz').evaluate('(e,text)=>e.textContent=text',QUESTION)
    if linkless:
        surface.page.locator('#receipt .comment-item-avatar').evaluate('e=>{const a=e.querySelector("a");a.replaceWith(...a.childNodes);e.querySelector("img").width=24;e.querySelector("img").height=24;}')
    state['root_envelope']['source_mode']='live_ui'
    anchor={'root_task_id':'a'*32,'root_agent_task_id':'b'*32,'root_envelope':state['root_envelope'],
        'prior_reply_text':TEXT,'prior_reply_sha256':hashlib.sha256(TEXT.encode()).hexdigest(),'instance_id':'choice-instance'}
    candidate={'sender_profile_url':A,'receiver_profile_url':B,'video_id':VID,'content':QUESTION,
        'native_comment_id':'7999999000000000099','parent_id_hint':'7999999000000000001'}
    receiver=adapter(surface,state,monkeypatch)
    return receiver,anchor,candidate


@pytest.mark.parametrize('linkless',[False,True])
def test_actual_flat_A_reply_after_save_receipt_is_query_only_weak_scope(surface,monkeypatch,linkless):
    receiver,anchor,candidate=setup(surface,monkeypatch,linkless=linkless)
    value=receiver.read_followup_reply(anchor,candidate,keep_open=True)
    env=make_followup_envelope(value,Binding('followup-all','choice-instance',A,B,None,True,query_scope='all_local'))
    assert validate_envelope(env,Binding('followup-all','choice-instance',A,B,None,True,query_scope='all_local'))==env
    assert env['comment_text']==QUESTION and env['native_comment_id'] is None
    assert env['evidence']['real_mention'] is False and env['evidence']['query_only'] is True
    assert env['evidence']['parent_scope']=='unique_followup_in_verified_root'
    assert surface.page.evaluate('window.sendCount')==0


@pytest.mark.parametrize('change',['other_A','wrong_B_header','different_root','duplicate_A','duplicate_prior','wrong_video','two_bodies','hidden_prior','arrow_missing'])
def test_ambiguous_wrong_scope_or_reply_to_other_account_rejected(surface,monkeypatch,change):
    receiver,anchor,candidate=setup(surface,monkeypatch)
    surface.page.evaluate('''change=>{
      const q=document.getElementById('selection'),b=document.getElementById('receipt');
      if(change==='other_A'){q.querySelector('.comment-item-avatar a').href='/user/other';q.querySelector('.comment-item-info-wrap a').href='/user/other';}
      if(change==='wrong_B_header')q.querySelectorAll('.comment-item-info-wrap a')[1].href='/user/other';
      if(change==='different_root')document.getElementById('root').after(q);
      if(change==='duplicate_A')q.after(q.cloneNode(true));
      if(change==='duplicate_prior')b.after(b.cloneNode(true));
      if(change==='two_bodies')q.querySelector('.FduGc_lz').after(Object.assign(document.createElement('div'),{textContent:'另外一句正文'}));
      if(change==='hidden_prior')b.style.display='none';
      if(change==='arrow_missing')q.querySelector('.ym1IYI4C').remove();
    }''',change)
    if change=='wrong_video':candidate['video_id']='7999999000000000012'
    with pytest.raises((ValueError,ReceiverDetailError)):receiver.read_followup_reply(anchor,candidate,keep_open=True)
    assert surface.page.evaluate('window.sendCount')==0


def test_no_avatar_prior_with_any_conflicting_href_is_rejected(surface,monkeypatch):
    receiver,anchor,candidate=setup(surface,monkeypatch,linkless=True)
    surface.page.locator('#receipt .comment-item-avatar').evaluate("e=>e.setAttribute('href','/user/other')")
    with pytest.raises(ReceiverDetailError):receiver.read_followup_reply(anchor,candidate)


def test_readonly_observer_never_opens_editor_or_submits(surface,monkeypatch):
    receiver,anchor,candidate=setup(surface,monkeypatch)
    receiver.read_followup_reply(anchor,candidate)
    assert surface.page.locator('#editor').inner_text()==''
    assert surface.page.evaluate('window.openCount')==surface.page.evaluate('window.sendCount')==0


def followup_target(surface,receiver,anchor,candidate):
    value=receiver.read_followup_reply(anchor,candidate)
    bind=Binding('followup-all','choice-instance',A,B,None,True,query_scope='all_local')
    env=make_followup_envelope(value,bind)
    return {k:v for k,v in env.items() if k!='evidence'}|{'followup_evidence':env['evidence'],
        'binding_id':bind.binding_id,'instance_id':bind.instance_id,'knowledge_base_id':None,'query_scope':'all_local'},bind


def configure_composer(surface):
    surface.page.locator('#header').evaluate('(e,text)=>e.textContent="回复@虚构A: "+text',QUESTION)
    surface.page.locator('#selection').evaluate('e=>e.querySelector(".comment-item-stats-container").addEventListener("click",()=>openReply(e))')


def test_prepare_has_full_fresh_followup_verification_without_send(surface,monkeypatch):
    receiver,anchor,candidate=setup(surface,monkeypatch)
    target,bind=followup_target(surface,receiver,anchor,candidate);configure_composer(surface)
    verification=prepare_followup_reply(receiver,target,ANSWER)
    from app.mention_agent.store import _verified_target
    assert _verified_target(target,verification,bind.to_dict()) is True
    assert verification['real_mention'] is False and verification['query_only'] is True
    assert surface.page.locator('#editor').inner_text()==ANSWER and surface.page.evaluate('window.sendCount')==0


def test_prepare_reports_actual_parent_quality_change_so_reservation_rejects(surface,monkeypatch):
    receiver,anchor,candidate=setup(surface,monkeypatch)
    target,bind=followup_target(surface,receiver,anchor,candidate);configure_composer(surface)
    assert target['followup_evidence']['direct_reply_to_prior'] is False
    surface.page.locator('#editor').evaluate('e=>e.addEventListener("input",()=>document.getElementById("receipt").append(document.getElementById("selection")),{once:true})')
    verification=prepare_followup_reply(receiver,target,ANSWER)
    assert verification['direct_reply_to_prior'] is True
    assert verification['parent_scope']=='direct_prior_reply'
    from app.mention_agent.store import _verified_target
    assert _verified_target(target,verification,bind.to_dict()) is False
    assert surface.page.evaluate('window.sendCount')==0


@pytest.mark.parametrize('linkless',[False,True])
def test_matching_B_answer_current_DOM_uses_root_screenshot_scope(surface,monkeypatch,linkless):
    receiver,anchor,candidate=setup(surface,monkeypatch)
    target,_=followup_target(surface,receiver,anchor,candidate)
    surface.page.evaluate('''({text,linkless})=>{
      const row=document.getElementById('receipt').cloneNode(true);row.id='followup-answer';
      row.querySelector('.FduGc_lz').textContent=text;
      if(linkless){const a=row.querySelector('.comment-item-avatar a');a.replaceWith(...a.childNodes);row.querySelector('img').width=24;row.querySelector('img').height=24;}
      document.getElementById('root').append(row);
    }''',{'text':ANSWER,'linkless':linkless})
    value=receiver._reply_state(target,ANSWER)
    assert value['matching_reply_count']==value['existing_matching_reply_count']==1
    assert value['reply_readback_quality']=='weak_unique_root'
    assert surface.page.locator('[data-e2e="comment-item"]').nth(value['root_index']).get_attribute('id')=='selection'
    assert surface.page.locator('[data-e2e="comment-item"]').nth(value['evidence_root_index']).get_attribute('id')=='root'
    assert surface.page.evaluate('window.sendCount')==0


def test_observed_native_child_ID_must_match_actual_candidate(surface,monkeypatch):
    receiver,anchor,candidate=setup(surface,monkeypatch)
    surface.page.locator('#selection').evaluate("e=>e.setAttribute('data-comment-id','7999999000000000088')")
    with pytest.raises(ReceiverDetailError):receiver.read_followup_reply(anchor,candidate)


def test_stop_before_observation_keeps_page_and_propagates(surface,monkeypatch):
    receiver,anchor,candidate=setup(surface,monkeypatch)
    receiver.choice_should_stop=lambda:True
    with pytest.raises(ReceiverPageError):receiver.read_followup_reply(anchor,candidate)
    assert receiver.opens==[] and surface.page.evaluate('window.sendCount')==0


def test_challenge_propagates_without_editor_or_send(surface,monkeypatch):
    receiver,anchor,candidate=setup(surface,monkeypatch)
    def challenge():raise ReceiverInterventionRequired('synthetic challenge')
    receiver.check_intervention=challenge
    with pytest.raises(ReceiverInterventionRequired):receiver.read_followup_reply(anchor,candidate)
    assert surface.page.locator('#editor').inner_text()=='' and surface.page.evaluate('window.sendCount')==0


def test_open_ended_expand_phrase_is_actual_A_body_not_a_hardcoded_intent_filter(surface,monkeypatch):
    receiver,anchor,candidate=setup(surface,monkeypatch)
    candidate['content']='展开讲讲'
    surface.page.locator('#selection .FduGc_lz').evaluate('(e,text)=>e.textContent=text',candidate['content'])
    value=receiver.read_followup_reply(anchor,candidate)
    assert value['command_text']=='展开讲讲' and value['evidence']['real_mention'] is False
    assert surface.page.evaluate('window.sendCount')==0


def test_expansion_control_text_is_not_a_public_A_question(surface,monkeypatch):
    receiver,anchor,candidate=setup(surface,monkeypatch)
    candidate['content']='展开更多'
    surface.page.locator('#root .replyContainer').first.evaluate('e=>{const b=document.createElement("button");b.textContent="展开更多";e.append(b);}')
    from app.mention_receiver.followup_browser import _args,_read
    receiver.open_original_from_notifications({**anchor['root_envelope']})
    value=_read(receiver,_args(anchor,candidate))
    assert value['details']==[] and value['counts']['readback_more_count']==1
    assert surface.page.evaluate('window.sendCount')==0
