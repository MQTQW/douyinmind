"""Operator review flags never double as a registered runtime launch."""
import pytest
from app.assistant import __main__ as entry


@pytest.mark.parametrize('option', ['--review-thread-followup', '--inspect-thread-followup'])
def test_review_and_runtime_cannot_be_combined_before_any_database_open(monkeypatch, option):
    def forbidden(*args, **kwargs):
        pytest.fail('conflicting CLI flags must be rejected before opening a database')
    monkeypatch.setattr(entry, 'AssistantQueue', forbidden)
    with pytest.raises(SystemExit) as result:
        entry.main(['--run-registered', option, 'native:'+'a'*64])
    assert result.value.code == 2
