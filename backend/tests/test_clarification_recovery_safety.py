"""Bounded clarification repair: one TEMP session ledger, inert peer views."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from app.assistant import clarification_continuation as module
from app.assistant.clarification_continuation import ClarificationContinuation
from app.assistant.native_lane import NativeLane
from app.mention_agent.contracts import canonical_json
from app.mention_receiver.browser import ReceiverInterventionRequired
from tests.test_intent_clarification_sessions import sample, response


EMPTY = {'links_repaired': 0, 'publications_observed': 0, 'needs_review': 0, 'errors': 0}


class Sources:
    """Expose Source's read observations without replacing coordinator checks."""
    def __init__(self, rows):
        self.rows = rows
        self.current = True

    @contextmanager
    def _connection(self):
        yield self

    def execute(self, sql, args):
        key = 'source_id' if 'source_id=?' in sql else 'task_id'
        rows = [deepcopy(row) for row in self.rows.values() if row.get(key) == args[0]]
        return SimpleNamespace(fetchall=lambda: rows)

    def admitted_source_current(self, source_id):
        return self.current and source_id in self.rows


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    c = sample(tmp_path)
    tasks, sources, old, replies = {}, {}, {}, {}
    flags = {'stop': False, 'desired': 'running', 'authorized': True, 'root_current': True}
    effects = {'model': 0, 'query': 0, 'import': 0, 'send': 0}

    def forbidden(kind):
        def call(*args, **kwargs):
            effects[kind] += 1
            pytest.fail('recovery attempted ' + kind)
        return call

    class Receipt:
        def __init__(self, plan_id):
            self.plan_id = plan_id

    decisions = {}
    hooks = {'verify': None}

    def verify(receipt, **kwargs):
        if hooks['verify'] is not None:
            hooks['verify'](receipt)
        return deepcopy(decisions[receipt.plan_id])

    monkeypatch.setattr(module, 'RecordedSemanticDecision', Receipt)
    monkeypatch.setattr(module, 'verify_semantic_decision', verify)
    source_view = Sources(sources)
    queue = SimpleNamespace(
        get_task=lambda key: deepcopy(tasks.get(key)),
        lookup_by_source_reference=lambda sender, receiver, mode, reference: next((deepcopy(task)
            for task in tasks.values() if all(task['envelope'][key] == value for key, value in (
                ('sender_profile_url', sender), ('receiver_profile_url', receiver),
                ('source_mode', mode), ('reference_id', reference)))), None),
        get_control=lambda: {'desired_state': flags['desired']},
        enqueue=forbidden('import'))
    bridge = SimpleNamespace(queue=queue, should_stop=lambda: flags['stop'],
        control=SimpleNamespace(snapshot=lambda: {'desired_state': flags['desired']}),
        registry=SimpleNamespace(authorize_snapshot=lambda auth: c.binding if flags['authorized'] else None),
        agent=SimpleNamespace(get_reply=lambda key: deepcopy(replies.get(key)),
            get_task=lambda key: deepcopy(next((row for row in old.values() if row['task_id'] == key), None)),
            claim=forbidden('model'), reserve_send=forbidden('send')),
        _find=lambda task: deepcopy(old.get(task['id'])),
        _semantic_receipt=lambda task: Receipt(task['plan_id']),
        _semantic_owner_scope=lambda task: {'synthetic': 'frozen-owner'},
        model_factory=forbidden('model'), knowledge_factory=forbidden('query'),
        execute=forbidden('import'), publish=forbidden('send'))
    lane = SimpleNamespace(source_store=source_view, browser_lane=SimpleNamespace(pending={}))
    coordinator = ClarificationContinuation(c.store, bridge, lane, SimpleNamespace(), clock=lambda: c.clock[0])
    # This suite supplies a previously verified root observation. The actual
    # Source/Queue/Registry/receipt checks for repairing its child stay intact.
    coordinator._root_current = lambda session: flags['root_current']
    c.__dict__.update(tasks=tasks, sources=sources, old=old, replies=replies, flags=flags,
        effects=effects, decisions=decisions, hooks=hooks, bridge=bridge, lane=lane,
        coordinator=coordinator, source_view=source_view, receipt_type=Receipt)
    return c


def peers(c):
    return canonical_json({'tasks': c.tasks, 'sources': c.sources, 'old': c.old,
        'replies': c.replies, 'effects': c.effects})


def planned(c):
    env = response(c)
    source_id = 'native:' + 'c' * 64
    c.store.claim_response(c.session['session_id'], source_id, env)
    item = {'binding': c.binding.to_dict(), 'envelope': env, 'kind': 'save',
        'authorization': {'action': 'import'}, 'user_id': c.task['user_id'], 'device_id': c.task['device_id']}
    decision = {'plan_id': 'semantic-' + 'e' * 64, 'action': 'save', 'target_state': 'one', 'grant_id': 'fake-grant'}
    c.session = c.store.plan(c.session['session_id'], source_id, env, decision, item)
    child = {**deepcopy(item), 'id': '4' * 32, 'state': 'queued', 'plan_id': decision['plan_id'],
        'execution_token': None, 'publication_token': None, 'lease_until': None, 'owner_run_id': None,
        'execution_attempts': 0, 'publish_attempts': 0, 'agent_task_id': None}
    c.tasks[child['id']] = child
    c.decisions[decision['plan_id']] = decision
    c.sources[source_id] = {'source_id': source_id, 'task_id': child['id'], 'state': 'admitted',
        'historical': False, 'first_seen': 1001.0, 'reference_id': env['reference_id'],
        'candidate': '{}', 'plan': canonical_json({'queue_item': item, 'envelope': env, 'reference_id': env['reference_id']})}
    c.child = child
    return child


def awaiting(c, number, *, visible=False):
    c.clock[0] += 1
    task = deepcopy(c.task)
    task['id'] = format(100 + number, '032x')
    task['created_at'] = c.clock[0]
    task['envelope'].update(command_id='recovery-command-' + str(number),
        reference_id='recovery-reference-' + str(number), native_comment_id=str(7999999000000010000 + number))
    source = deepcopy(c.source)
    source.update(source_id='native:' + format(100 + number, '064x'), task_id=task['id'], first_seen=c.clock[0])
    source['candidate']['native_comment_id'] = task['envelope']['native_comment_id']
    session = c.store.arm(task, source, reason_code='semantic_intent_unclear', semantic_plan_id='semantic-' + 'd' * 64)
    task.update(state='b_view_visible', agent_task_id=format(1000 + number, '032x'))
    c.tasks[task['id']] = task
    source['candidate'] = canonical_json(source['candidate'])
    c.sources[source['source_id']] = source
    c.old[task['id']] = {'task_id': task['agent_task_id'], 'state': 'b_view_visible' if visible else 'answer_ready'}
    if visible:
        reply = deepcopy(c.reply)
        reply.update(reply_id=format(2000 + number, '032x'), task_id=task['agent_task_id'])
        reply['target'].update({key: value for key, value in task['envelope'].items() if key != 'evidence'})
        reply['verification'] = {key: reply['target'][key] for key in
            ('sender_profile_url', 'receiver_profile_url', 'video_id', 'comment_text', 'reference_id', 'native_comment_id')}
        reply['verification'].update(real_mention=True, unique_count=1, current_receiver_verified=True)
        c.replies[task['agent_task_id']] = reply
    return session


def run(c, entry):
    return (c.coordinator.recover_links([deepcopy(c.child)]) if entry == 'links'
        else c.coordinator.reconcile_existing())


def assert_no_effects(c, before):
    assert peers(c) == before
    assert c.effects == {'model': 0, 'query': 0, 'import': 0, 'send': 0}


def test_twenty_stalled_publications_cannot_starve_the_twenty_first_and_cursor_wraps(recovery):
    c = recovery
    first = [awaiting(c, number) for number in range(20)]
    last = awaiting(c, 20, visible=True)
    before = peers(c)
    assert c.coordinator.reconcile_existing(limit=20) == EMPTY
    assert c.coordinator.reconcile_existing(limit=20) == {**EMPTY, 'publications_observed': 1}
    assert c.store.get(last['session_id'])['state'] == 'awaiting_reply'
    assert c.coordinator.reconcile_existing(limit=20) == EMPTY
    assert c.coordinator._recovery_cursor is not None
    assert all(c.store.get(row['session_id'])['state'] == 'awaiting_publication' for row in first)
    assert_no_effects(c, before)


def test_bad_json_first_record_is_local_and_does_not_block_later_publication(recovery):
    c = recovery
    broken = awaiting(c, 1)
    later = awaiting(c, 2, visible=True)
    with c.store._db(write=True) as db:
        db.execute('UPDATE clarification_sessions SET root=? WHERE session_id=?', ('{', broken['session_id']))
    before = peers(c)
    result = c.coordinator.reconcile_existing()
    assert result == {**EMPTY, 'publications_observed': 1, 'needs_review': 1, 'errors': 1}
    assert c.store.get(later['session_id'])['state'] == 'awaiting_reply'
    assert c.coordinator.last_recovery_diagnostic['code'] == 'clarification_recovery_record_unavailable'
    assert_no_effects(c, before)


@pytest.mark.parametrize('entry', ['links', 'scan'])
def test_both_recovery_entries_link_only_a_fresh_exact_unexecuted_plan_once(recovery, entry):
    c = recovery
    planned(c)
    before = peers(c)
    assert run(c, entry) == {**EMPTY, 'links_repaired': 1}
    linked = c.store.get(c.session['session_id'])
    assert linked['state'] == 'handed_off' and linked['child_task_id'] == c.child['id']
    assert run(c, entry) == EMPTY
    assert c.store.get(c.session['session_id']) == linked
    assert_no_effects(c, before)


@pytest.mark.parametrize('entry', ['links', 'scan'])
@pytest.mark.parametrize('barrier', ['reviewed_session', 'reviewed_task', 'unknown', 'agent_id', 'agent_lookup',
    'execution_token', 'publication_token', 'lease_until', 'owner_run_id', 'execution_attempts', 'publish_attempts',
    'permission', 'old_source', 'source_envelope', 'root_changed', 'untyped_receipt', 'receipt_changed', 'meaning_changed'])
def test_recovery_entries_share_effect_authority_and_sticky_review_barriers(recovery, entry, barrier):
    c = recovery
    planned(c)
    if barrier == 'reviewed_session':
        with c.store._db(write=True) as db:
            db.execute("UPDATE clarification_sessions SET state='needs_review',reason_code='prior_review' WHERE session_id=?",
                (c.session['session_id'],))
    elif barrier in {'reviewed_task', 'unknown'}:
        c.child['state'] = 'needs_review' if barrier == 'reviewed_task' else 'delivery_unknown'
    elif barrier == 'agent_id': c.child['agent_task_id'] = '8' * 32
    elif barrier == 'agent_lookup': c.old[c.child['id']] = {'task_id': '8' * 32, 'state': 'needs_review'}
    elif barrier in {'execution_token', 'publication_token', 'owner_run_id'}: c.child[barrier] = 'already-owned'
    elif barrier == 'lease_until': c.child[barrier] = 9999.0
    elif barrier in {'execution_attempts', 'publish_attempts'}: c.child[barrier] = 1
    elif barrier == 'permission': c.flags['authorized'] = False
    elif barrier == 'root_changed': c.flags['root_current'] = False
    elif barrier == 'untyped_receipt':
        c.bridge._semantic_receipt = lambda task: SimpleNamespace(plan_id=c.session['semantic_plan_id'])
    elif barrier == 'receipt_changed':
        c.bridge._semantic_receipt = lambda task: c.receipt_type('semantic-' + 'f' * 64)
    elif barrier == 'meaning_changed': c.decisions[c.session['semantic_plan_id']]['action'] = 'cancel'
    elif barrier == 'old_source': c.sources[c.session['response_source_id']]['first_seen'] = 999.0
    else:
        source = c.sources[c.session['response_source_id']]
        plan = json.loads(source['plan']); plan['queue_item']['envelope']['comment_text'] = 'different observed body'
        source['plan'] = canonical_json(plan)
    before = peers(c)
    result = run(c, entry)
    assert result['links_repaired'] == result['publications_observed'] == result['errors'] == 0
    held = c.store.get(c.session['session_id'])
    assert held['state'] == 'needs_review' and held['child_task_id'] is None
    assert run(c, entry) == EMPTY and c.store.get(c.session['session_id']) == held
    assert_no_effects(c, before)


@pytest.mark.parametrize('entry', ['links', 'scan'])
def test_receipt_validation_exception_is_reviewed_locally_without_losing_later_work(recovery, entry):
    c = recovery
    planned(c)
    later = awaiting(c, 1, visible=True)
    c.hooks['verify'] = lambda receipt: (_ for _ in ()).throw(ValueError('synthetic private validation detail'))
    before = peers(c)
    result = run(c, entry)
    assert result['errors'] == result['needs_review'] == 1 and result['links_repaired'] == 0
    assert c.store.get(c.session['session_id'])['reason_code'] == 'clarification_recovery_record_unavailable'
    if entry == 'links':
        assert c.coordinator.reconcile_existing()['publications_observed'] == 1
    else:
        assert result['publications_observed'] == 1
    assert c.store.get(later['session_id'])['state'] == 'awaiting_reply'
    assert 'private' not in canonical_json(c.coordinator.last_recovery_diagnostic)
    assert_no_effects(c, before)


@pytest.mark.parametrize('entry', ['links', 'scan'])
def test_task_claimed_during_receipt_validation_cannot_gain_a_repaired_link(recovery, entry):
    c = recovery
    planned(c)
    expected = json.loads(peers(c))
    expected['tasks'][c.child['id']]['execution_token'] = 'claimed-during-receipt-check'
    c.hooks['verify'] = lambda receipt: c.child.update(execution_token='claimed-during-receipt-check')
    result = run(c, entry)
    assert result == {**EMPTY, 'needs_review': 1}
    held = c.store.get(c.session['session_id'])
    assert held['state'] == 'needs_review' and held['child_task_id'] is None
    assert held['reason_code'] == 'clarification_recovery_link_changed'
    assert peers(c) == canonical_json(expected)


def test_disabled_capability_can_project_existing_publication_but_never_handoff(recovery):
    c = recovery
    planned(c)
    later = awaiting(c, 1, visible=True)
    c.store.set_enabled(False)
    before = peers(c)
    assert c.coordinator.recover_links([c.child]) == EMPTY
    assert c.coordinator.reconcile_existing() == {**EMPTY, 'publications_observed': 1}
    assert c.store.get(later['session_id'])['state'] == 'awaiting_reply'
    assert c.store.get(c.session['session_id'])['state'] == 'planned'
    assert_no_effects(c, before)


@pytest.mark.parametrize('entry', ['links', 'scan'])
@pytest.mark.parametrize('signal', ['stop', 'pause'])
@pytest.mark.parametrize('stage', ['before', 'after_verify'])
def test_stop_and_pause_interrupt_recovery_before_session_link(recovery, entry, signal, stage):
    c = recovery
    planned(c)
    def interrupt(*unused):
        c.flags['stop' if signal == 'stop' else 'desired'] = True if signal == 'stop' else 'paused'
    if stage == 'before': interrupt()
    else: c.hooks['verify'] = interrupt
    before = peers(c)
    assert run(c, entry) == EMPTY
    assert c.store.get(c.session['session_id']) == c.session
    assert_no_effects(c, before)


@pytest.mark.parametrize('entry', ['links', 'scan'])
@pytest.mark.parametrize('error', [ReceiverInterventionRequired, KeyboardInterrupt])
def test_recovery_does_not_swallow_intervention_or_base_exception(recovery, entry, error):
    c = recovery
    planned(c)
    c.hooks['verify'] = lambda receipt: (_ for _ in ()).throw(error('synthetic interruption'))
    before = peers(c)
    with pytest.raises(error): run(c, entry)
    assert c.store.get(c.session['session_id']) == c.session
    assert_no_effects(c, before)


def collecting_lane(c):
    lane = NativeLane.__new__(NativeLane)
    lane.clarification_coordinator = c.coordinator
    lane._owner = lambda: None
    lane._pending = {}
    lane.browser_lane = SimpleNamespace(last_detail_failure_count=0, semantic_resolution_provider=None)
    lane.semantic_intake_ready = None
    lane.choice_controller = None
    lane._checkpoint = lambda: not c.flags['stop'] and c.flags['desired'] == 'running'
    lane.should_stop = lambda: c.flags['stop']
    lane.queue = c.bridge.queue
    lane.source_store = c.source_view
    lane.source_store.recoverable_ui_plans = lambda: []
    candidate = {'sender_profile_url': c.binding.sender_profile_url, 'receiver_profile_url': c.binding.receiver_profile_url,
        'video_id': c.task['envelope']['video_id'], 'native_comment_id': '7999999000000099999',
        'content': '@助手 新的合成指令', 'source_kind': 'native_notice_candidate'}
    lane.source_store.claim_ui_candidate = lambda *args, **kwargs: {'candidate': deepcopy(candidate), 'source_id': 'new-inert-source'}
    lane._candidate = lambda value: value
    lane._check = lambda claim: True
    lane.registry = SimpleNamespace(find_user=lambda *args: {'active': True, 'verification_status': 'verified'},
        intake_binding=lambda *args: c.binding)
    lane.adapter = SimpleNamespace(expected_profile_url=c.binding.receiver_profile_url)
    lane.run_id = 'synthetic-owner'
    lane._reply_session = lambda candidate: None
    lane._mention = lambda claim: [{'new_mention_observed': claim['candidate']['native_comment_id']}]
    return lane


def test_bad_clarification_record_does_not_block_collecting_an_unrelated_new_mention(recovery):
    c = recovery
    broken = awaiting(c, 1)
    with c.store._db(write=True) as db:
        db.execute('UPDATE clarification_sessions SET queue_item=? WHERE session_id=?', ('{', broken['session_id']))
    lane = collecting_lane(c)
    before = peers(c)
    assert lane.collect() == [{'new_mention_observed': '7999999000000099999'}]
    assert lane.clarification_coordinator is c.coordinator
    assert_no_effects(c, before)
