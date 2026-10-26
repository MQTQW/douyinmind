"""Reader observation time belongs to the actual DOM read, not navigation.

Pure fake page, synthetic clock, no browser/process/network/database/model.
The consumer is the real Controller freshness predicate.
"""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.assistant.choice_controller import ChoiceController
from app.mention_receiver import choice_browser


@pytest.fixture
def reader(monkeypatch):
    clock = [1000.0]
    class ClockDateTime:
        @staticmethod
        def now(tz): return datetime.fromtimestamp(clock[0], tz)
    monkeypatch.setattr(choice_browser, 'datetime', ClockDateTime)
    monkeypatch.setattr(choice_browser, '_choice_observation', lambda *args, **kwargs: None)
    captured = []
    class Page:
        on_read = lambda self: None
        def evaluate(self, script, args):
            assert script is choice_browser.CHOICE_THREAD_JS
            captured.append(deepcopy(args)); self.on_read()
            return {'ok': True, 'counts': {'selection_count': 1}, 'details': [
                {'evidence': {'observed_at_utc': args['observed_at_utc']}}]}
    page = Page()
    receiver = SimpleNamespace(page=page, check_intervention=lambda: None)
    controller = ChoiceController.__new__(ChoiceController)
    controller.clock = lambda: clock[0]
    args = {'sender': 'synthetic-A', 'receiver': 'synthetic-B', 'video': '7999999000000000011',
            'root_text': 'synthetic frozen root', 'root_native': None, 'ask_text': 'synthetic frozen ask',
            'allowed_selections': ['2'], 'observed_at_utc': datetime.fromtimestamp(clock[0], timezone.utc).isoformat()}
    return clock, captured, page, receiver, controller, args


def test_navigation_122_seconds_does_not_make_the_new_dom_observation_stale(reader):
    clock, captured, page, receiver, controller, args = reader
    clock[0] += 122.95  # Simulates bounded original-root navigation and expansion.
    value = choice_browser._read_thread(receiver, args)
    proof = value['details'][0]['evidence']
    assert controller._fresh_time(proof) is True
    assert datetime.fromisoformat(proof['observed_at_utc']).timestamp() == clock[0]


def test_actual_read_gets_new_copy_without_changing_any_frozen_scope_or_args(reader):
    clock, captured, page, receiver, controller, args = reader
    original = deepcopy(args); clock[0] += 122.95
    choice_browser._read_thread(receiver, args)
    assert args == original
    observed = captured[0]; observed.pop('observed_at_utc')
    original.pop('observed_at_utc')
    assert observed == original


def test_a_read_that_itself_takes_121_seconds_is_not_restamped_after_return(reader):
    clock, captured, page, receiver, controller, args = reader
    started = clock[0]
    page.on_read = lambda: clock.__setitem__(0, clock[0] + 121)
    value = choice_browser._read_thread(receiver, args)
    proof = value['details'][0]['evidence']
    assert datetime.fromisoformat(proof['observed_at_utc']).timestamp() == started
    assert controller._fresh_time(proof) is False


def test_each_expansion_reread_has_its_own_actual_observation_time(reader):
    clock, captured, page, receiver, controller, args = reader
    times = []
    for advance in (101, 22, 2):
        clock[0] += advance
        value = choice_browser._read_thread(receiver, args)
        proof = value['details'][0]['evidence']
        times.append(proof['observed_at_utc'])
        assert controller._fresh_time(proof) is True
    assert len(set(times)) == 3


@pytest.mark.parametrize('elapsed,expected', [(120, True), (120.01, False), (-5, True), (-5.01, False)])
def test_controller_keeps_original_bounded_freshness_contract(reader, elapsed, expected):
    clock, captured, page, receiver, controller, args = reader
    proof = choice_browser._read_thread(receiver, args)['details'][0]['evidence']
    clock[0] += elapsed
    assert controller._fresh_time(proof) is expected
