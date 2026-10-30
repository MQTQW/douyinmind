"""Large append-only histories remain complete audit input, never a recent slice."""
from contextlib import contextmanager
from copy import deepcopy
import json
import sqlite3
import pytest

from app.assistant.native_lane import NativeLaneError
from app.assistant import legacy_bridge
from tests.test_followup_no_effects_lane import nofx_case, ledger_hashes, SOURCE


def fill_history(case, total):
    with case.queue._tx() as db:
        existing = db.execute('SELECT COUNT(*) FROM assistant_events').fetchone()[0]
        assert existing <= total
        db.executemany('INSERT INTO assistant_events(task_id,kind,metadata,created_at) VALUES(?,?,?,?)',
            ((case.anchor['root_task_id'], 'synthetic_history', json.dumps({'sequence': index}),
              case.row['first_seen']-1) for index in range(existing, total)))


def no_commit(case, expected='native_recovery_effects_unknown'):
    saved = deepcopy(case.row)
    with pytest.raises(NativeLaneError, match=expected):
        case.lane.review_no_effects_followup(SOURCE)
    assert case.row == saved and case.calls['commit'] == [] and case.calls['model'] == 0


def test_5259_complete_linked_events_allow_review_without_any_new_effect(nofx_case):
    c = nofx_case
    fill_history(c, 5259)
    before = ledger_hashes(c.directory)
    posts, sends = c.backend.posts, c.old_adapter.submits
    result = c.lane.review_no_effects_followup(SOURCE)
    assert result['status'] == 'followup_no_effects_review_queued'
    assert c.row['attempts'] == 2 and len(c.calls['commit']) == 1
    assert ledger_hashes(c.directory) == before and c.calls['model'] == 0
    assert (c.backend.posts, c.old_adapter.submits) == (posts, sends)


def test_history_tail_orphan_and_tail_change_are_not_hidden_by_batching(nofx_case):
    c = nofx_case
    fill_history(c, 5259)
    with c.queue._tx() as db:
        last = db.execute('SELECT MAX(id) FROM assistant_events').fetchone()[0]
        db.execute('UPDATE assistant_events SET task_id=? WHERE id=?', ('orphan-private-do-not-log', last))
    no_commit(c)
    diagnostic = c.lane.last_native_recovery_diagnostic
    assert diagnostic['stage'] == 'followup_nofx_audit_before_queue_history_links'
    assert diagnostic['exception_type'] == 'NativeLaneError'
    assert 'orphan-private-do-not-log' not in str(diagnostic)
    with c.queue._tx() as db:
        db.execute('UPDATE assistant_events SET task_id=? WHERE id=?', (c.anchor['root_task_id'], last))
    def change_tail():
        with c.queue._tx() as db:
            db.execute('UPDATE assistant_events SET metadata=? WHERE id=?',
                (json.dumps({'private': 'https://do-not-log.invalid/secret'}), last))
    c.on_read['on_read'] = change_tail
    no_commit(c)
    assert c.calls['read'] == 1
    assert c.lane.last_native_recovery_diagnostic['stage'] == 'followup_nofx_audit_after_witness_digest'
    assert 'do-not-log' not in str(c.lane.last_native_recovery_diagnostic)
    c.on_read['on_read'] = None
    root = c.queue.get_task(c.anchor['root_task_id'])
    duplicate = c.queue.enqueue(c.bridge._binding(root), c.envelope, kind='query',
        user_id=root['user_id'], device_id=root['device_id'], authorization=root['authorization'])
    with c.queue._tx() as db:
        db.execute("UPDATE assistant_tasks SET state='b_view_visible' WHERE id=?", (duplicate['id'],))
        db.execute('UPDATE assistant_events SET task_id=? WHERE id=?', (duplicate['id'], last))
    no_commit(c, 'native_recovery_effects_present')
    assert c.lane.last_native_recovery_diagnostic['stage'] == 'followup_nofx_audit_before_queue_attribution'


def test_log_hard_fence_and_original_entity_limit_refuse_without_partial_witness(nofx_case):
    c = nofx_case
    fill_history(c, 49999)
    digest = c.lane._followup_no_effects_audit(c.row['candidate'], c.snapshot(SOURCE), c.anchor)
    assert len(digest) == 64
    for total in (50000, 50001):
        fill_history(c, total)
        no_commit(c)
        assert c.lane.last_native_recovery_diagnostic['stage'] == 'followup_nofx_audit_before_read_assistant_events'
    with c.queue._tx() as db:
        db.execute("DELETE FROM assistant_events WHERE kind='synthetic_history'")
    from tests.test_followup_no_effects_lane import add_semantic
    add_semantic(c)
    with c.semantic._connection(write=True) as db:
        original = dict(db.execute('SELECT * FROM semantic_plans').fetchone())
        columns = tuple(original)
        values = []
        for index in range(1000):
            item = {**original, 'plan_id': 'synthetic-plan-'+str(index),
                'source_key': 'synthetic-source-'+str(index), 'request_key': 'synthetic-request-'+str(index)}
            values.append(tuple(item[key] for key in columns))
        db.executemany('INSERT INTO semantic_plans('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')', values)
    no_commit(c)
    assert c.lane.last_native_recovery_diagnostic['stage'] == 'followup_nofx_audit_before_read_semantic_plans'


def test_mid_batch_read_failure_exposes_only_fixed_stage_and_exception(nofx_case, monkeypatch):
    c = nofx_case
    fill_history(c, 5259)
    original = legacy_bridge._followup_owned_ro
    class FaultCursor:
        def __init__(self, cursor): self.cursor, self.batches = cursor, 0
        def fetchmany(self, amount):
            self.batches += 1
            if self.batches == 2:
                raise sqlite3.OperationalError('https://private-do-not-log.invalid/body')
            return self.cursor.fetchmany(amount)
    class ReadProxy:
        def __init__(self, db): self.db = db
        def execute(self, sql, *args):
            cursor = self.db.execute(sql, *args)
            return FaultCursor(cursor) if sql == 'SELECT * FROM assistant_events ORDER BY rowid' else cursor
    @contextmanager
    def fault(store):
        with original(store) as db:
            yield ReadProxy(db) if store is c.queue else db
    monkeypatch.setattr(legacy_bridge, '_followup_owned_ro', fault)
    no_commit(c)
    diagnostic = c.lane.last_native_recovery_diagnostic
    assert diagnostic['stage'] == 'followup_nofx_audit_before_read_assistant_events'
    assert diagnostic['exception_type'] == 'OperationalError'
    assert 'private-do-not-log' not in str(diagnostic)
