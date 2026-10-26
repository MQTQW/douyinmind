"""Real temporary-process crash/recovery and marker protection for usage ledger."""
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from app.assistant.usage_budget import DATABASE_NAME, UsageBudget, UsageBudgetError


def _hot_journal(path):
    source = textwrap.dedent("""
        import os,sqlite3,sys
        db=sqlite3.connect(sys.argv[1])
        db.execute('PRAGMA cache_size=5')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        for i in range(120):
            db.execute('INSERT INTO usage_budget_events(request_key,event,occurred_at_utc,detail) VALUES (?,?,?,?)',
                ('uncommitted','fixture','2026-10-22T03:00:00Z','x'*12000))
        os._exit(73)
    """)
    result = subprocess.run([sys.executable, "-c", source, str(path)],
        capture_output=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 73, result.stderr
    journal = Path(str(path) + "-journal")
    assert journal.is_file() and journal.stat().st_size > 512
    return journal


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_owned_hot_journal_reopens_and_preserves_committed_reservations(tmp_path):
    value = UsageBudget.prepare(tmp_path / DATABASE_NAME)
    value.set_limits(daily_tokens=100)
    value.reserve("before-crash", "r", 100)
    marker = value.marker_path.read_bytes()
    journal = _hot_journal(value.path)
    # Construction proves the component without trusting the coordination lock.
    reopened = UsageBudget(value.path)
    assert reopened.summary()["daily"]["held_estimated_tokens"] == 100
    assert value.marker_path.read_bytes() == marker
    with sqlite3.connect(value.path.as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("SELECT count(*) FROM usage_budget_events WHERE event='fixture'").fetchone()[0] == 0
    assert not journal.exists()


def test_cached_read_also_recovers_hot_journal_not_only_constructor(tmp_path):
    value = UsageBudget.prepare(tmp_path / DATABASE_NAME)
    value.reserve("before-crash", "r", 80)
    _hot_journal(value.path)
    assert value.summary()["all_time"]["held_estimated_tokens"] == 80
    assert value.settle("r", {"total_tokens": 70})["state"] == "settled"


def test_missing_marker_blocks_hot_journal_recovery_without_touching_files(tmp_path):
    value = UsageBudget.prepare(tmp_path / DATABASE_NAME)
    value.reserve("before-crash", "r", 80)
    journal = _hot_journal(value.path)
    value.marker_path.unlink()
    before = (_hash(value.path), _hash(journal))
    with pytest.raises(UsageBudgetError, match="usage_budget_marker_missing"):
        value.summary()
    assert (_hash(value.path), _hash(journal)) == before


def test_unrecognized_hot_database_is_never_recovered_or_adopted(tmp_path):
    value = UsageBudget.prepare(tmp_path / DATABASE_NAME)
    value.path.unlink()
    with sqlite3.connect(value.path) as db:
        # Same event shape lets the isolated crash helper write pages; no owner.
        db.execute('CREATE TABLE usage_budget_events(id INTEGER PRIMARY KEY,request_key TEXT,event TEXT,occurred_at_utc TEXT,detail TEXT)')
    journal = _hot_journal(value.path)
    before = (_hash(value.path), _hash(journal))
    with pytest.raises(UsageBudgetError, match="unsafe_usage_budget_storage"):
        value.summary()
    assert (_hash(value.path), _hash(journal)) == before
