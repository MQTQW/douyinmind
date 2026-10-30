"""The draft operation is inert IPC; it never starts paid execution or sends."""
import pytest

from tests.test_native_recovery_owner import case
from tests.test_assistant_native_recovery_mailbox import make, result_file
from app.assistant.native_recovery_mailbox import NativeRecoveryMailboxError


TASK = 'c' * 32
HASH = 'd' * 64
SOURCE = 'assistant-draft:' + TASK + ':' + HASH
OP = 'finalize_query_draft'


def test_owner_finalizes_first_draft_only_and_leaves_paused(case):
    calls = []
    case.state['manual_verification'] = {'active': False}
    case.lane._recovery_identity = lambda: calls.append('identity')
    def finalize(task_id, *, expected_candidate_sha256, reverify_identity):
        assert case.flags[0].is_set() and case.flags[1].is_set()
        assert task_id == TASK
        assert expected_candidate_sha256 == HASH
        reverify_identity()
        calls.append('draft_only')
        return {'source_id': SOURCE, 'status': 'operator_query_draft_finalized', 'dispatchable': False}
    case.owner.draft_finalizer = finalize
    result = case.owner(OP, SOURCE)
    assert result == {'ok': True, 'reviewed': True, 'queued_for_fresh_ui': False,
                      'historical_preserved': True, 'dispatchable': False}
    assert calls == ['identity', 'identity', 'draft_only']
    assert case.state['desired_state'] == 'paused'
    assert not case.flags[0].is_set() and not case.flags[1].is_set()


@pytest.mark.parametrize('mutation', [None, {'active': True}, {'active': 0}])
def test_unknown_or_active_manual_gate_cannot_finalize(case, mutation):
    case.state['manual_verification'] = mutation
    case.owner.draft_finalizer = lambda *a, **k: pytest.fail('unsafe finalization')
    assert not case.owner(OP, SOURCE)['ok']


def test_active_work_or_wrong_source_does_not_call_finalizer(case):
    case.state['manual_verification'] = {'active': False}
    case.owner.draft_finalizer = lambda *a, **k: pytest.fail('unsafe finalization')
    case.counts['queued'] = 1
    assert not case.owner(OP, SOURCE)['ok']
    case.counts.clear()
    assert not case.owner(OP, 'native:' + 'a' * 64)['ok']


def test_mailbox_claim_is_permanent_and_task_namespace_only(tmp_path):
    box, _, _ = make(tmp_path)
    request = box.submit(SOURCE, OP)
    seen = []
    def handler(operation, source):
        seen.append((operation, source))
        return {'ok': True, 'reviewed': True, 'queued_for_fresh_ui': False, 'dispatchable': False}
    result = box.process_one(handler)
    assert result['status'] == 'done' and result['no_model'] and result['no_import'] and result['no_send']
    assert result['queued_for_fresh_ui'] is False and result_file(box, request).exists()
    assert seen == [(OP, SOURCE)]
    assert box.process_one(lambda *_: pytest.fail('replayed draft intent')) is None
    with pytest.raises(NativeRecoveryMailboxError):
        box.submit('native:' + 'a' * 64, OP)


def test_cli_uses_only_task_owner_mailbox_and_cannot_run_registered(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from app.assistant import __main__ as cli
    from app.assistant import native_recovery_mailbox as mailbox
    calls = []
    control = SimpleNamespace(snapshot=lambda: {'run_id': 'a' * 32, 'state': 'paused'},
        start=lambda: pytest.fail('draft cannot start B'))
    class Box:
        def __init__(self, path, run_id, actual):
            assert path == tmp_path and run_id == 'a' * 32 and actual is control
        def submit(self, source, operation):
            calls.append((source, operation))
            return {'status': 'queued', 'no_model': True, 'no_send': True, 'dispatchable': False}
    monkeypatch.setattr(cli, 'STATE', tmp_path)
    monkeypatch.setattr(cli, 'AssistantQueue', lambda *a, **k: object())
    monkeypatch.setattr(cli, 'AssistantControl', lambda *a: control)
    monkeypatch.setattr(cli, 'start_guard', lambda: None)
    monkeypatch.setattr(mailbox, 'NativeRecoveryMailbox', Box)
    assert cli.main(['--finalize-query-draft', TASK, '--candidate-sha256', HASH]) == 0
    assert calls == [(SOURCE, OP)] and '"no_send": true' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(['--run-registered', '--finalize-query-draft', TASK, '--candidate-sha256', HASH])
    assert len(calls) == 1
    with pytest.raises(SystemExit):
        cli.main(['--finalize-query-draft', TASK])
    with pytest.raises(SystemExit):
        cli.main(['--candidate-sha256', HASH])


@pytest.mark.parametrize('arguments', [
    ['--candidate-sha256', ''],
    ['--finalize-query-draft', ''],
    ['--finalize-query-draft', '', '--candidate-sha256', ''],
    ['--finalize-query-draft', TASK, '--candidate-sha256', ''],
    ['--finalize-query-draft', '', '--candidate-sha256', HASH],
    ['--finalize-query-draft', ' ', '--candidate-sha256', HASH],
    ['--finalize-query-draft', TASK, '--candidate-sha256', ' '],
])
@pytest.mark.parametrize('registered', [False, True])
def test_cli_empty_draft_arguments_are_rejected_before_any_queue_or_control(
        tmp_path, monkeypatch, capsys, arguments, registered):
    from app.assistant import __main__ as cli
    from app.assistant import native_recovery_mailbox as mailbox
    effects = []
    def forbidden(name):
        def invoke(*args, **kwargs):
            effects.append(name)
            pytest.fail('invalid CLI arguments reached ' + name)
        return invoke
    monkeypatch.setattr(cli, 'STATE', tmp_path)
    for name in ('AssistantQueue', 'AssistantControl', 'start_guard', 'run_registered'):
        monkeypatch.setattr(cli, name, forbidden(name))
    monkeypatch.setattr(mailbox, 'NativeRecoveryMailbox', forbidden('mailbox'))
    argv = (['--run-registered'] if registered else []) + arguments
    with pytest.raises(SystemExit) as raised:
        cli.main(argv)
    assert raised.value.code == 2
    assert effects == [] and list(tmp_path.iterdir()) == []
    captured = capsys.readouterr()
    assert captured.out == '' and 'error:' in captured.err
