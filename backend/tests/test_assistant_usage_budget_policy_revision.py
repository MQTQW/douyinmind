"""Full-form policy compare-and-set, with temporary ledgers only."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import subprocess
import sys
import textwrap
import threading

import pytest

from app.assistant.usage_budget import DATABASE_NAME, UsageBudget, UsageBudgetError


def test_stale_full_form_cannot_overwrite_policy_or_existing_usage(tmp_path):
    path = tmp_path / DATABASE_NAME
    first = UsageBudget.prepare(path)
    other = UsageBudget(path)
    revision = first.summary()["policy"]["revision"]
    first.reserve("task", "request", 30)
    assert first.set_limits(per_task_tokens=100, daily_tokens=200, expected_revision=revision)["revision"] == 2
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(UsageBudgetError, match="^usage_policy_changed$"):
        other.set_limits(per_task_tokens=500, daily_tokens=600, expected_revision=revision)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert first.summary()["policy"] == {"per_task_tokens": 100, "daily_tokens": 200, "revision": 2}
    assert first.summary()["daily"]["held_estimated_tokens"] == 30


def test_two_threads_with_same_displayed_revision_only_one_change(tmp_path):
    path = tmp_path / DATABASE_NAME
    initial = UsageBudget.prepare(path)
    revision = initial.summary()["policy"]["revision"]
    barrier = threading.Barrier(2)
    def update(cap):
        ledger = UsageBudget(path)
        barrier.wait(timeout=10)
        try:
            return ("updated", ledger.set_limits(daily_tokens=cap, expected_revision=revision))
        except UsageBudgetError as error:
            return (error.code, None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(update, [100, 200]))
    assert sorted(item[0] for item in result) == ["updated", "usage_policy_changed"]
    final = initial.summary()["policy"]
    assert final["revision"] == 2 and final["daily_tokens"] in (100, 200)


def test_two_processes_cannot_both_write_using_same_revision(tmp_path):
    path = tmp_path / DATABASE_NAME
    ledger = UsageBudget.prepare(path)
    source = textwrap.dedent("""
        import sys
        from app.assistant.usage_budget import UsageBudget,UsageBudgetError
        value=UsageBudget(sys.argv[1])
        try:
            value.set_limits(daily_tokens=int(sys.argv[2]),expected_revision=1)
            print('updated')
        except UsageBudgetError as error:
            print(error.code)
    """)
    children = [subprocess.Popen([sys.executable, "-c", source, str(path), str(cap)],
        cwd=str(Path(__file__).resolve().parents[1]), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for cap in [100, 200]]
    results = [child.communicate(timeout=20) for child in children]
    assert all(child.returncode == 0 for child in children), results
    assert sorted(out.strip() for out, _ in results) == ["updated", "usage_policy_changed"]
    assert ledger.summary()["policy"]["revision"] == 2


@pytest.mark.parametrize("bad", [True, 0, -1, "1", 1.0, [], 1_000_000_000_001])
def test_invalid_revision_has_no_side_effect(tmp_path, bad):
    path = tmp_path / DATABASE_NAME
    ledger = UsageBudget.prepare(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(UsageBudgetError, match="^invalid_usage_revision$"):
        ledger.set_limits(daily_tokens=100, expected_revision=bad)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_trusted_compatibility_and_matching_noop_revision(tmp_path):
    ledger = UsageBudget.prepare(tmp_path / DATABASE_NAME)
    assert ledger.set_limits(daily_tokens=100)["revision"] == 2
    # Identical current forms are idempotent, not competing policy changes.
    assert ledger.set_limits(daily_tokens=100, expected_revision=2)["revision"] == 2
    with pytest.raises(UsageBudgetError, match="usage_policy_changed"):
        ledger.set_limits(daily_tokens=100, expected_revision=1)
