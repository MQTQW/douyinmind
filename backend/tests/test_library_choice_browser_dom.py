"""Public DOM only, fresh non-persistent Edge; every request intercepted.

Fixtures never log in, call a service or click an actual send control.
"""
from copy import deepcopy
from datetime import datetime,timezone
import html
import time
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from app.assistant.choice_intent import selection_forms
from app.mention_agent.choice_evidence import make_choice_envelope,choice_ask_reference
from app.mention_agent.contracts import Binding,validate_envelope
from app.mention_receiver.browser import NormalPageReceiver,ReceiverIdentity,ReceiverDetailError,ReceiverInterventionRequired
from app.mention_receiver.choice_browser import CHOICE_THREAD_JS,CHOICE_REPLY_STATE_JS,read_choice_replies,observe_current_choice_replies,prepare_choice_reply,choice_reply_state,_session_args


A='https://www.douyin.com/user/choice-browser-A'
B='https://www.douyin.com/user/choice-browser-B'
OTHER='https://www.douyin.com/user/choice-browser-other'
VID='7999999000000000011';NUMBER='1234567'
ROOT='@虚构B 请保存当前视频。';ASK='你目前有2个知识库：1.编程库；2.经济库。请回复库名。';ANSWER='收到，合成资料已完成入库。'
URL='https://www.douyin.com/user/self?modal_id='+VID
OPTIONS=[{'knowledge_base_id':7,'name':'编程库'},{'knowledge_base_id':8,'name':'经济库'}]


def session():
    bind=Binding('root-all','choice-instance',A,B,None,True,query_scope='all_local')
    env={'command_id':'root-command','reference_id':'root-reference','reference_quality':'verified_ui',
         'native_comment_id':None,'sender_profile_url':A,'receiver_profile_url':B,'video_id':VID,
         'comment_text':ROOT,'source_mode':'isolated','evidence':{'real_mention':True,'sender_verified':True,
         'video_verified':True,'unique_count':1,'observed_at_utc':datetime.now(timezone.utc).isoformat()}}
    ref=choice_ask_reference(root_reference_id=env['reference_id'],ask_ref='ask-fixed',ask_text=ASK,receiver_profile_url=B,video_id=VID)
    return {'root_binding':bind.to_dict(),'root_envelope':env,'choice_id':'a'*32,'ask_ref':'ask-fixed',
            'ask_reply_ref':ref,'ask_text':ASK,'options':OPTIONS,'state':'waiting_library'}


def comment(author,name,body,*,identity='',children='',controls=True,native='',hidden=False):
    attrs=f' id="{identity}"' if identity else ''
    if native:attrs+=' data-comment-id="'+native+'"'
    if hidden:attrs+=' style="display:none"'
    return (f'<article data-e2e="comment-item"{attrs}><a href="{author}">{name}</a><div class="body">{body}</div>'
            +('<span class="reply" onclick="openReply(this)">回复</span>' if controls else '')+children+'</article>')


def document(*,body='1',nested=True,prefix=False,author=A,ask_author=B,ask_text=ASK,second=False,root_text=ROOT,
             root_mention=B,video=VID,actor=B,header=None,existing='',likes='',show_selection=True):
    selection_body='回复<a href="'+B+'">@虚构B</a>：'+html.escape(body) if prefix else html.escape(body)
    selection=comment(author,'虚构A',selection_body,identity='selection',children=likes+existing,
                      hidden=not show_selection)
    ask_children=selection if nested else ''
    ask=comment(ask_author,'虚构B',html.escape(ask_text),identity='ask',children=ask_children)
    tail='' if nested else selection
    if second:tail+=comment(A,'虚构A','经济库',identity='second-selection',children='') if not nested else ''
    if second and nested:ask=ask[:-10]+comment(A,'虚构A','经济库')+'</article>'
    suffix=html.escape(root_text.removeprefix('@虚构B'))
    root_body=f'<a href="{root_mention}">@虚构B</a>'+suffix
    root=comment(A,'虚构A',root_body,identity='root',children='<div class="replies">'+ask+tail+'</div>')
    text=body if not prefix else '回复@虚构B：'+body
    header_text=header if header is not None else '回复@虚构A: '+text
    actor_html=f'<div id="comment-input-container"><a href="{actor}"><img></a></div>' if actor else ''
    return f'''<!doctype html><meta charset="utf-8"><style>body{{font:14px sans-serif;margin:4px}}
      article,div,section{{display:block;min-height:18px;padding:3px}}span,a{{display:inline-block;min-height:20px}}
      img{{width:20px;height:20px}}svg{{width:25px;height:25px}}[contenteditable]{{white-space:pre-wrap}}</style>
      <h1>虚构B</h1><p>抖音号：{NUMBER}</p>{actor_html}<section data-e2e="detail-video-info" data-e2e-aweme-id="{video}">合成视频</section>
      {root}<section class="comment-input-container" id="global" style="display:none"><span id="header">{html.escape(header_text)}</span>
      <div class="comment-input-inner-container"><div contenteditable="true" role="combobox" id="editor"></div>
      <span onclick="window.sendCount++"><svg><path fill="#FE2C55" d="M17.5 30C23.851"></path></svg></span></div></section>
      <script>window.sendCount=0;window.openCount=0;function openReply(e){{window.openCount++;document.getElementById('global').style.display='block';}}</script>'''


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        yield browser
        browser.close()


@pytest.fixture
def surface(browser):
    context=browser.new_context(viewport={'width':1200,'height':1600},service_workers='block')
    saved={'html':''};requests=[]
    def intercept(route):
        requests.append(route.request.url)
        if route.request.is_navigation_request() and route.request.resource_type=='document':
            route.fulfill(status=200,content_type='text/html; charset=utf-8',body=saved['html'])
        else:route.abort()
    context.route('**/*',intercept);page=context.new_page()
    def load(value,url=URL):saved['html']=value;page.goto(url,wait_until='domcontentloaded')
    yield SimpleNamespace(page=page,load=load,requests=requests)
    context.close()


def inspect(surface,**changes):
    args=_session_args(session(),selection_forms(OPTIONS));args.update(changes)
    return surface.page.evaluate(CHOICE_THREAD_JS,args)


def receiver(surface,monkeypatch):
    value=NormalPageReceiver(surface.page,NUMBER,B)
    monkeypatch.setattr(surface.page,'goto',lambda *_,**__:None)
    monkeypatch.setattr(value,'identity',lambda:ReceiverIdentity(NUMBER,'虚构B'))
    monkeypatch.setattr(value,'notifications',lambda:[{'source_profile_url':A,'exact_text':ROOT,'semantic_fingerprint':'fixture'}])
    monkeypatch.setattr(value,'detail',lambda *_,**__:{'sender_profile_url':A,'receiver_profile_url':B,'video_id':VID,
        'native_comment_id':None,'command_text':ROOT})
    def open_original(target, *, max_candidates=3):
        from app.mention_receiver.browser import SELF_URL
        value.check_intervention()
        surface.page.goto(SELF_URL,wait_until='domcontentloaded',timeout=30000)
        assert value.identity() is not None
        value._reply_identity_checked_at=time.monotonic()
        matches=[item for item in value.notifications() if item['source_profile_url']==target['sender_profile_url']
                 and ''.join(item['exact_text'].split())==''.join(target['comment_text'].split())]
        if len(matches)!=1:raise ReceiverDetailError('synthetic unique root unavailable',{})
        try:
            detail=value.detail(matches[0],keep_open=True)
        except ReceiverInterventionRequired:
            raise
        except Exception:
            raise ReceiverDetailError('synthetic original candidate unreadable',{'reason_code':'original_candidate_unreadable'}) from None
        return {**detail,'ok':True,'unique_count':1}
    monkeypatch.setattr(value,'open_original_from_notifications',open_original,raising=False)
    return value


def target(detail):
    bind=Binding('selected-library','choice-instance',A,B,7,True)
    env=make_choice_envelope({'source_profile_url':A,'exact_text':detail['command_text']},detail,bind,'isolated')
    assert validate_envelope(env,bind)==env
    return {key:value for key,value in env.items() if key!='evidence'}|{'choice_evidence':env['evidence']}


def test_direct_ask_parent_selection_is_verified_without_faking_real_mention(surface):
    surface.load(document())
    result=inspect(surface)
    assert result['ok'] and len(result['details'])==1
    detail=result['details'][0]
    assert detail['command_text']=='1' and detail['native_comment_id'] is None
    assert detail['evidence']['parent_scope']=='verified_ask_parent'
    assert detail['evidence']['direct_reply_to_ask'] is True and detail['evidence']['real_mention'] is False
    target(detail)


def test_flat_canonical_reply_label_retains_whole_actual_body_and_weaker_parent_scope(surface):
    surface.load(document(nested=False,prefix=True,body='编程库'))
    result=inspect(surface);assert result['ok'] and len(result['details'])==1
    detail=result['details'][0]
    assert detail['command_text']=='回复@虚构B：编程库'
    assert detail['evidence']['selection_value']=='编程库'
    assert detail['evidence']['parent_scope']=='unique_pending_choice_in_verified_root'
    assert detail['evidence']['direct_reply_to_ask'] is False and detail['evidence']['real_mention'] is False
    target(detail)


def test_flat_unaddressed_number_does_not_guess_reply_to_b(surface):
    surface.load(document(nested=False))
    assert inspect(surface)['details']==[]


@pytest.mark.parametrize('likes',[
    '<div><svg></svg><span>1</span></div>',
    '<button><span>1</span></button>',
    '<div role="button"><span>1</span></div>',
    '<svg></svg><span>1</span>',
])
def test_unrelated_a_reply_like_count_one_never_becomes_library_selection(surface,likes):
    surface.load(document(body='这个视频讲得很好。',likes=likes))
    assert inspect(surface)['details']==[]


@pytest.mark.parametrize('change',[
    {'author':OTHER},{'root_mention':OTHER},{'root_text':'@虚构B 另一条请求'},
    {'ask_author':OTHER},{'ask_text':'另一条选库提示'},{'video':'7999999000000000022'},
])
def test_wrong_owner_original_prompt_or_video_never_yields_selection(surface,change):
    surface.load(document(**change));value=inspect(surface)
    assert not value.get('ok') or value.get('details')==[]


def test_two_a_selections_are_ambiguous_not_two_unique_claims(surface):
    surface.load(document(second=True))
    value=inspect(surface)
    assert not value['ok'] and value['reason_code']=='choice_selection_not_unique'


def test_hidden_selection_cannot_satisfy_public_source(surface):
    surface.load(document(show_selection=False))
    assert inspect(surface)['details']==[]


def test_read_zero_selection_still_records_stable_fresh_ask_proof(surface,monkeypatch):
    surface.load(document(show_selection=False))
    adapter=receiver(surface,monkeypatch);state=session();state['ask_reply_ref']=None
    assert read_choice_replies(adapter,state,selection_forms(OPTIONS),keep_open=True)==[]
    proof=adapter.last_choice_ask_proof
    assert proof['ask_reply_ref']==session()['ask_reply_ref'] and proof['native_comment_id'] is None
    assert proof['source_kind']=='library_choice_ask' and proof['exact_body_in_original_thread'] is True


def test_prepare_only_fills_exact_selection_composer_and_never_sends(surface,monkeypatch):
    surface.load(document(actor=None))
    adapter=receiver(surface,monkeypatch)
    detail=inspect(surface)['details'][0];selected=target(detail)
    proof=prepare_choice_reply(adapter,selected,ANSWER)
    assert proof['root_reference_id']==session()['root_envelope']['reference_id']
    assert proof['parent_scope']=='verified_ask_parent'
    assert surface.page.locator('#editor').inner_text()==ANSWER
    assert surface.page.evaluate('window.sendCount')==0 and surface.page.evaluate('window.openCount')==1
    assert choice_reply_state(adapter,selected,ANSWER)['root_index']==detail['comment_index']


def test_wrong_full_parent_header_cannot_authorize_modal_composer(surface,monkeypatch):
    from app.mention_agent.publisher import ReplyPreflightError
    surface.load(document(actor=None,header='回复@虚构A: 别的正文'))
    adapter=receiver(surface,monkeypatch);selected=target(inspect(surface)['details'][0])
    with pytest.raises(ReplyPreflightError,match='composer_parent_not_verified'):prepare_choice_reply(adapter,selected,ANSWER)
    assert surface.page.evaluate('window.sendCount')==0 and surface.page.locator('#editor').inner_text()==''


def test_duplicate_b_prompt_is_not_a_unique_choice_parent(surface):
    surface.load(document())
    surface.page.evaluate("document.getElementById('root').append(document.getElementById('ask').cloneNode(true))")
    value=inspect(surface)
    assert not value['ok'] and value['reason_code']=='choice_prompt_not_unique'


def test_correct_prompt_outside_root_does_not_establish_parent(surface):
    surface.load(document())
    surface.page.evaluate("document.body.append(document.getElementById('ask'))")
    value=inspect(surface)
    assert value['ask_missing'] and value['details']==[]


def test_flat_homonym_link_to_other_profile_never_passes_canonical_b(surface):
    surface.load(document(nested=False,prefix=True,body='编程库'))
    surface.page.locator('#selection .body a').evaluate('(el,url)=>el.href=url',OTHER)
    assert inspect(surface)['details']==[]


def test_valid_numeric_body_with_separate_like_control_is_still_body_not_count(surface):
    surface.load(document(likes='<div><svg></svg><span>9</span></div>'))
    value=inspect(surface)
    assert len(value['details'])==1 and value['details'][0]['command_text']=='1'


def test_conflicting_public_native_attributes_do_not_invent_comment_id(surface):
    surface.load(document())
    surface.page.locator('#selection').evaluate("e=>{e.setAttribute('data-comment-id','123456');e.setAttribute('data-e2e-comment-id','987654')}")
    value=inspect(surface)
    assert not value['ok'] and value['reason_code']=='choice_selection_native_id_conflict'


def test_prepare_header_changed_before_submit_prevents_gate_and_send(surface,monkeypatch):
    from app.mention_agent.publisher import ReplyPreflightError
    surface.load(document(actor=None));adapter=receiver(surface,monkeypatch)
    selected=target(inspect(surface)['details'][0]);prepare_choice_reply(adapter,selected,ANSWER)
    monkeypatch.setattr(adapter,'_reply_state',lambda target,text:choice_reply_state(adapter,target,text))
    surface.page.locator('#header').evaluate("e=>e.textContent='回复@虚构A: 别的选库评论'")
    gates=[]
    with pytest.raises(ReplyPreflightError):adapter.submit_reply(selected,ANSWER,before_submit=lambda:gates.append(1))
    assert gates==[] and surface.page.evaluate('window.sendCount')==0


def test_same_b_body_flat_in_root_is_only_no_send_barrier_not_exact_readback(surface,monkeypatch):
    surface.load(document(actor=None));adapter=receiver(surface,monkeypatch)
    selected=target(inspect(surface)['details'][0]);adapter._reply_identity_checked_at=time.monotonic()
    child=comment(B,'虚构B',html.escape(ANSWER))
    surface.page.locator('#root').evaluate('(e,value)=>e.insertAdjacentHTML("beforeend",value)',child)
    value=choice_reply_state(adapter,selected,ANSWER)
    assert value['existing_matching_reply_count']==1 and value['matching_reply_count']==0


def test_read_expands_at_most_twice_and_uses_one_self_navigation(surface,monkeypatch):
    surface.load(document(show_selection=False));adapter=receiver(surface,monkeypatch)
    navigations=[];monkeypatch.setattr(surface.page,'goto',lambda url,**_:navigations.append(url))
    surface.page.locator('#root').evaluate('''e=>{const more=document.createElement('span');more.setAttribute('data-e2e','video-comment-more');
      more.textContent='展开更多回复';more.onclick=()=>{window.moreCount=(window.moreCount||0)+1;if(window.moreCount===2)document.getElementById('selection').style.display='block'};e.append(more)}''')
    found=read_choice_replies(adapter,session(),selection_forms(OPTIONS),keep_open=True)
    assert len(found)==1 and surface.page.evaluate('window.moreCount')==2 and len(navigations)==1
    assert surface.page.evaluate('window.sendCount')==0


def test_missing_prompt_after_two_expansions_is_deferred_not_empty_or_confirmed(surface,monkeypatch):
    surface.load(document(ask_text='别的正文'));adapter=receiver(surface,monkeypatch)
    surface.page.locator('#root').evaluate('''e=>{const more=document.createElement('span');more.setAttribute('data-e2e','video-comment-more');
      more.textContent='展开更多回复';more.onclick=()=>window.moreCount=(window.moreCount||0)+1;e.append(more)}''')
    with pytest.raises(ReceiverDetailError,match='选库提示'):
        read_choice_replies(adapter,session(),selection_forms(OPTIONS),keep_open=True)
    assert surface.page.evaluate('window.moreCount')==2 and adapter.last_choice_ask_proof is None


def test_intervention_before_read_performs_no_navigation_or_escape(surface,monkeypatch):
    surface.load(document());adapter=receiver(surface,monkeypatch);events=[]
    monkeypatch.setattr(surface.page,'goto',lambda *_,**__:events.append('goto'))
    monkeypatch.setattr(surface.page.keyboard,'press',lambda *_:events.append('escape'))
    def challenge():raise ReceiverInterventionRequired('synthetic verification')
    monkeypatch.setattr(adapter,'check_intervention',challenge)
    with pytest.raises(ReceiverInterventionRequired):read_choice_replies(adapter,session(),selection_forms(OPTIONS))
    assert events==[] and adapter.last_choice_ask_proof is None


def test_no_choice_read_clears_stale_proof_before_root_failure(surface,monkeypatch):
    surface.load(document());adapter=receiver(surface,monkeypatch);adapter.last_choice_ask_proof={'old':True}
    monkeypatch.setattr(adapter,'notifications',lambda:[])
    with pytest.raises(ReceiverDetailError):read_choice_replies(adapter,session(),selection_forms(OPTIONS),keep_open=True)
    assert adapter.last_choice_ask_proof is None


def test_image_only_a_reply_with_flat_like_one_is_not_a_choice(surface):
    surface.load(document(body='',likes='<svg></svg><span>1</span>'))
    surface.page.locator('#selection .body').evaluate("e=>e.innerHTML='<img alt=表情>'")
    assert inspect(surface)['details']==[]


def test_current_observer_has_zero_ui_inputs_and_derives_exact_persisted_selection(surface,monkeypatch):
    surface.load(document());adapter=receiver(surface,monkeypatch);events=[]
    monkeypatch.setattr(surface.page,'goto',lambda *_,**__:events.append('goto'))
    monkeypatch.setattr(adapter,'notifications',lambda:events.append('panel'))
    monkeypatch.setattr(surface.page.keyboard,'press',lambda *_:events.append('key'))
    old=inspect(surface)['details'][0]
    current=observe_current_choice_replies(adapter,session(),selection_forms(OPTIONS))
    assert len(current)==1 and current[0]['command_text']==old['command_text']
    assert target(current[0])['reference_id']==target(old)['reference_id']
    assert events==[] and surface.page.evaluate('window.openCount')==0 and surface.page.evaluate('window.sendCount')==0
    assert adapter.last_choice_ask_proof['ask_reply_ref']==session()['ask_reply_ref']


def test_leading_canonical_at_label_selection_keeps_full_body_for_late_notice_dedup(surface):
    surface.load(document(nested=False,prefix=True,body='编程库'))
    surface.page.locator('#selection .body').evaluate("(e,url)=>e.innerHTML='<a href=\"'+url+'\">@虚构B</a> 编程库'",B)
    value=inspect(surface)
    assert len(value['details'])==1
    detail=value['details'][0]
    assert detail['command_text']=='@虚构B 编程库' and detail['evidence']['selection_value']=='编程库'
    assert detail['evidence']['real_mention'] is False
    target(detail)


def test_hidden_same_b_body_is_send_barrier_but_never_positive_readback(surface,monkeypatch):
    hidden=comment(B,'虚构B',html.escape(ANSWER),hidden=True)
    surface.load(document(actor=None,existing=hidden));adapter=receiver(surface,monkeypatch)
    selected=target(inspect(surface)['details'][0]);adapter._reply_identity_checked_at=time.monotonic()
    value=choice_reply_state(adapter,selected,ANSWER)
    assert value['existing_matching_reply_count']==1 and value['matching_reply_count']==0


def test_held_ask_can_only_be_observed_not_resent_by_reader(surface,monkeypatch):
    surface.load(document(show_selection=False));adapter=receiver(surface,monkeypatch);state=session();state['state']='ask_held'
    assert read_choice_replies(adapter,state,selection_forms(OPTIONS),keep_open=True)==[]
    assert adapter.last_choice_ask_proof is not None and surface.page.evaluate('window.sendCount')==0


def test_root_detail_drift_is_local_deferral_and_clears_old_proof(surface,monkeypatch):
    from app.mention_receiver.browser import ReceiverPageError
    surface.load(document());adapter=receiver(surface,monkeypatch);adapter.last_choice_ask_proof={'old':True}
    def unavailable(*_,**__):raise ReceiverPageError('synthetic drift')
    monkeypatch.setattr(adapter,'detail',unavailable)
    with pytest.raises(ReceiverDetailError) as failure:
        read_choice_replies(adapter,session(),selection_forms(OPTIONS),keep_open=True)
    assert failure.value.diagnostics['reason_code']=='original_candidate_unreadable'
    assert adapter.last_choice_ask_proof is None


def test_flat_preparation_verification_matches_store_choice_gate_protocol(surface,monkeypatch):
    from app.mention_agent.store import _verified_target
    surface.load(document(actor=None));adapter=receiver(surface,monkeypatch)
    selected=target(inspect(surface)['details'][0])
    verification=prepare_choice_reply(adapter,selected,ANSWER)
    bind=Binding('selected-library','choice-instance',A,B,7,True)
    assert _verified_target(selected,verification,bind.to_dict()) is True
    assert surface.page.evaluate('window.sendCount')==0


@pytest.mark.parametrize('url',[
    'https://example.invalid/user/self?modal_id='+VID,
    'http://www.douyin.com/user/self?modal_id='+VID,
    'https://www.douyin.com/user/self?modal_id=7999999000000000022',
])
def test_matching_public_dom_on_wrong_origin_or_modal_route_is_not_source_evidence(surface,url):
    surface.load(document(),url=url)
    assert inspect(surface)['ok'] is False


def test_visible_b_wrapper_cannot_borrow_nested_c_body_as_exact_b_readback(surface,monkeypatch):
    surface.load(document(actor=None));adapter=receiver(surface,monkeypatch)
    selected=target(inspect(surface)['details'][0]);adapter._reply_identity_checked_at=time.monotonic()
    nested='<article data-e2e="comment-item"><a style="display:none" href="'+OTHER+'">隐藏C</a><div>'+html.escape(ANSWER)+'</div></article>'
    wrapper=comment(B,'虚构B','',children='<div>'+nested+'</div>',controls=False)
    surface.page.locator('#selection').evaluate('(e,value)=>e.insertAdjacentHTML("beforeend",value)',wrapper)
    state=choice_reply_state(adapter,selected,ANSWER)
    assert state['matching_reply_count']==0


def test_submit_state_cannot_downgrade_frozen_direct_ask_parent_after_prepare(surface,monkeypatch):
    from app.mention_agent.publisher import ReplyPreflightError
    surface.load(document(actor=None,prefix=True,body='编程库'));adapter=receiver(surface,monkeypatch)
    selected=target(inspect(surface)['details'][0]);prepare_choice_reply(adapter,selected,ANSWER)
    assert selected['choice_evidence']['direct_reply_to_ask'] is True
    surface.page.evaluate("document.getElementById('root').append(document.getElementById('selection'))")
    with pytest.raises(ReplyPreflightError,match='choice_frozen_parent_changed'):
        choice_reply_state(adapter,selected,ANSWER)
    assert surface.page.evaluate('window.sendCount')==0
