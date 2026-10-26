"""Default backend factory authorization fences, with no production dependencies.

The factory's module file, storage paths, settings, SQL session provider,
worker, media pipeline, and Chroma provider are all replaced by local fixtures.
Only public binding authorization is invoked; no import is submitted.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace

import pytest

from app.assistant.registry import Registry, REQUIRED_MARKER_NAME
from app.mention_agent.contracts import Binding
from app.services import mention_ingestion_service as service_module
from app.services.mention_ingestion_service import MentionIngestionError


@pytest.fixture
def factory(tmp_path, monkeypatch):
    backend = tmp_path / "isolated-backend"
    legacy_root = backend / "app/storage/mention_agent"
    legacy_root.mkdir(parents=True)
    database = backend / "business.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE knowledge_bases(id INTEGER PRIMARY KEY,name TEXT)")
        db.execute("INSERT INTO knowledge_bases VALUES(6,'隔离编程库')")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    binding = Binding("backend-fence-binding", "backend-fence-instance",
        "https://www.douyin.com/user/backend-fence-A", "https://www.douyin.com/user/backend-fence-B", 6, True)
    legacy = legacy_root / "local-binding.json"
    legacy.write_text(json.dumps(binding.to_dict()), encoding="utf-8")
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("unexpected-production-dependency")
        raise AssertionError("no SQL session, worker, pipeline, index, or model may run")

    worker = SimpleNamespace(start=forbidden, submit=forbidden, get_progress=forbidden, _running=True)

    def fake_module(name, **attributes):
        value = ModuleType(name)
        value.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, value)

    fake_module("app.core.config", settings=SimpleNamespace(
        chroma_persist_dir="app/storage/chroma", playwright_user_data_dir="app/storage/playwright"))
    fake_module("app.db.session", engine=SimpleNamespace(url=SimpleNamespace(database=str(database))),
                session_factory=forbidden)
    fake_module("app.services.worker", worker=worker)
    fake_module("app.services.knowledge_service", knowledge_service=SimpleNamespace(_run_sync=forbidden))
    fake_module("app.services.chroma_service", get_chroma_service=forbidden)
    monkeypatch.setattr(service_module, "__file__", str(backend / "app/services/mention_ingestion_service.py"))
    monkeypatch.setattr(service_module, "DEFAULT_ROOT", legacy_root)
    original_service = service_module.MentionIngestionService

    def isolated_service(**kwargs):
        # Function defaults were evaluated before patching DEFAULT_ROOT.
        return original_service(**kwargs, binding_path=legacy,
                                receipt_path=legacy_root / "backend-ingestion.sqlite3")

    monkeypatch.setattr(service_module, "MentionIngestionService", isolated_service)
    registry_path = backend / "app/storage/assistant/registry.sqlite3"

    def registry():
        return Registry(registry_path, database_path=database)

    payload = {"request_id": "backend-fence-request", "binding_id": binding.binding_id,
               "instance_id": binding.instance_id, "knowledge_base_id": 6, "video_id": "7529030669884853530"}
    context = SimpleNamespace(build=service_module.build_default_service, registry=registry,
        binding=binding, payload=payload, registry_path=registry_path,
        marker=registry_path.parent / REQUIRED_MARKER_NAME, calls=calls, legacy_root=legacy_root,
        database=database, legacy=legacy)
    yield context
    assert calls == []
    assert not (legacy_root / "backend-ingestion.sqlite3").exists()
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def denied(service, payload):
    with pytest.raises(MentionIngestionError) as error:
        service._binding_for(payload)
    assert error.value.status_code == 403


def test_never_configured_registry_keeps_only_original_legacy_compatibility(factory):
    service = factory.build()
    assert service._binding_for(factory.payload) == factory.binding
    assert not factory.registry_path.exists() and not factory.marker.exists()


@pytest.mark.parametrize("state", ["unverified", "revoked", "no_import"])
def test_registry_denial_does_not_fall_back_to_active_legacy_file(factory, state):
    registry = factory.registry()
    current_scope = dict(factory.payload)
    if state == "unverified":
        user = registry.register_user(factory.binding.sender_profile_url, factory.binding.receiver_profile_url)
        device = registry.register_device(user["user_id"], is_default=True)
        grant = registry.grant_library(user["user_id"], device["device_id"], 6, can_import=True, is_default=True)
        current_scope.update(binding_id=grant["binding_id"], instance_id=device["instance_id"])
    else:
        migrated = registry.import_legacy(factory.binding, verified_by="local-admin-fixture", allow_import=True)
        if state == "revoked":
            grant = registry.revoke_grant(migrated["grant_id"])
        else:
            grant = registry.grant_library(migrated["user_id"], migrated["device_id"], 6,
                can_import=False, public_reply_allowed=True, is_default=True)
        current_scope["binding_id"] = grant["binding_id"]
    service = factory.build()
    denied(service, factory.payload)
    # Denial also holds for the current matching ID; it is not just a stale-ID test.
    denied(service, current_scope)
    assert Binding.from_dict(json.loads(factory.legacy.read_text(encoding="utf-8"))) == factory.binding


def test_cached_registry_loss_never_reverts_to_legacy_even_when_marker_also_disappears(factory):
    registry = factory.registry()
    registry.import_legacy(factory.binding, verified_by="local-admin-fixture", allow_import=True)
    service = factory.build()
    assert service._binding_for(factory.payload) == factory.binding
    factory.registry_path.unlink()
    factory.marker.unlink()
    denied(service, factory.payload)
    assert not factory.registry_path.exists() and not factory.marker.exists()


def test_restart_with_only_permanent_marker_does_not_recreate_registry_or_use_legacy(factory):
    registry = factory.registry()
    registry.import_legacy(factory.binding, verified_by="local-admin-fixture", allow_import=True)
    marker = factory.marker.read_bytes()
    factory.registry_path.unlink()
    denied(factory.build(), factory.payload)
    assert not factory.registry_path.exists() and factory.marker.read_bytes() == marker


def test_corrupted_registry_or_marker_is_not_repaired_or_treated_as_old_mode(factory):
    registry = factory.registry()
    registry.import_legacy(factory.binding, verified_by="local-admin-fixture", allow_import=True)
    factory.registry_path.write_bytes(b"foreign-database-fixture")
    denied(factory.build(), factory.payload)
    assert factory.registry_path.read_bytes() == b"foreign-database-fixture"
    factory.registry_path.unlink()
    factory.marker.write_bytes(b"invalid-marker-fixture")
    denied(factory.build(), factory.payload)
    assert not factory.registry_path.exists() and factory.marker.read_bytes() == b"invalid-marker-fixture"


def test_broken_marker_symlink_is_present_for_fail_closed_decision(factory):
    factory.marker.parent.mkdir(parents=True)
    try:
        factory.marker.symlink_to(factory.marker.parent / "missing-fixture.json")
    except OSError:
        pytest.skip("symbolic links unavailable in this isolated Windows environment")
    assert not factory.marker.exists() and factory.marker.is_symlink()
    denied(factory.build(), factory.payload)
    assert not factory.registry_path.exists() and factory.marker.is_symlink()
