"""Public command discovery works without private setup or state initialization."""
import pytest

from app.assistant import __main__ as entry


def forbid_initialization(monkeypatch):
    def forbidden(*_, **__):
        pytest.fail('command discovery must not initialize state, config or browser')
    for name in ('AssistantQueue', 'AssistantControl', 'start_guard', 'build_intake_lane'):
        monkeypatch.setattr(entry, name, forbidden)


def test_help_is_available_without_private_configuration_or_databases(monkeypatch, capsys):
    forbid_initialization(monkeypatch)
    with pytest.raises(SystemExit) as result:
        entry.main(['--help'])
    assert result.value.code == 0
    text = capsys.readouterr().out
    assert '--inspect-thread-followup' in text
    assert '--inspect-assistant-reply' in text


def test_arbitrary_state_directory_is_rejected_before_any_initialization(tmp_path, monkeypatch):
    forbid_initialization(monkeypatch)
    target = tmp_path / 'unowned-state'
    with pytest.raises(SystemExit) as result:
        entry.main(['--state-dir', str(target)])
    assert result.value.code == 2
    assert not target.exists()
