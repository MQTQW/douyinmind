"""Fixed followup-anchor diagnostics: owned temp ledgers and synthetic UI only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import ast
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from tests.test_conversation_followup_bridge import prepared
from tests.test_thread_followup_owner_review import (
    case as observed_followup_case, semantic_case, inspection_lane,
)
from tests.test_native_recovery_owner import case as recovery_owner_case, SOURCE

COUNT_KEYS = {'user_count', 'device_count', 'root_task_count', 'origin_count'}
ZERO_COUNTS = dict.fromkeys(COUNT_KEYS, 0)
ONE_COUNTS = {'user_count': 1, 'device_count': 1, 'root_task_count': 1, 'origin_count': 1}


def hashes(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob('*') if path.is_file()
        and (path.name.endswith('.sqlite3') or path.name.endswith('.sqlite3-wal'))}


@pytest.fixture
def anchored_case(tmp_path):
    values = prepared(tmp_path)
    binding, agent, root, registry, migrated, queue, history, control, bridge, anchor, backend, adapter = values
    candidate = {'source_kind': 'native_reply_notice_candidate',
        'sender_profile_url': binding.sender_profile_url,
        'receiver_profile_url': binding.receiver_profile_url, 'video_id': root['video_id'],
        'parent_id_hint': root['native_comment_id'], 'recipient_uid': '99999999'}
    private = [binding.sender_profile_url, binding.receiver_profile_url, root['video_id'],
        root['native_comment_id'], root['comment_text'], root['reference_id'],
        anchor['root_task_id'], anchor['root_agent_task_id'],
        anchor['prior_reply_text'], anchor['prior_reply_sha256'], 'DIAGNOSTIC-PRIVATE']
    return SimpleNamespace(directory=tmp_path, binding=binding, agent=agent, registry=registry,
        queue=queue, bridge=bridge, anchor=anchor, candidate=candidate,
        backend=backend, adapter=adapter, private=private)


def assert_fixed(c, expected, counts=None):
    diagnostic = c.bridge.last_followup_anchor_diagnostic
    assert set(diagnostic) == {'code', 'counts'}
    assert diagnostic['code'] == expected
    assert set(diagnostic['counts']) == COUNT_KEYS
    assert all(type(value) is int and 0 <= value <= 1000000 for value in diagnostic['counts'].values())
    if counts is not None:
        assert diagnostic['counts'] == counts
    serialized = json.dumps(diagnostic, ensure_ascii=False)
    assert all(value not in serialized for value in c.private)
    return diagnostic


def readonly_call(c, call):
    before = hashes(c.directory)
    posts, submits = c.backend.posts, c.adapter.submits
    result = call()
    assert hashes(c.directory) == before
    assert (c.backend.posts, c.adapter.submits) == (posts, submits)
    return result


def test_anchor_diagnostics_have_fresh_fixed_counts_and_thread_local_state(anchored_case):
    c = anchored_case
    assert readonly_call(c, lambda: c.bridge.followup_anchors(c.candidate)) == [c.anchor]
    assert_fixed(c, 'followup_anchor_matched', ONE_COUNTS)
    foreign = {**c.candidate, 'sender_profile_url': 'https://www.douyin.com/user/synthetic-foreign-A'}
    def other_thread():
        assert readonly_call(c, lambda: c.bridge.followup_anchors(foreign)) == []
        return assert_fixed(c, 'followup_anchor_user_not_unique', ZERO_COUNTS)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(other_thread).result()['counts'] == ZERO_COUNTS
    # The other thread's refusal cannot overwrite this owner's matched result.
    assert_fixed(c, 'followup_anchor_matched', ONE_COUNTS)
    assert readonly_call(c, lambda: c.bridge.followup_anchors(foreign)) == []
    assert_fixed(c, 'followup_anchor_user_not_unique', ZERO_COUNTS)
    assert readonly_call(c, lambda: c.bridge.followup_anchors(c.candidate)) == [c.anchor]
    assert_fixed(c, 'followup_anchor_matched', ONE_COUNTS)
    c.bridge.reset_followup_anchor_diagnostic()
    assert c.bridge.last_followup_anchor_diagnostic == {}


def test_authorization_refusals_remain_distinct_without_mutating_ledgers(anchored_case, monkeypatch):
    c = anchored_case
    def error(*args, **kwargs):
        raise RuntimeError('DIAGNOSTIC-PRIVATE')
    different = type(c.binding).from_dict({**c.binding.to_dict(), 'knowledge_base_id': 999})
    cases = [
        ('source_authorizer_rejected', lambda patch: patch.setattr(c.bridge, 'source_authorizer', lambda task: False)),
        ('source_authorizer_error', lambda patch: patch.setattr(c.bridge, 'source_authorizer', error)),
        ('registry_snapshot_rejected', lambda patch: patch.setattr(c.registry, 'authorize_snapshot', lambda auth: None)),
        ('registry_binding_mismatch', lambda patch: patch.setattr(c.registry, 'authorize_snapshot', lambda auth: different)),
        ('registry_authorization_error', lambda patch: patch.setattr(c.registry, 'authorize_snapshot', error)),
        ('choice_authority_rejected', lambda patch: patch.setattr(c.bridge, 'choices', SimpleNamespace(authority_current=lambda task: False))),
        ('choice_authority_error', lambda patch: patch.setattr(c.bridge, 'choices', SimpleNamespace(authority_current=error))),
    ]
    for suffix, configure in cases:
        with monkeypatch.context() as patch:
            configure(patch)
            assert readonly_call(c, lambda: c.bridge.followup_anchors(c.candidate)) == [], suffix
            assert_fixed(c, 'followup_anchor_' + suffix,
                {'user_count': 1, 'device_count': 1, 'root_task_count': 1, 'origin_count': 0})
    for suffix, result in (('semantic_permission_rejected', lambda task: False), ('semantic_permission_error', error)):
        with monkeypatch.context() as patch:
            patch.setattr(c.bridge, 'semantic_store', object())
            patch.setattr(c.bridge, '_semantic_permission_current', result)
            assert readonly_call(c, lambda: c.bridge.followup_anchors(c.candidate)) == []
            assert_fixed(c, 'followup_anchor_' + suffix,
                {'user_count': 1, 'device_count': 1, 'root_task_count': 1, 'origin_count': 0})
    assert readonly_call(c, lambda: c.bridge.followup_anchors(c.candidate)) == [c.anchor]
    assert_fixed(c, 'followup_anchor_matched', ONE_COUNTS)


@contextmanager
def changed_cell(path, table, column, key_column, key, change):
    # Every path comes from prepared(tmp_path); no product ledger is opened.
    with sqlite3.connect(path) as db:
        original = db.execute('SELECT '+column+' FROM '+table+' WHERE '+key_column+'=?', (key,)).fetchone()[0]
        replacement = change(original) if callable(change) else change
        db.execute('UPDATE '+table+' SET '+column+'=? WHERE '+key_column+'=?', (replacement, key))
    try:
        yield
    finally:
        with sqlite3.connect(path) as db:
            db.execute('UPDATE '+table+' SET '+column+'=? WHERE '+key_column+'=?', (original, key))


def test_persisted_task_reply_and_binding_refusals_have_separate_fixed_codes(anchored_case, monkeypatch):
    c = anchored_case
    task_id, agent_id = c.anchor['root_task_id'], c.anchor['root_agent_task_id']
    original_raw = c.queue._raw
    # Simulate the two queue facts changing between the scoped root selection
    # and its exact re-read, so public diagnostics still exercise the root gate.
    for field, value, suffix in (('state', 'ready_to_publish', 'task_state'),
                                ('agent_task_id', None, 'agent_task_missing')):
        with monkeypatch.context() as patch:
            def projected(row):
                task = original_raw(row)
                return {**task, field: value} if task and task['id'] == task_id else task
            patch.setattr(c.queue, '_raw', projected)
            assert readonly_call(c, lambda: c.bridge.followup_anchors(c.candidate)) == []
            assert_fixed(c, 'followup_anchor_' + suffix)
    with sqlite3.connect(c.agent.path) as db:
        binding_id = db.execute('SELECT binding_id FROM agent_tasks WHERE task_id=?', (agent_id,)).fetchone()[0]
    alter_json = lambda column, value: lambda raw: json.dumps({**json.loads(raw), column: value})
    cells = [
        (c.agent.path, 'agent_replies', 'send_token', 'task_id', agent_id, None, 'reply_unreserved'),
        (c.agent.path, 'agent_replies', 'proposal', 'task_id', agent_id, alter_json('kind', 'clarification'), 'proposal_kind'),
        (c.agent.path, 'agent_replies', 'proposal', 'task_id', agent_id, alter_json('text', 7), 'proposal_text'),
        (c.agent.path, 'agent_replies', 'target', 'task_id', agent_id, alter_json('unexpected', 'DIAGNOSTIC-PRIVATE'), 'reply_target'),
        (c.agent.path, 'agent_bindings', 'payload', 'binding_id', binding_id, alter_json('unexpected', 'DIAGNOSTIC-PRIVATE'), 'binding_changed'),
    ]
    for path, table, column, key_column, key, change, suffix in cells:
        with changed_cell(path, table, column, key_column, key, change):
            assert readonly_call(c, lambda: c.bridge.followup_anchors(c.candidate)) == [], suffix
            assert_fixed(c, 'followup_anchor_' + suffix)
    assert readonly_call(c, lambda: c.bridge.followup_anchors(c.candidate)) == [c.anchor]
    assert_fixed(c, 'followup_anchor_matched', ONE_COUNTS)


def test_zero_anchor_inspection_projects_only_current_fixed_diagnostic_without_ui_or_effects(observed_followup_case):
    c = observed_followup_case
    lane, store, state, saved, events = inspection_lane(c)
    before = deepcopy(saved)
    def forbidden(*args, **kwargs):
        pytest.fail('anchor diagnostic inspection invoked a model, UI write or task effect')
    c.c.model.complete = forbidden
    c.coordinator.prepare = forbidden
    lane.queue.enqueue = forbidden
    lane.adapter.prepare_reply = forbidden
    lane.adapter.submit_reply = forbidden
    class DiagnosticBridge:
        def __init__(self):
            self.value = {'code': 'followup_anchor_matched', 'counts': dict(ONE_COUNTS)}
            self.emit = True
            self.calls = []
        @property
        def last_followup_anchor_diagnostic(self):
            return deepcopy(self.value)
        def reset_followup_anchor_diagnostic(self):
            self.calls.append('reset')
            self.value = {}
        def followup_anchors(self, candidate):
            self.calls.append('anchors')
            if self.emit:
                self.value = {'code': 'followup_anchor_source_authorizer_rejected', 'counts': {
                    **ONE_COUNTS, 'origin_count': 0, 'raw': 'DIAGNOSTIC-PRIVATE'}}
            return []
    bridge = DiagnosticBridge()
    c.coordinator.bridge = bridge
    result = lane.inspect_followup_source(saved['source_id'])
    assert result['matched'] is False and result['dispatchable'] is False
    diagnostic = lane.last_native_recovery_diagnostic
    assert diagnostic['code'] == 'followup_anchor_source_authorizer_rejected'
    assert diagnostic['anchor_counts'] == {**ONE_COUNTS, 'origin_count': 0}
    assert bridge.calls == ['reset', 'anchors']
    assert saved == before and 'read' not in events and c.c.model.requests == [] and not c.lane.pending
    assert 'DIAGNOSTIC-PRIVATE' not in str(diagnostic)
    # A subsequent call that never produces bridge evidence cannot borrow that result.
    bridge.emit = False
    result = lane.inspect_followup_source(saved['source_id'])
    assert result['matched'] is False
    diagnostic = lane.last_native_recovery_diagnostic
    assert diagnostic['code'] == 'followup_anchor_not_checked'
    assert not diagnostic.get('anchor_counts')
    assert saved == before and 'read' not in events and c.c.model.requests == []


def test_owner_projects_only_four_nonboolean_bounded_anchor_counts(recovery_owner_case):
    c = recovery_owner_case
    writes = []
    c.owner.control.queue = SimpleNamespace(update_control=lambda fn: writes.append(fn(dict(c.state))))
    c.lane.last_native_recovery_diagnostic = {
        'stage': 'followup_inspect_complete', 'code': 'followup_anchor_source_authorizer_rejected',
        'anchor_counts': {'user_count': 1, 'device_count': 1, 'root_task_count': 1, 'origin_count': 0,
            'raw': 'DIAGNOSTIC-PRIVATE'}}
    c.lane.inspect_followup_source = lambda source: {'source_id': source,
        'status': 'followup_source_inspected', 'dispatchable': False, 'matched': False}
    assert c.owner('inspect_thread_followup', SOURCE)['ok'] is True
    assert writes[0]['native_recovery']['diagnostic']['anchor_counts'] == {**ONE_COUNTS, 'origin_count': 0}
    assert 'DIAGNOSTIC-PRIVATE' not in str(writes)
    c.lane.last_native_recovery_diagnostic['anchor_counts'] = {
        'user_count': True, 'device_count': -1, 'root_task_count': 1000001, 'origin_count': 0,
        'raw': 'DIAGNOSTIC-PRIVATE'}
    c.owner('inspect_thread_followup', SOURCE)
    assert writes[-1]['native_recovery']['diagnostic']['anchor_counts'] == {'origin_count': 0}
    assert 'DIAGNOSTIC-PRIVATE' not in str(writes)


def test_only_explicit_published_choice_anchor_uses_published_authority(anchored_case, monkeypatch):
    c = anchored_case
    with sqlite3.connect(c.queue.path) as db:
        db.row_factory = sqlite3.Row
        task = c.queue._raw(db.execute('SELECT * FROM assistant_tasks WHERE id=?',
            (c.anchor['root_task_id'],)).fetchone())
    choice = deepcopy(task)
    choice['envelope']['evidence']['source_kind'] = 'library_choice_reply'
    class FakeChoices:
        def __init__(self):
            self.calls = []
            self.live = False
        def authority_current(self, value):
            self.calls.append('live')
            return self.live
        def published_followup_anchor_current(self, value):
            self.calls.append('published')
            return True
    choices = FakeChoices()
    monkeypatch.setattr(c.bridge, 'choices', choices)
    # Execution/send defaults, explicit False and truthy non-bools stay live.
    assert readonly_call(c, lambda: c.bridge._authorized(choice)) is False
    assert readonly_call(c, lambda: c.bridge._authorized(choice, published_anchor=False)) is False
    assert readonly_call(c, lambda: c.bridge._authorized(choice, published_anchor=1)) is False
    assert choices.calls == ['live', 'live', 'live']
    assert readonly_call(c, lambda: c.bridge._authorized(choice, published_anchor=True)) is True
    assert choices.calls[-1] == 'published'
    assert readonly_call(c, lambda: c.bridge._authorized(task, published_anchor=True)) is False
    assert choices.calls[-1] == 'live'
    choices.live = True
    assert readonly_call(c, lambda: c.bridge._authorized(choice)) is True
    assert choices.calls[-1] == 'live'
    with monkeypatch.context() as patch:
        patch.setattr(c.bridge, 'source_authorizer', lambda value: False)
        before = list(choices.calls)
        assert readonly_call(c, lambda: c.bridge._authorized(choice, published_anchor=True)) is False
        assert choices.calls == before
    with monkeypatch.context() as patch:
        patch.setattr(c.bridge, 'semantic_store', object())
        patch.setattr(c.bridge, '_semantic_permission_current', lambda value: False)
        assert readonly_call(c, lambda: c.bridge._authorized(choice, published_anchor=True)) is False
        assert choices.calls[-1] == 'published'
    # A missing published checker cannot borrow the execution checker's approval.
    with monkeypatch.context() as patch:
        patch.setattr(c.bridge, 'choices', SimpleNamespace(authority_current=choices.authority_current))
        assert readonly_call(c, lambda: c.bridge._authorized(choice, published_anchor=True)) is False

    module = ast.parse((Path(__file__).parents[1]/'app'/'assistant'/'legacy_bridge.py').read_text(encoding='utf-8'))
    explicit = [node for node in ast.walk(module) if isinstance(node, ast.Call)
        and any(keyword.arg == 'published_anchor' and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True for keyword in node.keywords)]
    assert len(explicit) == 1
    root = next(node for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == '_followup_root_anchor')
    assert explicit[0] in list(ast.walk(root))
    assert isinstance(explicit[0].func, ast.Attribute) and explicit[0].func.attr == '_authorized'
    assert any(isinstance(node, ast.If) and node.lineno < explicit[0].lineno
        and any(isinstance(value, ast.Constant) and value.value == 'b_view_visible' for value in ast.walk(node.test))
        and any(isinstance(value, ast.Return) for statement in node.body for value in ast.walk(statement))
        for node in ast.walk(root))
