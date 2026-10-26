"""Temporary-only quota/accounting tests; no provider, business DB or browser."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from app.assistant.usage_budget import (
    DATABASE_NAME, MARKER_BYTES, MARKER_NAME, UsageBudget, UsageBudgetError,
    UsageBudgetExceeded,
)


NOW = datetime(2026, 10, 22, 3, 24, 1, tzinfo=timezone.utc)


@pytest.fixture
def budget(tmp_path):
    clock = [NOW]
    ledger = UsageBudget.prepare(tmp_path / DATABASE_NAME, clock=lambda: clock[0])
    ledger.fixture_clock = clock
    return ledger


def usage(total, incoming=None, outgoing=None):
    value = {"total_tokens": total}
    if incoming is not None:
        value["prompt_tokens"] = incoming
    if outgoing is not None:
        value["completion_tokens"] = outgoing
    return value


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_defaults_are_unconfigured_but_real_calls_still_need_estimate(budget):
    assert budget.summary()["policy"] == {"per_task_tokens": None, "daily_tokens": None, "revision": 1}
    with pytest.raises(UsageBudgetError, match="invalid_token_amount"):
        budget.reserve("task", "request", None)
    reserved = budget.reserve("task", "request", 200)
    assert reserved["created"] and reserved["may_request"]
    assert budget.summary("task")["task"]["held_estimated_tokens"] == 200
    assert budget.summary()["reservation_is_estimate"]
    assert budget.summary()["currency_cost"] is None
    assert budget.summary()["excluded_services"] == ["embedding", "asr"]


def test_real_usage_replaces_estimate_and_duplicate_never_readmits(budget):
    first = budget.reserve("task", "model-request:1", 100)
    assert first["may_request"]
    assert not budget.reserve("task", "model-request:1", 100)["may_request"]
    settled = budget.settle("model-request:1", usage(70, 60, 10), outcome="failed")
    assert settled["state"] == "settled" and settled["outcome"] == "failed"
    assert budget.summary("task")["task"]["actual_tokens"] == 70
    assert budget.summary()["daily"]["held_estimated_tokens"] == 0
    assert budget.settle("model-request:1", usage(70, 60, 10))["total_tokens"] == 70
    assert not budget.reserve("task", "model-request:1", 100)["may_request"]
    with pytest.raises(UsageBudgetError, match="provider_usage_conflict"):
        budget.settle("model-request:1", usage(71))
    for changed_task, changed_estimate in [("other", 100), ("task", 101)]:
        with pytest.raises(UsageBudgetError, match="usage_request_conflict"):
            budget.reserve(changed_task, "model-request:1", changed_estimate)


def test_per_task_and_daily_limits_use_actual_plus_held(budget):
    budget.set_limits(per_task_tokens=150, daily_tokens=200)
    budget.reserve("a", "r1", 100)
    with pytest.raises(UsageBudgetExceeded, match="task_token_limit"):
        budget.reserve("a", "r2", 51)
    budget.reserve("b", "r3", 100)
    with pytest.raises(UsageBudgetExceeded, match="daily_token_limit"):
        budget.reserve("c", "r4", 1)
    budget.settle("r1", usage(50))
    assert budget.reserve("a", "r5", 50)["may_request"]
    assert budget.get_request("r2") is None
    assert budget.get_request("r4") is None


def test_zero_limit_blocks_and_policy_change_never_resets_spend(budget):
    budget.reserve("t", "r", 80)
    budget.settle("r", usage(60))
    assert budget.set_limits(per_task_tokens=0)["revision"] == 2
    with pytest.raises(UsageBudgetExceeded):
        budget.reserve("t", "r2", 1)
    assert budget.set_limits(per_task_tokens=100)["revision"] == 3
    assert budget.set_limits(per_task_tokens=100)["revision"] == 3
    assert budget.summary("t")["task"]["actual_tokens"] == 60


@pytest.mark.parametrize("missing", [None, {}, {"prompt_tokens": 3}, {"total_tokens": None}])
def test_missing_usage_keeps_reservation_and_does_not_mean_zero(budget, missing):
    budget.set_limits(daily_tokens=100)
    budget.reserve("t", "r", 100)
    assert budget.settle("r", missing)["state"] == "unknown"
    totals = budget.summary()["daily"]
    assert totals["actual_tokens"] == 0 and totals["held_estimated_tokens"] == 100
    assert totals["needs_reconciliation"]
    with pytest.raises(UsageBudgetExceeded):
        budget.reserve("another", "new", 1)
    assert budget.settle("r", usage(20))["state"] == "settled"
    assert budget.reserve("another", "new", 80)["may_request"]


def test_timeout_restart_keeps_hold_and_does_not_downgrade_settled(budget):
    budget.set_limits(daily_tokens=100)
    budget.reserve("t", "r", 100)
    budget.mark_unknown("r", reason="request_timeout")
    reopened = UsageBudget(budget.path, clock=lambda: NOW + timedelta(days=1))
    assert reopened.get_request("r")["state"] == "unknown"
    assert reopened.summary("t")["task"]["held_estimated_tokens"] == 100
    reopened.settle("r", usage(10))
    assert reopened.mark_unknown("r")["state"] == "settled"
    assert reopened.settle("r", None)["state"] == "settled"


def test_overrun_is_real_and_can_block_following_requests(budget):
    budget.set_limits(per_task_tokens=100, daily_tokens=100)
    budget.reserve("t", "r", 50)
    settled = budget.settle("r", usage(120))
    assert settled["overrun"] and settled["total_tokens"] == 120
    assert budget.summary()["daily"]["overrun_requests"] == 1
    with pytest.raises(UsageBudgetExceeded, match="task_token_limit"):
        budget.reserve("t", "next", 1)
    with pytest.raises(UsageBudgetExceeded, match="daily_token_limit"):
        budget.reserve("other", "other-next", 1)


def test_historical_failed_1140_import_once_and_retry_responses_separately(budget):
    old = "hist-agent:agent-original:response:70"
    for _ in range(3):
        budget.import_historical_usage("agent-original", old, usage(1140), NOW, outcome="failed")
    budget.import_historical_usage("agent-original", "hist-agent:agent-original:response:74", usage(1111), NOW)
    budget.import_historical_usage("agent-original", "hist-agent:agent-original:response:78", usage(2771), NOW)
    totals = budget.summary("agent-original")["task"]
    assert totals["actual_tokens"] == 5022 and totals["request_count"] == 3
    assert budget.get_request(old)["outcome"] == "failed"


def test_historical_unknown_without_estimate_blocks_only_relevant_configured_scope(budget):
    budget.import_historical_usage("old", "old-unknown", None, NOW)
    budget.set_limits(per_task_tokens=500, daily_tokens=1000)
    assert budget.summary()["daily"]["unknown_unbounded_requests"] == 1
    with pytest.raises(UsageBudgetExceeded, match="task_usage_unknown"):
        budget.reserve("old", "new1", 10)
    with pytest.raises(UsageBudgetExceeded, match="daily_usage_unknown"):
        budget.reserve("other", "new2", 10)
    budget.fixture_clock[0] += timedelta(days=1)
    assert budget.reserve("other", "new2", 10)["may_request"]
    with pytest.raises(UsageBudgetExceeded, match="task_usage_unknown"):
        budget.reserve("old", "new1", 10)
    budget.settle("old-unknown", usage(12))
    assert budget.reserve("old", "new1", 10)["may_request"]


def test_unknown_historical_can_reconcile_same_key_but_cannot_change_identity(budget):
    budget.import_historical_usage("t", "h", None, NOW)
    assert budget.import_historical_usage("t", "h", usage(7), NOW)["state"] == "settled"
    with pytest.raises(UsageBudgetError, match="usage_request_conflict"):
        budget.import_historical_usage("other", "h", usage(7), NOW)
    with pytest.raises(UsageBudgetError, match="usage_request_conflict"):
        budget.import_historical_usage("t", "h", usage(7), NOW + timedelta(seconds=1))


def test_admission_day_is_fixed_beijing_not_settlement_day(budget):
    budget.fixture_clock[0] = datetime(2026, 10, 22, 15, 59, 59, tzinfo=timezone.utc)
    budget.set_limits(daily_tokens=100)
    assert budget.reserve("t", "before-midnight", 100)["day_key"] == "2026-10-22"
    budget.fixture_clock[0] += timedelta(seconds=2)
    assert budget.reserve("other", "after-midnight", 100)["day_key"] == "2026-10-23"
    budget.settle("before-midnight", usage(90))
    assert budget.summary()["daily"]["actual_tokens"] == 0
    assert budget.summary()["all_time"]["actual_tokens"] == 90
    assert budget.get_request("before-midnight")["day_key"] == "2026-10-22"


def test_thread_contention_does_not_overadmit(budget):
    budget.set_limits(daily_tokens=100)
    def attempt(index):
        local = UsageBudget(budget.path, clock=lambda: NOW)
        try:
            return local.reserve("task" + str(index), "request" + str(index), 10)["may_request"]
        except UsageBudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(attempt, range(24)))
    assert sum(result) == 10
    assert budget.summary()["daily"]["committed_tokens"] == 100


def test_multiple_python_processes_share_atomic_admission(budget):
    budget.set_limits(daily_tokens=70)
    script = textwrap.dedent("""
        import sys
        from datetime import datetime
        from app.assistant.usage_budget import UsageBudget, UsageBudgetExceeded
        value=UsageBudget(sys.argv[1], clock=lambda: datetime.fromisoformat(sys.argv[3]))
        admitted=0
        for index in range(8):
            try:
                admitted += value.reserve(sys.argv[2], sys.argv[2]+':'+str(index),10)['may_request']
            except UsageBudgetExceeded:
                pass
        print(admitted)
    """)
    options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    # Child admission and parent summary must use the same accounting day;
    # wall-clock children would test a different day after this fixture date.
    children = [subprocess.Popen([sys.executable, "-c", script, str(budget.path), "process"+str(i), budget.fixture_clock[0].isoformat()],
                cwd=str(Path(__file__).resolve().parents[1]), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, **options) for i in range(3)]
    outputs = [child.communicate(timeout=30) for child in children]
    assert all(child.returncode == 0 for child in children), outputs
    assert sum(int(out.strip()) for out, _ in outputs) == 7
    assert budget.summary()["daily"]["committed_tokens"] == 70


def test_interrupted_process_after_reserve_keeps_estimate(budget):
    budget.set_limits(daily_tokens=100)
    script = textwrap.dedent("""
        import os,sys
        from app.assistant.usage_budget import UsageBudget
        UsageBudget(sys.argv[1]).reserve('crashed','request-before-crash',100)
        os._exit(73)
    """)
    child = subprocess.run([sys.executable, "-c", script, str(budget.path)],
        cwd=str(Path(__file__).resolve().parents[1]), capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=20)
    assert child.returncode == 73
    reopened = UsageBudget(budget.path)
    assert reopened.get_request("request-before-crash")["state"] == "reserved"
    with pytest.raises(UsageBudgetExceeded):
        reopened.reserve("new", "after-crash", 1)


@pytest.mark.parametrize("bad", [True, False, -1, 1.1, "2", 1_000_000_000_001])
def test_strict_numeric_inputs_do_not_write(budget, bad):
    before = digest(budget.path)
    with pytest.raises(UsageBudgetError):
        budget.set_limits(daily_tokens=bad)
    with pytest.raises(UsageBudgetError):
        budget.reserve("t", "bad", bad)
    assert digest(budget.path) == before


@pytest.mark.parametrize("bad", ["", " key", "key ", "a/b", "汉字", "https://host", "x\n", "x"*241, None])
def test_identities_are_opaque_bounded_ascii_not_paths_urls_or_prompt(budget, bad):
    before = digest(budget.path)
    with pytest.raises(UsageBudgetError):
        budget.reserve("t", bad, 1)
    assert digest(budget.path) == before


@pytest.mark.parametrize("bad", [{"total_tokens": True}, {"total_tokens": -1},
                                  {"total_tokens": 10, "prompt_tokens": "3"}, []])
def test_invalid_usage_leaves_reservation_intact(budget, bad):
    budget.reserve("t", "r", 100)
    before = digest(budget.path)
    with pytest.raises(UsageBudgetError, match="invalid_provider_usage"):
        budget.settle("r", bad)
    assert digest(budget.path) == before
    assert budget.get_request("r")["state"] == "reserved"


def test_explicit_provider_zero_is_distinct_from_missing(budget):
    budget.reserve("t", "r", 100)
    assert budget.settle("r", usage(0))["state"] == "settled"
    assert budget.summary()["daily"]["committed_tokens"] == 0
    assert not budget.summary()["daily"]["needs_reconciliation"]


def test_read_summary_and_duplicate_settlement_do_not_rewrite_database(budget):
    budget.reserve("t", "r", 100)
    budget.settle("r", usage(50))
    before = digest(budget.path)
    UsageBudget(budget.path).summary("t")
    budget.get_request("r")
    budget.settle("r", usage(50))
    budget.reserve("t", "r", 100)
    assert digest(budget.path) == before


def test_missing_read_does_not_create_directory_or_database(tmp_path):
    path = tmp_path / "not-created" / DATABASE_NAME
    with pytest.raises(UsageBudgetError, match="usage_budget_missing"):
        UsageBudget(path)
    assert not path.parent.exists()


def test_required_marker_prevents_lost_ledger_from_resetting_quota(budget):
    budget.reserve("t", "r", 100)
    budget.path.unlink()
    for call in (lambda: UsageBudget(budget.path), lambda: UsageBudget.prepare(budget.path)):
        with pytest.raises(UsageBudgetError, match="usage_budget_missing"):
            call()
    assert not budget.path.exists()
    assert budget.marker_path.read_bytes() == MARKER_BYTES


def test_missing_or_altered_marker_blocks_existing_cache_and_reopen(budget):
    budget.marker_path.unlink()
    before = digest(budget.path)
    with pytest.raises(UsageBudgetError, match="usage_budget_marker_missing"):
        budget.summary()
    with pytest.raises(UsageBudgetError, match="usage_budget_marker_missing"):
        UsageBudget.prepare(budget.path)
    assert digest(budget.path) == before
    budget.marker_path.write_bytes(b"not our marker")
    with pytest.raises(UsageBudgetError, match="usage_budget_marker"):
        budget.reserve("t", "r", 10)


def test_preexisting_empty_file_never_adopted(tmp_path):
    path = tmp_path / DATABASE_NAME
    path.touch()
    inode = path.stat().st_ino
    with pytest.raises(UsageBudgetError, match="foreign_usage_budget"):
        UsageBudget.prepare(path)
    assert path.read_bytes() == b"" and path.stat().st_ino == inode
    assert not (tmp_path / MARKER_NAME).exists()


def test_foreign_schema_and_cached_replacement_are_rejected(budget):
    path = budget.path
    path.unlink()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated(x)")
        db.execute("INSERT INTO unrelated VALUES(1)")
    before = digest(path)
    with pytest.raises(UsageBudgetError, match="unsafe_usage_budget_storage"):
        budget.reserve("t", "r", 1)
    assert digest(path) == before


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_orphan_sidecar_cannot_be_adopted_on_prepare(tmp_path, suffix):
    path = tmp_path / DATABASE_NAME
    sidecar = Path(str(path) + suffix)
    sidecar.write_bytes(b"unowned existing recovery bytes")
    with pytest.raises(UsageBudgetError, match="unsafe_usage_budget_storage"):
        UsageBudget.prepare(path)
    assert not path.exists() and sidecar.read_bytes() == b"unowned existing recovery bytes"
    assert not (tmp_path / MARKER_NAME).exists()


@pytest.mark.parametrize("target", ["main", "marker"])
def test_hardlink_redirection_is_rejected(budget, tmp_path, target):
    path = budget.path if target == "main" else budget.marker_path
    alias = tmp_path / (target + "-alias")
    os.link(path, alias)
    before = path.read_bytes()
    with pytest.raises(UsageBudgetError):
        budget.summary()
    assert path.read_bytes() == before


def test_transaction_failure_rolls_back_reservation_and_events(budget):
    original = budget._event
    def fail(*args, **kwargs):
        raise RuntimeError("injected isolated failure")
    budget._event = fail
    before = digest(budget.path)
    with pytest.raises(RuntimeError, match="injected"):
        budget.reserve("t", "r", 100)
    assert digest(budget.path) == before
    assert budget.get_request("r") is None
    budget._event = original
    assert budget.reserve("t", "r", 100)["may_request"]


def test_invalid_timestamp_has_no_import_side_effect(budget):
    for bad in ("yesterday", "2026-10-22T03:00:00", datetime(2026, 10, 22), None):
        with pytest.raises(UsageBudgetError, match="invalid_usage_timestamp"):
            budget.import_historical_usage("t", "h", usage(1), bad)
    assert budget.summary()["all_time"]["request_count"] == 0
