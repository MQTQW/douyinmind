"""A fresh ordinary A reply in one verified published thread, query only.

Native parent hints locate candidates; they are never promoted to DOM parent
IDs or real @ evidence. Registry/source/anchor checks remain the owner's job.
"""
from __future__ import annotations

from datetime import datetime
from collections.abc import Mapping
import hashlib
import re


ENVELOPE_KEYS=frozenset({'command_id','reference_id','reference_quality','native_comment_id',
    'sender_profile_url','receiver_profile_url','video_id','comment_text','source_mode','evidence'})
PROOF_REQUIRED=frozenset({'source_kind','evidence_version','query_only','instance_id','real_mention',
    'sender_verified','receiver_verified','video_verified','current_receiver_verified','unique_count',
    'root_chain_verified','prior_reply_visible_unique','reply_to_receiver_verified',
    'direct_reply_to_prior','parent_scope','sender_profile_url','receiver_profile_url','video_id',
    'root_task_id','root_agent_task_id','root_reference_id','root_comment_text',
    'root_native_comment_id','prior_reply_native_comment_id','prior_reply_text','prior_reply_sha256',
    'followup_reference_id','followup_text','native_comment_id','observed_at_utc'})
PARENT_SCOPES=frozenset({'direct_prior_reply','unique_followup_in_verified_root'})
_HEX=re.compile(r'[a-f0-9]{64}',re.ASCII)
_ID=re.compile(r'[1-9][0-9]{4,29}',re.ASCII)


def _text(value,limit=256):
    if (type(value) is not str or not value.strip() or len(value)>limit
            or any(ord(c)<32 and c not in '\r\n\t' for c in value)):
        raise ValueError('invalid_followup_text')
    return value


def _value(binding,key):
    return binding.get(key) if isinstance(binding,Mapping) else getattr(binding,key,None)


def _normal(value):return re.sub(r'\s+','',value)


def _identifier(kind,value):
    from .contracts import canonical_json
    return kind+':'+hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def followup_reference_id(*,sender,receiver,video_id,native_comment_id,comment_text,
                          root_reference_id,prior_reply_sha256):
    return _identifier('reference',{'kind':'conversation_followup','sender':sender,'receiver':receiver,
        'video_id':video_id,'native_comment_id':native_comment_id,
        'body':None if native_comment_id is not None else _normal(comment_text),
        'root_reference_id':root_reference_id,'prior_reply_sha256':prior_reply_sha256})


def validate_followup_envelope(envelope,binding=None):
    from .contracts import public_profile,canonical_json
    if type(envelope) is not dict or set(envelope)!=ENVELOPE_KEYS:
        raise ValueError('invalid_followup_envelope_fields')
    e=envelope
    for key in ('command_id','reference_id'):_text(e[key])
    sender,receiver=public_profile(e['sender_profile_url']),public_profile(e['receiver_profile_url'])
    if sender!=e['sender_profile_url'] or receiver!=e['receiver_profile_url'] or sender==receiver:
        raise ValueError('followup_identity_conflict')
    if e['source_mode'] not in {'live_ui','isolated','replay'}:raise ValueError('invalid_followup_source_mode')
    if type(e['video_id']) is not str or not _ID.fullmatch(e['video_id']):raise ValueError('invalid_followup_video')
    _text(e['comment_text'],4000)
    native=e['native_comment_id']
    if native is not None and (type(native) is not str or not _ID.fullmatch(native)):
        raise ValueError('invalid_followup_native_id')
    if e['reference_quality']!=('native' if native is not None else 'verified_ui'):
        raise ValueError('followup_reference_quality_conflict')
    p=e['evidence']
    if (type(p) is not dict or set(p)!=PROOF_REQUIRED or p['source_kind']!='conversation_followup'
            or type(p['evidence_version']) is not int or p['evidence_version']!=1
            or p['real_mention'] is not False or p['query_only'] is not True):
        raise ValueError('invalid_followup_proof')
    for key in ('sender_verified','receiver_verified','video_verified','current_receiver_verified',
                'root_chain_verified','prior_reply_visible_unique','reply_to_receiver_verified'):
        if p[key] is not True:raise ValueError('followup_parent_or_identity_unverified')
    if type(p['unique_count']) is not int or p['unique_count']!=1:raise ValueError('followup_not_unique')
    if (p['parent_scope'] not in PARENT_SCOPES or type(p['direct_reply_to_prior']) is not bool
            or p['direct_reply_to_prior']!=(p['parent_scope']=='direct_prior_reply')):
        raise ValueError('followup_parent_quality_conflict')
    if type(p['instance_id']) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',p['instance_id']):
        raise ValueError('invalid_followup_instance')
    for key in ('root_task_id','root_agent_task_id'):
        if type(p[key]) is not str or not re.fullmatch(r'[a-f0-9]{32}',p[key]):raise ValueError('invalid_followup_anchor_task')
    _text(p['root_reference_id']);_text(p['root_comment_text'],4000);_text(p['prior_reply_text'],8000)
    if (type(p['prior_reply_sha256']) is not str or not _HEX.fullmatch(p['prior_reply_sha256'])
            or hashlib.sha256(p['prior_reply_text'].encode()).hexdigest()!=p['prior_reply_sha256']):
        raise ValueError('followup_prior_reply_hash_conflict')
    if (any(p[k]!=e[k] for k in ('sender_profile_url','receiver_profile_url','video_id','native_comment_id'))
            or p['followup_text']!=e['comment_text'] or p['followup_reference_id']!=e['reference_id']
            or p['root_reference_id']==e['reference_id']):raise ValueError('followup_scope_conflict')
    for key in ('root_native_comment_id','prior_reply_native_comment_id'):
        value=p[key]
        if value is not None and (type(value) is not str or not _ID.fullmatch(value)):
            raise ValueError('invalid_followup_parent_native_id')
    if native is not None and native in {p['root_native_comment_id'],p['prior_reply_native_comment_id']}:
        raise ValueError('followup_is_own_ancestor')
    try:observed=datetime.fromisoformat(p['observed_at_utc'].replace('Z','+00:00'))
    except (TypeError,ValueError,AttributeError):raise ValueError('invalid_followup_observed_at') from None
    if observed.tzinfo is None:raise ValueError('followup_observed_timezone_required')
    expected=followup_reference_id(sender=sender,receiver=receiver,video_id=e['video_id'],
        native_comment_id=native,comment_text=e['comment_text'],root_reference_id=p['root_reference_id'],
        prior_reply_sha256=p['prior_reply_sha256'])
    if e['reference_id']!=expected:raise ValueError('followup_reference_not_from_observation')
    if binding is not None:
        if (_value(binding,'active') is not True or _value(binding,'instance_id')!=p['instance_id']
                or _value(binding,'sender_profile_url')!=sender or _value(binding,'receiver_profile_url')!=receiver):
            raise ValueError('followup_binding_conflict')
        expected_command=_identifier('command',{'binding_id':_value(binding,'binding_id'),
            'instance_id':p['instance_id'],'reference_id':expected,'comment_text':e['comment_text'],'source_mode':e['source_mode']})
        if e['command_id']!=expected_command:raise ValueError('followup_command_binding_conflict')
    if len(canonical_json(p).encode())>30000:raise ValueError('followup_proof_too_large')
    return {**e,'sender_profile_url':sender,'receiver_profile_url':receiver}


def make_followup_envelope(detail,binding,source_mode='live_ui'):
    from .contracts import public_profile
    if type(detail) is not dict or detail.get('ok') is not True:raise ValueError('followup_detail_unverified')
    sender,receiver=public_profile(detail['sender_profile_url']),public_profile(detail['receiver_profile_url'])
    native=detail.get('native_comment_id');text=_text(detail['command_text'],4000)
    proof=dict(detail['evidence'])
    if proof.get('instance_id') not in (None,_value(binding,'instance_id')):raise ValueError('followup_instance_changed')
    proof.update(instance_id=_value(binding,'instance_id'),followup_text=text,native_comment_id=native)
    reference=followup_reference_id(sender=sender,receiver=receiver,video_id=detail['video_id'],
        native_comment_id=native,comment_text=text,root_reference_id=proof['root_reference_id'],
        prior_reply_sha256=proof['prior_reply_sha256'])
    proof['followup_reference_id']=reference
    result={'command_id':_identifier('command',{'binding_id':_value(binding,'binding_id'),
        'instance_id':proof['instance_id'],'reference_id':reference,'comment_text':text,'source_mode':source_mode}),
        'reference_id':reference,'reference_quality':'native' if native is not None else 'verified_ui',
        'native_comment_id':native,'sender_profile_url':sender,'receiver_profile_url':receiver,
        'video_id':detail['video_id'],'comment_text':text,'source_mode':source_mode,'evidence':proof}
    return validate_followup_envelope(result,binding)
