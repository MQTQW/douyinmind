"""Independent device-inbox counterchecks: synthetic keys and temporary DBs.

No transport, live account, model, real task, or application database is used.
"""
import ast
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.assistant.device_inbox import DeviceInbox, DeviceInboxError, INBOX, NONCES
from app.assistant.queue import AssistantQueue, QueueError
from app.assistant.registry import Registry, canonical_json, payload_sha256
from app.mention_agent.contracts import Binding


class Case(SimpleNamespace):
    def __repr__(self): return "<synthetic device inbox fixture; credentials omitted>"


def make_case(tmp_path, *, prepare=True):
    now = [1000.0]
    registry = Registry(tmp_path / "registry.sqlite3", catalog_reader=lambda: [{"id": 7, "name": "合成资料库"}], clock=lambda: now[0])
    binding = Binding("inbox-review", "review-local", "https://www.douyin.com/user/inbox-review-A",
                      "https://www.douyin.com/user/inbox-review-B", 7, True)
    migrated = registry.import_legacy(binding, verified_by="synthetic-local-owner", allow_import=True)
    credential = registry.provision_device_credential(migrated["device_id"])["credential"]
    payload = {"command_id": "command:inbox-review", "reference_id": "reference:inbox-review", "reference_quality": "verified_ui",
               "native_comment_id": None, "sender_profile_url": binding.sender_profile_url,
               "receiver_profile_url": binding.receiver_profile_url, "video_id": "7999999000000000044",
               "comment_text": "@合成助手 解释一个合成问题", "source_mode": "isolated",
               "evidence": {"real_mention": True, "sender_verified": True, "video_verified": True,
                            "unique_count": 1, "observed_at_utc": datetime.now(timezone.utc).isoformat()}}
    metadata = {"queue_task_id": "remote-review-task", "device_id": migrated["device_id"],
                "instance_id": binding.instance_id, "user_id": migrated["user_id"], "binding_id": binding.binding_id,
                "knowledge_base_id": 7, "video_id": payload["video_id"], "command_id": payload["command_id"],
                "reference_id": payload["reference_id"], "payload_sha256": payload_sha256(payload)}
    inbox = DeviceInbox(registry)
    if prepare: inbox.prepare_schema()
    return Case(registry=registry, binding=binding, migrated=migrated, credential=credential,
                payload=payload, metadata=metadata, inbox=inbox, now=now)


@pytest.fixture
def case(tmp_path): return make_case(tmp_path)


def seal_as_device(packet, credential, **changes):
    """A device knows this HMAC key; it cannot thereby prove a platform @."""
    unsigned = {key: value for key, value in {**packet, **changes}.items() if key != "signature"}
    signature = hmac.new(hashlib.sha256(credential.encode()).digest(), canonical_json(unsigned).encode(), hashlib.sha256).hexdigest()
    return {**unsigned, "signature": signature}


def counts(case):
    with case.registry._db() as db:
        return {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in (INBOX, NONCES, "device_task_nonces")}


def test_device_self_signed_live_ui_is_not_a_trusted_hub_origin(case):
    packet = case.registry.sign_task(case.metadata)
    forged = {**case.payload, "source_mode": "live_ui"}
    packet = seal_as_device(packet, case.credential, payload_sha256=payload_sha256(forged))
    with pytest.raises(DeviceInboxError, match="origin"):
        case.inbox.accept(packet, case.credential, payload=forged)
    assert set(counts(case).values()) == {0}


def test_nonce_and_complete_inbox_roll_back_together_on_mid_commit_sql_failure(case):
    packet = case.registry.sign_task(case.metadata)
    with case.registry._db(write=True) as db:
        db.execute("CREATE TRIGGER fail_nonce_map BEFORE INSERT ON " + NONCES +
                   " BEGIN SELECT RAISE(ABORT,'synthetic commit failure'); END")
    with pytest.raises(Exception):
        case.inbox.accept(packet, case.credential, payload=case.payload)
    assert set(counts(case).values()) == {0}, "no consumed nonce or half-saved task may survive"
    with case.registry._db(write=True) as db: db.execute("DROP TRIGGER fail_nonce_map")
    accepted = case.inbox.accept(packet, case.credential, payload=case.payload)
    assert accepted["created"] is True and set(counts(case).values()) == {1}


def test_construction_or_unprepared_accept_does_not_adopt_or_prepare_schema(tmp_path):
    sample = make_case(tmp_path, prepare=False)
    with sample.registry._db() as db:
        before = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    packet = sample.registry.sign_task(sample.metadata)
    with pytest.raises(DeviceInboxError, match="not_prepared"):
        sample.inbox.accept(packet, sample.credential, payload=sample.payload)
    with sample.registry._db() as db:
        after = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        assert db.execute("SELECT COUNT(*) FROM device_task_nonces").fetchone()[0] == 0
    assert before == after and INBOX not in after


@pytest.mark.parametrize("field,value", [("video_id", "7999999000000000055"), ("command_id", "command:other"), ("reference_id", "reference:other")])
def test_valid_device_signature_cannot_hide_metadata_envelope_scope_disagreement(case, field, value):
    packet = seal_as_device(case.registry.sign_task(case.metadata), case.credential, **{field: value})
    with pytest.raises(DeviceInboxError, match="scope"):
        case.inbox.accept(packet, case.credential, payload=case.payload)
    assert set(counts(case).values()) == {0}


@pytest.mark.parametrize("extra", [{"url": "https://example.invalid/untrusted"}, {"shell": "arbitrary"}, {"credential": "not-accepted"}])
def test_device_payload_has_no_arbitrary_url_shell_or_credential_fields(case, extra):
    payload = {**case.payload, **extra}
    packet = seal_as_device(case.registry.sign_task(case.metadata), case.credential, payload_sha256=payload_sha256(payload))
    with pytest.raises(DeviceInboxError, match="payload"):
        case.inbox.accept(packet, case.credential, payload=payload)
    assert set(counts(case).values()) == {0}


def test_current_scope_change_rejects_a_previously_signed_new_admission_without_burning_nonce(case):
    packet = case.registry.sign_task(case.metadata)
    case.registry.grant_library(case.migrated["user_id"], case.migrated["device_id"], 7,
                               can_query=True, can_import=True, public_reply_allowed=False, is_default=True)
    with pytest.raises(DeviceInboxError, match="authorization_changed"):
        case.inbox.accept(packet, case.credential, payload=case.payload)
    assert set(counts(case).values()) == {0}


def test_receipt_is_device_scoped_status_and_never_platform_or_execution_authority(case):
    packet = case.registry.sign_task(case.metadata)
    receipt = case.inbox.accept(packet, case.credential, payload=case.payload)
    read = case.inbox.get_receipt(receipt["receipt_id"], case.credential, expected_device_id=case.migrated["device_id"])
    assert read["platform_origin_verified"] is False and read["execution_enabled"] is False
    for key in ("packet", "signature", "credential", "credential_hash", "payload", "evidence", "authorization", "handoff_token"):
        assert key not in read
    assert case.credential not in json.dumps(read)
    with pytest.raises(DeviceInboxError):
        case.inbox.get_receipt(receipt["receipt_id"], case.credential, expected_device_id="another-device")


def test_rotated_current_credential_can_observe_old_receipt_but_cannot_reauthorize_it(case):
    receipt = case.inbox.accept(case.registry.sign_task(case.metadata), case.credential, payload=case.payload)
    replacement = case.registry.provision_device_credential(case.migrated["device_id"])["credential"]
    with pytest.raises(DeviceInboxError, match="authentication"):
        case.inbox.get_receipt(receipt["receipt_id"], case.credential, expected_device_id=case.migrated["device_id"])
    read = case.inbox.get_receipt(receipt["receipt_id"], replacement, expected_device_id=case.migrated["device_id"])
    assert read["currently_authorized"] is False and read["state"] == "received"


def test_another_valid_user_device_cannot_read_first_users_receipt_even_on_same_library_number(case):
    receipt = case.inbox.accept(case.registry.sign_task(case.metadata), case.credential, payload=case.payload)
    other = case.registry.register_user("https://www.douyin.com/user/inbox-other-A", case.binding.receiver_profile_url, "另一合成用户")
    case.registry.verify_user(other["user_id"], verified_by="synthetic-owner")
    device = case.registry.register_device(other["user_id"], instance_id=case.binding.instance_id, label="合成同机设备")
    case.registry.verify_device(device["device_id"], verified_by="synthetic-owner")
    case.registry.grant_library(other["user_id"], device["device_id"], 7, can_query=True, public_reply_allowed=False)
    other_credential = case.registry.provision_device_credential(device["device_id"])["credential"]
    assert case.registry.authenticate_device(device["device_id"], other_credential) is not None
    with pytest.raises(DeviceInboxError, match="receipt_not_found"):
        case.inbox.get_receipt(receipt["receipt_id"], other_credential, expected_device_id=device["device_id"])
    with pytest.raises(DeviceInboxError, match="receipt_not_found"):
        case.inbox.get_receipt_by_remote_task(case.metadata["queue_task_id"], other_credential, expected_device_id=device["device_id"])


def test_lost_accept_response_is_readable_by_remote_id_after_expiry_without_reacceptance(case):
    receipt = case.inbox.accept(case.registry.sign_task(case.metadata, ttl_seconds=5), case.credential, payload=case.payload)
    before = counts(case)
    case.now[0] += 6
    observed = case.inbox.get_receipt_by_remote_task(case.metadata["queue_task_id"], case.credential,
                                                   expected_device_id=case.migrated["device_id"])
    assert observed["receipt_id"] == receipt["receipt_id"] and observed["state"] == "received"
    assert observed["execution_enabled"] is False and counts(case) == before


@pytest.mark.parametrize("field", ["queue_task_id", "command_id", "reference_id"])
def test_device_signed_raw_whitespace_ids_cannot_create_unaddressable_receipts(case, field):
    original = case.registry.sign_task(case.metadata)
    value = " " + original[field]
    payload = dict(case.payload)
    if field in {"command_id", "reference_id"}: payload[field] = value
    packet = seal_as_device(original, case.credential, **{field: value}, payload_sha256=payload_sha256(payload))
    with pytest.raises(DeviceInboxError, match="identity"):
        case.inbox.accept(packet, case.credential, payload=payload)
    assert set(counts(case).values()) == {0}


def test_new_handoff_is_quarantined_and_cannot_be_promoted_by_a_result_callback(case, tmp_path):
    case.inbox.accept(case.registry.sign_task(case.metadata), case.credential, payload=case.payload)
    queue = AssistantQueue(tmp_path / "queue.sqlite3")
    receipt = case.inbox.handoff_once(queue, run_id="synthetic-handoff")
    task = queue.get_task(receipt["local_task_id"])
    assert task["state"] == "waiting_desktop" and task["envelope"]["source_mode"] == "isolated"
    result = queue.apply_reconciliation(task["id"], {"state": "answer_ready", "binding_active": True,
        "public_reply_allowed": True, "safe_to_publish": True, "send_token_present": False, "reply_state": "ready"})
    assert result["state"] == "waiting_desktop"
    assert queue.claim_next("execute") is None and queue.claim_next("publish") is None
    with pytest.raises(QueueError): queue.resume(task["id"])


def test_foundation_is_not_imported_by_any_live_entry_or_http_router():
    backend = Path(__file__).resolve().parents[1]
    paths = [backend / "app/main.py", backend / "app/api/router.py"]
    paths += list((backend / "app/api/routes").glob("*.py"))
    paths += [p for p in (backend / "app/assistant").glob("*.py") if p.name != "device_inbox.py"]
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf8"))):
            if isinstance(node, ast.Import): assert all("device_inbox" not in item.name for item in node.names), str(path)
            if isinstance(node, ast.ImportFrom):
                assert "device_inbox" not in (node.module or ""), str(path)
                assert all(item.name != "DeviceInbox" for item in node.names), str(path)
