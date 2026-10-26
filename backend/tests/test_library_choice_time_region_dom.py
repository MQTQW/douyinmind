"""Refresh-time location metadata replay, no live account or service."""
from pathlib import Path
import json

import pytest

from tests import test_library_choice_plain_header_dom as plain
from tests.test_library_choice_plain_header_dom import browser, surface, observed, BODY
from tests.test_library_choice_browser_dom import inspect, session, OPTIONS, target
from app.assistant.choice_intent import selection_forms
from app.mention_receiver.choice_browser import CHOICE_THREAD_JS, _session_args


def refreshed(surface):
    observed(surface)
    capture = (Path(__file__).with_name('fixtures') / 'choice_refreshed_region_sanitized.html').read_text(encoding='utf-8')
    surface.page.locator('#root').evaluate('(e,value)=>e.outerHTML=value', capture)
    surface.page.evaluate('''()=>{
      const rows=[...document.querySelectorAll('[data-e2e="comment-item"]')];
      rows[0].id='root';rows[1].id='ask';rows[2].id='selection';
    }''')


def test_full_refreshed_parent_has_one_actual_choice_not_two_body_candidates(surface):
    refreshed(surface)
    args = _session_args(session(), selection_forms(OPTIONS))
    # Diagnostics-only clone: the old business filtering is untouched. This
    # proves how many safe maximal candidates the captured refresh produced.
    debug = CHOICE_THREAD_JS.replace('if(nodes.length!==1)continue;',
        'counts.debug_body_candidate_count=nodes.length;if(nodes.length!==1)continue;')
    result = surface.page.evaluate(debug, args)
    print('diagnostic_counts', json.dumps(result.get('counts'), sort_keys=True))
    assert len(result['details']) == 1
    detail = result['details'][0]
    assert detail['command_text'] == BODY
    assert detail['evidence']['real_mention'] is False and detail['evidence']['direct_reply_to_ask'] is False
    assert result['counts']['debug_body_candidate_count'] == 1
    target(detail)


@pytest.mark.parametrize('metadata', ['43分钟前·广东', '1小时前·上海', '10-22·广东', '2026-10-22·英国', '刚刚·广东'])
def test_complete_time_region_block_before_stats_is_metadata(surface, metadata):
    refreshed(surface)
    surface.page.locator('#selection .VAQA49VP').evaluate('(e,value)=>e.textContent=value', metadata)
    assert len(inspect(surface)['details']) == 1


def test_a_second_real_body_is_not_ignored_just_because_first_matches_library(surface):
    refreshed(surface)
    surface.page.locator('#selection .VAQA49VP').evaluate("e=>e.insertAdjacentHTML('beforebegin','<div>另外这一句不要忽略。</div>')")
    assert inspect(surface)['details'] == []


def test_time_like_words_inside_genuine_body_are_not_metadata(surface):
    refreshed(surface)
    surface.page.locator('#selection .FduGc_lz').evaluate('(e,body)=>e.innerHTML="<p>"+body+"</p><p>43分钟前·广东</p>"', BODY)
    assert inspect(surface)['details'] == []


def test_extra_independent_time_like_body_is_not_the_terminal_metadata_block(surface):
    refreshed(surface)
    surface.page.locator('#selection .VAQA49VP').evaluate("e=>e.insertAdjacentHTML('beforebegin','<div><span>43分钟前·广东</span></div>')")
    assert inspect(surface)['details'] == []


def test_arbitrary_long_text_starting_like_time_is_not_metadata(surface):
    refreshed(surface)
    surface.page.locator('#selection .VAQA49VP').evaluate("e=>e.textContent='43分钟前·广东，还有另一个正文要求'")
    assert inspect(surface)['details'] == []


def test_full_refreshed_dom_reaches_same_original_choice_cas(surface, tmp_path, monkeypatch):
    monkeypatch.setattr(plain, 'observed', refreshed)
    plain.test_observed_plain_choice_enters_real_temporary_choice_cas_once(surface, tmp_path)
