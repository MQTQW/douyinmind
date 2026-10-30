"""No-effects followup owner lane: real temp ledgers, synthetic UI, no new model calls."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from types import SimpleNamespace
import threading
import time

import pytest

from app.assistant import native_lane as lane_module
from app.assistant.native_lane import NativeLane, NativeLaneError
from app.assistant.native_source_store import NativeSourceStore, NativeStoreError
from app.assistant.semantic_plan_store import SemanticPlanStore, ui_semantic_digest
from app.assistant.semantic_routing import SemanticIntentInterpreter
from app.assistant.semantic_resolution import owner_scope_from_envelope, catalog_snapshot_from_grants
from app.assistant.thread_followup import ThreadFollowupCoordinator
from app.assistant.usage_budget import UsageBudget
from app.mention_agent.followup_evidence import make_followup_envelope
from app.mention_receiver.browser import ReceiverDetailError, ReceiverPageError, ReceiverInterventionRequired
from tests.test_conversation_followup_bridge import prepared, child_detail

SOURCE = 'native:' + 'f'*64
REVIEW_KEYS = {'operation','reviewed_at','root_task_id','root_agent_task_id',
    'ui_reference_id','ui_digest','cross_ledger_digest','operator_reason'}


def ledger_hashes(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob('*.sqlite3')}


@pytest.fixture
def nofx_case(tmp_path, monkeypatch):
    binding, agent, root, registry, migrated, queue, history, control, bridge, anchor, backend, old_adapter = prepared(tmp_path)
    semantic = SemanticPlanStore.prepare(tmp_path/'semantic.sqlite3')
    budget = UsageBudget.prepare(tmp_path/'budget'/'usage-budget.sqlite3')
    calls = {'model':0,'identity':0,'read':0,'commit':[]}
    class ForbiddenModel:
        def complete(self, *args, **kwargs):
            calls['model'] += 1
            pytest.fail('no-effects review invoked a model')
    provider = SemanticIntentInterpreter(registry, semantic, ForbiddenModel(), budget)
    bridge.semantic_store = semantic
    bridge.usage_budget = budget
    now = time.time()
    text = '请回顾这段合成视频'
    candidate = {'namespace':'douyin_web_v1','source_kind':'native_reply_notice_candidate',
        'sender_uid':'8001','recipient_uid':'9001','sender_sec_uid':binding.sender_profile_url.rsplit('/',1)[-1],
        'sender_profile_url':binding.sender_profile_url,'receiver_profile_url':binding.receiver_profile_url,
        'video_id':root['video_id'],'content':text,'native_comment_id':'7999999000000000092',
        'parent_id_hint':root['native_comment_id'],'root_comment_id':None,'parent_comment_id':None,
        'native_notice_id':'7999999000000000093','notice_type':31,'comment_type':2,
        'native_source_key':SOURCE,'identity_link_revision':'1','dispatchable':False,'relation_status':'unknown',
        'created_time':str(int(now))}
    row = {'source_id':SOURCE,'state':'held','hold_code':'native_unknown_lease','historical':False,
        'has_at':False,'has_reply':True,'attempts':2,'plan':None,'task_id':None,'reference_id':None,
        'candidate':candidate,'immutable_hash':'d'*64,'identity_revision':'1',
        'first_seen':now,'last_seen':now,'lease_until':now-1,'claim_owner':'synthetic-expired-ui-owner',
        'claim_fingerprint':'c'*64,'prior_audit_hash':'b'*64}
    source = NativeSourceStore(tmp_path/'native'/'sources.sqlite3','9001',binding.receiver_profile_url,create=True)
    def snapshot(source_id):
        assert source_id == SOURCE
        return {key:deepcopy(row[key]) for key in ('source_id','state','hold_code','attempts','immutable_hash',
            'identity_revision','first_seen','last_seen','lease_until','claim_owner','claim_fingerprint','prior_audit_hash','candidate')}
    def commit(permit):
        assert permit.store is source and permit.snapshot == snapshot(SOURCE)
        assert set(permit.review) == REVIEW_KEYS
        calls['commit'].append(deepcopy(permit.review))
        row.update(state='fresh',hold_code=None)
        return {'source_id':SOURCE,'status':'followup_no_effects_review_queued','attempts_preserved':2,'dispatchable':False}
    monkeypatch.setattr(source,'followup_no_effects_review_snapshot',snapshot,raising=False)
    monkeypatch.setattr(source,'_commit_followup_no_effects_review',commit,raising=False)
    monkeypatch.setattr(source,'get_source',lambda key:deepcopy(row) if key == SOURCE else None)
    class ControlState(dict):
        def sync(self): queue.update_control(lambda old: dict(self))
        def __setitem__(self, key, value): super().__setitem__(key, value); self.sync()
        def clear(self): super().clear(); self.sync()
        def update(self, *args, **kwargs): super().update(*args, **kwargs); self.sync()
    state = ControlState({'run_id':'a'*32,'state':'paused','desired_state':'paused','process_alive':True,
        'worker_state':'idle','pid':os.getpid(),'manual_verification':{'active':False}})
    state.sync()
    monkeypatch.setattr(queue,'get_control',lambda:dict(state))
    stopped = [False]
    def identity():
        calls['identity'] += 1
        return True
    current = {'on_read':None}
    def read(found, observed, **kwargs):
        calls['read'] += 1
        if current['on_read'] is not None:
            current['on_read']()
        return child_detail(found,text=observed['content'])
    adapter = SimpleNamespace(expected_profile_url=binding.receiver_profile_url,
        check_intervention=lambda:None,read_followup_reply=read)
    base = SimpleNamespace(adapter=adapter,registry=registry,queue=queue,conversation=history,
        run_id=state['run_id'],should_stop=lambda:stopped[0],admission_lock=threading.RLock(),
        semantic_resolution_provider=provider,pending={},choice_controller=SimpleNamespace(agent=bridge.agent))
    lane = NativeLane(base,SimpleNamespace(reverify_identity_checkpoint=identity),source)
    coordinator = ThreadFollowupCoordinator(base,bridge)
    coordinator.prepare = lambda *args,**kwargs:pytest.fail('no-effects review used planning prepare')
    lane.followup_coordinator = coordinator
    detail = child_detail(anchor,text=text)
    envelope = make_followup_envelope(detail,binding)
    user = registry.snapshot()['users'][0]
    device = user['devices'][0]
    owner = owner_scope_from_envelope(envelope,user,device)
    return SimpleNamespace(directory=tmp_path,binding=binding,agent=agent,registry=registry,migrated=migrated,
        queue=queue,history=history,bridge=bridge,anchor=anchor,backend=backend,old_adapter=old_adapter,
        semantic=semantic,budget=budget,provider=provider,calls=calls,source=source,row=row,snapshot=snapshot,
        state=state,stopped=stopped,on_read=current,lane=lane,envelope=envelope,owner=owner)


def reject(c):
    before = deepcopy(c.row)
    posts, submits = c.backend.posts, c.old_adapter.submits
    try:
        result = c.lane.review_no_effects_followup(SOURCE)
    except (NativeLaneError, NativeStoreError, ReceiverDetailError, ReceiverPageError, ValueError):
        pass
    else:
        assert not result or result.get('status') != 'followup_no_effects_review_queued'
    assert c.row == before and not c.calls['commit'] and c.calls['model'] == 0
    assert (c.backend.posts,c.old_adapter.submits) == (posts,submits)


def add_semantic(c, state='unknown', *, fee_only=False):
    plan_id = 'semantic-'+'1'*64
    owner = {**c.owner,'reference_id':'reference:other-ui-alias','root_reference_id':'reference:other-ui-alias'}
    catalog = {'revision':'synthetic-catalog','options':[]}
    with c.semantic._connection(write=True) as db:
        db.execute('INSERT INTO semantic_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (plan_id,'native:synthetic-alias','2'*64,'3'*64,json.dumps(owner),json.dumps(catalog),'root',
             ' 请 回顾这段合成视频 ', 'command-synthetic','semantic-request-'+'1'*64,
             state,'4'*32,None,json.dumps({'total_tokens':20}),None,time.time(),time.time(),None,None))
        if fee_only:
            db.execute('INSERT INTO semantic_ui_bindings VALUES(?,?,?,?,?,?,?,?)',
                (plan_id,'5'*64,json.dumps(c.owner),json.dumps(catalog),'root',0,time.time(),time.time()))
    return plan_id


def test_no_effects_review_observes_only_and_preserves_published_ancestor_barriers(nofx_case):
    c = nofx_case
    before = ledger_hashes(c.directory)
    posts, submits = c.backend.posts, c.old_adapter.submits
    result = c.lane.review_no_effects_followup(SOURCE)
    assert result['source_id'] == SOURCE and result['dispatchable'] is False
    assert result['status'] == 'followup_no_effects_review_queued'
    assert result['attempts_preserved'] == 2 and c.row['state'] == 'fresh'
    assert len(c.calls['commit']) == 1 and c.calls['read'] == 1 and c.calls['identity'] >= 1
    review = c.calls['commit'][0]
    assert review['root_task_id'] == c.anchor['root_task_id']
    assert review['root_agent_task_id'] == c.anchor['root_agent_task_id']
    assert review['ui_reference_id'] == c.envelope['reference_id']
    assert c.calls['model'] == 0 and not c.lane._pending
    assert ledger_hashes(c.directory) == before
    assert (c.backend.posts,c.old_adapter.submits) == (posts,submits)


def test_any_overlapping_semantic_state_or_fee_only_alias_blocks_review(nofx_case):
    c = nofx_case
    for state in ('reserved','unknown','held','budget_blocked','complete'):
        add_semantic(c,state,fee_only=state == 'unknown')
        reject(c)
        with c.semantic._connection(write=True) as db:
            db.execute('DELETE FROM semantic_ui_bindings')
            db.execute('DELETE FROM semantic_plans')
    assert c.calls['model'] == 0


def clear_budget(c):
    with c.budget._db(write=True) as db:
        db.execute('DELETE FROM usage_budget_events')
        db.execute('DELETE FROM usage_budget_requests')


def test_since_source_all_global_budget_outcomes_and_orphan_events_block(nofx_case, monkeypatch):
    c = nofx_case
    monkeypatch.setattr(c.budget,'_clock',lambda:datetime.fromtimestamp(c.row['first_seen']+2,timezone.utc))
    for state in ('reserved','unknown','settled'):
        key = 'synthetic-unrelated-'+state
        c.budget.reserve('unrelated-global-scope',key,50)
        if state == 'unknown': c.budget.mark_unknown(key)
        elif state == 'settled': c.budget.settle(key,{'total_tokens':0,'prompt_tokens':0,'completion_tokens':0})
        reject(c)
        clear_budget(c)
    with c.budget._db(write=True) as db:
        db.execute('INSERT INTO usage_budget_events(request_key,event,occurred_at_utc,detail) VALUES(?,?,?,?)',
            ('orphan-unattributable','reserved',
             datetime.fromtimestamp(c.row['first_seen']+2,timezone.utc).isoformat().replace('+00:00','Z'),'{}'))
    reject(c)


@pytest.mark.parametrize('ledger',('queue','agent'))
def test_existing_same_question_with_ui_null_cid_and_text_alias_blocks(nofx_case, ledger):
    c = nofx_case
    root = c.queue.get_task(c.anchor['root_task_id'])
    binding = c.bridge._binding(root)
    detail = child_detail(c.anchor,text=' 请 回顾这段合成视频 ')
    detail['native_comment_id'] = detail['evidence']['native_comment_id'] = None
    envelope = make_followup_envelope(detail,binding)
    if ledger == 'queue':
        c.queue.enqueue(binding,envelope,kind='query',user_id=root['user_id'],
            device_id=root['device_id'],authorization=root['authorization'])
    else:
        c.agent.enqueue(binding.binding_id,envelope)
    reject(c)


def test_paused_run_idle_identity_and_stop_gates_do_not_reach_commit(nofx_case, monkeypatch):
    c = nofx_case
    original = dict(c.state)
    for key, value in (('state','running'),('desired_state','running'),('run_id','b'*32),
                       ('worker_state','processing'),('process_alive',False)):
        c.state.clear(); c.state.update(original); c.state[key] = value
        reject(c)
    c.state.clear(); c.state.update(original)
    with monkeypatch.context() as patch:
        patch.setattr(c.lane.native_receiver,'reverify_identity_checkpoint',lambda:False)
        reject(c)
    c.stopped[0] = True
    reject(c)


def test_no_unique_anchor_or_invalid_fresh_ui_never_reuses_old_proof(nofx_case, monkeypatch):
    c = nofx_case
    for anchors in ([],[c.anchor,c.anchor]):
        with monkeypatch.context() as patch:
            patch.setattr(c.bridge,'followup_anchors',lambda candidate:deepcopy(anchors))
            reject(c)
    with monkeypatch.context() as patch:
        def wrong(found,observed,**kwargs):
            detail = child_detail(found,text=observed['content'])
            detail['evidence']['prior_reply_visible_unique'] = False
            return detail
        patch.setattr(c.lane.adapter,'read_followup_reply',wrong)
        reject(c)
    assert c.calls['model'] == 0


def test_after_ui_control_source_semantic_budget_and_grant_races_never_commit(nofx_case):
    c = nofx_case
    original_row, original_state = deepcopy(c.row), dict(c.state)
    posts, submits = c.backend.posts, c.old_adapter.submits
    def budget_race():
        with c.budget._db(write=True) as db:
            db.execute('INSERT INTO usage_budget_events(request_key,event,occurred_at_utc,detail) VALUES(?,?,?,?)',
                ('new-orphan-during-ui','reserved',
                 datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),'{}'))
    races = [
        lambda:c.state.update(desired_state='running'),
        lambda:c.row.update(immutable_hash='e'*64),
        lambda:add_semantic(c,'unknown',fee_only=True),
        budget_race,
        lambda:c.registry.revoke_grant(c.migrated['grant_id']),
    ]
    for change in races:
        c.row.clear(); c.row.update(deepcopy(original_row))
        c.state.clear(); c.state.update(original_state)
        c.on_read['on_read'] = change
        try:
            result = c.lane.review_no_effects_followup(SOURCE)
        except (NativeLaneError,NativeStoreError,ReceiverDetailError,ReceiverPageError,ValueError):
            pass
        else:
            assert not result or result.get('status') != 'followup_no_effects_review_queued'
        assert not c.calls['commit'] and c.row['state'] == 'held'
        assert c.calls['model'] == 0 and (c.backend.posts,c.old_adapter.submits) == (posts,submits)
        with c.semantic._connection(write=True) as db:
            db.execute('DELETE FROM semantic_ui_bindings')
            db.execute('DELETE FROM semantic_plans')
        clear_budget(c)


def test_untrusted_semantic_provider_store_or_budget_cannot_certify_zero_effects(nofx_case, monkeypatch):
    c = nofx_case
    with monkeypatch.context() as patch:
        patch.setattr(c.lane.browser_lane,'semantic_resolution_provider',
            SimpleNamespace(store=c.semantic,budget=c.budget))
        reject(c)
    with monkeypatch.context() as patch:
        patch.setattr(c.provider,'store',object())
        reject(c)
    with monkeypatch.context() as patch:
        patch.setattr(c.provider,'budget',object())
        reject(c)


def test_native_inbox_all_states_and_observation_aliases_block_without_processing(nofx_case, monkeypatch):
    from app.assistant.native_semantic_inbox import (NativeSemanticInbox, _BASE,
        native_message_source_key, native_message_digest, validate_native_message)
    c = nofx_case
    inbox = NativeSemanticInbox.prepare(c.directory/'meaning.sqlite3',
        c.row['candidate']['recipient_uid'], c.binding.receiver_profile_url)
    c.provider.native_inbox = inbox
    monkeypatch.setattr(inbox, 'is_processing', lambda: pytest.fail('read audit expired a meaning claim'))
    value = {key: deepcopy(c.row['candidate'].get(key)) for key in _BASE}
    value.update(source_kind='native_notice_candidate', notice_type=45, at_sub_type=2, parent_id_hint=None)
    value = validate_native_message(value)
    for state in ('historical', 'pending', 'claimed', 'understood', 'held'):
        with inbox._connection(write=True) as db:
            db.execute('INSERT INTO messages(source_key,immutable_hash,message,state,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
                (native_message_source_key(value),native_message_digest(value),json.dumps(value),state,time.time(),time.time()))
        reject(c)
        with inbox._connection(write=True) as db:
            db.execute('DELETE FROM messages')
    value.update(content='另一条无关合成文字', native_comment_id='7999999000000000081', native_notice_id='7999999000000000082')
    with inbox._connection(write=True) as db:
        key = native_message_source_key(value)
        db.execute('INSERT INTO messages(source_key,immutable_hash,message,state,first_seen,last_seen) VALUES(?,?,?,?,?,?)',
            (key,native_message_digest(value),json.dumps(value),'pending',time.time(),time.time()))
        db.execute('INSERT INTO observations VALUES(?,?,?,?)',
            (key,c.row['candidate']['native_notice_id'],'synthetic-fingerprint',time.time()))
    reject(c)


def test_invalid_or_naive_budget_time_is_unknown_and_never_skipped(nofx_case):
    c = nofx_case
    for stamp in ('not-a-time', '2026-10-28T10:00:00'):
        with c.budget._db(write=True) as db:
            db.execute('INSERT INTO usage_budget_events(request_key,event,occurred_at_utc,detail) VALUES(?,?,?,?)',
                ('unattributable-orphan','reserved',stamp,'{}'))
        with pytest.raises(NativeLaneError, match='native_recovery_effects_unknown'):
            c.lane.review_no_effects_followup(SOURCE)
        assert not c.calls['commit'] and c.row['state'] == 'held' and c.calls['model'] == 0
        clear_budget(c)


def foreign_paid_history(c):
    """Complete unrelated historical scope; old budget and an actual retained barrier."""
    from app.mention_agent.contracts import Binding
    foreign = Binding('nofx-foreign-binding','nofx-foreign-instance',
        'https://www.douyin.com/user/nofx-foreign-A',
        'https://www.douyin.com/user/nofx-foreign-B',7,True)
    envelope = deepcopy(c.anchor['root_envelope'])
    envelope.update(command_id='nofx-foreign-command',reference_id='nofx-foreign-reference',
        sender_profile_url=foreign.sender_profile_url,receiver_profile_url=foreign.receiver_profile_url,
        video_id='7999999000000000071',native_comment_id='7999999000000000072',
        comment_text='@合成B 另一条完整合法的历史问题')
    c.agent.put_binding(foreign.to_dict())
    task = c.agent.enqueue(foreign.binding_id,envelope)
    claim = c.agent.claim(task['task_id'])
    text = '另一作用域的合成历史回答'
    c.agent.complete(task['task_id'],claim['claim_token'],
        {'kind':'answer','text':text,'source_ids':[],'sources':[]})
    with c.agent._tx() as db:
        db.execute("UPDATE agent_tasks SET state='b_view_visible' WHERE task_id=?",(task['task_id'],))
        db.execute("UPDATE agent_replies SET state='b_view_visible',send_token=?,evidence=? WHERE task_id=?",
            ('f'*48,json.dumps({'submit_gate_passed':True,'exact_body_in_original_thread':True,
                'reply_sha256':hashlib.sha256(text.encode()).hexdigest()}),task['task_id']))
    # Historical deactivation is attribution, never a reason to ignore the row.
    c.agent.deactivate(foreign.binding_id)
    clock = c.budget._clock
    c.budget._clock = lambda:datetime.fromtimestamp(c.row['first_seen']-10,timezone.utc)
    try:
        c.budget.reserve('unrelated-historical-scope','nofx-foreign-paid',50)
        c.budget.settle('nofx-foreign-paid',{'total_tokens':0})
    finally:
        c.budget._clock = clock
    with c.agent._tx() as db:
        reply = dict(db.execute('SELECT * FROM agent_replies WHERE task_id=?',(task['task_id'],)).fetchone())
    return task['task_id'],envelope,reply


def test_complete_different_scope_inactive_history_is_excluded_without_clearing_barrier(nofx_case):
    c = nofx_case
    task_id,envelope,reply = foreign_paid_history(c)
    before = ledger_hashes(c.directory)
    result = c.lane.review_no_effects_followup(SOURCE)
    assert result['status']=='followup_no_effects_review_queued'
    assert c.calls['model']==0 and ledger_hashes(c.directory)==before
    with c.agent._tx() as db:
        after = dict(db.execute('SELECT * FROM agent_replies WHERE task_id=?',(task_id,)).fetchone())
    assert after==reply and after['send_token']=='f'*48


def test_missing_agent_or_reply_attribution_never_hides_old_paid_barrier(nofx_case):
    c = nofx_case
    task_id,envelope,reply = foreign_paid_history(c)
    for field in ('sender_profile_url','receiver_profile_url','video_id','comment_text','reference_id','source_mode','native_comment_id'):
        bad = deepcopy(envelope)
        bad.update(comment_text=c.row['candidate']['content'],native_comment_id=c.row['candidate']['native_comment_id'])
        bad.pop(field)
        with c.agent._tx() as db:
            db.execute('UPDATE agent_tasks SET envelope=? WHERE task_id=?',(json.dumps(bad),task_id))
        with pytest.raises(NativeLaneError,match='native_recovery_effects_unknown'):
            c.lane.review_no_effects_followup(SOURCE)
        assert not c.calls['commit'] and c.row['state']=='held' and c.calls['model']==0
        with c.agent._tx() as db:
            db.execute('UPDATE agent_tasks SET envelope=? WHERE task_id=?',(json.dumps(envelope),task_id))
        bad = json.loads(reply['target'])
        bad.update(comment_text=c.row['candidate']['content'],native_comment_id=c.row['candidate']['native_comment_id'])
        bad.pop(field)
        with c.agent._tx() as db:
            db.execute('UPDATE agent_replies SET target=? WHERE task_id=?',(json.dumps(bad),task_id))
        with pytest.raises(NativeLaneError,match='native_recovery_effects_unknown'):
            c.lane.review_no_effects_followup(SOURCE)
        with c.agent._tx() as db:
            barrier = db.execute('SELECT send_token FROM agent_replies WHERE task_id=?',(task_id,)).fetchone()[0]
            db.execute('UPDATE agent_replies SET target=? WHERE task_id=?',(reply['target'],task_id))
        assert barrier=='f'*48 and not c.calls['commit'] and c.calls['model']==0


def test_exact_source_and_invalid_native_facts_cannot_be_excluded_as_another_scope(nofx_case):
    c = nofx_case
    foreign_paid_history(c)
    for problem in ('exact-source','array-facts','missing-actor-facts'):
        plan = add_semantic(c,'unknown')
        owner = {**c.owner,'sender_profile_url':'https://www.douyin.com/user/nofx-foreign-A',
            'receiver_profile_url':'https://www.douyin.com/user/nofx-foreign-B','video_id':'7999999000000000071'}
        with c.semantic._connection(write=True) as db:
            db.execute('UPDATE semantic_plans SET owner_scope=?,original_text=? WHERE plan_id=?',
                (json.dumps(owner),'另一条合法作用域的文本',plan))
            if problem=='exact-source':
                db.execute('UPDATE semantic_plans SET source_key=? WHERE plan_id=?',(SOURCE,plan))
            else:
                db.execute('UPDATE semantic_plans SET native_facts=? WHERE plan_id=?',
                    (json.dumps([] if problem=='array-facts' else {'native_comment_id':c.row['candidate']['native_comment_id']}),plan))
        with pytest.raises(NativeLaneError,match='native_recovery_effects_present' if problem=='exact-source' else 'native_recovery_effects_unknown'):
            c.lane.review_no_effects_followup(SOURCE)
        assert not c.calls['commit'] and c.row['state']=='held' and c.calls['model']==0
        with c.semantic._connection(write=True) as db:
            db.execute('DELETE FROM semantic_plans')
