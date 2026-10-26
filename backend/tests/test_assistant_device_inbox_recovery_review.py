"""Independent handoff-crash review: only fictional devices and temporary DBs."""
from datetime import datetime, timezone
import hashlib
import threading

import pytest

from app.assistant.device_inbox import DeviceInbox, DeviceInboxError
from app.assistant.queue import AssistantQueue
from app.assistant.registry import Registry, payload_sha256
from app.mention_agent.contracts import Binding


@pytest.fixture
def fixture(tmp_path):
    clock = [1000.0]
    catalog = [{'id': 6, 'name': 'synthetic review library'}]
    registry = Registry(tmp_path / 'registry.sqlite3', catalog_reader=lambda: catalog, clock=lambda: clock[0])
    binding = Binding('inbox-review-binding', 'inbox-review-instance',
        'https://www.douyin.com/user/inbox-review-synthetic-A',
        'https://www.douyin.com/user/inbox-review-synthetic-B', 6, True)
    migrated = registry.import_legacy(binding, verified_by='synthetic-local-review', allow_import=True)
    credential = registry.provision_device_credential(migrated['device_id'])['credential']
    payload = {'command_id': 'review-command', 'reference_id': 'review-original-reference',
        'reference_quality': 'verified_ui', 'native_comment_id': None,
        'sender_profile_url': binding.sender_profile_url, 'receiver_profile_url': binding.receiver_profile_url,
        'video_id': '7999999000000000077', 'comment_text': '@synthetic-B synthetic isolation review question',
        'source_mode': 'isolated', 'evidence': {'real_mention': True, 'unique_count': 1,
            'sender_verified': True, 'video_verified': True, 'observed_at_utc': datetime.now(timezone.utc).isoformat()}}
    metadata = {'queue_task_id': 'review-remote-task', 'device_id': migrated['device_id'],
        'instance_id': binding.instance_id, 'user_id': migrated['user_id'], 'binding_id': binding.binding_id,
        'knowledge_base_id': 6, 'video_id': payload['video_id'], 'command_id': payload['command_id'],
        'reference_id': payload['reference_id'], 'payload_sha256': payload_sha256(payload)}
    packet = registry.sign_task(metadata)
    inbox = DeviceInbox(registry)
    inbox.prepare_schema()
    queue = AssistantQueue(tmp_path / 'queue.sqlite3')
    return registry, binding, migrated, credential, payload, packet, clock, catalog, inbox, queue


def existing(queue, binding, migrated, scope, payload):
    return queue.enqueue(binding, payload, kind='query', user_id=migrated['user_id'],
        device_id=migrated['device_id'], authorization=scope, initial_state='waiting_desktop')


def receipt(inbox, admitted, credential, migrated):
    return inbox.get_receipt(admitted['receipt_id'], credential, expected_device_id=migrated['device_id'])


def test_enqueue_committed_then_ack_interrupted_recovers_one_original_task_on_reopen(fixture, monkeypatch):
    registry, binding, migrated, credential, payload, packet, clock, catalog, inbox, queue = fixture
    admitted = inbox.accept(packet, credential, payload=payload)
    with monkeypatch.context() as patch:
        def fail_before_ack(*args, **kwargs):
            raise RuntimeError('synthetic interruption after queue commit')
        patch.setattr(inbox, 'finish_handoff', fail_before_ack)
        with pytest.raises(DeviceInboxError, match='device_inbox_handoff_unknown'):
            inbox.handoff_once(queue, run_id='first-process', lease_seconds=1)
    assert queue.list_tasks()['total'] == 1
    found = queue.lookup_by_source_reference(binding.sender_profile_url, binding.receiver_profile_url,
        payload['source_mode'], payload['reference_id'])
    assert found['state'] == 'waiting_desktop'
    assert receipt(inbox, admitted, credential, migrated)['state'] == 'handoff_reserved'
    clock[0] += 2
    reopened = DeviceInbox(Registry(registry.path, catalog_reader=lambda: catalog, clock=lambda: clock[0]))
    result = reopened.handoff_once(AssistantQueue(queue.path), run_id='second-process', lease_seconds=1)
    assert result['state'] == 'handed_off' and result['local_task_id'] == found['id']
    assert queue.list_tasks()['total'] == 1 and queue.get_task(found['id']) == found
    assert queue.claim_next('execute') is None and queue.claim_next('publish') is None


def test_expired_worker_cannot_ack_over_new_owner_after_the_same_queue_commit(fixture, monkeypatch):
    registry, binding, migrated, credential, payload, packet, clock, catalog, inbox, queue = fixture
    admitted = inbox.accept(packet, credential, payload=payload)
    committed, release = threading.Event(), threading.Event()
    original_enqueue = queue.enqueue
    errors = []
    def delayed_return(*args, **kwargs):
        result = original_enqueue(*args, **kwargs)
        committed.set()
        assert release.wait(5), 'synthetic bounded release not observed'
        return result
    def old_worker():
        try:
            inbox.handoff_once(queue, run_id='old-worker', lease_seconds=1)
        except DeviceInboxError as error:
            errors.append(str(error))
    with monkeypatch.context() as patch:
        patch.setattr(queue, 'enqueue', delayed_return)
        thread = threading.Thread(target=old_worker, name='isolated-expired-handoff')
        thread.start()
        try:
            assert committed.wait(5)
            clock[0] += 2
            reopened = DeviceInbox(Registry(registry.path, catalog_reader=lambda: catalog, clock=lambda: clock[0]))
            current = reopened.handoff_once(AssistantQueue(queue.path), run_id='new-worker', lease_seconds=1)
            assert current['state'] == 'handed_off'
        finally:
            release.set()
            thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == ['stale_handoff_claim']
    assert queue.list_tasks()['total'] == 1
    final = receipt(inbox, admitted, credential, migrated)
    assert final['state'] == 'handed_off' and final['local_task_id'] == current['local_task_id']


def test_revoke_then_private_regrant_never_upgrades_or_rebinds_an_existing_queue_task(fixture, monkeypatch):
    registry, binding, migrated, credential, payload, packet, clock, catalog, inbox, queue = fixture
    admitted = inbox.accept(packet, credential, payload=payload)
    with monkeypatch.context() as patch:
        patch.setattr(inbox, 'finish_handoff', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('synthetic gap')))
        with pytest.raises(DeviceInboxError):
            inbox.handoff_once(queue, run_id='before-revoke', lease_seconds=1)
    original = queue.lookup_by_source_reference(binding.sender_profile_url, binding.receiver_profile_url,
        payload['source_mode'], payload['reference_id'])
    registry.revoke_grant(migrated['grant_id'])
    private = registry.grant_library(migrated['user_id'], migrated['device_id'], 6,
        can_import=True, is_default=True)
    assert private['public_reply_allowed'] is False and private['binding_id'] != binding.binding_id
    clock[0] += 2
    recovered = DeviceInbox(Registry(registry.path, catalog_reader=lambda: catalog, clock=lambda: clock[0]))
    result = recovered.handoff_once(queue, run_id='after-revoke', lease_seconds=1)
    assert result['state'] == 'handed_off' and result['currently_authorized'] is False
    assert queue.get_task(original['id']) == original
    assert registry.authorize_snapshot(original['authorization']) is None
    assert queue.claim_next('execute') is None and queue.claim_next('publish') is None


def test_accepted_but_not_queued_old_scope_is_held_after_regrant_without_new_task(fixture):
    registry, binding, migrated, credential, payload, packet, clock, catalog, inbox, queue = fixture
    admitted = inbox.accept(packet, credential, payload=payload)
    registry.revoke_grant(migrated['grant_id'])
    private = registry.grant_library(migrated['user_id'], migrated['device_id'], 6,
        can_import=True, is_default=True)
    result = inbox.handoff_once(queue, run_id='fresh-but-old-scope')
    assert result['state'] == 'held' and result['reason_code'] == 'authorization_changed'
    assert result['binding_id'] == binding.binding_id and result['binding_id'] != private['binding_id']
    assert queue.list_tasks()['total'] == 0
    assert receipt(inbox, admitted, credential, migrated)['currently_authorized'] is False


def test_existing_original_with_conflicting_text_is_not_acknowledged_or_mutated(fixture):
    registry, binding, migrated, credential, payload, packet, clock, catalog, inbox, queue = fixture
    admitted = inbox.accept(packet, credential, payload=payload)
    scope = registry.authorization_snapshot(binding.binding_id, binding.instance_id, 6)
    original = existing(queue, binding, migrated, scope, payload | {
        'command_id': 'earlier-conflicting-command', 'comment_text': 'synthetic earlier conflicting text'})
    before = hashlib.sha256(queue.path.read_bytes()).hexdigest()
    result = inbox.handoff_once(queue, run_id='conflict-review')
    assert result['state'] == 'held' and result['reason_code'] == 'queue_original_conflict'
    assert result['local_task_id'] is None
    assert queue.get_task(original['id']) == original and queue.list_tasks()['total'] == 1
    assert hashlib.sha256(queue.path.read_bytes()).hexdigest() == before


def test_packet_expiry_after_atomic_accept_does_not_silently_drop_durable_inbox(fixture):
    registry, binding, migrated, credential, payload, packet, clock, catalog, inbox, queue = fixture
    admitted = inbox.accept(packet, credential, payload=payload)
    clock[0] += 1000
    reopened = DeviceInbox(Registry(registry.path, catalog_reader=lambda: catalog, clock=lambda: clock[0]))
    assert receipt(reopened, admitted, credential, migrated)['state'] == 'received'
    handed = reopened.handoff_once(queue, run_id='later-local-handoff')
    assert handed['state'] == 'handed_off' and handed['currently_authorized'] is True
    task = queue.get_task(handed['local_task_id'])
    assert task['state'] == 'waiting_desktop' and task['envelope']['source_mode'] == 'isolated'
    assert queue.claim_next('execute') is None
