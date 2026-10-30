"""Meaning from a private model receipt; permissions still come from Registry."""
import hashlib
import json


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()


def catalog_snapshot_from_grants(grants):
    """Stable current catalog; heartbeat/online timestamps are not meaning."""
    options=[];revision=[]
    for ordinal,grant in enumerate(grants,1):
        aliases=list(grant.get('aliases',[]))
        item={'grant_id':grant['grant_id'],'name':grant['name'],'aliases':aliases,
            'can_query':grant.get('can_query') is True,'can_import':grant.get('can_import') is True,
            'ordinal':ordinal}
        options.append(item)
        revision.append({**item,**{k:grant.get(k) for k in ('version','auth_revision','binding_id',
            'knowledge_base_id','active','library_exists','public_reply_allowed','is_default')}})
    return {'revision':'catalog-'+_digest(revision),'options':options}


def owner_scope_from_envelope(envelope,user,device,*,root_reference_id=None):
    return {'user_id':user['user_id'],'device_id':device['device_id'],'instance_id':device['instance_id'],
        'sender_profile_url':envelope['sender_profile_url'],'receiver_profile_url':envelope['receiver_profile_url'],
        'video_id':envelope['video_id'],'reference_id':envelope['reference_id'],
        'root_reference_id':root_reference_id or envelope['reference_id']}


def semantic_source_key(envelope):
    return 'semantic-source-'+_digest({k:envelope.get(k) for k in ('sender_profile_url',
        'receiver_profile_url','video_id','source_mode','reference_id')})


def parsed_intent_from_receipt(envelope,user,device,grants,receipt):
    from .commands import ParsedIntent
    from .semantic_plan_store import verify_semantic_decision,ui_semantic_digest
    value=verify_semantic_decision(receipt,mode='root',original_text=envelope['comment_text'],
        frozen_ui_digest=ui_semantic_digest(envelope),
        owner_scope=owner_scope_from_envelope(envelope,user,device),
        catalog_snapshot=catalog_snapshot_from_grants(grants))
    from .semantic_intent_model import FOLLOWUP_CONTEXT
    query_followup=value.get('followup_context')==FOLLOWUP_CONTEXT
    action,target,grant=value['action'],value['target_state'],value['grant_id']
    if envelope.get('evidence',{}).get('source_kind')=='conversation_followup' and action=='save':
        return ParsedIntent('clarify',reason_code='followup_save_requires_mention',
            clarification='这里可以继续根据资料提问；如果要保存视频，请重新 @ 助手并说明要保存的内容。')
    if action=='cancel':
        return ParsedIntent('clarify',reason_code='semantic_cancelled',
            clarification='好的，这次不保存，也不执行其他操作。')
    if target in {'ambiguous','unknown'}:
        return ParsedIntent('clarify',reason_code='semantic_library_ambiguous' if target=='ambiguous' else 'unknown_library',
            clarification=('还没能确定你指的是哪个知识库，请再说明要在哪个资料范围查询。' if query_followup
                else '还没能确定你指的是哪个知识库，请再说明一下；本次尚未保存。'),explicit_library=True)
    if action=='save' and target=='unspecified':
        return ParsedIntent('clarify',reason_code='save_library_required',
            clarification='请先选择要保存到哪个知识库，选择前不会保存。')
    if action=='save' and target=='one':
        selected=next((item for item in grants if item['grant_id']==grant),None)
        if selected is None or selected.get('can_import') is not True:
            return ParsedIntent('clarify',grant_id=grant,reason_code='import_not_allowed',explicit_library=True,
                clarification='当前账号尚未获准向这个库保存视频，可以继续查询资料。')
    if action in {'save','query'}:
        return ParsedIntent(action,grant_id=grant,explicit_library=target=='one')
    return ParsedIntent('clarify',reason_code='semantic_intent_unclear',
        clarification=('你想进一步了解当前视频的哪一部分？可以再具体说一下。' if query_followup
            else '还没确定你是想保存这条视频，还是根据资料提问，可以再具体说一下吗？'))
