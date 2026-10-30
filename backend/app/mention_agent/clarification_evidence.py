"""Physical reply proof for a separately armed intent-clarification session.

Projection through the existing thread validator validates geometry only. It
does not make this an ordinary query followup or a real @ notification.
"""
from copy import deepcopy
import hashlib
import math
import re

from .contracts import canonical_json
from .followup_evidence import make_followup_envelope, validate_followup_envelope, PROOF_REQUIRED

SOURCE_KIND = 'intent_clarification_reply'
EXTRA = frozenset({'clarification_session_id', 'clarification_root_hash', 'clarification_activated_at',
                   'clarification_capability', 'physical_reference_id', 'allowed_actions'})


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _project(value, binding=None):
    envelope = deepcopy(value)
    proof = envelope['evidence']
    physical = proof['physical_reference_id']
    for key in EXTRA: proof.pop(key)
    proof.update(source_kind='conversation_followup', query_only=True)
    envelope['reference_id'] = physical
    if binding is not None:
        binding_id=binding.get('binding_id') if isinstance(binding,dict) else binding.binding_id
        envelope['command_id']='command:'+_digest({'binding_id':binding_id,
            'instance_id':proof['instance_id'],'reference_id':physical,
            'comment_text':envelope['comment_text'],'source_mode':envelope['source_mode']})
    return envelope


def validate_clarification_envelope(value, binding=None):
    if type(value) is not dict or type(value.get('evidence')) is not dict:
        raise ValueError('invalid_clarification_evidence')
    proof = value['evidence']
    expected = (PROOF_REQUIRED - {'query_only'}) | EXTRA
    if (set(proof) != expected or proof.get('source_kind') != SOURCE_KIND
            or proof.get('clarification_capability') != 'intent_clarification_v1'
            or any(type(proof.get(key)) is not str or not re.fullmatch(r'[a-f0-9]{64}', proof[key])
                for key in ('clarification_session_id', 'clarification_root_hash'))
            or type(proof.get('clarification_activated_at')) not in (int, float)
            or not math.isfinite(proof['clarification_activated_at']) or proof['clarification_activated_at'] <= 0
            or proof.get('allowed_actions') != ['save', 'clarify', 'cancel']
            or type(proof.get('physical_reference_id')) is not str):
        raise ValueError('invalid_clarification_evidence')
    expected_reference = 'clarification-reply:' + _digest({'session': proof['clarification_session_id'],
        'physical_reference': proof['physical_reference_id']})
    if value.get('reference_id') != expected_reference:
        raise ValueError('clarification_reference_conflict')
    if value.get('command_id')!='command-'+_digest({'session':proof['clarification_session_id'],'reference':expected_reference}):
        raise ValueError('clarification_command_conflict')
    validate_followup_envelope(_project(value,binding), binding)
    return deepcopy(value)


def make_clarification_envelope(detail, binding, session):
    physical = make_followup_envelope(detail, binding)
    root, published = session['root'], session['published_reply']
    proof = physical['evidence']
    if (proof['root_task_id'] != root['id']
            or proof['root_agent_task_id'] != published['agent_task_id']
            or any(physical.get(key)!=root['envelope'][key] for key in ('sender_profile_url','receiver_profile_url','video_id'))
            or proof['root_reference_id'] != root['envelope']['reference_id']
            or proof['root_comment_text'] != root['envelope']['comment_text']
            or proof['root_native_comment_id'] not in (None,root['source_native_comment_id'])
            or proof['prior_reply_sha256'] != published['text_sha256']
            or proof['prior_reply_text'] != published['text']):
        raise ValueError('clarification_parent_conflict')
    template = {'clarification_session_id': session['session_id'], 'clarification_root_hash': session['immutable_hash'],
        'clarification_activated_at': root['activated_at'], 'clarification_capability': 'intent_clarification_v1',
        'allowed_actions': list(root['allowed_actions'])}
    return rebuild_clarification_envelope(detail, binding, template)


def rebuild_clarification_envelope(detail, binding, template):
    physical = make_followup_envelope(detail, binding)
    value = deepcopy(physical)
    value['evidence'].pop('query_only')
    value['evidence'].update({key: deepcopy(template[key]) for key in EXTRA - {'physical_reference_id'}})
    value['evidence'].update(source_kind=SOURCE_KIND, physical_reference_id=physical['reference_id'])
    value['reference_id'] = 'clarification-reply:' + _digest({'session': template['clarification_session_id'],
        'physical_reference': physical['reference_id']})
    value['command_id'] = 'command-' + _digest({'session': template['clarification_session_id'], 'reference': value['reference_id']})
    return validate_clarification_envelope(value, binding)


def ui_projection(value):
    validate_clarification_envelope(value)
    return _project(value)
