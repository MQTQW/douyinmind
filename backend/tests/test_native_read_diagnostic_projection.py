"""Fixed ordinary READ observations never become permissions or recovery."""
from copy import deepcopy
from types import SimpleNamespace

from app.assistant.__main__ import record_native_read_diagnostic


def case():
    state = {'run_id': 'synthetic-run', 'state': 'running', 'desired_state': 'running'}
    def update(callback):
        state.update(callback(deepcopy(state)))
    return state, SimpleNamespace(queue=SimpleNamespace(update_control=update))


def observation(**changes):
    return {'stage': 'original_ui_lookup', 'code': 'native_read_page_unready',
            'exception_type': 'ReceiverPageError', 'viewport_counts': {
                'configured': 1, 'expanded': 1, 'restored': 1, 'restore_failed': 0}, **changes}


def test_only_fresh_whitelisted_observation_is_projected_without_control_changes():
    state, control = case()
    value = observation(private_text='synthetic private text', source_id='private source')
    value['viewport_counts'].update(unknown=1, bool_value=True, bad_value=8)
    lane = SimpleNamespace(last_native_read_diagnostic=value)
    assert not record_native_read_diagnostic(control, 'synthetic-run', lane, value)
    assert record_native_read_diagnostic(control, 'synthetic-run', lane, {})
    saved = state['native_read_diagnostic']
    assert set(saved) == {'stage', 'code', 'exception_type', 'viewport_counts', 'observed_at'}
    assert saved['viewport_counts'] == observation()['viewport_counts']
    assert state['state'] == state['desired_state'] == 'running'
    assert 'private' not in str(saved)


def test_old_owner_and_unrecognized_fields_cannot_publish_positive_or_arbitrary_data():
    state, control = case()
    lane = SimpleNamespace(last_native_read_diagnostic=observation())
    record_native_read_diagnostic(control, 'different-run', lane, None)
    assert 'native_read_diagnostic' not in state
    for change in ({'code': 'matched'}, {'stage': 'arbitrary text'},
                   {'exception_type': 'private exception'}, {'code': []}):
        lane.last_native_read_diagnostic = observation(**change)
        assert not record_native_read_diagnostic(control, 'synthetic-run', lane, None)
        assert 'native_read_diagnostic' not in state


def test_diagnostic_write_failure_never_overrides_original_intake_outcome():
    def fail(callback):
        raise RuntimeError('synthetic write failure')
    control = SimpleNamespace(queue=SimpleNamespace(update_control=fail))
    lane = SimpleNamespace(last_native_read_diagnostic=observation())
    assert not record_native_read_diagnostic(control, 'synthetic-run', lane, None)


def test_lookup_stage_is_only_a_bounded_integer_observation():
    for number in (0, 2, 10, True, -1, 11, 'private stage', {}):
        state, control = case()
        lane = SimpleNamespace(last_native_read_diagnostic=observation(lookup_stage_index=number))
        assert record_native_read_diagnostic(control, 'synthetic-run', lane, None)
        saved = state['native_read_diagnostic']
        if type(number) is int and 0 <= number <= 10:
            assert saved['lookup_stage_index'] == number
        else:
            assert 'lookup_stage_index' not in saved
