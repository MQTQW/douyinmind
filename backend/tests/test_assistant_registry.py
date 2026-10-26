"""Offline registry authorization; temporary files and fake local catalogs."""
import hashlib
import json
import os
import sqlite3

import pytest

from app.assistant import registry as registry_module
from app.assistant.registry import (REQUIRED_MARKER_COMPONENT, REQUIRED_MARKER_NAME,
    Registry, RegistryError, read_local_catalog)
from app.mention_agent.contracts import Binding


A = "https://www.douyin.com/user/registry-fixture-A"
B = "https://www.douyin.com/user/registry-fixture-B"
OTHER = "https://www.douyin.com/user/registry-fixture-other"
CATALOG = [{"id": 6, "name": "编程库"}, {"id": 8, "name": "学习资料"}, {"id": 9, "name": "只读库"}]


@pytest.fixture
def registry(tmp_path):
    now = [1000.0]
    value = Registry(tmp_path / "registry.sqlite3", catalog_reader=lambda: CATALOG, clock=lambda: now[0])
    value.fixture_time = now
    return value


def legacy(registry):
    binding = Binding("legacy-binding-fixture", "legacy-instance-fixture", A, B, 6, True, True)
    result = registry.import_legacy(binding, verified_by="local-admin-fixture", allow_import=True)
    return binding, result


def new_verified(registry, sender=OTHER):
    user = registry.register_user(sender, B, "待验证账号")
    assert user["verification_status"] == "unverified"
    registry.verify_user(user["user_id"], verified_by="local-admin-fixture")
    device = registry.register_device(user["user_id"], is_default=True)
    registry.verify_device(device["device_id"], verified_by="local-admin-fixture")
    registry.mark_local_device_online(device["device_id"])
    return user["user_id"], device["device_id"]


def test_explicit_create_does_not_adopt_preexisting_empty_file(tmp_path):
    path = tmp_path / "registry.sqlite3"
    path.touch()
    identity = path.stat().st_ino
    with pytest.raises(RegistryError, match="registry_missing_or_empty"):
        Registry(path)
    assert path.read_bytes() == b"" and path.stat().st_ino == identity
    assert not (tmp_path / REQUIRED_MARKER_NAME).exists()


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_missing_main_with_orphan_sidecar_is_not_bootstrapped(tmp_path, suffix):
    path = tmp_path / "registry.sqlite3"
    sidecar = tmp_path / (path.name + suffix)
    sidecar.write_bytes(b"unowned recoverable bytes")
    before = sidecar.read_bytes()
    with pytest.raises(RegistryError, match="orphan_sqlite_sidecar"):
        Registry(path)
    assert not path.exists()
    assert sidecar.read_bytes() == before
    assert not (tmp_path / REQUIRED_MARKER_NAME).exists()


def test_bootstrap_requires_this_exclusive_creation_not_racing_file(tmp_path, monkeypatch):
    path = tmp_path / "registry.sqlite3"
    original = registry_module.os.open

    def raced_open(filename, flags, *args, **kwargs):
        if os.fspath(filename) == os.fspath(path) and flags & os.O_EXCL:
            path.touch()
            raise FileExistsError(os.fspath(path))
        return original(filename, flags, *args, **kwargs)

    monkeypatch.setattr(registry_module.os, "open", raced_open)
    with pytest.raises(RegistryError, match="registry_bootstrap_file_changed"):
        Registry(path)
    assert path.read_bytes() == b""
    assert not (tmp_path / REQUIRED_MARKER_NAME).exists()


def test_bootstrap_rechecks_created_fd_identity_before_schema(tmp_path, monkeypatch):
    path = tmp_path / "registry.sqlite3"
    original = Registry._new_schema_connection

    def replaced_main(self, identity):
        self.path.rename(tmp_path / "original-empty.sqlite3")
        self.path.touch()
        return original(self, identity)

    monkeypatch.setattr(Registry, "_new_schema_connection", replaced_main)
    with pytest.raises(RegistryError, match="registry_bootstrap_file_changed"):
        Registry(path)
    assert path.read_bytes() == b""
    assert (tmp_path / "original-empty.sqlite3").read_bytes() == b""
    assert not (tmp_path / REQUIRED_MARKER_NAME).exists()


def test_empty_registry_exposes_candidates_but_no_grants_or_demo(registry):
    snapshot = registry.snapshot()
    assert snapshot["users"] == []
    assert snapshot["local_catalog"] == CATALOG
    assert snapshot["cross_device_status"] == "local_protocol_only_not_remote_verified"
    assert "not_platform_ownership_proof" in snapshot["verification_scope"]


def test_explicit_legacy_import_only_preserves_existing_scope_and_binding_id(registry):
    old, migrated = legacy(registry)
    assert migrated["imported"] is True
    assert registry.authorize_binding(old.binding_id, old.instance_id, 6) == old
    assert registry.local_instance_id == old.instance_id
    user = registry.get_user(migrated["user_id"])
    assert user["verification_status"] == "verified"
    assert user["verification_method"] == "legacy_local_admin_import"
    assert len(user["devices"][0]["libraries"]) == 1
    assert user["devices"][0]["libraries"][0]["knowledge_base_id"] == 6
    again = registry.import_legacy(old, verified_by="local-admin-fixture", allow_import=False)
    assert again["imported"] is False
    assert again["grant"]["can_import"] is True


def test_new_account_never_inherits_legacy_public_or_verification(registry):
    old, migrated = legacy(registry)
    user = registry.register_user(OTHER, B)
    device = registry.register_device(user["user_id"], is_default=True)
    grant = registry.grant_library(user["user_id"], device["device_id"], 8, is_default=True)
    assert grant["public_reply_allowed"] is False and grant["can_import"] is False
    assert registry.authorize_binding(grant["binding_id"], device["instance_id"], 8, "query") is None
    registry.verify_user(user["user_id"], verified_by="local-admin-fixture")
    assert registry.authorize_binding(registry.get_grant(grant["grant_id"])["binding_id"],
                                      device["instance_id"], 8, "query") is None
    registry.verify_device(device["device_id"], verified_by="local-admin-fixture")
    current = registry.get_grant(grant["grant_id"])
    selected = registry.authorize_binding(current["binding_id"], device["instance_id"], 8, "query")
    assert selected and selected.public_reply_allowed is False
    assert registry.authorize_binding(current["binding_id"], device["instance_id"], 8, "import") is None
    assert registry.authorize_binding(old.binding_id, old.instance_id, 6) == old


def test_intake_identity_can_use_import_only_grant_without_authorizing_query(registry):
    old, migrated = legacy(registry)
    grant = registry.grant_library(migrated["user_id"], migrated["device_id"], 6,
        can_query=False, can_import=True, public_reply_allowed=False, is_default=True)
    assert registry.default_binding(A, B, "query") is None
    candidate = registry.intake_binding(A, B)
    assert candidate.binding_id == grant["binding_id"]
    assert registry.authorize_binding(candidate.binding_id, candidate.instance_id, 6, "query") is None
    assert registry.authorize_binding(candidate.binding_id, candidate.instance_id, 6, "import") == candidate
    registry.revoke_user(migrated["user_id"])
    assert registry.intake_binding(A, B) is None


def test_user_revocation_between_authentication_and_heartbeat_write_is_fenced(registry, monkeypatch):
    old, migrated = legacy(registry)
    credential = registry.provision_device_credential(migrated["device_id"])["credential"]
    before = registry.get_device(migrated["device_id"])["last_seen_at"]
    registry.fixture_time[0] += 10
    authenticate = registry.authenticate_device

    def concurrently_revoke(device_id, supplied_credential):
        result = authenticate(device_id, supplied_credential)
        registry.revoke_user(migrated["user_id"])
        return result

    monkeypatch.setattr(registry, "authenticate_device", concurrently_revoke)
    with pytest.raises(RegistryError, match="device_authentication_failed"):
        registry.heartbeat_device(migrated["device_id"], credential)
    assert registry.get_device(migrated["device_id"])["last_seen_at"] == before


def test_permission_change_retires_old_binding_and_never_revives_it(registry):
    old, migrated = legacy(registry)
    updated = registry.grant_library(migrated["user_id"], migrated["device_id"], 6,
        can_import=True, public_reply_allowed=False, is_default=True)
    assert updated["binding_id"] != old.binding_id
    assert registry.authorize_binding(old.binding_id, old.instance_id, 6) is None
    changed = registry.grant_library(migrated["user_id"], migrated["device_id"], 6,
        can_import=True, public_reply_allowed=True, is_default=True)
    assert changed["binding_id"] not in {old.binding_id, updated["binding_id"]}
    assert registry.authorize_binding(old.binding_id, old.instance_id, 6) is None


def test_alias_edit_keeps_binding_scope_but_invalidates_old_parse_snapshot(registry):
    old, migrated = legacy(registry)
    authorization = registry.authorization_snapshot(old.binding_id, old.instance_id, 6, "save")
    assert registry.authorize_snapshot(authorization, action="save") == old
    updated = registry.grant_library(migrated["user_id"], migrated["device_id"], 6,
        aliases=[" 编程 ", "代码库"], can_import=True, public_reply_allowed=True, is_default=True)
    assert updated["binding_id"] == old.binding_id
    assert registry.authorize_snapshot(authorization, action="save") is None
    assert registry.authorize_binding(old.binding_id, old.instance_id, 6) == old


@pytest.mark.parametrize("kind", ["user", "device", "grant"])
def test_revocation_and_reverification_never_restore_old_binding(registry, kind):
    old, migrated = legacy(registry)
    authorization = registry.authorization_snapshot(old.binding_id, old.instance_id, 6, "import")
    if kind == "user":
        registry.revoke_user(migrated["user_id"])
    elif kind == "device":
        registry.revoke_device(migrated["device_id"])
    else:
        registry.revoke_grant(migrated["grant_id"])
    assert registry.authorize_snapshot(authorization) is None
    registry.import_legacy(old, verified_by="local-admin-fixture", allow_import=True)
    assert registry.authorize_binding(old.binding_id, old.instance_id, 6) is None
    if kind == "user":
        registry.verify_user(migrated["user_id"], verified_by="local-admin-fixture")
    elif kind == "device":
        registry.verify_device(migrated["device_id"], verified_by="local-admin-fixture")
    else:
        registry.grant_library(migrated["user_id"], migrated["device_id"], 6,
                               can_import=True, public_reply_allowed=True)
    new = registry.get_grant(migrated["grant_id"])
    assert new["binding_id"] != old.binding_id
    assert registry.authorize_binding(new["binding_id"], old.instance_id, 6) is not None


def test_cross_user_device_grant_is_rejected(registry):
    _, migrated = legacy(registry)
    second, _ = new_verified(registry)
    with pytest.raises(RegistryError, match="device_owner_mismatch"):
        registry.grant_library(second, migrated["device_id"], 8)


def test_remote_device_cannot_use_local_catalog_or_backend_import(registry):
    user, _ = new_verified(registry)
    remote = registry.register_device(user, instance_id="other-computer-fixture")
    registry.verify_device(remote["device_id"], verified_by="local-admin-fixture")
    assert registry.get_device(remote["device_id"])["is_local"] is False
    with pytest.raises(RegistryError, match="remote_catalog_not_verified"):
        registry.grant_library(user, remote["device_id"], 6)
    assert registry.authorize_binding("unknown", "other-computer-fixture", 6) is None


def test_default_can_switch_back_to_older_grant_without_unique_index_race(registry):
    _, migrated = legacy(registry)
    registry.grant_library(migrated["user_id"], migrated["device_id"], 8, is_default=True)
    registry.set_default_library(migrated["user_id"], migrated["device_id"], 6)
    grants = registry.get_device(migrated["device_id"])["libraries"]
    assert [item["knowledge_base_id"] for item in grants if item["is_default"]] == [6]


def test_device_offline_and_authenticated_heartbeat_are_separate_from_authorization(registry):
    old, migrated = legacy(registry)
    provision = registry.provision_device_credential(migrated["device_id"])
    assert registry.authorize_binding(old.binding_id, old.instance_id, 6) == old
    serialized = json.dumps(registry.snapshot())
    assert provision["credential"] not in serialized and "credential_hash" not in serialized
    assert registry.authenticate_device(migrated["device_id"], "wrong") is None
    registry.fixture_time[0] += 91
    assert registry.get_device(migrated["device_id"])["online"] is False
    registry.heartbeat_device(migrated["device_id"], provision["credential"])
    assert registry.get_device(migrated["device_id"])["online"] is True
    rotated = registry.provision_device_credential(migrated["device_id"])
    assert registry.authenticate_device(migrated["device_id"], provision["credential"]) is None
    assert registry.authenticate_device(migrated["device_id"], rotated["credential"])
    assert registry.authorize_binding(old.binding_id, old.instance_id, 6) is None


def test_library_candidates_are_read_only_and_do_not_create_missing_db(tmp_path):
    path = tmp_path / "business.sqlite3"
    with pytest.raises(RegistryError, match="catalog_unavailable"):
        read_local_catalog(path)
    assert not path.exists()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE knowledge_bases(id INTEGER,name TEXT)")
        db.execute("INSERT INTO knowledge_bases VALUES(6,'候选库')")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert read_local_catalog(path) == [{"id": 6, "name": "候选库"}]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_foreign_registry_file_is_preserved(tmp_path):
    path = tmp_path / "business.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE private_rows(value TEXT)")
        db.execute("INSERT INTO private_rows VALUES('preserve')")
    before = path.read_bytes()
    with pytest.raises(RegistryError, match="foreign_registry_database"):
        Registry(path, catalog_reader=lambda: CATALOG)
    assert path.read_bytes() == before


@pytest.mark.parametrize("key,value", [
    ("user_revision", True), ("device_revision", 1.0), ("grant_revision", "1"),
    ("grant_version", False), ("grant_version", 0), ("public_reply_allowed", 1),
    ("knowledge_base_id", "6"), ("action", []), ("user_id", []),
])
def test_authorization_snapshot_requires_exact_field_types(registry, key, value):
    old, _ = legacy(registry)
    authorization = registry.authorization_snapshot(old.binding_id, old.instance_id, 6)
    assert registry.authorize_snapshot(authorization) == old
    assert registry.authorize_snapshot({**authorization, key: value}) is None
    assert registry.authorize_snapshot({**authorization, "unexpected": True}) is None


def test_different_users_same_library_id_have_independent_authority(registry):
    old, migrated = legacy(registry)
    second_user, second_device = new_verified(registry)
    grant = registry.grant_library(second_user, second_device, 6, is_default=True)
    device = registry.get_device(second_device)
    first = registry.authorization_snapshot(old.binding_id, old.instance_id, 6, "query")
    second = registry.authorization_snapshot(grant["binding_id"], device["instance_id"], 6, "query")
    assert first["knowledge_base_id"] == second["knowledge_base_id"] == 6
    assert first["user_id"] != second["user_id"] and first["binding_id"] != second["binding_id"]
    assert registry.authorize_snapshot({**second, "user_id": first["user_id"]}) is None
    assert registry.authorize_snapshot({**second, "grant_id": first["grant_id"]}) is None
    registry.revoke_user(migrated["user_id"])
    assert registry.authorize_snapshot(first) is None
    assert registry.authorize_snapshot(second).sender_profile_url == OTHER


@pytest.mark.parametrize("alias", ["编程\u202e库", "学\u200b习库", "代码\ufeff库", "编\n程库", "Git\x00库"])
def test_invisible_or_direction_control_aliases_are_rejected_without_scope_change(registry, alias):
    old, migrated = legacy(registry)
    before = registry.get_grant(migrated["grant_id"])
    with pytest.raises(RegistryError, match="invalid_alias"):
        registry.grant_library(migrated["user_id"], migrated["device_id"], 6, aliases=[alias])
    assert registry.get_grant(migrated["grant_id"]) == before


def test_normal_unicode_aliases_normalize_without_cross_library_inheritance(registry):
    old, migrated = legacy(registry)
    updated = registry.grant_library(migrated["user_id"], migrated["device_id"], 6,
        aliases=[" Ｇｉｔ ", "git", "编程资料"], can_import=True, public_reply_allowed=True, is_default=True)
    assert updated["aliases"] == ["git", "编程资料"]
    assert updated["binding_id"] == old.binding_id


def test_catalog_loss_fails_authorization_closed_without_exception(registry):
    old, _ = legacy(registry)
    authorization = registry.authorization_snapshot(old.binding_id, old.instance_id, 6)

    def unavailable():
        raise OSError("fake catalog unavailable")

    registry.catalog_reader = unavailable
    assert registry.authorize_snapshot(authorization) is None
    assert registry.authorize_binding(old.binding_id, old.instance_id, 6) is None


def test_legacy_dataclass_is_validated_before_explicit_migration(registry):
    malformed = Binding("bad-binding", "bad-instance", A, B, 6, 1, True)
    with pytest.raises(RegistryError, match="invalid_legacy_binding"):
        registry.import_legacy(malformed, verified_by="local-admin-fixture", allow_import=True)
    assert registry.snapshot()["users"] == []


def test_new_registry_has_durable_fixed_marker_and_existing_open_is_readonly(registry):
    marker = registry.required_marker_path
    assert marker.name == REQUIRED_MARKER_NAME
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "component": REQUIRED_MARKER_COMPONENT, "version": 1}
    before = (registry.path.read_bytes(), marker.read_bytes())
    reopened = Registry(registry.path, catalog_reader=lambda: CATALOG, allow_create=False)
    assert reopened.snapshot()["users"] == []
    assert (registry.path.read_bytes(), marker.read_bytes()) == before


def test_old_premarker_registry_opens_without_writes_and_explicit_migration_sets_marker(registry):
    with sqlite3.connect(registry.path) as db:
        db.execute("DELETE FROM registry_metadata WHERE key='requires_marker'")
    registry.required_marker_path.unlink()
    before = registry.path.read_bytes()
    old = Registry(registry.path, catalog_reader=lambda: CATALOG, allow_create=False)
    assert old.snapshot()["users"] == []
    assert registry.path.read_bytes() == before and not old.required_marker_path.exists()
    binding = Binding("pre-marker-binding", "pre-marker-instance", A, B, 6, True)
    old.import_legacy(binding, verified_by="local-admin-fixture", allow_import=True)
    assert old.required_marker_path.is_file()
    assert Registry(registry.path, catalog_reader=lambda: CATALOG,
                    allow_create=False).authorize_binding(binding.binding_id, binding.instance_id, 6) == binding


def test_missing_required_marker_blocks_existing_registry_and_authorization(registry):
    old, _ = legacy(registry)
    registry.required_marker_path.unlink()
    assert registry.authorize_binding(old.binding_id, old.instance_id, 6) is None
    with pytest.raises(RegistryError, match="registry_required_marker_missing"):
        Registry(registry.path, catalog_reader=lambda: CATALOG, allow_create=False)
    assert not registry.required_marker_path.exists()


def test_lost_registry_with_permanent_marker_is_not_fresh_setup(registry):
    marker = registry.required_marker_path.read_bytes()
    registry.path.unlink()
    for allow_create in (False, True):
        with pytest.raises(RegistryError, match="registry_missing_after_initialization"):
            Registry(registry.path, catalog_reader=lambda: CATALOG, allow_create=allow_create)
    assert not registry.path.exists() and registry.required_marker_path.read_bytes() == marker


@pytest.mark.parametrize("value", [b"not-json", b'{"component":"other","version":1}',
    ('{"component":"' + REQUIRED_MARKER_COMPONENT + '","version":true}').encode()])
def test_invalid_marker_never_overwrites_or_initializes_registry(tmp_path, value):
    marker = tmp_path / REQUIRED_MARKER_NAME
    marker.write_bytes(value)
    path = tmp_path / "registry.sqlite3"
    with pytest.raises(RegistryError, match="invalid_registry_required_marker"):
        Registry(path, catalog_reader=lambda: CATALOG)
    assert not path.exists() and marker.read_bytes() == value


@pytest.mark.parametrize("kind", ["hardlink", "symlink"])
def test_marker_rejects_links_and_preserves_target(tmp_path, kind):
    target = tmp_path / "must-preserve.json"
    original = b'{"private_fixture":"preserve"}'
    target.write_bytes(original)
    marker = tmp_path / REQUIRED_MARKER_NAME
    if kind == "hardlink":
        os.link(target, marker)
    else:
        try:
            marker.symlink_to(target)
        except OSError:
            pytest.skip("symbolic links unavailable in this isolated Windows environment")
    path = tmp_path / "registry.sqlite3"
    with pytest.raises(RegistryError, match="unsafe_registry_required_marker"):
        Registry(path, catalog_reader=lambda: CATALOG)
    assert not path.exists() and target.read_bytes() == original


def test_marker_write_failure_is_reported_and_next_open_stays_blocked(tmp_path, monkeypatch):
    path = tmp_path / "registry.sqlite3"
    marker = tmp_path / REQUIRED_MARKER_NAME
    original_open = registry_module.os.open

    def deny_marker(filename, *args, **kwargs):
        if os.fspath(filename) == os.fspath(marker):
            raise PermissionError("fake marker write denial")
        return original_open(filename, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(registry_module.os, "open", deny_marker)
        with pytest.raises(RegistryError, match="registry_required_marker_write_failed"):
            Registry(path, catalog_reader=lambda: CATALOG)
    assert path.exists() and not marker.exists()
    with pytest.raises(RegistryError, match="registry_required_marker_missing"):
        Registry(path, catalog_reader=lambda: CATALOG, allow_create=False)
    assert not marker.exists()


def test_product_library_revocation_prevents_same_number_recreation_from_reviving_grants(registry):
    old, migrated = legacy(registry)
    second_user, second_device = new_verified(registry)
    second_grant = registry.grant_library(second_user, second_device, 6, is_default=True)
    other = registry.grant_library(migrated["user_id"], migrated["device_id"], 8)
    frozen = registry.authorization_snapshot(old.binding_id, old.instance_id, 6)
    result = registry.revoke_library(6)
    assert result == {"knowledge_base_id": 6, "reason": "library_deleted", "revoked_count": 2}
    assert registry.revoke_library(6)["revoked_count"] == 0
    # Simulate ordinary deletion followed by a new row with the same ID/name.
    registry.catalog_reader = lambda: [item for item in CATALOG if item["id"] != 6]
    assert registry.authorize_snapshot(frozen) is None
    registry.catalog_reader = lambda: CATALOG
    assert registry.authorize_binding(old.binding_id, old.instance_id, 6) is None
    assert registry.get_grant(second_grant["grant_id"])["active"] is False
    assert registry.get_grant(other["grant_id"])["active"] is True
    regranted = registry.grant_library(migrated["user_id"], migrated["device_id"], 6,
                                      can_import=True, public_reply_allowed=True)
    assert regranted["binding_id"] != old.binding_id
    assert registry.authorize_binding(old.binding_id, old.instance_id, 6) is None
    assert registry.get_grant(second_grant["grant_id"])["active"] is False
