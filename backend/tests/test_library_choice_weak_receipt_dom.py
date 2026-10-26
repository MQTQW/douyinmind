"""Later A DOM is layout evidence, replayed as explicit synthetic actors only.

Fresh offline Edge; all routes intercepted. No production ledger or real actor.
"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import time

import pytest

from tests.test_library_choice_plain_header_dom import browser, surface, observed
from tests.test_library_choice_browser_dom import A, B, OTHER, VID, NUMBER, session
from app.assistant.choice_intent import selection_forms
from app.mention_agent.choice_evidence import choice_ask_reference, make_choice_envelope
from app.mention_agent.contracts import Binding
from app.mention_receiver import choice_browser as module
from app.mention_receiver.browser import NormalPageReceiver, ReceiverDetailError, ReceiverInterventionRequired, ReceiverPageError


TEXT = '这个视频已加入“测试长视频”知识库，文字稿和检索资料已就绪，现在可以根据它提问。'
SELECT = '收藏到测试长视频吧'
OPTIONS = [{'knowledge_base_id': i, 'name': name} for i, name in enumerate(
    ['111', '1', '多久时间', '全部收藏', '测试长视频', '验收测试 · 演示资料'], 1)]


def complete(surface, *, actor=True):
    observed(surface)
    capture = (Path(__file__).with_name('fixtures') / 'choice_complete_receipt_sanitized.html').read_text(encoding='utf-8')
    surface.page.locator('#root').evaluate('(e,value)=>e.outerHTML=value', capture)
    surface.page.evaluate('''()=>{
      const rows=[...document.querySelectorAll('[data-e2e="comment-item"]')];
      rows[0].id='root';rows[1].id='ask';rows[2].id='selection';rows[3].id='receipt';
    }''')
    state = session()
    bodies = surface.page.evaluate('''()=>[...document.querySelectorAll('[data-e2e="comment-item"]')].map(n=>
      [...n.querySelectorAll('.FduGc_lz')].find(e=>e.closest('[data-e2e="comment-item"]')===n).innerText)''')
    state['root_envelope']['comment_text'] = bodies[0]
    state['choice_id'] = 'c' * 32
    state['ask_ref'] = 'synthetic-complete-ask'
    state['ask_text'] = bodies[1]
    state['options'] = OPTIONS
    state['ask_reply_ref'] = choice_ask_reference(root_reference_id=state['root_envelope']['reference_id'],
        ask_ref=state['ask_ref'], ask_text=state['ask_text'], receiver_profile_url=B, video_id=VID)
    if not actor:
        surface.page.locator('p').filter(has_text='抖音号').evaluate_all('nodes=>nodes.forEach(e=>e.remove())')
    return state


def args(state, **changes):
    value = module._session_args(state, selection_forms(OPTIONS))
    value.update(selection_text=SELECT, native_comment_id=None, expected_direct_parent=False,
                 expected_ask_native=None, reply=TEXT, identity_fresh=True, expected_public_number=NUMBER)
    value.update(changes)
    return value


def inspect(surface, state):
    return surface.page.evaluate(module.CHOICE_REPLY_STATE_JS, args(state))


def target(surface, state):
    result = surface.page.evaluate(module.CHOICE_THREAD_JS, args(state))
    detail = result['details'][0]
    binding = Binding('synthetic-selected5', 'choice-instance', A, B, 5, True)
    envelope = make_choice_envelope({'source_profile_url': A, 'exact_text': SELECT}, detail, binding, 'isolated')
    return {key: value for key, value in envelope.items() if key != 'evidence'} | {'choice_evidence': envelope['evidence']}


def adapter(surface, state, monkeypatch):
    value = NormalPageReceiver(surface.page, NUMBER, B)
    value.choice_should_stop = lambda: False
    value.opens = []
    def open_original(expected, *, max_candidates=3):
        value.opens.append((deepcopy(expected), max_candidates))
        value._reply_identity_checked_at = time.monotonic()
        assert expected['comment_text'] == state['root_envelope']['comment_text']
        assert expected['reference_id'] == state['root_envelope']['reference_id']
        return {'ok': True, 'unique_count': 1, 'sender_profile_url': A, 'receiver_profile_url': B, 'video_id': VID,
            'native_comment_id': None, 'command_text': state['root_envelope']['comment_text']}
    monkeypatch.setattr(value, 'open_original_from_notifications', open_original, raising=False)
    monkeypatch.setattr(value, 'prepare_reply', lambda *a, **k: pytest.fail('Read-only observer must not prepare'))
    monkeypatch.setattr(value, 'submit_reply', lambda *a, **k: pytest.fail('Read-only observer must not send'))
    return value


def test_real_flat_layout_with_explicit_synthetic_b_actor_is_weak_positive(surface):
    state = complete(surface)
    value = inspect(surface, state)
    assert value['current_receiver_verified'] is True
    assert value['existing_matching_reply_count'] == value['matching_reply_count'] == 1
    assert value['reply_readback_quality'] == 'weak_unique_root'
    assert value['strict_matching_reply_count'] == 0 and value['weak_matching_reply_count'] == 1


def test_later_partial_a_capture_without_actor_cannot_confirm(surface, monkeypatch):
    state = complete(surface, actor=False)
    value = adapter(surface, state, monkeypatch)
    observe = getattr(module, 'observe_existing_choice_reply', None)
    assert callable(observe)
    result = observe(value, target(surface, state), TEXT)
    assert result['outcome'] == 'delivery_unknown' and result['verification'] is None
    assert result['exact_body_in_original_thread'] is False


def test_existing_observer_never_fills_clears_or_sends_and_keeps_frozen_weak_proof(surface, monkeypatch):
    state = complete(surface)
    selected = target(surface, state)
    value = adapter(surface, state, monkeypatch)
    surface.page.locator('#editor').evaluate("e=>e.textContent='operator draft, leave it alone'")
    surface.page.evaluate("window.inputCount=0;document.addEventListener('input',()=>window.inputCount++)")
    observe = getattr(module, 'observe_existing_choice_reply', None)
    assert callable(observe)
    result = observe(value, selected, TEXT)
    assert result['outcome'] == 'b_view_visible' and result['exact_body_in_original_thread'] is True
    assert result['readback_quality'] == 'weak_unique_root'
    proof = result['verification']
    assert proof['current_receiver_verified'] is True
    assert proof['parent_scope'] == selected['choice_evidence']['parent_scope']
    assert proof['direct_reply_to_ask'] is False and proof['real_mention'] is False
    assert result['counts']['selected_source_count'] == 1
    assert surface.page.locator('#editor').inner_text() == 'operator draft, leave it alone'
    assert surface.page.evaluate('window.inputCount') == surface.page.evaluate('window.sendCount') == 0
    assert len(value.opens) == 1 and value.opens[0][1] == 3


@pytest.mark.parametrize('change', [
    "e.querySelector('.comment-item-avatar a').href=other",
    "e.querySelectorAll('.comment-item-info-wrap a')[0].href=other",
    "e.querySelectorAll('.comment-item-info-wrap a')[1].href=other",
    "e.querySelector('.ym1IYI4C').remove()",
    "e.querySelector('.ym1IYI4C').style.opacity='0'",
    "e.querySelector('.FduGc_lz').style.display='none'",
    "e.style.opacity='0'",
    "e.style.visibility='hidden'",
    "e.querySelector('.FduGc_lz').textContent='另一段正文'",
    "e.querySelector('.FduGc_lz').textContent=e.querySelector('.FduGc_lz').textContent.replace('这个','这 个')",
])
def test_flat_receipt_needs_visible_semantic_b_author_a_target_and_exact_own_body(surface, change):
    state = complete(surface)
    surface.page.locator('#receipt').evaluate('(e,other)=>{' + change + '}', OTHER)
    value = inspect(surface, state)
    assert value.get('matching_reply_count', 0) == 0


@pytest.mark.parametrize('hidden', [False, True])
def test_duplicate_same_body_stays_no_resend_barrier_and_never_single_positive(surface, hidden):
    state = complete(surface)
    surface.page.locator('#receipt').evaluate('''(e,hidden)=>{
      const clone=e.cloneNode(true);clone.id='duplicate';if(hidden)clone.style.display='none';e.parentElement.append(clone);
    }''', hidden)
    value = inspect(surface, state)
    assert value['existing_matching_reply_count'] == 2
    assert value.get('weak_matching_reply_count', 0) == 0


def test_flat_b_container_cannot_borrow_nested_other_author_body(surface):
    state = complete(surface)
    surface.page.locator('#receipt .FduGc_lz').evaluate('''(e,{other,text})=>{
      e.innerHTML='<article data-e2e="comment-item"><a style="display:none" href="'+other+'">隐藏C</a><div>'+text+'</div></article>';
    }''', {'other': OTHER, 'text': TEXT})
    value = inspect(surface, state)
    assert value.get('matching_reply_count', 0) == 0


def test_receipt_in_another_root_is_not_a_reply_to_this_selection(surface):
    state = complete(surface)
    surface.page.locator('#receipt').evaluate('''e=>{
      const other=document.createElement('article');other.setAttribute('data-e2e','comment-item');
      document.body.append(other);other.append(e);
    }''')
    assert inspect(surface, state)['matching_reply_count'] == 0


@pytest.mark.parametrize('hidden', [False, True])
def test_frozen_a_source_must_be_unique_even_for_a_hidden_same_body_row(surface, hidden):
    state = complete(surface)
    surface.page.locator('#selection').evaluate('''(e,hidden)=>{
      const clone=e.cloneNode(true);clone.id='second-selection';if(hidden)clone.style.display='none';e.parentElement.append(clone);
    }''', hidden)
    assert inspect(surface, state).get('matching_reply_count', 0) == 0


def test_root_mismatch_from_strict_opener_never_falls_back(surface, monkeypatch):
    state = complete(surface)
    value = adapter(surface, state, monkeypatch)
    def wrong(*a, **k):
        raise ReceiverDetailError('fixed synthetic target unavailable', {})
    monkeypatch.setattr(value, 'open_original_from_notifications', wrong)
    monkeypatch.setattr(value, 'notifications', lambda: pytest.fail('Never fallback'))
    observe = getattr(module, 'observe_existing_choice_reply', None)
    assert callable(observe)
    result = observe(value, target(surface, state), TEXT)
    assert result['outcome'] == 'delivery_unknown' and result['verification'] is None


def test_wrong_video_returned_from_strict_opener_is_rejected(surface, monkeypatch):
    state = complete(surface)
    value = adapter(surface, state, monkeypatch)
    monkeypatch.setattr(value, 'open_original_from_notifications', lambda *a, **k: {
        'sender_profile_url': A, 'receiver_profile_url': B, 'video_id': '7999999000000000088',
        'native_comment_id': None, 'command_text': state['root_envelope']['comment_text']})
    observe = getattr(module, 'observe_existing_choice_reply', None)
    assert callable(observe)
    assert observe(value, target(surface, state), TEXT)['outcome'] == 'delivery_unknown'


def test_stop_and_human_verification_propagate_without_cleanup_or_send(surface, monkeypatch):
    state = complete(surface)
    value = adapter(surface, state, monkeypatch)
    keys=[]
    monkeypatch.setattr(surface.page.keyboard, 'press', lambda key: keys.append(key))
    observe = getattr(module, 'observe_existing_choice_reply', None)
    assert callable(observe)
    selected = target(surface, state)
    value.choice_should_stop = lambda: True
    with pytest.raises(ReceiverPageError):observe(value, selected, TEXT)
    value.choice_should_stop = lambda: False
    monkeypatch.setattr(value, 'check_intervention', lambda: (_ for _ in ()).throw(ReceiverInterventionRequired('synthetic')))
    with pytest.raises(ReceiverInterventionRequired):observe(value, selected, TEXT)
    assert keys == [] and surface.page.evaluate('window.sendCount') == 0


def test_read_only_target_only_filter_cannot_hide_a_second_different_choice(surface, monkeypatch):
    state=complete(surface)
    selected=target(surface,state)
    value=adapter(surface,state,monkeypatch)
    surface.page.locator('#selection').evaluate('''e=>{
      const other=e.cloneNode(true);other.id='another-choice';other.querySelector('.FduGc_lz').textContent='存到111';e.parentElement.append(other);
    }''')
    result=module.observe_existing_choice_reply(value,selected,TEXT)
    assert result['outcome']=='delivery_unknown' and result['counts']['selected_source_count']==2


def test_explicit_direct_parent_mode_is_retained(surface, monkeypatch):
    from tests.test_library_choice_browser_dom import document, receiver as legacy_receiver, target as legacy_target, inspect as legacy_inspect, comment
    surface.load(document(actor=None))
    value=legacy_receiver(surface,monkeypatch)
    selected=legacy_target(legacy_inspect(surface)['details'][0])
    surface.page.locator('#selection').evaluate('(e,body)=>e.insertAdjacentHTML("beforeend",body)',comment(B,'虚构B',TEXT))
    result=module.observe_existing_choice_reply(value,selected,TEXT)
    assert result['outcome']=='b_view_visible' and result['readback_quality']=='direct_selected_parent'
    assert result['counts']['strict_matching_reply_count']==1 and result['counts']['weak_matching_reply_count']==0


def test_live_receiver_without_strict_target_method_has_no_legacy_fallback(surface, monkeypatch):
    state=complete(surface);value=adapter(surface,state,monkeypatch)
    monkeypatch.setattr(value,'open_original_from_notifications',None)
    monkeypatch.setattr(value,'notifications',lambda:pytest.fail('No live fallback'))
    assert module.observe_existing_choice_reply(value,target(surface,state),TEXT)['outcome']=='delivery_unknown'


def test_not_ok_full_detail_from_strict_method_is_rejected(surface, monkeypatch):
    state=complete(surface);value=adapter(surface,state,monkeypatch)
    monkeypatch.setattr(value,'open_original_from_notifications',lambda *a,**k:{'ok':False,'unique_count':1,
        'sender_profile_url':A,'receiver_profile_url':B,'video_id':VID,'native_comment_id':None,
        'command_text':state['root_envelope']['comment_text']})
    assert module.observe_existing_choice_reply(value,target(surface,state),TEXT)['outcome']=='delivery_unknown'


def test_observer_freezes_target_before_adapter_callback_can_mutate_caller(surface, monkeypatch):
    state=complete(surface);value=adapter(surface,state,monkeypatch)
    selected=target(surface,state);original=deepcopy(selected);opener=value.open_original_from_notifications
    def mutate(*a,**k):
        selected['choice_evidence']['choice_id']='f'*32
        selected['comment_text']='a changed caller object'
        return opener(*a,**k)
    monkeypatch.setattr(value,'open_original_from_notifications',mutate)
    result=module.observe_existing_choice_reply(value,selected,TEXT)
    assert result['outcome']=='b_view_visible'
    assert result['verification']['choice_id']==original['choice_evidence']['choice_id']
    assert result['verification']['comment_text']==original['comment_text']


def test_same_nickname_different_a_profile_is_not_the_frozen_selection(surface, monkeypatch):
    state=complete(surface);selected=target(surface,state);value=adapter(surface,state,monkeypatch)
    surface.page.locator('#selection').evaluate('''(e,other)=>{
      e.querySelector('.comment-item-avatar a').href=other;
      e.querySelectorAll('.comment-item-info-wrap a')[0].href=other;
    }''',OTHER)
    assert module.observe_existing_choice_reply(value,selected,TEXT)['outcome']=='delivery_unknown'


def test_duplicate_original_roots_are_not_one_verified_original(surface, monkeypatch):
    state=complete(surface);selected=target(surface,state);value=adapter(surface,state,monkeypatch)
    surface.page.locator('#root').evaluate("e=>{const copy=e.cloneNode(true);copy.id='second-root';document.body.append(copy)}")
    assert module.observe_existing_choice_reply(value,selected,TEXT)['outcome']=='delivery_unknown'


def test_root_same_body_without_semantic_b_to_a_header_is_only_barrier(surface):
    state=complete(surface)
    surface.page.locator('#receipt').evaluate('''(e,{b,text})=>{
      const row=document.createElement('article');row.setAttribute('data-e2e','comment-item');
      row.innerHTML='<a href="'+b+'">虚构B</a><div>'+text+'</div>';e.replaceWith(row);
    }''',{'b':B,'text':TEXT})
    result=inspect(surface,state)
    assert result['existing_matching_reply_count']==1 and result['matching_reply_count']==0


def test_a_second_genuine_receipt_body_block_is_not_ignored(surface):
    state=complete(surface)
    surface.page.locator('#receipt .FduGc_lz').evaluate("e=>e.insertAdjacentHTML('afterend','<div>这段正文不能丢弃。</div>')")
    result=inspect(surface,state)
    assert result['existing_matching_reply_count']==1 and result['matching_reply_count']==0


def test_real_shared_full_target_lookup_connects_to_choice_observer_without_compose(surface, monkeypatch):
    from app.mention_receiver.browser import DETAIL_JS, ReceiverIdentity
    state=complete(surface)
    selected=target(surface,state)
    value=NormalPageReceiver(surface.page,NUMBER,B)
    value.choice_should_stop=lambda:False
    navigations=[]
    monkeypatch.setattr(surface.page,'goto',lambda url,**k:navigations.append(url))
    monkeypatch.setattr(value,'identity',lambda:ReceiverIdentity(NUMBER,'synthetic B'))
    monkeypatch.setattr(value,'notifications',lambda:[{'source_profile_url':A,
        'exact_text':state['root_envelope']['comment_text'],'semantic_fingerprint':'synthetic-card'}])
    details=[]
    def detail(notice,*,keep_open=False):
        assert keep_open is True
        details.append(notice)
        return surface.page.evaluate(DETAIL_JS,{'sender':A,'receiver':B,'wanted':state['root_envelope']['comment_text']})
    monkeypatch.setattr(value,'detail',detail)
    monkeypatch.setattr(value,'prepare_reply',lambda *a,**k:pytest.fail('No prepare'))
    monkeypatch.setattr(value,'submit_reply',lambda *a,**k:pytest.fail('No send'))
    result=module.observe_existing_choice_reply(value,selected,TEXT)
    assert result['outcome']=='b_view_visible' and result['readback_quality']=='weak_unique_root'
    assert value.last_original_lookup['final_verified']==1
    assert len(navigations)==len(details)==2
    assert surface.page.evaluate('window.sendCount')==0


@pytest.mark.parametrize('where',['before_header','after_stats'])
def test_unknown_own_prose_outside_body_interval_cannot_be_ignored(surface,where):
    state=complete(surface)
    surface.page.locator('#receipt').evaluate('''(row,where)=>{
      const extra=document.createElement('div');extra.textContent='另一段实际正文，不能被忽略。';
      if(where==='before_header')row.querySelector('.comment-item-info-wrap').before(extra);
      else row.querySelector('.comment-item-stats-container').parentElement.after(extra);
    }''',where)
    result=inspect(surface,state)
    assert result['existing_matching_reply_count']==1
    assert result['matching_reply_count']==0 and result['weak_matching_reply_count']==0


def test_stop_inside_shared_opener_is_not_swallowed_as_delivery_unknown(surface,monkeypatch):
    state=complete(surface);selected=target(surface,state);value=adapter(surface,state,monkeypatch)
    stopped=[False];value.choice_should_stop=lambda:stopped[0]
    def stop(*a,**k):
        stopped[0]=True
        raise ReceiverPageError('synthetic original lookup stopped')
    monkeypatch.setattr(value,'open_original_from_notifications',stop)
    with pytest.raises(ReceiverPageError,match='停止'):
        module.observe_existing_choice_reply(value,selected,TEXT)


@pytest.mark.parametrize('region',['.comment-item-info-wrap','.comment-item-stats-container'])
def test_metadata_role_does_not_allow_unrecognized_additional_own_prose(surface,region):
    state=complete(surface)
    surface.page.locator('#receipt '+region).evaluate("e=>e.insertAdjacentHTML('beforeend','<div>另一段实际正文，不能忽略。</div>')")
    result=inspect(surface,state)
    assert result['existing_matching_reply_count']==1
    assert result['matching_reply_count']==0 and result['weak_matching_reply_count']==0
