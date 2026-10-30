"""Paused browser-owner first-draft finalization; no model, retrieval or send."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import threading
import time

from app.mention_agent.contracts import canonical_json


class QueryDraftOwnerError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _scope(bridge, task_id):
    control = bridge.control.snapshot()
    manual = control.get('manual_verification')
    if (threading.get_ident() != bridge.browser_owner_thread_id
            or control.get('pid') != os.getpid() or control.get('process_alive') is not True
            or control.get('state') != 'paused' or control.get('desired_state') != 'paused'
            or control.get('worker_state') != 'idle' or type(manual) is not dict
            or manual.get('active') is not False or bridge.should_stop()):
        raise QueryDraftOwnerError('operator_query_draft_not_safe')
    task = bridge.queue.get_task(task_id)
    if (task is None or task.get('kind') != 'query' or task.get('state') != 'needs_review'
            or task.get('reason_code') != 'model_round_budget_exceeded'
            or task.get('publication_token') is not None
            or type(task.get('publish_attempts', 0)) is not int or task.get('publish_attempts', 0) != 0
            or bridge._authorized(task) is not True):
        raise QueryDraftOwnerError('operator_query_draft_scope_changed')
    envelope = task.get('envelope', {})
    if envelope.get('evidence', {}).get('source_kind') != 'conversation_followup':
        raise QueryDraftOwnerError('operator_query_draft_target_unsupported')
    return task, bridge._binding(task), control


def _budget(bridge, task, snapshot):
    """Existing settled requests only; never reserve or alter their accounting."""
    from .semantic_plan_store import decision_metadata, RecordedSemanticDecision
    receipt = bridge._semantic_receipt(task)
    if type(receipt) is not RecordedSemanticDecision or bridge.usage_budget is None:
        raise QueryDraftOwnerError('operator_query_draft_budget_unverified')
    metadata = decision_metadata(receipt)
    if metadata.get('action') != 'query':
        raise QueryDraftOwnerError('operator_query_draft_budget_unverified')
    identifiers = snapshot.get('budget_request_ids')
    if (type(identifiers) is not list or not identifiers or len(identifiers) != len(set(identifiers))
            or any(type(key) is not str or not key for key in identifiers)):
        raise QueryDraftOwnerError('operator_query_draft_budget_unverified')
    wanted = [metadata['request_key'], *identifiers]
    if len(wanted) != len(set(wanted)):
        raise QueryDraftOwnerError('operator_query_draft_budget_unverified')
    from .legacy_bridge import _followup_owned_ro
    with _followup_owned_ro(bridge.agent) as db:
        response_rows = list(db.execute("SELECT metadata FROM agent_steps WHERE task_id=? AND kind='model_response' ORDER BY id",
                                       (task['agent_task_id'],)))
    usages = {}
    for response in response_rows:
        value = json.loads(response[0])
        key, usage = value.get('budget_request_id'), value.get('usage')
        if (type(key) is not str or key in usages or type(usage) is not dict
                or type(usage.get('total_tokens')) is not int or usage['total_tokens'] < 0):
            raise QueryDraftOwnerError('operator_query_draft_budget_unverified')
        usages[key] = usage
    if set(usages) != set(identifiers):
        raise QueryDraftOwnerError('operator_query_draft_budget_unverified')
    with bridge.usage_budget._db() as db:
        scope_rows = list(db.execute('SELECT request_key FROM usage_budget_requests WHERE task_id=?',
                                    (metadata['budget_scope_id'],)))
        if {row[0] for row in scope_rows} != set(wanted):
            raise QueryDraftOwnerError('operator_query_draft_budget_unverified')
        rows = []
        for key in wanted:
            row = db.execute('SELECT * FROM usage_budget_requests WHERE request_key=?', (key,)).fetchone()
            if row is None:
                raise QueryDraftOwnerError('operator_query_draft_budget_unverified')
            value = dict(row)
            if (value['task_id'] != metadata['budget_scope_id'] or value['state'] != 'settled'
                    or value['outcome'] != 'completed' or type(value['total_tokens']) is not int
                    or value['total_tokens'] < 0):
                raise QueryDraftOwnerError('operator_query_draft_budget_unverified')
            rows.append(value)
    def usage_matches(row, usage):
        for column, names in (('total_tokens', ('total_tokens',)),
                              ('input_tokens', ('input_tokens', 'prompt_tokens')),
                              ('output_tokens', ('output_tokens', 'completion_tokens'))):
            recorded = [usage[name] for name in names if name in usage]
            if (not recorded or any(type(number) is not int or number < 0 for number in recorded)
                    or len(set(recorded)) != 1 or type(row[column]) is not int
                    or row[column] != recorded[0]):
                return False
        return row['total_tokens'] == row['input_tokens'] + row['output_tokens']
    if not usage_matches(rows[0], metadata['usage']) or any(
            not usage_matches(row, usages[row['request_key']]) for row in rows[1:]):
        raise QueryDraftOwnerError('operator_query_draft_budget_unverified')
    # The Agent snapshot independently checks every completed response and its
    # request ID. Preserve the settled ledger, including all failed text rounds.
    return _digest({'scope': metadata['budget_scope_id'], 'requests': rows, 'completed_responses': usages})


def _fresh_ui(bridge, task, binding):
    from app.mention_receiver.followup_browser import _target_args
    from app.mention_agent.followup_evidence import make_followup_envelope
    from app.mention_agent.store import _verified_target
    from .semantic_plan_store import ui_semantic_digest
    envelope = deepcopy(task['envelope'])
    target = {key: value for key, value in envelope.items() if key != 'evidence'}
    target.update(binding_id=binding.binding_id, instance_id=binding.instance_id,
                  knowledge_base_id=binding.knowledge_base_id,
                  followup_evidence=envelope['evidence'])
    if binding.is_all_local:
        target['query_scope'] = 'all_local'
    anchor, candidate, _ = _target_args(target)
    detail = bridge.adapter.read_followup_reply(anchor, candidate, keep_open=True)
    fresh = make_followup_envelope(detail, binding)
    verification = {key: fresh[key] for key in ('sender_profile_url', 'receiver_profile_url',
        'video_id', 'comment_text', 'reference_id', 'native_comment_id')}
    verification.update(fresh['evidence'])
    stamp = datetime.fromisoformat(fresh['evidence']['observed_at_utc'].replace('Z', '+00:00'))
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    if not -5 <= age <= 120 or not _verified_target(target, verification, binding.to_dict()):
        raise QueryDraftOwnerError('operator_query_draft_ui_unverified')
    return ui_semantic_digest(fresh)


def finalize_query_draft(bridge, task_id, *, expected_candidate_sha256, reverify_identity, fetch_bundle=None):
    """Create only the first ready draft; ordinary reconcile/resume publishes."""
    from .operator_draft_service import (fetch_operator_draft_bundle,
        operator_snapshot_digest, validate_operator_proposal)
    from .legacy_bridge import _followup_owned_ro
    from app.mention_agent.store import _operator_query_draft_permit
    if (type(task_id) is not str or not re.fullmatch(r'[a-f0-9]{32}', task_id)
            or type(expected_candidate_sha256) is not str or not re.fullmatch(r'[a-f0-9]{64}', expected_candidate_sha256)
            or not callable(reverify_identity)):
        raise QueryDraftOwnerError('operator_query_draft_not_safe')
    fetch = fetch_operator_draft_bundle if fetch_bundle is None else fetch_bundle
    with bridge.admission_lock:
        task, binding, control = _scope(bridge, task_id)
        reverify_identity()
        task, binding, control = _scope(bridge, task_id)
        snapshot = bridge.agent.operator_query_draft_snapshot(task['agent_task_id'])
        bundle = fetch(task_id)
        style_key = 'assistant:' + task_id
        with _followup_owned_ro(bridge.agent) as db:
            prefix = bridge.agent._reply_prefix(db, style_key)
        if prefix is None:
            raise QueryDraftOwnerError('operator_query_draft_style_missing')
        expected = operator_snapshot_digest(task, snapshot, task['authorization'], binding,
            style_key, prefix, control['run_id'], binding.receiver_profile_url)
        if (bundle.get('task_id') != task_id or bundle.get('agent_task_id') != task['agent_task_id']
                or bundle.get('candidate_sha256') != expected_candidate_sha256
                or bundle.get('run_id') != control['run_id'] or bundle.get('snapshot_digest') != expected
                or bundle.get('reply_style_key') != style_key or bundle.get('prefix') != prefix):
            raise QueryDraftOwnerError('operator_query_draft_snapshot_changed')
        evidence = bundle.get('evidence')
        refs = snapshot['recorded_refs']
        if type(evidence) is not list or len(evidence) != len(refs):
            raise QueryDraftOwnerError('operator_query_draft_evidence_changed')
        by_source = {item['source_id']: item for item in refs}
        if len(by_source) != len(refs):
            raise QueryDraftOwnerError('operator_query_draft_evidence_changed')
        seen = set()
        for item in evidence:
            ref = by_source.get(item.get('source_id')) if type(item) is dict else None
            if (ref is None or item['source_id'] in seen or type(item.get('text')) is not str
                    or item.get('platform_item_id') != task['envelope']['video_id']
                    or any(item.get(key) != ref[key] for key in ('platform_item_id', 'chunk_id', 'text_sha256'))
                    or hashlib.sha256(item['text'].encode('utf-8')).hexdigest() != ref['text_sha256']):
                raise QueryDraftOwnerError('operator_query_draft_evidence_changed')
            seen.add(item['source_id'])
        maximum = bundle.get('effective_max_chars')
        if type(maximum) is not int or not 1 <= maximum <= 320 - len(prefix):
            raise QueryDraftOwnerError('operator_query_draft_text_unverified')
        resolution = bridge._resolution(task)
        if resolution is None or resolution.action != 'query':
            raise QueryDraftOwnerError('operator_query_draft_scope_changed')
        proposal = validate_operator_proposal(bundle['proposal'], evidence, prefix, maximum,
            comment_text=task['envelope']['comment_text'], library_name=resolution.library['name'])
        budget_hash = _budget(bridge, task, snapshot)
        ui_hash = _fresh_ui(bridge, task, binding)
        reverify_identity()
        current, current_binding, current_control = _scope(bridge, task_id)
        again = bridge.agent.operator_query_draft_snapshot(task['agent_task_id'])
        latest = fetch(task_id)
        if (current != task or current_binding != binding or current_control['run_id'] != control['run_id']
                or again != snapshot or _budget(bridge, current, again) != budget_hash
                or any(latest.get(key) != bundle.get(key) for key in
                    ('snapshot_digest', 'evidence_digest', 'candidate_sha256', 'proposal', 'evidence'))):
            raise QueryDraftOwnerError('operator_query_draft_snapshot_changed')
        proof = {'operation': 'operator_finalize_query_draft',
            'operator_reason': 'explicit_operator_grounded_first_reply',
            'queue_task_id': task_id, 'queue_kind': 'query', 'run_id': control['run_id'],
            'current_pid': os.getpid(), 'reviewed_at': time.time(),
            'authorization_current': True, 'fresh_ui_verified': True, 'fresh_ui_digest': ui_hash,
            'evidence_sha_verified': True, 'reply_validated': True,
            'budget_requests_settled': True, 'budget_snapshot_digest': budget_hash,
            'reply_style_key': style_key, 'prefix_sha256': hashlib.sha256(prefix.encode('utf-8')).hexdigest()}
        bridge.agent._commit_operator_query_draft(
            _operator_query_draft_permit(bridge.agent, snapshot, proposal, proof))
        return {'source_id': 'assistant-draft:' + task_id + ':' + expected_candidate_sha256,
                'status': 'operator_query_draft_finalized',
                'dispatchable': False}
