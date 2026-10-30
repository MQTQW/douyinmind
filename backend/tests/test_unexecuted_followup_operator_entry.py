"""Independent operator entry remains paused, local, inert and once-claimed."""
from types import SimpleNamespace

import pytest

from tests.test_native_recovery_owner import case
from tests.test_assistant_native_recovery_mailbox import make, SOURCE, result_file
from app.assistant.native_recovery_mailbox import NativeRecoveryMailboxError


OP = 'review_unexecuted_followup'


def prepare(case):
    case.state['manual_verification'] = {'active': False}
    def review(source):
        assert case.flags[0].is_set() and case.flags[1].is_set()
        case.events.append(('unexecuted_review', source))
        return {'source_id': source, 'status': 'followup_no_effects_review_queued',
                'attempts_preserved': 2, 'dispatchable': False}
    case.lane.review_no_effects_followup = review


def test_only_explicit_new_entry_dispatches_inert_independent_review(case):
    prepare(case)
    value = case.owner(OP, SOURCE)
    assert value == {'ok': True, 'reviewed': True, 'queued_for_fresh_ui': True,
                     'historical_preserved': True, 'dispatchable': False}
    assert case.events == [('unexecuted_review', SOURCE)]
    assert case.state['desired_state'] == 'paused'
    assert not case.flags[0].is_set() and not case.flags[1].is_set()


@pytest.mark.parametrize('state', ['queued', 'waiting_desktop', 'waiting_ingestion',
    'ready_to_publish', 'waiting_external', 'waiting_library', 'needs_reconcile'])
def test_any_active_queue_phase_blocks_new_audit_before_lane(case, state):
    prepare(case)
    case.counts[state] = 1
    assert case.owner(OP, SOURCE) == {'ok': False, 'code': 'native_recovery_not_safe'}
    assert not case.events


@pytest.mark.parametrize('manual', [None, {}, {'active': True}, {'active': 0}])
def test_missing_unknown_or_active_manual_scope_is_refused(case, manual):
    prepare(case)
    case.state['manual_verification'] = manual
    assert case.owner(OP, SOURCE)['ok'] is False
    assert not case.events


def test_effects_refusal_never_becomes_fresh_read_or_clears_review_flags(case):
    prepare(case)
    from app.assistant.native_lane import NativeLaneError
    def refuse(source):
        raise NativeLaneError('native_recovery_effects_present')
    case.lane.review_no_effects_followup = refuse
    assert case.owner(OP, SOURCE) == {'ok': False, 'code': 'native_recovery_effects_present'}
    assert not case.flags[0].is_set() and not case.flags[1].is_set()
    assert case.state['desired_state'] == 'paused'


def test_mailbox_fixed_operation_is_never_automatically_replayed(tmp_path):
    box, control, clock = make(tmp_path)
    request = box.submit(SOURCE, OP)
    calls = []
    def handle(operation, source):
        calls.append((operation, source))
        return {'ok': False, 'code': 'native_recovery_effects_present'}
    result = box.process_one(handle)
    assert result['status'] == 'rejected' and result['code'] == 'native_recovery_effects_present'
    assert result['no_model'] and result['no_import'] and result['no_send']
    assert calls == [(OP, SOURCE)]
    assert result_file(box, request).exists()
    assert box.process_one(lambda *_: pytest.fail('unknown/rejected claim replay')) is None
    with pytest.raises(NativeRecoveryMailboxError):
        box.submit('assistant-task:' + 'a' * 32, OP)


def test_cli_registers_only_new_owner_request_without_start_or_effects(tmp_path, monkeypatch, capsys):
    from app.assistant import __main__ as cli
    from app.assistant import native_recovery_mailbox as mailbox
    calls = []
    control = SimpleNamespace(snapshot=lambda: {'run_id': 'a' * 32, 'state': 'paused'},
        start=lambda: pytest.fail('review cannot Start B'))
    class Box:
        def __init__(self, path, run, current):
            assert path == tmp_path and run == 'a' * 32 and current is control
        def submit(self, source, operation):
            calls.append((source, operation))
            return {'status': 'queued', 'no_model': True, 'no_import': True,
                    'no_send': True, 'dispatchable': False}
    monkeypatch.setattr(cli, 'STATE', tmp_path)
    monkeypatch.setattr(cli, 'AssistantQueue', lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, 'AssistantControl', lambda *args: control)
    monkeypatch.setattr(cli, 'start_guard', lambda: None)
    monkeypatch.setattr(mailbox, 'NativeRecoveryMailbox', Box)
    assert cli.main(['--review-unexecuted-followup', SOURCE]) == 0
    assert calls == [(SOURCE, OP)]
    assert '"no_send": true' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(['--run-registered', '--review-unexecuted-followup', SOURCE])
    assert len(calls) == 1
