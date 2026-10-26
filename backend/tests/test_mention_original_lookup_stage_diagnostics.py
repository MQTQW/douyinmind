"""Fixed lookup stages disambiguate failures without changing any page action."""
from copy import deepcopy

import pytest

from app.mention_receiver.browser import ReceiverDetailError
from tests.test_mention_original_target_lookup import TARGET, detail, notice, receiver
from tests.test_mention_original_history_lookup import OTHER, setup


@pytest.mark.parametrize(('boundary', 'expected_stage'), [
    ('goto', 1), ('identity', 2), ('notifications', 3),
])
def test_generic_failures_keep_safe_boundary_even_when_all_candidate_counts_are_zero(
        monkeypatch, boundary, expected_stage):
    value, page, calls = receiver(monkeypatch, [notice('one')], {'one': detail()})
    def fail(*_args, **_kwargs):
        raise TimeoutError('private-url-token-or-page-text-must-not-be-projected')
    monkeypatch.setattr(page if boundary == 'goto' else value, boundary, fail)
    with pytest.raises(ReceiverDetailError) as error:
        value.open_original_from_notifications(TARGET)
    assert error.value.diagnostics['lookup_stage_index'] == expected_stage
    assert error.value.diagnostics['reason_code'] == 'original_lookup_unavailable'
    assert error.value.diagnostics['candidate_count'] == 0
    assert error.value.diagnostics['page_open_count'] == (0 if boundary == 'goto' else 1)
    assert calls == [] and page.keys == []
    assert 'private-' not in str(error.value.diagnostics)


@pytest.mark.parametrize(('boundary', 'expected_stage'), [
    ('handle', 4), ('scroll', 5), ('hydration', 6), ('position', 7),
])
def test_history_failures_are_separated_without_more_clicks_or_scans(monkeypatch, boundary, expected_stage):
    value, page, calls = setup(monkeypatch, [[OTHER()], [notice('old')]])
    original = page.evaluate_handle
    def fail(*_args, **_kwargs):
        raise RuntimeError('do-not-export-synthetic-secret')
    def handle(script):
        if boundary == 'handle':
            return fail()
        result = original(script)
        evaluate = result.evaluate
        def guarded_evaluate(js, args):
            if boundary == 'scroll' and isinstance(args, dict):
                return fail()
            if boundary == 'position' and not isinstance(args, dict):
                return fail()
            return evaluate(js, args)
        result.evaluate = guarded_evaluate
        return result
    monkeypatch.setattr(page, 'evaluate_handle', handle)
    if boundary == 'hydration':
        monkeypatch.setattr(page, 'wait_for_timeout', fail)
    with pytest.raises(ReceiverDetailError) as error:
        value.open_original_from_notifications(TARGET)
    assert error.value.diagnostics['lookup_stage_index'] == expected_stage
    assert error.value.diagnostics['reason_code'] == 'original_lookup_unavailable'
    assert calls == [] and page.keys == []
    assert 'synthetic-secret' not in str(error.value.diagnostics)
    assert all(item.disposed for item in page.handles)


@pytest.mark.parametrize(('fail_final', 'expected_stage'), [(False, 8), (True, 9)])
def test_candidate_and_final_detail_failures_keep_existing_reject_reason(monkeypatch, fail_final, expected_stage):
    def details(_row, call):
        if not fail_final or call == 2:
            raise RuntimeError('private-detail')
        return detail()
    value, page, calls = receiver(monkeypatch, [notice('one')], details)
    with pytest.raises(ReceiverDetailError) as error:
        value.open_original_from_notifications(TARGET)
    assert error.value.diagnostics['lookup_stage_index'] == expected_stage
    assert error.value.diagnostics['reason_code'] == 'original_candidate_unreadable'
    assert len(calls) == (2 if fail_final else 1)
    assert page.keys == []


def test_success_exports_only_fixed_integer_stage_and_preserves_frozen_result(monkeypatch):
    expected = detail()
    value, page, calls = receiver(monkeypatch, [notice('one')], {'one': expected})
    actual = value.open_original_from_notifications(TARGET)
    assert actual == deepcopy(expected)
    assert value.last_original_lookup['lookup_stage_index'] == 10
    assert value.last_original_lookup['final_verified'] == 1
    assert len(calls) == len(page.navigations) == 2 and page.keys == []


def test_invalid_target_is_stage_zero_and_still_does_not_navigate(monkeypatch):
    value, page, calls = receiver(monkeypatch, [notice('one')], {'one': detail()})
    with pytest.raises(ReceiverDetailError) as error:
        value.open_original_from_notifications({**TARGET, 'video_id': None})
    assert error.value.diagnostics['lookup_stage_index'] == 0
    assert error.value.diagnostics['reason_code'] == 'invalid_original_target'
    assert page.navigations == calls == []
