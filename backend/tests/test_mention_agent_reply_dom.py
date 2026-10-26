"""Bounded reply adapter on synthetic offline pages; no real account/API."""
import html
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from app.mention_receiver.browser import NormalPageReceiver, ReceiverIdentity, REPLY_STATE_JS, DETAIL_JS
from app.mention_agent.publisher import ReplyPreflightError


A = 'https://www.douyin.com/user/sender-synthetic'
B = 'https://www.douyin.com/user/receiver-synthetic'
OTHER = 'https://www.douyin.com/user/other-synthetic'
VID = '7999999000000000011'
QUESTION = '@助手 请根据测试库回答问题 DM-SYNTHETIC-02'
ANSWER = '这是依据原文的测试答案。\n[来源：资料1、资料2]'
TARGET = dict(sender_profile_url=A, receiver_profile_url=B, video_id=VID,
              native_comment_id=None, comment_text=QUESTION, reference_id='synthetic-reference', source_mode='live_ui')


def child(owner=B, text=ANSWER):
    return f'<article data-e2e="comment-item"><a href="{owner}">作者</a><div>{html.escape(text)}</div></article>'


def root(children='', *, cid='', editor_owned=True, draft='', arrow=True):
    editor = ('<section class="comment-input-inner-container" style="display:none" id="editor-area">'
              f'<div contenteditable="true" role="combobox">{html.escape(draft)}</div>'
              '<span id="send" onclick="postReply()"><svg><path fill="#FE2C55" '
              f'd="{"M17.5 30C23.851" if arrow else "changed-shape"}"></path></svg></span></section>')
    return (f'<article data-e2e="comment-item" {cid} id="root">'
            f'<a href="{A}">提问者</a><div><a href="{B}">@助手</a> 请根据测试库回答问题 DM-SYNTHETIC-02</div>'
            '<span onclick="document.getElementById(\'editor-area\').style.display=\'block\'">回复</span>'
            + (editor if editor_owned else '') + children + '</article>' + ('' if editor_owned else editor))


def document(body=None, *, actor=B, **kwargs):
    return (f'''<!doctype html><meta charset="utf-8"><style>
      body{{font:16px/1.5 sans-serif}}article,section,div{{min-height:20px;padding:5px}}
      svg{{width:36px;height:36px}}span{{display:inline-block;min-width:35px;min-height:25px}}
      img{{width:20px;height:20px}}[contenteditable]{{white-space:pre-wrap}}
      </style><section data-e2e="detail-video-info" data-e2e-aweme-id="{VID}">虚构视频</section>
      <div id="comment-input-container"><a href="{actor}"><img alt="头像"></a></div>'''
      + (root(**kwargs) if body is None else body) + f'''<script>
      window.submitCount=0;
      function postReply(){{window.submitCount++;const r=document.getElementById('root');
       const e=document.querySelector('[contenteditable]');const n=document.createElement('article');
       n.setAttribute('data-e2e','comment-item');const a=document.createElement('a');a.href='{B}';a.textContent='助手';
       const d=document.createElement('div');d.textContent=e.innerText;n.append(a,d);r.append(n);e.textContent='';}}
      </script>''')


@pytest.fixture(scope='module')
def installed_browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True,proxy={'server':'http://127.0.0.1:9'},
            args=['--disable-background-networking','--disable-component-update','--disable-sync','--disable-quic'])
        yield browser
        browser.close()


@pytest.fixture
def page(installed_browser):
    context = installed_browser.new_context(viewport={'width':1100,'height':1000}, service_workers='block',offline=True)
    context.route('**/*', lambda route: route.abort())
    page = context.new_page()
    yield page
    context.close()


def state(page, **args):
    return page.evaluate(REPLY_STATE_JS, dict(sender=A, receiver=B, wanted=QUESTION, video=VID,
        native_comment_id=None, reply=ANSWER) | args)


def adapter(page, monkeypatch, content):
    page.set_content(content)
    value = NormalPageReceiver(page, '123456789', B)
    monkeypatch.setattr(value, 'identity', lambda: ReceiverIdentity('123456789', '助手'))
    monkeypatch.setattr(page, 'goto', lambda *a, **k: None)
    monkeypatch.setattr(value, 'notifications', lambda:[{'source_profile_url':A,'exact_text':QUESTION,
        'semantic_fingerprint':'synthetic-reply-notification','pre_index':0}])
    # This suite starts at an inline fake DOM, rather than an actual notice
    # click. Still use the unchanged product detail reader to prove that DOM.
    monkeypatch.setattr(value, 'detail', lambda notice,**kwargs:page.evaluate(DETAIL_JS,
        {'sender':A,'wanted':QUESTION,'receiver':B}))
    return value


def test_exact_b_reply_must_belong_to_original_parent(page):
    page.set_content(document(root(children=child())))
    assert state(page)['matching_reply_count'] == 1
    page.set_content(document(root() + child()))
    assert state(page)['matching_reply_count'] == 0
    page.set_content(document(root(children=child(owner=OTHER))))
    assert state(page)['matching_reply_count'] == 0


def test_nested_reply_cannot_borrow_another_authors_identity(page):
    page.set_content(document(root(children=child(owner=OTHER, text='引用助手：'+ANSWER))))
    assert state(page)['matching_reply_count'] == 0


def test_hidden_matching_reply_blocks_but_is_not_visibility_proof(page, monkeypatch):
    hidden = child().replace('<article ', '<article style="display:none" ', 1)
    value=adapter(page,monkeypatch,document(children=hidden))
    observed=state(page)
    assert observed['matching_reply_count'] == 0
    assert observed['existing_matching_reply_count'] == 1
    with pytest.raises(ReplyPreflightError,match='existing_matching_reply'):
        value.prepare_reply(TARGET,ANSWER)
    assert page.evaluate('window.submitCount') == 0


def test_same_visible_original_twice_is_not_unique(page):
    page.set_content(document(root()+root()))
    assert state(page)['ok'] is False


def test_native_id_cannot_disappear_or_change(page):
    page.set_content(document())
    assert state(page, native_comment_id='7999999000000000099')['ok'] is False
    page.set_content(document(cid='data-comment-id="7999999000000000099"'))
    assert state(page, native_comment_id='7999999000000000099')['ok'] is True
    assert state(page, native_comment_id='7999999000000000088')['ok'] is False


def test_wrong_video_or_current_actor_is_rejected(page):
    page.set_content(document(actor=OTHER))
    assert state(page)['current_receiver_verified'] is False
    assert state(page, video='7999999000000000022')['ok'] is False


def test_prepare_literal_multiline_then_one_click_readback(page, monkeypatch, tmp_path):
    value = adapter(page, monkeypatch, document())
    verification = value.prepare_reply(TARGET, ANSWER)
    assert verification['native_comment_id'] is None
    assert page.evaluate('window.submitCount') == 0
    calls=[]
    result=value.submit_reply(TARGET, ANSWER, before_submit=lambda:calls.append('gate'))
    assert result == {'outcome':'b_view_visible','exact_body_in_original_thread':True}
    assert calls == ['gate']
    assert page.evaluate('window.submitCount') == 1
    value.save_reply_evidence(TARGET, ANSWER, tmp_path/'proof.png')
    assert (tmp_path/'proof.png').is_file()


def test_existing_matching_reply_prevents_draft_and_click(page, monkeypatch):
    value=adapter(page,monkeypatch,document(children=child()))
    with pytest.raises(ReplyPreflightError,match='existing_matching_reply'):
        value.prepare_reply(TARGET,ANSWER)
    assert page.evaluate('window.submitCount') == 0


def test_existing_composer_draft_is_not_overwritten(page, monkeypatch):
    value=adapter(page,monkeypatch,document(draft='原来未发送的文字'))
    with pytest.raises(ReplyPreflightError,match='existing_composer_draft'):
        value.prepare_reply(TARGET,ANSWER)
    assert page.get_by_role('combobox').inner_text() == '原来未发送的文字'


def test_global_composer_cannot_replace_owned_inline_editor(page, monkeypatch):
    value=adapter(page,monkeypatch,document(editor_owned=False))
    with pytest.raises(ReplyPreflightError,match='composer_parent_not_verified'):
        value.prepare_reply(TARGET,ANSWER)


def test_changed_send_icon_fails_closed(page, monkeypatch):
    value=adapter(page,monkeypatch,document(arrow=False))
    with pytest.raises(ReplyPreflightError,match='draft_or_send_control_not_verified'):
        value.prepare_reply(TARGET,ANSWER)
    assert page.evaluate('window.submitCount') == 0


def test_target_changes_between_prepare_and_submit_no_gate(page, monkeypatch):
    value=adapter(page,monkeypatch,document())
    value.prepare_reply(TARGET,ANSWER)
    page.locator('#root > a').evaluate('(n)=>n.href="'+OTHER+'"')
    calls=[]
    with pytest.raises(ReplyPreflightError):
        value.submit_reply(TARGET,ANSWER,before_submit=lambda:calls.append('gate'))
    assert not calls and page.evaluate('window.submitCount') == 0


def test_gate_failure_prevents_actual_submit(page, monkeypatch):
    value=adapter(page,monkeypatch,document())
    value.prepare_reply(TARGET,ANSWER)
    def gate():raise ReplyPreflightError('run_stopped')
    with pytest.raises(ReplyPreflightError,match='run_stopped'):
        value.submit_reply(TARGET,ANSWER,before_submit=gate)
    assert page.evaluate('window.submitCount') == 0


def test_missing_notice_fingerprint_still_rejects_before_touching_inline_draft(page,monkeypatch):
    value=adapter(page,monkeypatch,document(draft='原有未发文字'))
    monkeypatch.setattr(value,'notifications',lambda:[{'source_profile_url':A,'exact_text':QUESTION}])
    with pytest.raises(ReplyPreflightError,match='original_notifications_unavailable'):
        value.prepare_reply(TARGET,ANSWER)
    assert page.get_by_role('combobox',include_hidden=True).inner_text()=='原有未发文字'
    assert page.evaluate('window.submitCount')==0
