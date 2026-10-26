"""Lost accept-ACK review, with fictional credentials and no transport."""
import hashlib
import json

import pytest

from app.assistant.device_inbox import DeviceInbox, DeviceInboxError
from app.assistant.registry import Registry
from tests.test_assistant_device_inbox_recovery_review import fixture as inbox_fixture


def test_accept_response_lost_then_ttl_expired_can_query_original_task_without_writes(inbox_fixture):
    registry, binding, migrated, credential, payload, packet, clock, catalog, inbox, queue = inbox_fixture
    hidden_ack = inbox.accept(packet, credential, payload=payload)
    # A real caller here retains its remote task ID, not the lost random receipt.
    known_remote_id = packet['queue_task_id']
    clock[0] += 1000
    reopened = DeviceInbox(Registry(registry.path, catalog_reader=lambda: catalog, clock=lambda: clock[0]))
    before = hashlib.sha256(registry.path.read_bytes()).hexdigest()
    found = reopened.get_receipt_by_remote_task(known_remote_id, credential,
        expected_device_id=migrated['device_id'])
    assert found['receipt_id'] == hidden_ack['receipt_id'] and found['state'] == 'received'
    assert found['remote_task_id'] == known_remote_id and found['local_task_id'] is None
    assert found['platform_origin_verified'] is False and found['execution_enabled'] is False
    assert hashlib.sha256(registry.path.read_bytes()).hexdigest() == before
    assert queue.list_tasks()['total'] == 0
    assert not set(found) & {'credential', 'credential_hash', 'packet', 'signature', 'payload',
        'authorization', 'handoff_token', 'lease_until', 'nonce'}
    assert credential not in json.dumps(found)


def test_other_authenticated_device_cannot_query_owner_remote_task(inbox_fixture):
    registry, binding, migrated, credential, payload, packet, clock, catalog, inbox, queue = inbox_fixture
    inbox.accept(packet, credential, payload=payload)
    other = registry.register_user('https://www.douyin.com/user/status-review-synthetic-other', binding.receiver_profile_url)
    registry.verify_user(other['user_id'], verified_by='synthetic-local-review')
    device = registry.register_device(other['user_id'], is_default=True)
    registry.verify_device(device['device_id'], verified_by='synthetic-local-review')
    second_credential = registry.provision_device_credential(device['device_id'])['credential']
    before = hashlib.sha256(registry.path.read_bytes()).hexdigest()
    with pytest.raises(DeviceInboxError, match='device_inbox_receipt_not_found'):
        inbox.get_receipt_by_remote_task(packet['queue_task_id'], second_credential,
            expected_device_id=device['device_id'])
    with pytest.raises(DeviceInboxError, match='device_authentication_failed'):
        inbox.get_receipt_by_remote_task(packet['queue_task_id'], credential,
            expected_device_id=device['device_id'])
    assert hashlib.sha256(registry.path.read_bytes()).hexdigest() == before


def test_remote_task_status_after_private_regrant_retains_original_authority(inbox_fixture):
    registry, binding, migrated, credential, payload, packet, clock, catalog, inbox, queue = inbox_fixture
    admitted = inbox.accept(packet, credential, payload=payload)
    registry.revoke_grant(migrated['grant_id'])
    new = registry.grant_library(migrated['user_id'], migrated['device_id'], 6, can_import=True)
    before = hashlib.sha256(registry.path.read_bytes()).hexdigest()
    found = inbox.get_receipt_by_remote_task(packet['queue_task_id'], credential,
        expected_device_id=migrated['device_id'])
    assert found['receipt_id'] == admitted['receipt_id'] and found['binding_id'] == binding.binding_id
    assert found['binding_id'] != new['binding_id'] and found['currently_authorized'] is False
    assert found['state'] == 'received' and found['execution_enabled'] is False
    assert queue.list_tasks()['total'] == 0 and hashlib.sha256(registry.path.read_bytes()).hexdigest() == before


def test_credential_rotation_between_authentication_and_status_read_is_rechecked(inbox_fixture, monkeypatch):
    registry, binding, migrated, credential, payload, packet, clock, catalog, inbox, queue = inbox_fixture
    inbox.accept(packet, credential, payload=payload)
    authenticate = registry.authenticate_device
    def rotating(device_id, supplied):
        authenticated = authenticate(device_id, supplied)
        registry.provision_device_credential(device_id)
        return authenticated
    with monkeypatch.context() as patch:
        patch.setattr(registry, 'authenticate_device', rotating)
        with pytest.raises(DeviceInboxError, match='device_authentication_failed'):
            inbox.get_receipt_by_remote_task(packet['queue_task_id'], credential,
                expected_device_id=migrated['device_id'])
    assert queue.list_tasks()['total'] == 0


def test_malformed_status_reference_neither_creates_receipt_nor_prepares_new_work(inbox_fixture):
    registry, binding, migrated, credential, payload, packet, clock, catalog, inbox, queue = inbox_fixture
    before = hashlib.sha256(registry.path.read_bytes()).hexdigest()
    for value in ([], {}, '', ' with spaces ', 'x' * 257, 'https://example.invalid/anywhere'):
        with pytest.raises(DeviceInboxError, match='invalid_inbox_identity'):
            inbox.get_receipt_by_remote_task(value, credential, expected_device_id=migrated['device_id'])
    with pytest.raises(DeviceInboxError, match='device_inbox_receipt_not_found'):
        inbox.get_receipt_by_remote_task('unknown-task', credential, expected_device_id=migrated['device_id'])
    assert hashlib.sha256(registry.path.read_bytes()).hexdigest() == before
    assert queue.list_tasks()['total'] == 0
