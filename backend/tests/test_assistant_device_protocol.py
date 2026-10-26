"""HMAC protocol foundation only: fake local devices, no remote transport."""
from contextlib import contextmanager

import pytest

from app.assistant.registry import Registry, RegistryError, payload_sha256, verify_signed_task
from app.mention_agent.contracts import Binding


@pytest.fixture
def protocol(tmp_path):
    now = [1000.0]
    catalog = [{"id": 6, "name": "编程库"}]
    registry = Registry(tmp_path / "registry.sqlite3", catalog_reader=lambda: catalog, clock=lambda: now[0])
    binding = Binding("old-binding", "old-instance", "https://www.douyin.com/user/protocol-A",
                      "https://www.douyin.com/user/protocol-B", 6, True)
    migrated = registry.import_legacy(binding, verified_by="local-admin-fixture", allow_import=True)
    credential = registry.provision_device_credential(migrated["device_id"])["credential"]
    payload = {"question": "离线协议样本，不包含真实资料", "action": "query"}
    metadata = {"queue_task_id": "queue-fixture", "device_id": migrated["device_id"],
        "instance_id": binding.instance_id, "user_id": migrated["user_id"],
        "binding_id": binding.binding_id, "knowledge_base_id": 6, "video_id": "7529030669884853530",
        "command_id": "command:fixture", "reference_id": "reference:fixture",
        "payload_sha256": payload_sha256(payload)}
    return registry, binding, migrated, credential, payload, metadata, now, catalog


def test_signed_task_validates_exact_destination_body_and_durable_nonce(protocol):
    registry, binding, migrated, credential, payload, metadata, now, catalog = protocol
    packet = registry.sign_task(metadata)
    checked = verify_signed_task(packet, credential, expected_device_id=migrated["device_id"],
        expected_instance_id=binding.instance_id, expected_user_id=migrated["user_id"],
        payload=payload, now=now[0])
    assert checked["queue_task_id"] == metadata["queue_task_id"]
    assert registry.verify_device_task(packet, credential, payload=payload) == checked
    with pytest.raises(RegistryError, match="task_nonce_replayed"):
        registry.verify_device_task(packet, credential, payload=payload)
    restarted = Registry(registry.path, catalog_reader=lambda: catalog, clock=lambda: now[0])
    with pytest.raises(RegistryError, match="task_nonce_replayed"):
        restarted.verify_device_task(packet, credential, payload=payload)
    assert credential not in str(packet) and "credential_hash" not in str(registry.snapshot())


@pytest.mark.parametrize("change", [
    {"video_id": "7529030669884853531"}, {"knowledge_base_id": 8},
    {"command_id": "command:other"}, {"reference_id": "reference:other"},
    {"signature": "0" * 64}, {"action": "import"},
])
def test_changed_signed_fields_are_rejected(protocol, change):
    registry, _, _, credential, payload, metadata, _, _ = protocol
    packet = {**registry.sign_task(metadata), **change}
    with pytest.raises(RegistryError):
        registry.verify_device_task(packet, credential, payload=payload)


def test_changed_payload_or_other_device_destination_never_verifies(protocol):
    registry, binding, migrated, credential, payload, metadata, now, _ = protocol
    packet = registry.sign_task(metadata)
    with pytest.raises(RegistryError, match="signed_task_payload_changed"):
        registry.verify_device_task(packet, credential, payload={**payload, "question": "changed"})
    with pytest.raises(RegistryError, match="signed_task_destination_mismatch"):
        verify_signed_task(packet, credential, expected_device_id="different-device",
            expected_instance_id=binding.instance_id, expected_user_id=migrated["user_id"],
            payload=payload, now=now[0])


def test_expired_or_future_task_is_rejected_without_business_effect(protocol):
    registry, _, _, credential, payload, metadata, now, _ = protocol
    packet = registry.sign_task(metadata, ttl_seconds=60)
    now[0] += 61
    with pytest.raises(RegistryError, match="signed_task_expired"):
        registry.verify_device_task(packet, credential, payload=payload)
    now[0] -= 70
    with pytest.raises(RegistryError, match="signed_task_expired"):
        registry.verify_device_task(packet, credential, payload=payload)


@pytest.mark.parametrize("change", ["public_permission", "aliases", "revocation", "credential_rotation"])
def test_fresh_authorization_and_credentials_fence_old_tasks(protocol, change):
    registry, _, migrated, credential, payload, metadata, _, _ = protocol
    packet = registry.sign_task(metadata)
    if change == "public_permission":
        registry.grant_library(migrated["user_id"], migrated["device_id"], 6,
            can_import=True, public_reply_allowed=False, is_default=True)
    elif change == "aliases":
        registry.grant_library(migrated["user_id"], migrated["device_id"], 6,
            aliases=["新别名"], can_import=True, public_reply_allowed=True, is_default=True)
    elif change == "revocation":
        registry.revoke_device(migrated["device_id"])
    else:
        registry.provision_device_credential(migrated["device_id"])
    with pytest.raises(RegistryError):
        registry.verify_device_task(packet, credential, payload=payload)


def test_signer_does_not_accept_url_path_or_arbitrary_scope(protocol):
    registry, _, _, _, _, metadata, _, _ = protocol
    for extra in ({"url": "https://elsewhere.invalid"}, {"path": "C:/private"}, {"credential": "no"}):
        with pytest.raises(RegistryError, match="invalid_signed_task_fields"):
            registry.sign_task({**metadata, **extra})
    with pytest.raises(RegistryError, match="signed_task_scope_not_authorized"):
        registry.sign_task({**metadata, "instance_id": "remote-computer"})
    for ttl in (0, 3601, True, "300"):
        with pytest.raises(RegistryError, match="invalid_signed_task_policy"):
            registry.sign_task(metadata, ttl_seconds=ttl)


@pytest.mark.parametrize("current", [float("nan"), float("inf"), True, "1000"])
def test_invalid_clock_cannot_bypass_expiry(protocol, current):
    registry, binding, migrated, credential, payload, metadata, _, _ = protocol
    packet = registry.sign_task(metadata)
    with pytest.raises(RegistryError, match="invalid_task_clock"):
        verify_signed_task(packet, credential, expected_device_id=migrated["device_id"],
            expected_instance_id=binding.instance_id, expected_user_id=migrated["user_id"],
            payload=payload, now=current)


def test_expiry_after_waiting_for_write_transaction_does_not_consume_nonce(protocol, monkeypatch):
    registry, _, _, credential, payload, metadata, now, _ = protocol
    packet = registry.sign_task(metadata, ttl_seconds=5)
    original_db = registry._db

    @contextmanager
    def delayed_transaction(*, write=False):
        with original_db(write=write) as db:
            if write:
                now[0] = 1006.0
            yield db

    with monkeypatch.context() as patch:
        patch.setattr(registry, "_db", delayed_transaction)
        with pytest.raises(RegistryError, match="signed_task_expired"):
            registry.verify_device_task(packet, credential, payload=payload)
    with registry._db() as db:
        assert db.execute("SELECT COUNT(*) FROM device_task_nonces").fetchone()[0] == 0
    now[0] = 1001.0
    assert registry.verify_device_task(packet, credential, payload=payload)["queue_task_id"] == metadata["queue_task_id"]
