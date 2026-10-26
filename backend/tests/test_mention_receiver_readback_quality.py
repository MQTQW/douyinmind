"""Success metadata projection only: fake controls, no browser or network."""
from types import SimpleNamespace

import pytest

from app.mention_receiver.browser import NormalPageReceiver


class Control:
    def __init__(self): self.clicks = 0
    def nth(self, _index): return self
    def locator(self, _selector): return self
    def and_(self, _other): return self
    def count(self): return 1
    def click(self, **_kwargs): self.clicks += 1


def publish(monkeypatch, *, quality=None, present=True):
    control = Control()
    page = SimpleNamespace(locator=lambda _selector: control)
    receiver = NormalPageReceiver(page, '123456789', 'https://www.douyin.com/user/synthetic-quality-B')
    state = {'composer_scope': 'inline', 'root_index': 0, 'matching_reply_count': 1,
             'existing_matching_reply_count': 1}
    if present: state['reply_readback_quality'] = quality
    monkeypatch.setattr(receiver, '_reply_state', lambda *_args: state)
    monkeypatch.setattr(receiver, '_validate_reply_draft', lambda *_args: None)
    monkeypatch.setattr(receiver, '_check_challenge', lambda: None)
    gates = []
    result = receiver.submit_reply({}, 'synthetic reply', before_submit=lambda: gates.append(True))
    assert gates == [True] and control.clicks == 1
    return result


@pytest.mark.parametrize('quality', ['weak_unique_root', 'direct_selected_parent'])
def test_success_keeps_only_the_observed_whitelisted_quality(monkeypatch, quality):
    assert publish(monkeypatch, quality=quality) == {
        'outcome': 'b_view_visible', 'exact_body_in_original_thread': True,
        'reply_readback_quality': quality,
    }


def test_unknown_or_private_quality_does_not_escape(monkeypatch):
    for quality in ['native', 'unknown', 'SYNTHETIC_PRIVATE_VALUE', None, {}, 5]:
        assert publish(monkeypatch, quality=quality) == {
            'outcome': 'b_view_visible', 'exact_body_in_original_thread': True,
        }


def test_normal_generic_missing_quality_keeps_original_success_payload(monkeypatch):
    assert publish(monkeypatch, present=False) == {
        'outcome': 'b_view_visible', 'exact_body_in_original_thread': True,
    }
