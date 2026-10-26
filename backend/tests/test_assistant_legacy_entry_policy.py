"""Once migrated, old CLI tools cannot silently revive legacy permissions."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.assistant.legacy_policy import legacy_testing_blocked


@pytest.mark.parametrize('name',['registry.sqlite3','registry-required.json'])
def test_any_migration_artifact_blocks_legacy_entry(tmp_path,name):
    assert not legacy_testing_blocked(tmp_path)
    (tmp_path/name).write_bytes(b'present but even damaged authority must fail closed')
    assert legacy_testing_blocked(tmp_path)


def test_old_launcher_does_not_reach_receiver_or_knowledge(monkeypatch):
    from app.mention_agent import launcher
    monkeypatch.setattr(launcher,'legacy_testing_blocked',lambda:True)
    launch=Mock(side_effect=AssertionError('old launch must not run'))
    monkeypatch.setattr(launcher,'launch',launch)
    assert launcher.main([])==2 and launch.call_count==0


def test_old_live_receiver_cannot_open_any_state_or_browser(monkeypatch):
    from app.mention_receiver import __main__ as receiver
    monkeypatch.setattr(receiver,'legacy_testing_blocked',lambda:True)
    state=Mock(side_effect=AssertionError('must not open profile or state'))
    monkeypatch.setattr(receiver,'preflight_state',state)
    assert receiver.main(['--agent-binding','fictional.json','--agent-marker','FICTION'])==2
    assert state.call_count==0


@pytest.mark.parametrize('command',[
    ['process','fictional.json'],
    ['reserve-send','fictional-task','fictional-proof.json','--output','fictional-token.json'],
    ['revise-unsent','fictional-task','--reason','fixture'],
    ['record-send','fictional-token.json','b_view_visible','--evidence','fictional.json'],
    ['bind','--sender-profile','https://www.douyin.com/user/a','--receiver-profile','https://www.douyin.com/user/b','--library-id','7'],
])
def test_old_operator_mutations_stop_before_reading_database(monkeypatch,command):
    from app.mention_agent import __main__ as operator
    monkeypatch.setattr(operator,'legacy_testing_blocked',lambda:True)
    database=Mock(side_effect=AssertionError('must not open database'))
    monkeypatch.setattr(operator,'configured_database',database)
    assert operator.main(command)==2 and database.call_count==0
