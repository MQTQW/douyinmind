"""Atomic inbox foundation: temporary fictional scopes, no runtime providers."""
from datetime import datetime, timezone
import hashlib
import hmac
import json

import pytest

from app.assistant.device_inbox import DeviceInbox, DeviceInboxError, INBOX, NONCES
from app.assistant.queue import AssistantQueue
from app.assistant.registry import Registry, canonical_json, payload_sha256
from app.mention_agent.contracts import Binding


KEY = "f" * 64  # Public synthetic fixture, not a provisioned device credential.
A = "https://www.douyin.com/user/inbox-fixture-A"
B = "https://www.douyin.com/user/inbox-fixture-B"


@pytest.fixture
def scope(tmp_path):
    now = [1000.0]
    registry = Registry(tmp_path / "registry.sqlite3", catalog_reader=lambda: [{"id": 6, "name": "fixture-library"}],
                        clock=lambda: now[0])
    binding = Binding("fixture-inbox-binding", "fixture-inbox-instance", A, B, 6, True)
    migrated = registry.import_legacy(binding, verified_by="isolated-local-admin", allow_import=True)
    with registry._db(write=True) as db:
        db.execute("UPDATE assistant_devices SET credential_hash=? WHERE device_id=?",
            (hashlib.sha256(KEY.encode()).digest(), migrated["device_id"]))
    inbox = DeviceInbox(registry)
    payload = {"command_id": "fixture-command", "reference_id": "fixture-reference",
        "reference_quality": "native", "native_comment_id": "synthetic-comment-only",
        "sender_profile_url": A, "receiver_profile_url": B, "video_id": "7999999000000000001",
        "comment_text": "synthetic isolated question", "source_mode": "isolated",
        "evidence": {"real_mention": True, "sender_verified": True, "video_verified": True,
            "unique_count": 1, "observed_at_utc": datetime.now(timezone.utc).isoformat()}}
    return registry, binding, migrated, inbox, payload, now, tmp_path


def packet(scope, *, payload=None, remote_id="fixture-hub-task", **changes):
    registry, binding, migrated, _, original, _, _ = scope
    selected = original if payload is None else payload
    metadata = {"queue_task_id": remote_id, "device_id": migrated["device_id"], "instance_id": binding.instance_id,
        "user_id": migrated["user_id"], "binding_id": binding.binding_id, "knowledge_base_id": 6,
        "video_id": selected["video_id"], "command_id": selected["command_id"],
        "reference_id": selected["reference_id"], "payload_sha256": payload_sha256(selected), **changes}
    return registry.sign_task(metadata)


def counts(registry):
    with registry._db() as db:
        return (db.execute("SELECT COUNT(*) FROM " + INBOX).fetchone()[0],
                db.execute("SELECT COUNT(*) FROM " + NONCES).fetchone()[0],
                db.execute("SELECT COUNT(*) FROM device_task_nonces").fetchone()[0])


def test_schema_is_explicit_and_foreign_partial_table_is_not_adopted(scope):
    registry, _, _, inbox, payload, _, _ = scope
    with pytest.raises(DeviceInboxError, match="device_inbox_not_prepared"):
        inbox.accept(packet(scope), KEY, payload=payload)
    with registry._db() as db:
        assert db.execute("SELECT COUNT(*) FROM device_task_nonces").fetchone()[0] == 0
    with registry._db(write=True) as db:
        db.execute("CREATE TABLE assistant_device_inbox_meta(precious TEXT)")
        db.execute("INSERT INTO assistant_device_inbox_meta VALUES('preserve')")
    before = registry.path.read_bytes()
    with pytest.raises(DeviceInboxError, match="foreign_device_inbox_schema"):
        inbox.prepare_schema()
    assert registry.path.read_bytes() == before


def test_accept_and_restart_return_one_original_frozen_receipt(scope):
    registry, binding, migrated, inbox, payload, now, _ = scope
    assert inbox.prepare_schema() == {"prepared": True, "created": True}
    assert inbox.prepare_schema() == {"prepared": True, "created": False}
    original = packet(scope)
    first = inbox.accept(original, KEY, payload=payload)
    assert first["created"] and first["state"] == "received" and first["currently_authorized"]
    reopened = Registry(registry.path, allow_create=False, clock=lambda: now[0],
                        catalog_reader=lambda: [{"id": 6, "name": "fixture-library"}])
    again = DeviceInbox(reopened).accept(original, KEY, payload=payload)
    assert again["receipt_id"] == first["receipt_id"] and again["replayed"] and not again["created"]
    assert counts(reopened) == (1, 1, 1)
    with reopened._db() as db:
        row = db.execute("SELECT * FROM " + INBOX).fetchone()
        assert json.loads(row["payload_json"]) == payload
        frozen = json.loads(row["authorization_json"])
        assert len(frozen) == 13 and frozen == registry.authorization_snapshot(binding.binding_id, binding.instance_id, 6)


@pytest.mark.parametrize("after_nonce", [False, True])
def test_failure_before_commit_rolls_back_receipt_and_both_nonce_tables(scope, after_nonce):
    registry, _, _, _, payload, _, _ = scope

    class Broken(DeviceInbox):
        def _save_nonce(self, *args):
            if after_nonce:
                super()._save_nonce(*args)
            raise RuntimeError("synthetic-before-commit-crash")

    inbox = Broken(registry)
    inbox.prepare_schema()
    original = packet(scope)
    with pytest.raises(RuntimeError, match="synthetic-before-commit"):
        inbox.accept(original, KEY, payload=payload)
    assert counts(registry) == (0, 0, 0)
    restarted = DeviceInbox(registry)
    assert restarted.accept(original, KEY, payload=payload)["created"]
    assert counts(registry) == (1, 1, 1)


def test_legacy_verify_nonce_without_receipt_is_never_claimed_as_received(scope):
    registry, _, _, inbox, payload, _, _ = scope
    inbox.prepare_schema()
    original = packet(scope)
    registry.verify_device_task(original, KEY, payload=payload)
    with pytest.raises(DeviceInboxError, match="legacy_nonce_without_inbox"):
        inbox.accept(original, KEY, payload=payload)
    assert counts(registry) == (0, 0, 1)


def test_new_nonce_same_business_returns_old_receipt_but_conflicts_cannot_upgrade(scope):
    registry, binding, migrated, inbox, payload, _, _ = scope
    inbox.prepare_schema()
    first = inbox.accept(packet(scope), KEY, payload=payload)
    again = inbox.accept(packet(scope), KEY, payload=payload)
    assert first["receipt_id"] == again["receipt_id"] and counts(registry) == (1, 2, 2)
    registry.grant_library(migrated["user_id"], migrated["device_id"], 6,
        aliases=["new-alias"], can_import=True, public_reply_allowed=True, is_default=True)
    with pytest.raises(DeviceInboxError, match="device_inbox_scope_conflict"):
        inbox.accept(packet(scope), KEY, payload=payload)
    assert counts(registry) == (1, 2, 2)
    read = inbox.get_receipt(first["receipt_id"], KEY, expected_device_id=migrated["device_id"])
    assert read["currently_authorized"] is False


def test_reused_nonce_with_different_valid_packet_is_rejected(scope):
    registry, _, _, inbox, payload, _, _ = scope
    inbox.prepare_schema()
    original = packet(scope)
    inbox.accept(original, KEY, payload=payload)
    changed = packet(scope, remote_id="different-hub-task")
    changed["nonce"] = original["nonce"]
    unsigned = {key: value for key, value in changed.items() if key != "signature"}
    changed["signature"] = hmac.new(hashlib.sha256(KEY.encode()).digest(),
        canonical_json(unsigned).encode(), hashlib.sha256).hexdigest()
    with pytest.raises(DeviceInboxError, match="device_inbox_nonce_conflict"):
        inbox.accept(changed, KEY, payload=payload)
    assert counts(registry) == (1, 1, 1)


@pytest.mark.parametrize("field", ["video_id", "command_id", "reference_id"])
def test_hash_alone_does_not_allow_payload_to_disagree_with_fixed_packet(scope, field):
    registry, _, _, inbox, payload, _, _ = scope
    inbox.prepare_schema()
    changed = {**payload, field: "7999999000000000002" if field == "video_id" else "different-reference"}
    original = packet(scope, payload=changed, **{field: payload[field]})
    with pytest.raises(DeviceInboxError, match="inbox_payload_scope_mismatch"):
        inbox.accept(original, KEY, payload=changed)
    assert counts(registry) == (0, 0, 0)


@pytest.mark.parametrize("change", ["extra_secret", "evidence_secret", "capacity", "surrogate"])
def test_unknown_secret_fields_and_oversize_payloads_are_not_persisted(scope, change):
    registry, _, _, inbox, payload, _, _ = scope
    inbox.prepare_schema()
    changed = {**payload}
    if change == "extra_secret":
        changed["cookie"] = "synthetic-private-field"
    elif change == "evidence_secret":
        changed["evidence"] = {**payload["evidence"], "api_key": "synthetic-private-field"}
    elif change == "capacity":
        changed["comment_text"] = "x" * 33000
    else:
        changed["comment_text"] = "\ud800"
    # Input filtering happens before attempting to authenticate an unsigned packet.
    with pytest.raises(DeviceInboxError):
        inbox.accept({}, KEY, payload=changed)
    assert counts(registry) == (0, 0, 0)


def test_expired_packet_cannot_be_accepted_again_but_current_identity_can_read_status(scope):
    registry, _, migrated, inbox, payload, now, _ = scope
    inbox.prepare_schema()
    original = packet(scope)
    first = inbox.accept(original, KEY, payload=payload)
    now[0] += 301
    with pytest.raises(DeviceInboxError, match="signed_task_expired"):
        inbox.accept(original, KEY, payload=payload)
    read = inbox.get_receipt(first["receipt_id"], KEY, expected_device_id=migrated["device_id"])
    assert read["receipt_id"] == first["receipt_id"] and read["state"] == "received"
    assert counts(registry) == (1, 1, 1)


def test_revoked_scope_duplicate_reads_original_but_new_handoff_is_held(scope):
    registry, _, migrated, inbox, payload, _, root = scope
    inbox.prepare_schema()
    original = packet(scope)
    first = inbox.accept(original, KEY, payload=payload)
    registry.revoke_grant(migrated["grant_id"])
    again = inbox.accept(original, KEY, payload=payload)
    assert again["receipt_id"] == first["receipt_id"] and not again["currently_authorized"]
    queue = AssistantQueue(root / "queue.sqlite3")
    result = inbox.handoff_once(queue, run_id="fixture-run")
    assert result["state"] == "held" and result["reason_code"] == "authorization_changed"
    assert sum(queue.counts().values()) == 0


def test_transport_expiry_does_not_delete_durable_task_and_queue_cannot_execute_it(scope):
    registry, _, _, inbox, payload, now, root = scope
    inbox.prepare_schema()
    inbox.accept(packet(scope), KEY, payload=payload)
    now[0] += 301
    queue = AssistantQueue(root / "queue.sqlite3")
    result = inbox.handoff_once(queue, run_id="fixture-run")
    assert result["state"] == "handed_off"
    assert queue.get_task(result["local_task_id"])["state"] == "waiting_desktop"
    assert queue.claim_next("execute") is None and queue.claim_next("publish") is None


def test_enqueue_commit_ack_gap_recovers_by_readonly_lookup_even_after_revocation(scope, monkeypatch):
    registry, _, migrated, _, payload, now, root = scope

    class LoseAck(DeviceInbox):
        def finish_handoff(self, *args, **kwargs):
            raise RuntimeError("synthetic-after-queue-commit-before-ack")

    inbox = LoseAck(registry)
    inbox.prepare_schema()
    first = inbox.accept(packet(scope), KEY, payload=payload)
    queue = AssistantQueue(root / "queue.sqlite3")
    with pytest.raises(DeviceInboxError, match="handoff_unknown"):
        inbox.handoff_once(queue, run_id="old-run")
    assert sum(queue.counts().values()) == 1
    registry.revoke_grant(migrated["grant_id"])
    now[0] += 31
    restarted = DeviceInbox(registry)

    def no_enqueue(*args, **kwargs):
        raise AssertionError("recovery must not call enqueue when original exists")

    monkeypatch.setattr(queue, "enqueue", no_enqueue)
    result = restarted.handoff_once(queue, run_id="new-run")
    assert result["receipt_id"] == first["receipt_id"] and result["state"] == "handed_off"
    assert not result["currently_authorized"] and sum(queue.counts().values()) == 1


def test_expired_claim_cannot_ack_over_new_owner(scope):
    registry, _, _, inbox, payload, now, _ = scope
    inbox.prepare_schema()
    inbox.accept(packet(scope), KEY, payload=payload)
    old = inbox.claim_handoff(run_id="old-run", lease_seconds=1)
    now[0] += 2
    new = inbox.claim_handoff(run_id="new-run", lease_seconds=30)
    with pytest.raises(DeviceInboxError, match="stale_handoff_claim"):
        inbox.finish_handoff(old["receipt_id"], old["handoff_token"], local_task_id="old-task")
    result = inbox.finish_handoff(new["receipt_id"], new["handoff_token"], local_task_id="new-task")
    assert result["local_task_id"] == "new-task"


def second_user(scope):
    registry, _, _, _, original, _, _ = scope
    sender = "https://www.douyin.com/user/inbox-fixture-other-A"
    user = registry.register_user(sender, B)
    registry.verify_user(user["user_id"], verified_by="isolated-local-admin")
    device = registry.register_device(user["user_id"], is_default=True)
    registry.verify_device(device["device_id"], verified_by="isolated-local-admin")
    grant = registry.grant_library(user["user_id"], device["device_id"], 6, is_default=True)
    key = "a" * 64
    with registry._db(write=True) as db:
        db.execute("UPDATE assistant_devices SET credential_hash=? WHERE device_id=?",
                   (hashlib.sha256(key.encode()).digest(), device["device_id"]))
    payload = {**original, "sender_profile_url": sender}
    binding = registry.authorize_binding(grant["binding_id"], device["instance_id"], 6, "query")
    metadata = {"queue_task_id": "fixture-hub-task", "device_id": device["device_id"],
        "instance_id": device["instance_id"], "user_id": user["user_id"], "binding_id": binding.binding_id,
        "knowledge_base_id": 6, "video_id": payload["video_id"], "command_id": payload["command_id"],
        "reference_id": payload["reference_id"], "payload_sha256": payload_sha256(payload)}
    return user, device, key, payload, metadata


def test_two_users_same_kb_number_remote_id_and_opaque_reference_stay_isolated(scope):
    registry, _, migrated, inbox, payload, _, root = scope
    inbox.prepare_schema()
    first = inbox.accept(packet(scope), KEY, payload=payload)
    user, device, key, other_payload, metadata = second_user(scope)
    second = inbox.accept(registry.sign_task(metadata), key, payload=other_payload)
    assert first["knowledge_base_id"] == second["knowledge_base_id"] == 6
    assert first["remote_task_id"] == second["remote_task_id"] and first["receipt_id"] != second["receipt_id"]
    with pytest.raises(DeviceInboxError, match="receipt_not_found"):
        inbox.get_receipt(first["receipt_id"], key, expected_device_id=device["device_id"])
    queue = AssistantQueue(root / "queue.sqlite3")
    results = [inbox.handoff_once(queue, run_id="fixture-run") for _ in range(2)]
    assert len({row["local_task_id"] for row in results}) == 2
    assert queue.counts()["waiting_desktop"] == 2
    registry.revoke_user(migrated["user_id"])
    assert inbox.get_receipt(second["receipt_id"], key, expected_device_id=device["device_id"])["currently_authorized"]


def test_other_users_valid_signature_cannot_attribute_payload_to_first_sender(scope):
    registry, _, _, inbox, first_payload, _, _ = scope
    inbox.prepare_schema()
    _, device, key, _, metadata = second_user(scope)
    changed = {**metadata, "payload_sha256": payload_sha256(first_payload)}
    signed = registry.sign_task(changed)
    with pytest.raises(DeviceInboxError, match="invalid_inbox_payload"):
        inbox.accept(signed, key, payload=first_payload)
    assert counts(registry) == (0, 0, 0)


def test_lost_accept_ack_can_be_read_by_remote_id_after_packet_expiry_without_writes(scope):
    registry, _, migrated, inbox, payload, now, _ = scope
    inbox.prepare_schema()
    original = packet(scope)
    first = inbox.accept(original, KEY, payload=payload)
    now[0] += 301
    before = registry.path.read_bytes()
    result = DeviceInbox(registry).get_receipt_by_remote_task(original["queue_task_id"], KEY,
        expected_device_id=migrated["device_id"])
    assert result["receipt_id"] == first["receipt_id"] and result["state"] == "received"
    assert registry.path.read_bytes() == before
    registry.revoke_grant(migrated["grant_id"])
    held = inbox.get_receipt_by_remote_task(original["queue_task_id"], KEY,
        expected_device_id=migrated["device_id"])
    assert held["receipt_id"] == first["receipt_id"] and not held["currently_authorized"]
    registry.revoke_device(migrated["device_id"])
    with pytest.raises(DeviceInboxError, match="device_authentication_failed"):
        inbox.get_receipt_by_remote_task(original["queue_task_id"], KEY,
            expected_device_id=migrated["device_id"])


@pytest.mark.parametrize("kind", ["clarify", "unknown", "status", "save"])
def test_existing_queue_wrong_kind_cannot_be_adopted_and_bytes_are_preserved(scope, kind):
    registry, binding, migrated, inbox, payload, _, root = scope
    inbox.prepare_schema()
    inbox.accept(packet(scope), KEY, payload=payload)
    queue = AssistantQueue(root / "queue.sqlite3")
    queue.enqueue(binding, payload, kind=kind, user_id=migrated["user_id"],
        device_id=migrated["device_id"],
        authorization=registry.authorization_snapshot(binding.binding_id, binding.instance_id, 6),
        initial_state="waiting_desktop")
    before = queue.path.read_bytes()
    result = inbox.handoff_once(queue, run_id="fixture-run")
    assert result["state"] == "held" and result["reason_code"] == "queue_original_conflict"
    assert result["local_task_id"] is None and queue.path.read_bytes() == before


def test_inbox_does_not_accept_a_remote_id_that_its_receipt_lookup_cannot_address(scope):
    registry, _, _, inbox, payload, _, _ = scope
    inbox.prepare_schema()
    # Legacy v1 metadata validates a stripped form but returns the raw value.
    # This new fixed-schema boundary must reject rather than consume it.
    original = packet(scope, remote_id=" fixture-hub-task ")
    with pytest.raises(DeviceInboxError, match="invalid_inbox_identity"):
        inbox.accept(original, KEY, payload=payload)
    assert counts(registry) == (0, 0, 0)
