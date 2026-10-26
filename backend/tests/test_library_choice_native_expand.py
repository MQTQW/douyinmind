"""Real captured root/native button structure, replayed with no network/account."""
import time
from pathlib import Path

import pytest

from tests.test_library_choice_plain_header_dom import browser, surface, observed, BODY
from tests.test_library_choice_browser_dom import inspect, receiver, session, OPTIONS
from app.assistant.choice_intent import selection_forms
from app.mention_receiver import choice_browser as module
from app.mention_receiver.browser import ReceiverDetailError, ReceiverPageError, ReceiverInterventionRequired


def collapsed(surface, *, delay_ms=1100, never=False, two=False):
    observed(surface)
    surface.page.evaluate('''({delay,never,two,capture})=>{
      const before=document.getElementById('root');
      const ask=document.getElementById('ask').cloneNode(true),selection=document.getElementById('selection').cloneNode(true);
      before.outerHTML=capture;
      const root=document.querySelector('[data-e2e="comment-item"]');root.id='root';window.nativeExpandCount=0;
      const button=root.querySelector('button');
      button.onclick=()=>{
        window.nativeExpandCount++;
        if(never)return;
        const skeleton=document.createElement('div');skeleton.id='choice-skeleton';button.before(skeleton);
        setTimeout(()=>{
          skeleton.remove();
          if(two&&window.nativeExpandCount===1){button.before(ask);button.innerHTML='<span>展开1条回复</span>';}
          else{if(!document.getElementById('ask'))button.before(ask);button.before(selection);button.innerHTML='<span>收起</span>';}
        },delay);
      };
    }''', {'delay': delay_ms, 'never': never, 'two': two,
           'capture': (Path(__file__).with_name('fixtures') / 'choice_collapsed_native_sanitized.html').read_text(encoding='utf-8')})


def test_real_native_root_button_is_found_in_same_shared_selector_set(surface):
    collapsed(surface)
    result = inspect(surface)
    assert result['ask_missing'] and result['expand_index'] is not None
    selector = getattr(module, 'CHOICE_EXPAND_SELECTOR', '[data-e2e="video-comment-more"]')
    node = surface.page.locator(selector).nth(result['expand_index'])
    assert node.evaluate('e=>e.tagName') == 'BUTTON'
    assert node.inner_text() == '展开2条回复'


def test_native_expand_waits_for_real_async_ask_and_selection_without_reclick(surface, monkeypatch):
    collapsed(surface, delay_ms=1100)
    adapter = receiver(surface, monkeypatch)
    adapter.choice_should_stop = lambda: False
    found = module.read_choice_replies(adapter, session(), selection_forms(OPTIONS), keep_open=True)
    assert len(found) == 1 and found[0]['command_text'] == BODY
    assert surface.page.evaluate('window.nativeExpandCount') == 1
    assert found[0]['evidence']['direct_reply_to_ask'] is False
    assert found[0]['evidence']['real_mention'] is False


def test_native_load_timeout_never_reclicks_unchanged_root_button(surface, monkeypatch):
    collapsed(surface, never=True)
    adapter = receiver(surface, monkeypatch)
    with pytest.raises(ReceiverDetailError):
        module.read_choice_replies(adapter, session(), selection_forms(OPTIONS), keep_open=True)
    assert surface.page.evaluate('window.nativeExpandCount') == 1


def test_inserting_a_row_menu_does_not_make_same_native_button_new(surface, monkeypatch):
    collapsed(surface, never=True)
    surface.page.locator('#root button').evaluate('''button=>{
      const old=button.onclick;button.onclick=()=>{
        old();const menu=document.createElement('div');menu.setAttribute('data-e2e','video-comment-more');
        menu.textContent='...';button.before(menu);
      };
    }''')
    adapter = receiver(surface, monkeypatch)
    with pytest.raises(ReceiverDetailError):
        module.read_choice_replies(adapter, session(), selection_forms(OPTIONS), keep_open=True)
    assert surface.page.evaluate('window.nativeExpandCount') == 1


def test_two_distinct_native_expand_labels_are_bounded_at_two_clicks(surface, monkeypatch):
    collapsed(surface, delay_ms=300, two=True)
    adapter = receiver(surface, monkeypatch)
    found = module.read_choice_replies(adapter, session(), selection_forms(OPTIONS), keep_open=True)
    assert len(found) == 1 and surface.page.evaluate('window.nativeExpandCount') == 2


@pytest.mark.parametrize('kind', ['other_root', 'child_row', 'body_span', 'ellipsis', 'collapse', 'duplicate'])
def test_only_unique_native_expand_button_owned_by_original_root_is_clickable(surface, kind):
    collapsed(surface)
    surface.page.evaluate('''kind=>{
      const root=document.getElementById('root'),button=root.querySelector('button');
      if(kind==='other_root'){const row=document.createElement('article');row.setAttribute('data-e2e','comment-item');document.body.append(row);row.append(button);}
      if(kind==='child_row'){const row=document.createElement('article');row.setAttribute('data-e2e','comment-item');root.append(row);row.append(button);}
      if(kind==='body_span'){const span=document.createElement('span');span.textContent='展开2条回复';button.replaceWith(span);}
      if(kind==='ellipsis')button.textContent='...';
      if(kind==='collapse')button.textContent='收起';
      if(kind==='duplicate')root.append(button.cloneNode(true));
    }''', kind)
    assert inspect(surface)['expand_index'] is None


def test_stop_after_native_click_interrupts_wait_and_sends_no_cleanup_key(surface, monkeypatch):
    collapsed(surface, delay_ms=1100)
    adapter = receiver(surface, monkeypatch)
    events = []
    monkeypatch.setattr(surface.page.keyboard, 'press', lambda value: events.append(value))
    adapter.choice_should_stop = lambda: surface.page.evaluate('window.nativeExpandCount') > 0
    with pytest.raises(ReceiverPageError, match='停止'):
        module.read_choice_replies(adapter, session(), selection_forms(OPTIONS), keep_open=False)
    assert surface.page.evaluate('window.nativeExpandCount') == 1 and events == []


def test_preexisting_stop_has_no_navigation_or_keypress(surface, monkeypatch):
    collapsed(surface)
    adapter = receiver(surface, monkeypatch)
    events = []
    monkeypatch.setattr(surface.page, 'goto', lambda *_, **__: events.append('goto'))
    monkeypatch.setattr(surface.page.keyboard, 'press', lambda value: events.append(value))
    adapter.choice_should_stop = lambda: True
    with pytest.raises(ReceiverPageError, match='停止'):
        module.read_choice_replies(adapter, session(), selection_forms(OPTIONS))
    assert events == []


def test_intervention_after_click_is_preserved_without_second_click_or_escape(surface, monkeypatch):
    collapsed(surface, delay_ms=1100)
    adapter = receiver(surface, monkeypatch)
    events = []
    monkeypatch.setattr(surface.page.keyboard, 'press', lambda value: events.append(value))
    def check():
        if surface.page.evaluate('window.nativeExpandCount'):
            raise ReceiverInterventionRequired('synthetic verification')
    adapter.check_intervention = check
    with pytest.raises(ReceiverInterventionRequired):
        module.read_choice_replies(adapter, session(), selection_forms(OPTIONS))
    assert surface.page.evaluate('window.nativeExpandCount') == 1 and events == []


def test_new_foreign_global_button_cannot_redirect_verified_element_click(surface, monkeypatch):
    collapsed(surface, delay_ms=300)
    adapter = receiver(surface, monkeypatch)
    evaluate_handle = surface.page.evaluate_handle
    def insert_before_snapshot(*args, **kwargs):
        surface.page.evaluate('''()=>{
          if(document.getElementById('foreign-button'))return;
          const row=document.createElement('article');row.setAttribute('data-e2e','comment-item');
          row.innerHTML='<button id="foreign-button">关注</button>';document.body.prepend(row);
          window.foreignClickCount=0;row.querySelector('button').onclick=()=>window.foreignClickCount++;
        }''')
        return evaluate_handle(*args, **kwargs)
    monkeypatch.setattr(surface.page, 'evaluate_handle', insert_before_snapshot)
    found = module.read_choice_replies(adapter, session(), selection_forms(OPTIONS), keep_open=True)
    assert len(found) == 1
    assert surface.page.evaluate('window.nativeExpandCount') == 1
    assert surface.page.evaluate('window.foreignClickCount') == 0


def test_detached_verified_handle_fails_without_clicking_replacement(surface, monkeypatch):
    from playwright.sync_api import ElementHandle, Error
    collapsed(surface)
    adapter = receiver(surface, monkeypatch)
    click = ElementHandle.click
    def detach(element, **kwargs):
        element.evaluate('''e=>{
          const replacement=e.cloneNode(true);replacement.onclick=()=>window.nativeExpandCount++;
          e.replaceWith(replacement);
        }''')
        return click(element, timeout=150)
    monkeypatch.setattr(ElementHandle, 'click', detach)
    with pytest.raises(Error):
        module.read_choice_replies(adapter, session(), selection_forms(OPTIONS), keep_open=True)
    assert surface.page.evaluate('window.nativeExpandCount') == 0


def test_stop_after_handle_acquisition_prevents_click_and_cleanup(surface, monkeypatch):
    collapsed(surface)
    adapter = receiver(surface, monkeypatch)
    stopped = [False]
    adapter.choice_should_stop = lambda: stopped[0]
    evaluate_handle = surface.page.evaluate_handle
    def stop_after_snapshot(*args, **kwargs):
        handle = evaluate_handle(*args, **kwargs)
        stopped[0] = True
        return handle
    monkeypatch.setattr(surface.page, 'evaluate_handle', stop_after_snapshot)
    keys = []
    monkeypatch.setattr(surface.page.keyboard, 'press', lambda value: keys.append(value))
    with pytest.raises(ReceiverPageError, match='停止'):
        module.read_choice_replies(adapter, session(), selection_forms(OPTIONS))
    assert surface.page.evaluate('window.nativeExpandCount') == 0 and keys == []
