"""Fixed local paths and lazy web integration; reading never enables the bot."""
from __future__ import annotations

import json
from pathlib import Path

from app.mention_agent.service import configured_database
from app.storage_guard import assert_no_orphan_sqlite_files, StorageGuardError
from .control import AssistantControl
from .queue import AssistantQueue, QueueError
from .registry import Registry


BACKEND = Path(__file__).resolve().parents[2]
STORAGE = BACKEND/'app/storage'
STATE = STORAGE/'assistant'
AGENT_PATH = STORAGE/'mention_agent/agent.sqlite3'
RECEIVER_STATE = STORAGE/'mention_receiver'


def registry_if_configured(state_dir=STATE, database=None):
    path = Path(state_dir)/'registry.sqlite3'
    required=path.parent/'registry-required.json'
    if not path.exists() and not path.is_symlink():
        if required.exists() or required.is_symlink():
            raise QueueError('registry_required_but_missing')
        if path.parent.is_dir():
            try:
                assert_no_orphan_sqlite_files(path)
            except StorageGuardError:
                raise QueueError('registry_orphaned_state') from None
        return None
    return Registry(path,database_path=database or configured_database(),
        protected_paths=(AGENT_PATH,STORAGE/'mention_agent/backend-ingestion.sqlite3',RECEIVER_STATE,
                         STORAGE/'chroma',STORAGE/'playwright'),allow_create=False)


def receiver_configuration():
    # This file contains public number/profile/notification method only. The
    # browser login profile is owned solely by the normal-page receiver.
    from app.mention_receiver.__main__ import configuration
    return configuration(RECEIVER_STATE)


def active_local_bindings(registry, receiver_profile_url=None):
    snapshot=registry.snapshot()
    results=[]
    for user in snapshot['users']:
        if not user['active'] or user['verification_status']!='verified':
            continue
        if receiver_profile_url is not None and user['receiver_profile_url']!=receiver_profile_url:
            continue
        for device in user['devices']:
            if not device['is_local'] or device['revoked'] or not device['enabled'] or device['verification_status']!='verified':
                continue
            all_binding = registry.authorize_all_local(user['user_id'], device['device_id'])
            if all_binding is not None:
                results.append({'binding': all_binding, 'user': user, 'device': device,
                    'grant': {'name': '全部入库资料', 'is_default': True, 'can_import': False,
                              'query_scope': 'all_local'}})
            for grant in device['libraries']:
                for action in ('query','import'):
                    binding=registry.authorize_binding(grant['binding_id'],device['instance_id'],grant['knowledge_base_id'],action)
                    if binding is not None:
                        results.append({'binding':binding,'user':user,'device':device,'grant':grant})
                        break
    return results


def binding_summary(state_dir=STATE, database=None):
    registry=registry_if_configured(state_dir,database)
    if registry is None:
        return None
    receiver=receiver_configuration()
    choices=active_local_bindings(registry,receiver['receiver_profile_url'])
    if not choices:
        return None
    selected=next((item for item in choices if item['grant']['is_default']),choices[0])
    binding,user,grant=selected['binding'],selected['user'],selected['grant']
    allow_import = grant['can_import']
    if binding.is_all_local:
        allow_import = any(not item['binding'].is_all_local and item['grant']['can_import']
            and item['binding'].sender_profile_url == binding.sender_profile_url
            and item['binding'].instance_id == binding.instance_id for item in choices)
    return {**binding.to_dict(),'sender_display_name':user['label'],
        'receiver_public_number':receiver['receiver_public_number'],
        'knowledge_base_name':grant['name'],'allow_import':allow_import,
        **({'import_requires_library_choice': True} if binding.is_all_local else {})}


def start_guard(state_dir=STATE, database=None):
    registry=registry_if_configured(state_dir,database)
    if registry is None:
        raise QueueError('assistant_setup_required')
    try:
        receiver=receiver_configuration()
    except Exception:
        raise QueueError('assistant_receiver_not_configured') from None
    if not active_local_bindings(registry,receiver['receiver_profile_url']):
        raise QueueError('verified_local_binding_required')
    return True


def build_web_service():
    from .routes import AssistantAPI
    queue=AssistantQueue(STATE/'queue.sqlite3',protected_paths=(configured_database(),STORAGE/'mention_agent',
        RECEIVER_STATE,STORAGE/'chroma',STATE/'registry.sqlite3',STATE/'conversation.sqlite3'))
    control=AssistantControl(queue,STATE)
    return AssistantAPI(queue,control,binding_provider=binding_summary,start_guard=start_guard)
