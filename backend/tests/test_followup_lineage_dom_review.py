"""Fresh nonpersistent intercepted DOM boundaries for lineage-based locators."""
import pytest
from tests.test_conversation_followup_browser import setup, QUESTION
from tests.test_library_choice_weak_receipt_dom import browser, surface
from app.mention_receiver.browser import ReceiverDetailError


def add_later_B_answer(surface):
    surface.page.evaluate("""() => {
      const row=document.getElementById('receipt').cloneNode(true);
      row.id='later-B-answer';row.querySelector('.FduGc_lz').textContent='另一条独立的已发表回答。';
      document.getElementById('root').append(row);
    }""")


def test_additional_different_B_answer_does_not_forge_immediate_parent(surface,monkeypatch):
    receiver,origin,candidate=setup(surface,monkeypatch)
    add_later_B_answer(surface)
    detail=receiver.read_followup_reply(origin,candidate)
    proof=detail['evidence']
    assert proof['parent_scope']=='unique_followup_in_verified_root'
    assert proof['direct_reply_to_prior'] is False
    assert proof['prior_reply_sha256']==origin['prior_reply_sha256']
    assert proof['root_native_comment_id'] is None
    assert detail['native_comment_id'] is None
    assert surface.page.evaluate('window.sendCount')==0


def test_same_full_body_in_another_unidentified_Root_is_still_ambiguous(surface,monkeypatch):
    receiver,origin,candidate=setup(surface,monkeypatch)
    surface.page.evaluate("""() => {
      const copy=document.getElementById('root').cloneNode(true);
      copy.id='independent-root';document.getElementById('root').after(copy);
    }""")
    with pytest.raises(ReceiverDetailError):receiver.read_followup_reply(origin,candidate)
    assert surface.page.evaluate('window.sendCount')==0


def test_actual_nested_reply_under_other_B_is_never_claimed_as_direct_to_origin(surface,monkeypatch):
    receiver,origin,candidate=setup(surface,monkeypatch)
    add_later_B_answer(surface)
    surface.page.evaluate("() => document.getElementById('later-B-answer').append(document.getElementById('selection'))")
    detail=receiver.read_followup_reply(origin,candidate)
    # Existing weak Root-level proof is broader than an immediate DOM parent.
    # Do not upgrade it to a direct reply to the chosen participation receipt.
    assert detail['evidence']['parent_scope']=='unique_followup_in_verified_root'
    assert detail['evidence']['direct_reply_to_prior'] is False
    assert detail['evidence']['prior_reply_sha256']==origin['prior_reply_sha256']
    assert surface.page.locator('#selection').evaluate('e=>e.parentElement.id')=='later-B-answer'
    assert surface.page.evaluate('window.sendCount')==0


def test_two_same_A_followup_bodies_remain_ambiguous_with_multiple_B_replies(surface,monkeypatch):
    receiver,origin,candidate=setup(surface,monkeypatch)
    add_later_B_answer(surface)
    surface.page.evaluate("() => document.getElementById('selection').after(document.getElementById('selection').cloneNode(true))")
    with pytest.raises(ReceiverDetailError):receiver.read_followup_reply(origin,candidate)
    assert surface.page.evaluate('window.sendCount')==0
