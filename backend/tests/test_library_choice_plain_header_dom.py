"""Observed public A -> B header replay, sanitized and completely offline.

The captured layout is data, not a live account. Every request is intercepted;
no send method is used. Flat arrows never establish an exact native parent.
"""
from pathlib import Path
from types import SimpleNamespace
import html
import time

import pytest

from tests.test_library_choice_browser_dom import (
    browser, document, inspect, receiver, target, session, A, B, OTHER, URL, ANSWER,
)
from app.mention_receiver.choice_browser import (choice_reply_state, prepare_choice_reply, CHOICE_THREAD_JS, _session_args)


BODY = '存到编程库'
CAPTURE = Path(__file__).with_name('fixtures') / 'choice_plain_arrow_sanitized.html'


@pytest.fixture
def surface(browser):
    context = browser.new_context(viewport={'width': 1200, 'height': 4000}, service_workers='block')
    saved = {'html': ''}
    requests = []
    def intercept(route):
        requests.append(route.request.url)
        if route.request.is_navigation_request() and route.request.resource_type == 'document':
            route.fulfill(status=200, content_type='text/html; charset=utf-8', body=saved['html'])
        else:
            route.abort()
    context.route('**/*', intercept)
    page = context.new_page()
    def load(value, url=URL):
        saved['html'] = value
        page.goto(url, wait_until='domcontentloaded')
    yield SimpleNamespace(page=page, load=load, requests=requests)
    context.close()


def observed(surface):
    surface.load(document(actor=None, body=BODY, header='回复@虚构A: ' + BODY))
    surface.page.add_style_tag(content='''
      div { min-height: 0; padding: 0; }
      .comment-item-info-wrap a, .comment-item-avatar a { display: inline-block; }
      .ym1IYI4C { display:inline-block; width:0; height:0; border-style:solid;
        border-color:transparent; border-right-width:0; }
      .aSKePbp5 { display:none; }
    ''')
    surface.page.locator('#root').evaluate('(node,value)=>node.outerHTML=value', CAPTURE.read_text(encoding='utf-8'))
    surface.page.evaluate('''() => {
      const rows=[...document.querySelectorAll('[data-e2e="comment-item"]')];
      rows[0].id='root'; rows[1].id='ask'; rows[2].id='selection';
    }''')


def append_b_header_reply(surface, *, nested=False, row_html=None):
    surface.page.evaluate('''({a,b,answer,nested,rowHTML})=>{
      const template=document.createElement('template');template.innerHTML=rowHTML||'';
      const row=rowHTML?template.content.firstElementChild:document.getElementById('selection').cloneNode(true);
      row.id='b-reply';
      const avatar=row.querySelector('.comment-item-avatar a'); avatar.href=b;
      const links=row.querySelectorAll('.comment-item-info-wrap a');
      links[0].href=b; links[0].querySelector('[data-click-from="title"]').textContent='虚构B';
      links[1].href=a; links[1].querySelector('[data-click-from="title"]').textContent='虚构A';
      row.querySelector('.FduGc_lz').textContent=answer;
      (nested?document.getElementById('selection'):document.getElementById('root')).append(row);
    }''', {'a': A, 'b': B, 'answer': ANSWER, 'nested': nested, 'rowHTML': row_html})


def test_observed_plain_a_arrow_b_header_is_a_weak_choice_not_a_mention(surface):
    observed(surface)
    value = inspect(surface)
    assert value['ok'] and len(value['details']) == 1
    detail = value['details'][0]
    assert detail['command_text'] == BODY and detail['sender_display_name'] == '虚构A'
    proof = detail['evidence']
    assert proof['parent_scope'] == 'unique_pending_choice_in_verified_root'
    assert proof['direct_reply_to_ask'] is False and proof['real_mention'] is False
    assert 'receiver_reply_label' not in proof and 'selection_value' not in proof
    assert detail['native_comment_id'] is None
    target(detail)


def test_observed_flat_b_arrow_a_same_body_is_no_send_barrier_only(surface, monkeypatch):
    observed(surface)
    row = surface.page.locator('#selection').evaluate('e=>e.outerHTML')
    surface.load(document(actor=None))
    surface.page.add_style_tag(content='.ym1IYI4C{display:inline-block;width:0;height:0;border-style:solid;border-color:transparent;border-right-width:0}')
    details = inspect(surface)['details']
    assert len(details) == 1
    adapter = receiver(surface, monkeypatch)
    adapter._reply_identity_checked_at = time.monotonic()
    selected = target(details[0])
    append_b_header_reply(surface, row_html=row)
    result = choice_reply_state(adapter, selected, ANSWER)
    assert result['existing_matching_reply_count'] == 1
    assert result['matching_reply_count'] == 0


def test_observed_plain_header_can_prepare_exact_selection_without_send(surface, monkeypatch):
    observed(surface)
    details = inspect(surface)['details']
    assert len(details) == 1
    adapter = receiver(surface, monkeypatch)
    surface.page.locator('#selection').evaluate("e=>e.querySelector('.comment-item-stats-container').addEventListener('click',()=>openReply(e))")
    selected = target(details[0])
    proof = prepare_choice_reply(adapter, selected, ANSWER)
    assert proof['direct_reply_to_ask'] is False and proof['real_mention'] is False
    assert surface.page.locator('#editor').inner_text() == ANSWER
    assert surface.page.evaluate('window.sendCount') == 0


@pytest.mark.parametrize('mutation', [
    "e.querySelector('.comment-item-avatar a').href=other",
    "e.querySelectorAll('.comment-item-info-wrap a')[1].href=other",
    "e.querySelector('.ym1IYI4C').remove()",
    "e.querySelector('.ym1IYI4C').style.display='none'",
    "e.querySelector('.ym1IYI4C').style.borderLeftWidth='0px'",
    "e.querySelectorAll('.comment-item-info-wrap a')[0].style.display='none'",
    "e.querySelectorAll('.comment-item-info-wrap a')[1].style.display='none'",
])
def test_plain_header_identity_or_target_conflict_is_not_guessed(surface, mutation):
    observed(surface)
    surface.page.locator('#selection').evaluate('(e,other)=>{' + mutation + '}', OTHER)
    assert inspect(surface)['details'] == []


def test_profile_links_in_body_do_not_create_plain_reply_header(surface):
    observed(surface)
    surface.page.locator('#selection .comment-item-info-wrap').evaluate("e=>e.innerHTML='<a href=\"' + e.querySelector('a').href + '\"><span data-click-from=\"title\">虚构A</span></a>'")
    surface.page.locator('#selection .FduGc_lz').evaluate('(e,b)=>e.innerHTML+=\'<a href="\'+b+\'">虚构B</a>\'', B)
    assert inspect(surface)['details'] == []


@pytest.mark.parametrize('style', ['opacity:0', 'visibility:hidden', 'border-left-color:transparent'])
def test_invisible_arrow_does_not_assert_a_public_reply_target(surface, style):
    observed(surface)
    surface.page.locator('#selection .ym1IYI4C').evaluate('(e,style)=>e.style.cssText+=";"+style', style)
    assert inspect(surface)['details'] == []


def test_observed_plain_choice_enters_real_temporary_choice_cas_once(surface, tmp_path):
    from tests.library_choice_fixture import authorization
    from app.assistant.library_choice import LibraryChoiceStore, DATABASE_NAME
    from app.assistant.choice_intent import selection_forms
    from app.mention_agent.contracts import Binding
    from app.mention_agent.choice_evidence import make_choice_envelope
    observed(surface)
    state = session()
    state['ask_text'] = '你目前有2个知识库，可保存2个。分别是：1. 111；2. 1。请回复完整库名。'
    binding = Binding.from_dict(state['root_binding'])
    selected = Binding('selected-numeric-name', binding.instance_id, A, B, 1, True)
    second = Binding('selected-other-number', binding.instance_id, A, B, 2, True)
    options = [{'knowledge_base_id': item.knowledge_base_id, 'name': name,
                'authorization': authorization(item, grant='grant-' + str(item.knowledge_base_id), action='import')}
               for item, name in ((selected, '111'), (second, '1'))]
    store = LibraryChoiceStore.prepare(tmp_path / DATABASE_NAME)
    row = store.create(root_task_id='root-plain-capture', root_binding=state['root_binding'],
        root_envelope=state['root_envelope'], ask_ref=state['ask_ref'], ask_text=state['ask_text'],
        options=options, authorization=authorization(binding))
    surface.page.locator('#ask .FduGc_lz').first.evaluate('(e,text)=>e.textContent=text', state['ask_text'])
    surface.page.locator('#selection .FduGc_lz').evaluate("e=>e.textContent='存到111'")
    args = _session_args(row, selection_forms(options))
    result = surface.page.evaluate(CHOICE_THREAD_JS, args)
    assert len(result['details']) == 1
    detail = result['details'][0]
    from app.mention_receiver.choice_browser import _record_ask_proof
    adapter = SimpleNamespace()
    _record_ask_proof(adapter, args)
    waiting = store.record_ask_outcome(row['choice_id'], ask_ref=row['ask_ref'], outcome='b_view_visible',
        ask_reply_ref=args['ask_reply_ref'], proof=adapter.last_choice_ask_proof)
    envelope = make_choice_envelope({'source_profile_url': A, 'exact_text': detail['command_text']}, detail, selected, 'isolated')
    arguments = dict(expected_revision=waiting['revision'], selection_ref=envelope['reference_id'],
        selection_envelope=envelope, selected_kb_id=1, selected_authorization=options[0]['authorization'])
    saved = store.reserve_selection(row['choice_id'], **arguments)
    repeated = store.reserve_selection(row['choice_id'], **arguments)
    assert saved['state'] == 'selected' and saved['selected_kb_id'] == 1
    assert saved['selection_envelope']['comment_text'] == '存到111'
    assert saved['selection_envelope']['evidence']['direct_reply_to_ask'] is False
    assert saved['selection_envelope']['evidence']['real_mention'] is False
    assert repeated['child_key'] == saved['child_key'] and repeated['revision'] == saved['revision']
    assert surface.page.evaluate('window.sendCount') == 0
