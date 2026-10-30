"""Spent choice anchors survive unrelated catalog growth; all ledgers are temporary."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json

import pytest

from app.mention_agent.imports import import_status_proposal
from tests.test_assistant_choice_controller import case, root, publish_fact, Adapter, admit
from tests.test_mention_agent_store import ingestion_observation


def published_child(case):
    original, session, _ = root(case)
    root_agent_id = publish_fact(case, original, session)
    with case.queue._tx() as db:
        db.execute("UPDATE assistant_tasks SET state='b_view_visible',reply_state='b_view_visible',agent_task_id=?,publication_token=? WHERE id=?",
            (root_agent_id, '1'*48, original['id']))
    child = admit(case, case.controller.collect_pending(Adapter(case), 'synthetic-run'))[0]
    case.agent.put_binding(child['binding'])
    old = case.agent.enqueue(child['binding_id'], child['envelope'])
    claim = case.agent.claim(old['task_id'])
    job = case.agent.ensure_ingestion(old['task_id'], claim['claim_token'])
    dispatched = case.agent.reserve_ingestion(old['task_id'], claim['claim_token'])
    case.agent.finish_ingestion_submission(job['job_id'], dispatched['dispatch_token'], 'accepted',
        {'backend_request_id':job['job_id'],'http_status':200})
    done = case.agent.observe_ingestion(job['job_id'], ingestion_observation(job))
    case.agent.complete(old['task_id'], claim['claim_token'], import_status_proposal(done, '生活库'))
    verification = {**child['envelope']['evidence'], **{key:child['envelope'][key] for key in (
        'sender_profile_url','receiver_profile_url','video_id','comment_text','reference_id','native_comment_id')},
        'current_receiver_verified':True,'observed_at_utc':datetime.now(timezone.utc).isoformat()}
    reserved = case.agent.reserve_send(old['task_id'], verification)
    assert reserved is not None
    case.agent.finish_send(old['task_id'], reserved['send_token'], 'b_view_visible', {
        'submit_gate_passed':True,'exact_body_in_original_thread':True,
        'reply_sha256':sha256(reserved['text'].encode()).hexdigest(),
        'observed_at_utc':datetime.now(timezone.utc).isoformat()})
    with case.queue._tx() as db:
        db.execute("UPDATE assistant_tasks SET state='b_view_visible',reply_state='b_view_visible',import_state='succeeded',agent_task_id=?,publication_token=? WHERE id=?",
            (old['task_id'], '2'*48, child['id']))
    return case.queue.get_task(child['id']), case.store.get(session['choice_id']), case.queue.get_task(original['id'])


def test_reproduces_live_catalog_invalidation_but_spent_anchor_survives_read_only(case):
    child, session, _ = published_child(case)
    assert case.controller.authority_current(child)
    assert case.controller.published_followup_anchor_current(child)
    case.catalog.append({'id':99,'name':'无关合成新库'})
    assert not case.controller.authority_current(child)  # Reproduces the old completed-anchor failure.
    before = {path:path.read_bytes() for path in (case.queue.path,case.agent.path,case.store.path,case.registry.path)}
    case.clock[0] += 86400  # A historical publication proof is not a new 120s UI observation.
    assert case.controller.published_followup_anchor_current(child)
    assert not case.controller.before_publish(child)  # No new execute/send authority was granted.
    assert all(path.read_bytes()==saved for path,saved in before.items())


def test_unspent_menu_remains_strict_after_unrelated_catalog_growth(case):
    task, session, _ = root(case)
    from tests.test_assistant_choice_controller import draft
    draft(case,task,session)
    assert case.controller.before_publish(task)
    case.catalog.append({'id':99,'name':'无关合成新库'})
    assert not case.controller.before_publish(task)
    assert not case.controller.published_followup_anchor_current(task)
    assert case.store.get(session['choice_id'])['state']=='needs_review'


@pytest.mark.parametrize('change', ['selected_grant','root_owner','target_deleted','root_cancelled','root_unsettled','user_revoked'])
def test_published_anchor_still_requires_both_live_authorizations(case, change):
    child, session, original = published_child(case)
    case.catalog.append({'id':99,'name':'无关合成新库'})
    if change=='selected_grant':
        case.registry.revoke_grant(session['selected_authorization']['grant_id'])
    elif change=='root_owner':
        case.registry.set_local_owner_access(case.prior['user_id'],case.prior['device_id'],
            query_all_local=False,catalog_read_allowed=False,public_reply_allowed=False,verified_by='synthetic-owner')
    elif change=='target_deleted':
        case.catalog[:]=[item for item in case.catalog if item['id']!=8]
        case.catalog.append({'id':100,'name':'生活库'})  # Same name is never the former ID/scope.
    elif change in {'root_cancelled','root_unsettled'}:
        with case.queue._tx() as db:
            db.execute("UPDATE assistant_tasks SET state=? WHERE id=?",('cancelled' if change=='root_cancelled' else 'queued',original['id']))
    else:
        case.registry.revoke_user(case.prior['user_id'])
    assert not case.controller.published_followup_anchor_current(child)


@pytest.mark.parametrize('change', ['unknown_child','unknown_ask','missing_send','bad_sha','bad_parent','bad_selection_hash',
                                  'wrong_child','bad_immutable','unknown_import','changed_selection','bad_ask_quality'])
def test_unknown_or_broken_historical_chain_is_not_an_anchor(case, change):
    child, session, original = published_child(case)
    case.catalog.append({'id':99,'name':'无关合成新库'})
    if change=='unknown_child':
        with case.agent._tx() as db:
            db.execute("UPDATE agent_replies SET state='delivery_unknown' WHERE task_id=?",(child['agent_task_id'],))
            db.execute("UPDATE agent_tasks SET state='delivery_unknown' WHERE task_id=?",(child['agent_task_id'],))
    elif change=='unknown_ask':
        with case.agent._tx() as db:
            db.execute("UPDATE agent_replies SET state='delivery_unknown' WHERE task_id=?",(original['agent_task_id'],))
            db.execute("UPDATE agent_tasks SET state='delivery_unknown' WHERE task_id=?",(original['agent_task_id'],))
    elif change in {'missing_send','bad_sha','unknown_import'}:
        with case.agent._tx() as db:
            if change=='missing_send':
                db.execute('UPDATE agent_replies SET send_token=NULL WHERE task_id=?',(child['agent_task_id'],))
            elif change=='bad_sha':
                proof={'submit_gate_passed':True,
                    'exact_body_in_original_thread':True,'reply_sha256':'0'*64,'observed_at_utc':datetime.now(timezone.utc).isoformat()}
                db.execute('UPDATE agent_replies SET evidence=? WHERE task_id=?',(json.dumps(proof),child['agent_task_id']))
            else:
                raw=json.loads(db.execute('SELECT proposal FROM agent_replies WHERE task_id=?',(child['agent_task_id'],)).fetchone()[0])
                raw['import_state']='submission_unknown'
                db.execute('UPDATE agent_replies SET proposal=? WHERE task_id=?',(json.dumps(raw),child['agent_task_id']))
    else:
        with case.store._db(True) as db:
            if change in {'bad_parent','bad_ask_quality'}:
                proof=deepcopy(session['ask_proof'])
                proof['root_reference_id' if change=='bad_parent' else 'reference_quality']='synthetic-wrong-parent'
                db.execute('UPDATE choice_sessions SET ask_proof=? WHERE choice_id=?',(json.dumps(proof),session['choice_id']))
            elif change=='bad_selection_hash':
                db.execute('UPDATE choice_selection_refs SET envelope_hash=? WHERE choice_id=?',('0'*64,session['choice_id']))
            elif change=='wrong_child':
                db.execute('UPDATE choice_sessions SET child_task_id=? WHERE choice_id=?',('f'*32,session['choice_id']))
            elif change=='changed_selection':
                value=deepcopy(session['selection_envelope']);value['evidence']['ask_text']='合成错误菜单'
                db.execute('UPDATE choice_sessions SET selection_envelope=? WHERE choice_id=?',(json.dumps(value),session['choice_id']))
            else:
                db.execute('UPDATE choice_sessions SET immutable=? WHERE choice_id=?',('{}',session['choice_id']))
    assert not case.controller.published_followup_anchor_current(child)


def test_caller_cannot_upgrade_a_queued_child_or_change_its_frozen_scope(case):
    child, _, _ = published_child(case)
    altered=deepcopy(child);altered['binding']['knowledge_base_id']=7
    assert not case.controller.published_followup_anchor_current(altered)
    with case.queue._tx() as db:
        db.execute("UPDATE assistant_tasks SET state='queued' WHERE id=?",(child['id'],))
    assert not case.controller.published_followup_anchor_current(child)
