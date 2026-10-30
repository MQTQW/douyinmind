"""Operator-authored FIRST query draft: temporary ledgers, no API/query/model."""
from copy import deepcopy
import hashlib
import json
import os
import time
from types import SimpleNamespace

import pytest

from app.mention_agent.store import (AgentStore,AgentStoreError,_operator_query_draft_permit,
    _OperatorQueryDraftPermit)
from tests.test_mention_agent_store import binding, envelope
from tests.test_conversation_followup_evidence import binding as followup_binding, detail as followup_detail
from app.mention_agent.followup_evidence import make_followup_envelope

QUEUE_ID = '1'*32


def eligible(tmp_path, *, followup=False):
    store = AgentStore(tmp_path/'agent.sqlite3')
    b = followup_binding().to_dict() if followup else binding()
    env = make_followup_envelope(followup_detail(),followup_binding()) if followup else envelope()
    store.put_binding(b)
    task = store.enqueue(b['binding_id'],env)
    claim = store.claim(task['task_id'])
    ref = {'source_id':'资料1','platform_item_id':env['video_id'],'chunk_id':'synthetic:1',
        'text_sha256':hashlib.sha256('合成原文讲解资料整理。'.encode()).hexdigest()}
    for round_number in range(1,5):
        request = 'live-'+format(round_number,'048x')
        store.record_step(task['task_id'],claim['claim_token'],'model_request',
            {'round':round_number,'budget_request_id':request})
        store.record_step(task['task_id'],claim['claim_token'],'model_response',
            {'round':round_number,'budget_request_id':request,
             'usage':{'total_tokens':10,'prompt_tokens':6,'completion_tokens':4}})
        name = 'query_knowledge_base' if round_number == 1 else 'propose_reply'
        store.record_step(task['task_id'],claim['claim_token'],'tool_call',{'round':round_number,'tool_name':name})
        result = ({'round':round_number,'tool_name':name,'status':'ok','knowledge_base_id':b['knowledge_base_id'],
                   'evidence_refs':[ref]} if round_number == 1 else
                  {'round':round_number,'tool_name':name,'status':'error','code':'invalid_public_reply'})
        store.record_step(task['task_id'],claim['claim_token'],'tool_result',result)
    store.needs_review(task['task_id'],claim['claim_token'],'model_round_budget_exceeded')
    prefix = store.freeze_reply_prefix('assistant:'+QUEUE_ID,'少爷')
    snapshot = store.operator_query_draft_snapshot(task['task_id'])
    proposal = {'kind':'answer','text':'少爷，少爷：原文介绍了资料整理的方法。','source_ids':['资料1'],
        'sources':[{'source_id':'资料1','platform_item_id':env['video_id'],'chunk_id':'synthetic:1',
                    'title':'合成资料','url':'https://www.douyin.com/video/'+env['video_id']}]}
    proof = {'operation':'operator_finalize_query_draft','operator_reason':'explicit_operator_grounded_first_reply',
        'queue_task_id':QUEUE_ID,'queue_kind':'query','run_id':'2'*32,'current_pid':os.getpid(),
        'reviewed_at':time.time(),'authorization_current':True,'fresh_ui_verified':True,
        'fresh_ui_digest':'3'*64,'evidence_sha_verified':True,'reply_validated':True,
        'budget_requests_settled':True,'budget_snapshot_digest':'4'*64,
        'reply_style_key':'assistant:'+QUEUE_ID,'prefix_sha256':hashlib.sha256(prefix.encode()).hexdigest()}
    return SimpleNamespace(store=store,task_id=task['task_id'],claim=claim,snapshot=snapshot,proposal=proposal,proof=proof)


def logical(c):
    with c.store._tx() as db:
        return {'task':dict(db.execute('SELECT * FROM agent_tasks WHERE task_id=?',(c.task_id,)).fetchone()),
            'steps':[tuple(row) for row in db.execute('SELECT * FROM agent_steps WHERE task_id=? ORDER BY id',(c.task_id,))],
            'replies':[tuple(row) for row in db.execute('SELECT * FROM agent_replies WHERE task_id=?',(c.task_id,))],
            'marker':db.execute('SELECT value FROM agent_meta WHERE key=?',('operator_query_draft_v1:'+c.task_id,)).fetchone()}


@pytest.mark.parametrize('followup',(False,True))
def test_snapshot_is_readonly_and_commit_creates_one_first_ready_reply_without_replay(tmp_path,followup):
    c = eligible(tmp_path,followup=followup)
    before_bytes = c.store.path.read_bytes()
    assert c.store.operator_query_draft_snapshot(c.task_id) == c.snapshot
    assert c.store.path.read_bytes() == before_bytes
    assert c.snapshot['completed_model_rounds'] == 4 and len(c.snapshot['budget_request_ids']) == 4
    assert len(c.snapshot['recorded_refs']) == 1 and 'claim_token' not in c.snapshot['task']
    before = logical(c)
    permit = _operator_query_draft_permit(c.store,c.snapshot,c.proposal,c.proof)
    result = c.store._commit_operator_query_draft(permit)
    assert result['status'] == 'operator_query_draft_ready' and result['state'] == 'answer_ready'
    assert result['dispatchable'] is False and result['audit_recorded'] is True
    after = logical(c)
    assert after['task']['reason_code'] == 'model_round_budget_exceeded'
    for key in ('claim_token','lease_until','attempts','envelope','immutable_hash'):
        assert after['task'][key] == before['task'][key]
    assert after['steps'] == before['steps'] and len(after['replies']) == 1
    reply = c.store.get_reply(c.task_id)
    assert reply['state'] == 'ready' and reply['send_token'] is None
    assert reply['text'] == '少爷，原文介绍了资料整理的方法。'
    assert reply['target'].get('followup_evidence') == (c.snapshot['envelope']['evidence'] if followup else None)
    audit = json.loads(after['marker'][0])
    assert audit['provenance'] == 'operator_authored_grounded_answer_not_recorded_model_draft'
    assert not audit['model_replayed'] and not audit['query_replayed'] and not audit['ingestion_submitted']
    assert c.claim['claim_token'] not in after['marker'][0]
    with pytest.raises(AgentStoreError):
        c.store._commit_operator_query_draft(permit)


def test_plain_forged_cross_store_or_altered_permits_never_create_reply(tmp_path):
    c = eligible(tmp_path)
    original = logical(c)
    with pytest.raises(AgentStoreError):
        _OperatorQueryDraftPermit(c.store,c.snapshot,c.proposal,c.proof,_brand=object())
    with pytest.raises(AgentStoreError):
        c.store._commit_operator_query_draft({'snapshot':c.snapshot,'proposal':c.proposal,'proof':c.proof})
    other = AgentStore(tmp_path/'other.sqlite3')
    with pytest.raises(AgentStoreError):
        other._commit_operator_query_draft(_operator_query_draft_permit(c.store,c.snapshot,c.proposal,c.proof))
    for field in ('snapshot','proposal','proof'):
        permit = _operator_query_draft_permit(c.store,c.snapshot,c.proposal,c.proof)
        if field == 'snapshot': permit.snapshot['steps_sha256'] = 'a'*64
        elif field == 'proposal': permit.proposal['text'] = '被替换的正文'
        else: permit.proof['reviewed_at'] += 1
        with pytest.raises(AgentStoreError):
            c.store._commit_operator_query_draft(permit)
    assert logical(c) == original


def test_proof_false_stale_pid_prefix_and_unrecorded_sources_fail_without_state_change(tmp_path):
    c = eligible(tmp_path)
    original = logical(c)
    for key,value in (('authorization_current',False),('fresh_ui_verified',False),('evidence_sha_verified',False),
                      ('reply_validated',False),('budget_requests_settled',False),('queue_kind','save'),
                      ('current_pid',os.getpid()+1),('reviewed_at',time.time()-121),('prefix_sha256','0'*64),
                      ('reply_style_key','assistant:'+'9'*32)):
        proof = {**c.proof,key:value}
        with pytest.raises(AgentStoreError):
            c.store._commit_operator_query_draft(_operator_query_draft_permit(c.store,c.snapshot,c.proposal,proof))
    for change in ('unknown_id','wrong_chunk','wrong_video','bad_body','duplicate_sources'):
        proposal = deepcopy(c.proposal)
        if change == 'unknown_id': proposal['source_ids'] = ['未记录来源']
        elif change == 'wrong_chunk': proposal['sources'][0]['chunk_id'] = 'other:2'
        elif change == 'wrong_video': proposal['sources'][0]['platform_item_id'] = '7999999000000000099'
        elif change == 'bad_body': proposal['text'] = '@其他账号 https://invalid.example'
        else: proposal['sources'].append(deepcopy(proposal['sources'][0]))
        with pytest.raises(AgentStoreError):
            c.store._commit_operator_query_draft(_operator_query_draft_permit(c.store,c.snapshot,proposal,c.proof))
    assert logical(c) == original


def test_task_steps_binding_or_prefix_change_invalidates_snapshot(tmp_path):
    c = eligible(tmp_path)
    permit = _operator_query_draft_permit(c.store,c.snapshot,c.proposal,c.proof)
    with c.store._tx() as db:
        step = db.execute('SELECT id,metadata FROM agent_steps WHERE task_id=? ORDER BY id DESC LIMIT 1',(c.task_id,)).fetchone()
        meta = json.loads(step['metadata']);meta['elapsed_ms'] = 1
        db.execute('UPDATE agent_steps SET metadata=? WHERE id=?',(json.dumps(meta),step['id']))
    with pytest.raises(AgentStoreError,match='snapshot_changed'):
        c.store._commit_operator_query_draft(permit)
    assert c.store.get_reply(c.task_id) is None


def test_other_lifecycle_states_reasons_and_inactive_binding_are_not_eligible(tmp_path):
    c = eligible(tmp_path)
    for state,reason in [('processing','model_round_budget_exceeded'),('queued','model_round_budget_exceeded'),
        ('answer_ready','model_round_budget_exceeded'),('delivery_unknown','model_round_budget_exceeded'),
        ('send_reserved','model_round_budget_exceeded'),('needs_review','knowledge_query_failed'),
        ('needs_review','model_budget_unknown'),('needs_review','processing_lease_expired')]:
        # Deliberate corruption/alternative lifecycle in this owned TEMP fixture only.
        with c.store._tx() as db:
            db.execute('UPDATE agent_tasks SET state=?,reason_code=? WHERE task_id=?',(state,reason,c.task_id))
        with pytest.raises(AgentStoreError):
            c.store.operator_query_draft_snapshot(c.task_id)
    with c.store._tx() as db:
        db.execute("UPDATE agent_tasks SET state='needs_review',reason_code='model_round_budget_exceeded' WHERE task_id=?",(c.task_id,))
        raw = db.execute('SELECT payload FROM agent_bindings').fetchone()[0]
        b = json.loads(raw);b['active'] = False
        db.execute('UPDATE agent_bindings SET payload=?',(json.dumps(b),))
    with pytest.raises(AgentStoreError):
        c.store.operator_query_draft_snapshot(c.task_id)


def test_incomplete_or_unknown_model_request_and_bad_query_evidence_never_certify_snapshot(tmp_path):
    c = eligible(tmp_path)
    with c.store._tx() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM agent_steps WHERE task_id=? ORDER BY id',(c.task_id,))]
    response = next(row for row in rows if row['kind'] == 'model_response')
    query = next(row for row in rows if row['kind'] == 'tool_result')
    last = rows[-1]
    cases = [(response,lambda meta:meta.pop('budget_request_id')),
             (response,lambda meta:meta.update(budget_request_id='live-'+'9'*48)),
             (response,lambda meta:meta.update(usage={'total_tokens':True})),
             (query,lambda meta:meta.update(evidence_refs=[])),
             (last,lambda meta:meta.update(code='invalid_source_ids'))]
    for row,change in cases:
        meta = json.loads(row['metadata']);change(meta)
        with c.store._tx() as db:
            db.execute('UPDATE agent_steps SET metadata=? WHERE id=?',(json.dumps(meta),row['id']))
        with pytest.raises(AgentStoreError):
            c.store.operator_query_draft_snapshot(c.task_id)
        with c.store._tx() as db:
            db.execute('UPDATE agent_steps SET metadata=? WHERE id=?',(row['metadata'],row['id']))
    with c.store._tx() as db:
        db.execute('INSERT INTO agent_steps(task_id,kind,metadata,created_at) VALUES(?,?,?,?)',
            (c.task_id,'model_request',json.dumps({'round':5,'budget_request_id':'live-'+'8'*48}),time.time()))
    with pytest.raises(AgentStoreError):
        c.store.operator_query_draft_snapshot(c.task_id)
    assert c.store.get_reply(c.task_id) is None


def test_existing_draft_send_barrier_ingestion_or_spent_marker_cannot_be_reopened(tmp_path):
    c = eligible(tmp_path)
    original = logical(c)
    permit = _operator_query_draft_permit(c.store,c.snapshot,c.proposal,c.proof)
    for state,token in (('ready',None),('reserved','a'*48),('delivery_unknown','b'*48),('b_view_visible','c'*48)):
        with c.store._tx() as db:
            db.execute('INSERT INTO agent_replies(reply_id,task_id,proposal,target,state,created_at,send_token) VALUES(?,?,?,?,?,?,?)',
                ('9'*32,c.task_id,json.dumps(c.proposal),'{}',state,time.time(),token))
        before = logical(c)
        with pytest.raises(AgentStoreError):
            c.store._commit_operator_query_draft(permit)
        assert logical(c) == before
        with c.store._tx() as db:
            db.execute('DELETE FROM agent_replies WHERE task_id=?',(c.task_id,))
    for table in ('agent_task_ingestions','agent_query_retries'):
        with c.store._tx() as db:
            db.execute('CREATE TABLE '+table+'(task_id TEXT)')
            db.execute('INSERT INTO '+table+' VALUES(?)',(c.task_id,))
        with pytest.raises(AgentStoreError):
            c.store.operator_query_draft_snapshot(c.task_id)
        with c.store._tx() as db:
            db.execute('DROP TABLE '+table)
    with c.store._tx() as db:
        db.execute('INSERT INTO agent_meta(key,value) VALUES(?,?)',('operator_query_draft_v1:'+c.task_id,'{}'))
    with pytest.raises(AgentStoreError,match='already_used'):
        c.store.operator_query_draft_snapshot(c.task_id)
    assert c.store.get_reply(c.task_id) is None
    assert logical(c)['steps'] == original['steps']


def test_first_reply_marker_prefix_and_ready_transition_are_one_atomic_transaction(tmp_path):
    c = eligible(tmp_path)
    original = logical(c)
    permit = _operator_query_draft_permit(c.store,c.snapshot,c.proposal,c.proof)
    with c.store._tx() as db:
        db.execute("CREATE TRIGGER synthetic_operator_abort BEFORE INSERT ON agent_replies BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    with pytest.raises(Exception):
        c.store._commit_operator_query_draft(permit)
    assert logical(c) == original
    assert c.store.reply_prefix('agent:'+c.task_id) is None
    with c.store._tx() as db:
        db.execute('DROP TRIGGER synthetic_operator_abort')
    assert c.store._commit_operator_query_draft(permit)['state'] == 'answer_ready'
    assert c.store.get_reply(c.task_id)['send_token'] is None
