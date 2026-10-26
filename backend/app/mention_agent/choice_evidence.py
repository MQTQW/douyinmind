"""Bounded public evidence for one A reply choosing a pending save library.

These structural checks never grant an import. The persistent choice controller
must separately recheck its published prompt, single pending root, frozen option
and fresh registry authorization before admission and before dispatch.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import re


ENVELOPE_KEYS = frozenset({
    'command_id', 'reference_id', 'reference_quality', 'native_comment_id',
    'sender_profile_url', 'receiver_profile_url', 'video_id', 'comment_text',
    'source_mode', 'evidence',
})
PROOF_REQUIRED = frozenset({
    'source_kind', 'evidence_version', 'choice_id', 'instance_id', 'real_mention',
    'sender_verified', 'receiver_verified', 'video_verified', 'unique_count',
    'direct_reply_to_ask', 'root_chain_verified', 'parent_scope',
    'sender_profile_url', 'receiver_profile_url', 'video_id',
    'root_reference_id', 'root_comment_text', 'ask_ref', 'ask_reply_ref', 'ask_text',
    'selection_reference_id', 'selection_text', 'observed_at_utc',
    'unique_pending_choice', 'ask_visible_unique', 'receiver_reply_target_verified',
})
PROOF_OPTIONAL = frozenset({'diagnostics', 'native_comment_id', 'root_native_comment_id', 'ask_native_comment_id',
                            'selection_value', 'receiver_reply_label'})
PARENT_SCOPES = frozenset({'verified_ask_parent', 'unique_pending_choice_in_verified_root'})


def _text(value, limit=256):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('invalid_choice_evidence_text')
    return value


def _value(value, key):
    return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)


def _normal(value):
    return re.sub(r'\s+', '', value)


def _identifier(namespace, value):
    from .contracts import canonical_json
    return namespace + ':' + hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def choice_ask_reference(*, root_reference_id, ask_ref, ask_text, receiver_profile_url, video_id):
    """Stable, explicitly UI-based B prompt reference, never a native ID."""
    return _identifier('verified-ui-ask', {
        'root_reference_id': _text(root_reference_id), 'ask_ref': _text(ask_ref),
        'ask_text': _text(ask_text, 320), 'receiver': receiver_profile_url, 'video_id': video_id})


def choice_reference_id(*, sender, receiver, video_id, native_comment_id,
                        comment_text, root_reference_id, ask_reply_ref):
    identity = {'receiver': receiver, 'sender': sender, 'video': video_id,
                'native_comment_id': native_comment_id,
                'ui_body': None if native_comment_id is not None else _normal(comment_text)}
    if native_comment_id is None:
        # Explicitly a UI observation of a parent-scoped reply, never a made-up
        # platform/native comment ID. Identical text in different prompts differs.
        identity['verified_ui_parent'] = {'root_reference_id': root_reference_id,
                                          'ask_reply_ref': ask_reply_ref}
    return _identifier('reference', identity)


def validate_choice_envelope(envelope, binding=None, *, root_binding=None, root_envelope=None,
                             ask_ref=None, ask_reply_ref=None, ask_text=None, choice_id=None):
    from .contracts import canonical_json, public_profile
    if not isinstance(envelope, dict) or set(envelope) != ENVELOPE_KEYS:
        raise ValueError('invalid_choice_envelope_fields')
    e = envelope
    for key in ('command_id', 'reference_id'):
        _text(e[key])
    if e['source_mode'] not in {'live_ui', 'replay', 'isolated'}:
        raise ValueError('invalid_choice_source_mode')
    sender, receiver = public_profile(e['sender_profile_url']), public_profile(e['receiver_profile_url'])
    if sender == receiver:
        raise ValueError('choice_sender_is_assistant')
    if not isinstance(e['video_id'], str) or not re.fullmatch(r'\d{5,30}', e['video_id']):
        raise ValueError('invalid_choice_video')
    _text(e['comment_text'], 4000)
    native = e['native_comment_id']
    if native is not None and (not isinstance(native, str) or not re.fullmatch(r'\d{5,30}', native)):
        raise ValueError('invalid_choice_native_id')
    if e['reference_quality'] != ('native' if native is not None else 'verified_ui'):
        raise ValueError('choice_reference_quality_conflict')
    proof = e['evidence']
    if (not isinstance(proof, dict) or not PROOF_REQUIRED <= set(proof)
            or set(proof) - PROOF_REQUIRED - PROOF_OPTIONAL
            or proof['source_kind'] != 'library_choice_reply'
            or type(proof['evidence_version']) is not int or proof['evidence_version'] != 1):
        raise ValueError('invalid_choice_proof_fields')
    if type(proof['real_mention']) is not bool:
        raise ValueError('choice_mention_observation_must_be_boolean')
    for key in ('sender_verified', 'receiver_verified', 'video_verified', 'root_chain_verified',
                'unique_pending_choice', 'ask_visible_unique', 'receiver_reply_target_verified'):
        if proof[key] is not True:
            raise ValueError('choice_parent_or_identity_not_verified')
    if type(proof['unique_count']) is not int or proof['unique_count'] != 1:
        raise ValueError('choice_reply_not_unique')
    if proof['parent_scope'] not in PARENT_SCOPES:
        raise ValueError('unknown_choice_parent_scope')
    if type(proof['direct_reply_to_ask']) is not bool or proof['direct_reply_to_ask'] != (
            proof['parent_scope'] == 'verified_ask_parent'):
        raise ValueError('choice_parent_quality_overclaimed')
    if not isinstance(proof['choice_id'], str) or not re.fullmatch(r'[a-f0-9]{32,64}', proof['choice_id']):
        raise ValueError('invalid_choice_id')
    if not isinstance(proof['instance_id'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', proof['instance_id']):
        raise ValueError('invalid_choice_instance')
    for key in ('root_reference_id', 'ask_ref', 'ask_reply_ref', 'selection_reference_id'):
        _text(proof[key])
    _text(proof['root_comment_text'], 4000)
    _text(proof['ask_text'], 320)
    if (proof['sender_profile_url'] != sender or proof['receiver_profile_url'] != receiver
            or proof['video_id'] != e['video_id'] or proof['selection_text'] != e['comment_text']
            or proof['selection_reference_id'] != e['reference_id']
            or e['reference_id'] in {proof['root_reference_id'], proof['ask_ref'], proof['ask_reply_ref']}):
        raise ValueError('choice_proof_scope_conflict')
    for key in ('native_comment_id', 'root_native_comment_id', 'ask_native_comment_id'):
        value = proof.get(key)
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r'\d{5,30}', value)):
            raise ValueError('invalid_choice_parent_native_id')
    if 'native_comment_id' in proof and proof['native_comment_id'] != native:
        raise ValueError('choice_native_evidence_conflict')
    if native is not None and native in {proof.get('root_native_comment_id'), proof.get('ask_native_comment_id')}:
        raise ValueError('choice_reply_is_its_own_parent')
    try:
        observed = datetime.fromisoformat(proof['observed_at_utc'].replace('Z', '+00:00'))
    except (AttributeError, TypeError, ValueError):
        raise ValueError('invalid_choice_observation_time') from None
    if observed.tzinfo is None:
        raise ValueError('choice_observation_timezone_required')
    if len(canonical_json(proof)) > 16000:
        raise ValueError('choice_proof_too_large')
    if 'selection_value' in proof or 'receiver_reply_label' in proof:
        from app.assistant.choice_intent import normalized_choice, selection_value_from_public_text
        if not {'selection_value', 'receiver_reply_label'} <= set(proof):
            raise ValueError('choice_reply_value_and_label_must_pair')
        label = proof['receiver_reply_label']
        if not isinstance(label, str) or not 1 < len(label) <= 80 or not label.startswith('@'):
            raise ValueError('invalid_choice_receiver_reply_label')
        expected_value = selection_value_from_public_text(e['comment_text'], label)
        if normalized_choice(proof.get('selection_value')) != expected_value:
            raise ValueError('choice_selection_value_not_derived_from_original_body')
    expected_ref = choice_reference_id(sender=sender, receiver=receiver, video_id=e['video_id'],
        native_comment_id=native, comment_text=e['comment_text'],
        root_reference_id=proof['root_reference_id'], ask_reply_ref=proof['ask_reply_ref'])
    if e['reference_id'] != expected_ref:
        raise ValueError('choice_reference_not_derived_from_observation')
    if binding is not None:
        if (type(_value(binding, 'knowledge_base_id')) is not int or _value(binding, 'knowledge_base_id') <= 0
                or _value(binding, 'active') is not True
                or _value(binding, 'sender_profile_url') != sender or _value(binding, 'receiver_profile_url') != receiver
                or _value(binding, 'instance_id') != proof['instance_id']):
            raise ValueError('choice_selected_library_binding_conflict')
        expected_command = _identifier('command', {
            'binding_id': _value(binding, 'binding_id'), 'instance_id': _value(binding, 'instance_id'),
            'reference_id': e['reference_id'], 'comment_text': e['comment_text'], 'source_mode': e['source_mode']})
        if e['command_id'] != expected_command:
            raise ValueError('choice_command_binding_conflict')
    if root_binding is not None:
        if any(_value(root_binding, key) != value for key, value in (
                ('sender_profile_url', sender), ('receiver_profile_url', receiver),
                ('instance_id', proof['instance_id']))):
            raise ValueError('choice_original_binding_conflict')
    if root_envelope is not None:
        if (not isinstance(root_envelope, dict)
                or any(root_envelope.get(key) != e[key] for key in (
                    'sender_profile_url', 'receiver_profile_url', 'video_id', 'source_mode'))
                or root_envelope.get('reference_id') != proof['root_reference_id']
                or root_envelope.get('comment_text') != proof['root_comment_text']
                or root_envelope.get('evidence', {}).get('real_mention') is not True):
            raise ValueError('choice_original_save_root_conflict')
    for key, expected in (('ask_ref', ask_ref), ('ask_reply_ref', ask_reply_ref),
                          ('ask_text', ask_text), ('choice_id', choice_id)):
        if expected is not None and proof[key] != expected:
            raise ValueError('choice_frozen_prompt_conflict')
    return {**e, 'sender_profile_url': sender, 'receiver_profile_url': receiver}


def make_choice_envelope(notice, detail, binding, source_mode='live_ui'):
    """Use only a fresh, parent-verified choice detail, preserving its A text."""
    from .contracts import public_profile
    if (not isinstance(notice, Mapping) or not isinstance(detail, Mapping)
            or detail.get('ok') is not True or not isinstance(detail.get('evidence'), dict)):
        raise ValueError('choice_detail_required')
    proof = dict(detail['evidence'])
    sender, receiver = public_profile(_value(binding, 'sender_profile_url')), public_profile(_value(binding, 'receiver_profile_url'))
    if (public_profile(notice.get('source_profile_url')) != sender
            or source_mode == 'live_ui' and not isinstance(notice.get('semantic_fingerprint'), str)
            or source_mode == 'live_ui' and not notice['semantic_fingerprint'].strip()
            or detail.get('sender_profile_url') != sender or detail.get('receiver_profile_url') != receiver
            or detail.get('unique_count') != 1 or type(detail.get('unique_count')) is not int
            or detail.get('source_url') != 'https://www.douyin.com/video/' + str(detail.get('video_id'))
            or _normal(_text(notice.get('exact_text'), 4000)) != _normal(_text(detail.get('command_text'), 4000))):
        raise ValueError('choice_notice_detail_conflict')
    text, video, native = detail['command_text'], detail['video_id'], detail.get('native_comment_id')
    if detail.get('native_comment_id_conflict'):
        raise ValueError('choice_native_id_conflict')
    reference = choice_reference_id(sender=sender, receiver=receiver, video_id=video,
        native_comment_id=native, comment_text=text,
        root_reference_id=proof.get('root_reference_id'), ask_reply_ref=proof.get('ask_reply_ref'))
    proof.update({'selection_reference_id': reference, 'selection_text': text,
                  'observed_at_utc': datetime.now(timezone.utc).isoformat()})
    envelope = {'command_id': _identifier('command', {
        'binding_id': _value(binding, 'binding_id'), 'instance_id': _value(binding, 'instance_id'),
        'reference_id': reference, 'comment_text': text, 'source_mode': source_mode}),
        'reference_id': reference, 'reference_quality': 'native' if native is not None else 'verified_ui',
        'native_comment_id': native, 'sender_profile_url': sender, 'receiver_profile_url': receiver,
        'video_id': video, 'comment_text': text, 'source_mode': source_mode, 'evidence': proof}
    return validate_choice_envelope(envelope, binding)
