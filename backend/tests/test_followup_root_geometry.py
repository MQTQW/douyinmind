"""Offline geometry counterexamples for the strict ordinary-followup root read."""
from copy import deepcopy
import json
import time

import pytest

from app.mention_receiver.browser import DETAIL_JS, ReceiverDetailError
from app.mention_receiver import followup_browser as followup
from tests.test_conversation_followup_browser import setup, browser, surface


def no_editor_or_send(surface):
    assert surface.page.evaluate('window.sendCount') == 0
    assert surface.page.evaluate('window.openCount') == 0
    assert surface.page.evaluate('window.inputCount') == 0
    assert surface.page.locator('#editor').inner_text() == ''


def capture_raw_read(surface, monkeypatch):
    evaluate = surface.page.evaluate
    reads = []
    def observed(expression, *args, **kwargs):
        value = evaluate(expression, *args, **kwargs)
        if expression == followup.FOLLOWUP_THREAD_JS:
            reads.append(deepcopy(value))
        return value
    monkeypatch.setattr(surface.page, 'evaluate', observed)
    return reads


def strict_detail(surface, target):
    return surface.page.evaluate(DETAIL_JS, {
        'sender': target['sender_profile_url'], 'receiver': target['receiver_profile_url'],
        'wanted': target['comment_text'],
    })


def root_geometry(surface):
    return surface.page.evaluate(r'''() => {
      const selector='[data-e2e="comment-item"]', root=document.getElementById('root');
      const own=css=>[...root.querySelectorAll(css)].find(e=>e.closest(selector)===root);
      const rect=e=>{const r=e.getBoundingClientRect();return {top:r.top,bottom:r.bottom,width:r.width,height:r.height};};
      return {height:innerHeight,root:rect(root),header:rect(own('.comment-item-info-wrap')),
        body:rect(own('.FduGc_lz')),followup:rect(document.getElementById('selection'))};
    }''')


def test_strict_opener_unique_then_root_content_offscreen_fails_without_reusing_old_detail(surface, monkeypatch):
    receiver, anchor, candidate = setup(surface, monkeypatch)
    surface.page.set_viewport_size({'width': 1200, 'height': 900})
    surface.page.evaluate("window.inputCount=0;document.addEventListener('input',()=>window.inputCount++)")
    reads = capture_raw_read(surface, monkeypatch)
    observed = {}
    def open_then_scroll(target, *, max_candidates=3):
        detail = strict_detail(surface, target)
        assert detail['ok'] is True and detail['unique_count'] == 1
        assert 'comment_index' not in detail  # The real shared opener does not supply one.
        observed['opener'] = deepcopy(detail)
        surface.page.evaluate(r'''() => {
          const video=document.querySelector('[data-e2e="detail-video-info"]');
          video.style.position='fixed';video.style.top='0';video.style.left='0';video.style.zIndex='5';
          const root=document.getElementById('root');
          const replies=[...root.querySelectorAll('.replyContainer')].find(e=>e.closest('[data-e2e="comment-item"]')===root);
          const spacer=document.createElement('div');spacer.id='geometry-spacer';spacer.style.height='2200px';
          replies.before(spacer);
          document.getElementById('selection').scrollIntoView({block:'center'});
        }''')
        observed['geometry'] = root_geometry(surface)
        receiver._reply_identity_checked_at = time.monotonic()
        receiver.last_original_lookup = {'candidate_count':1,'checked_count':1,'match_count':1,'final_verified':1}
        return detail
    monkeypatch.setattr(receiver, 'open_original_from_notifications', open_then_scroll)
    with pytest.raises(ReceiverDetailError) as failure:
        receiver.read_followup_reply(anchor, candidate, keep_open=True)
    assert failure.value.diagnostics['reason_code'] == 'followup_original_root_not_unique'
    geometry = observed['geometry']
    assert geometry['header']['bottom'] < 0 and geometry['body']['bottom'] < 0
    assert 0 < geometry['followup']['bottom'] and geometry['followup']['top'] < geometry['height']
    assert geometry['root']['height'] > 0 and geometry['root']['bottom'] > 0
    assert len(reads) == 1
    counts = reads[0]['counts']
    assert counts['original_root_count'] == counts['detail_ok'] == 0
    assert counts['command_match_count'] == 0 and counts['video_id_count'] == 1
    assert counts['visible_comment_count'] > 0
    diagnostic = receiver.last_followup_diagnostic
    assert diagnostic['counts']['root_count'] == diagnostic['counts']['detail_ok'] == 0
    assert diagnostic['lookup_counts']['final_verified'] == 1
    print(json.dumps({'case':'root_own_content_offscreen','counts':counts,'geometry':geometry},sort_keys=True))
    no_editor_or_send(surface)


def test_probe_oversized_root_scroll_target_geometry(surface, monkeypatch):
    receiver, anchor, candidate = setup(surface, monkeypatch)
    surface.page.set_viewport_size({'width': 1200, 'height': 900})
    surface.page.evaluate("window.inputCount=0;document.addEventListener('input',()=>window.inputCount++)")
    surface.page.evaluate(r'''() => {
      const video=document.querySelector('[data-e2e="detail-video-info"]');
      video.style.position='fixed';video.style.top='0';video.style.left='0';video.style.zIndex='5';
      const root=document.getElementById('root');
      const replies=[...root.querySelectorAll('.replyContainer')].find(e=>e.closest('[data-e2e="comment-item"]')===root);
      const spacer=document.createElement('div');spacer.style.height='2200px';replies.before(spacer);
      document.getElementById('selection').scrollIntoView({block:'center'});
    }''')
    index = surface.page.evaluate("Array.from(document.querySelectorAll('[data-e2e=\"comment-item\"]')).indexOf(document.getElementById('root'))")
    before = root_geometry(surface)
    root = surface.page.locator('[data-e2e="comment-item"]').nth(index)
    root.scroll_into_view_if_needed(timeout=2000)
    after_root = root_geometry(surface)
    args = followup._args(anchor, candidate)
    args.update(identity_fresh=True, expected_public_number=receiver.expected_number)
    raw_after_root = surface.page.evaluate(followup.FOLLOWUP_THREAD_JS, args)

    surface.page.locator('#selection').evaluate("e=>e.scrollIntoView({block:'center'})")
    body_handle = root.evaluate_handle(r'''root => [...root.querySelectorAll('.FduGc_lz')]
        .find(e=>e.closest('[data-e2e="comment-item"]')===root)''')
    try:
        body = body_handle.as_element()
        assert body is not None
        body.scroll_into_view_if_needed(timeout=2000)
    finally:
        body_handle.dispose()
    after_body = root_geometry(surface)
    raw_after_body = surface.page.evaluate(followup.FOLLOWUP_THREAD_JS, args)
    assert before['header']['bottom'] < 0 and before['body']['bottom'] < 0
    assert after_root['header']['bottom'] < 0 and after_root['body']['bottom'] < 0
    assert after_body['header']['top'] >= 0 and after_body['body']['bottom'] < after_body['height']
    assert raw_after_root['counts']['original_root_count'] == raw_after_root['counts']['detail_ok'] == 0
    assert raw_after_body['counts']['original_root_count'] == raw_after_body['counts']['detail_ok'] == 1
    print(json.dumps({'case':'oversized_root_scroll_probe','index':index,'before':before,
        'after_root_scroll':after_root,'after_body_scroll':after_body,
        'root_scroll_counts':raw_after_root['counts'],'body_scroll_counts':raw_after_body['counts']},sort_keys=True))
    no_editor_or_send(surface)



def test_detail_unique_but_semantic_root_identity_rejected_is_not_a_geometry_failure(surface, monkeypatch):
    receiver, anchor, candidate = setup(surface, monkeypatch)
    surface.page.evaluate("window.inputCount=0;document.addEventListener('input',()=>window.inputCount++)")
    reads = capture_raw_read(surface, monkeypatch)
    observed = {}
    def open_with_different_author_header(target, *, max_candidates=3):
        initial = strict_detail(surface, target)
        assert initial['ok'] is True and initial['unique_count'] == 1
        # Keep the same visible author link/text, but remove the semantic title
        # marker required by rowIdentity. DETAIL_JS deliberately has a broader shape.
        surface.page.locator('#root .comment-item-info-wrap').first.evaluate(
            "e=>e.querySelector('[data-click-from=\"title\"]').removeAttribute('data-click-from')")
        detail = strict_detail(surface, target)
        assert detail['ok'] is True and detail['unique_count'] == 1
        assert 'comment_index' not in detail
        observed['geometry'] = root_geometry(surface)
        receiver._reply_identity_checked_at = time.monotonic()
        receiver.last_original_lookup = {'candidate_count':1,'checked_count':1,'match_count':1,'final_verified':1}
        return detail
    monkeypatch.setattr(receiver, 'open_original_from_notifications', open_with_different_author_header)
    with pytest.raises(ReceiverDetailError) as failure:
        receiver.read_followup_reply(anchor, candidate, keep_open=True)
    assert failure.value.diagnostics['reason_code'] == 'followup_original_root_not_unique'
    geometry = observed['geometry']
    assert geometry['header']['top'] >= 0 and geometry['header']['bottom'] < geometry['height']
    assert geometry['body']['top'] >= 0 and geometry['body']['bottom'] < geometry['height']
    assert len(reads) == 1
    counts = reads[0]['counts']
    assert counts['detail_ok'] == counts['sender_match_count'] == counts['command_match_count'] == counts['video_id_count'] == 1
    assert counts['original_root_count'] == 0
    diagnostic = receiver.last_followup_diagnostic
    assert diagnostic['counts']['detail_ok'] == 1 and diagnostic['counts']['root_count'] == 0
    print(json.dumps({'case':'semantic_author_header_mismatch','counts':counts,'geometry':geometry},sort_keys=True))
    no_editor_or_send(surface)
