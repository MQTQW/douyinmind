"""Local-admin routes with isolated registry/catalog; no real account or DB."""
import json
import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.assistant import registry_routes as routes
from app.assistant import registry as registry_module
from app.mention_agent.contracts import Binding


PREFIX = "/api/assistant/registry"
ORIGIN = {"origin": "http://127.0.0.1:5173"}
A = "https://www.douyin.com/user/route-fixture-A"
B = "https://www.douyin.com/user/route-fixture-B"


@pytest.fixture
def local(tmp_path, monkeypatch):
    path = tmp_path / "registry.sqlite3"
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(Binding("old-binding", "old-instance", A, B, 6, True).to_dict()), encoding="utf-8")
    now = [1000.0]
    monkeypatch.setattr(routes, "REGISTRY_PATH", path)
    monkeypatch.setattr(routes, "LEGACY_PATH", legacy)
    monkeypatch.setattr(routes, "_registry", None)
    monkeypatch.setattr(routes, "catalog_reader", lambda: [{"id": 6, "name": "编程库"}, {"id": 8, "name": "学习资料"}])
    monkeypatch.setattr(routes, "admin_sessions", routes.AdminSessions(clock=lambda: now[0]))
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000), headers=ORIGIN) as client:
        yield client, path, now, app


def authenticate(client):
    response = client.post(PREFIX + "/admin-session", json={"confirm_local_admin": True})
    assert response.status_code == 200
    value = response.json()
    client.headers["authorization"] = "Bearer " + value["admin_token"]
    return value


def test_get_missing_returns_real_missing_without_creating_registry(local):
    client, path, _, _ = local
    response = client.get(PREFIX)
    assert response.status_code == 200
    value = response.json()
    assert value["configured"] is False and value["users"] == []
    assert value["local_catalog"] == [{"id": 6, "name": "编程库"}, {"id": 8, "name": "学习资料"}]
    assert value["registry_id"] is None and value["local_instance_id"] is None
    assert not path.exists()


def test_get_missing_parent_remains_unconfigured_without_mkdir(local, monkeypatch):
    client, path, _, _ = local
    new_parent = path.parent / "not-created" / "nested"
    monkeypatch.setattr(routes, "REGISTRY_PATH", new_parent / path.name)
    response = client.get(PREFIX)
    assert response.status_code == 200 and response.json()["configured"] is False
    assert not new_parent.parent.exists()


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_missing_main_orphan_get_and_create_fail_without_changing_sidecar(local, suffix):
    client, path, _, _ = local
    sidecar = path.parent / (path.name + suffix)
    sidecar.write_bytes(b"foreign journal bytes")
    before = sidecar.read_bytes()
    response = client.get(PREFIX)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "orphan_sqlite_sidecar"
    authenticate(client)
    created = client.post(PREFIX + "/users", json={"sender_profile_url": A, "receiver_profile_url": B})
    assert created.status_code == 409
    assert created.json()["detail"]["code"] == "orphan_sqlite_sidecar"
    assert not path.exists()
    assert sidecar.read_bytes() == before
    assert not (path.parent / registry_module.REQUIRED_MARKER_NAME).exists()


def test_get_existing_only_opens_readonly_connections_and_preserves_bytes(local, monkeypatch):
    client, path, _, _ = local
    authenticate(client)
    created = client.post(PREFIX + "/users", json={"sender_profile_url": A, "receiver_profile_url": B})
    assert created.status_code == 200
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(routes, "_registry", None)
    original_connect = registry_module.sqlite3.connect
    opened = []

    def connect(path_value, *args, **kwargs):
        opened.append((str(path_value), kwargs.get("uri")))
        return original_connect(path_value, *args, **kwargs)

    monkeypatch.setattr(registry_module.sqlite3, "connect", connect)
    value = client.get(PREFIX)
    assert value.status_code == 200 and len(value.json()["users"]) == 1
    assert opened and all("mode=ro" in path_value and uri is True for path_value, uri in opened)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_get_empty_registry_does_not_repair_or_create_schema(local):
    client, path, _, _ = local
    path.touch()
    response = client.get(PREFIX)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "registry_missing_or_empty"
    assert path.read_bytes() == b""


def test_lost_registry_with_marker_cannot_get_or_create_a_fresh_setup(local, monkeypatch):
    client, path, _, _ = local
    authenticate(client)
    assert client.post(PREFIX + "/users", json={"sender_profile_url": A, "receiver_profile_url": B}).status_code == 200
    marker = path.parent / registry_module.REQUIRED_MARKER_NAME
    assert marker.is_file()
    path.unlink()
    monkeypatch.setattr(routes, "_registry", None)
    for response in (client.get(PREFIX), client.post(PREFIX + "/users",
        json={"sender_profile_url": A, "receiver_profile_url": B})):
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "registry_missing_after_initialization"
    assert not path.exists()


def test_management_session_requires_explicit_declaration_but_is_not_ownership_proof(local):
    client, path, _, _ = local
    assert client.post(PREFIX + "/admin-session", json={"confirm_local_admin": False}).status_code == 422
    assert client.post(PREFIX + "/admin-session", json={"confirm_local_admin": 1}).status_code == 422
    value = authenticate(client)
    assert value["scope"] == "local_admin_declaration_not_account_ownership_proof"
    assert not path.exists()
    assert value["admin_token"] not in json.dumps(client.get(PREFIX).json())


def test_unauthorized_write_does_not_create_registry(local):
    client, path, _, _ = local
    response = client.post(PREFIX + "/users", json={"sender_profile_url": A, "receiver_profile_url": B})
    assert response.status_code == 401
    assert not path.exists()


def test_explicit_legacy_migration_only_grants_existing_library_and_preserves_binding(local):
    client, path, _, _ = local
    authenticate(client)
    value = client.post(PREFIX + "/migrate-legacy", json={"confirm": True, "allow_import": True}).json()
    assert value["imported"] is True and path.exists()
    snapshot = client.get(PREFIX).json()
    assert snapshot["configured"] is True
    user = snapshot["users"][0]
    assert user["verification_status"] == "verified" and user["verification_method"] == "legacy_local_admin_import"
    assert [grant["knowledge_base_id"] for grant in user["devices"][0]["libraries"]] == [6]
    assert routes.get_registry().authorize_binding("old-binding", "old-instance", 6).public_reply_allowed is True
    again = client.post(PREFIX + "/migrate-legacy", json={"confirm": True, "allow_import": True}).json()
    assert again["imported"] is False


def test_new_user_and_device_need_separate_verification_and_public_choice(local):
    client, _, _, _ = local
    authenticate(client)
    created = client.post(PREFIX + "/users", json={"sender_profile_url": A, "receiver_profile_url": B,
                                                  "label": "临时新账号"}).json()
    assert created["verification_status"] == "unverified"
    user = created["user_id"]
    device = client.post(PREFIX + f"/users/{user}/devices", json={"is_default": True}).json()
    assert device["verification_status"] == "unverified"
    did = device["device_id"]
    grant = client.put(PREFIX + f"/users/{user}/devices/{did}/libraries/6",
                       json={"aliases": ["编程"], "is_default": True}).json()
    assert grant["can_import"] is False and grant["public_reply_allowed"] is False
    assert routes.get_registry().authorize_binding(grant["binding_id"], device["instance_id"], 6, "query") is None
    assert client.post(PREFIX + f"/users/{user}/verify", json={"confirm": True}).status_code == 200
    assert client.post(PREFIX + f"/devices/{did}/verify", json={"confirm": True}).status_code == 200
    current = client.get(PREFIX).json()["users"][0]["devices"][0]["libraries"][0]
    selected = routes.get_registry().authorize_binding(current["binding_id"], device["instance_id"], 6, "query")
    assert selected and selected.public_reply_allowed is False
    assert routes.get_registry().authorize_binding(current["binding_id"], device["instance_id"], 6, "import") is None


def test_no_client_verified_flag_path_or_delivery_url_can_be_smuggled(local):
    client, path, _, _ = local
    authenticate(client)
    for extra in ({"verified": True}, {"path": "C:/private"}, {"url": "https://elsewhere.invalid"}):
        response = client.post(PREFIX + "/users", json={"sender_profile_url": A,
                               "receiver_profile_url": B, **extra})
        assert response.status_code == 422
    assert not path.exists()


def test_verification_method_is_server_chosen(local):
    client, _, _, _ = local
    authenticate(client)
    user = client.post(PREFIX + "/users", json={"sender_profile_url": A, "receiver_profile_url": B}).json()
    response = client.post(PREFIX + f"/users/{user['user_id']}/verify",
                           json={"confirm": True, "method": "pairing"})
    assert response.status_code == 422
    assert client.get(PREFIX).json()["users"][0]["verification_status"] == "unverified"
    assert client.post(PREFIX + f"/users/{user['user_id']}/verify", json={"confirm": False}).status_code == 422


def test_remote_device_cannot_receive_local_catalog_grant(local):
    client, _, _, _ = local
    authenticate(client)
    user = client.post(PREFIX + "/users", json={"sender_profile_url": A, "receiver_profile_url": B}).json()["user_id"]
    device = client.post(PREFIX + f"/users/{user}/devices",
                        json={"instance_id": "other-computer", "is_default": True}).json()
    did = device["device_id"]
    client.post(PREFIX + f"/devices/{did}/verify", json={"confirm": True})
    response = client.put(PREFIX + f"/users/{user}/devices/{did}/libraries/6", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "remote_catalog_not_verified"
    assert client.get(PREFIX).json()["users"][0]["devices"][0]["is_local"] is False


def test_revoke_keeps_tombstone_and_requires_confirmation(local):
    client, _, _, _ = local
    authenticate(client)
    migrated = client.post(PREFIX + "/migrate-legacy", json={"confirm": True, "allow_import": True}).json()
    assert client.request("DELETE", PREFIX + "/grants/" + migrated["grant_id"], json={"confirm": True}).status_code == 200
    grant = client.get(PREFIX).json()["users"][0]["devices"][0]["libraries"][0]
    assert grant["active"] is False
    assert routes.get_registry().authorize_binding("old-binding", "old-instance", 6) is None
    client.post(PREFIX + "/migrate-legacy", json={"confirm": True, "allow_import": True})
    assert client.get(PREFIX).json()["users"][0]["devices"][0]["libraries"][0]["active"] is False


def test_session_expiry_and_origin_change_cannot_mutate(local):
    client, path, now, _ = local
    authenticate(client)
    response = client.post(PREFIX + "/users", headers={"origin": "http://localhost:5173"},
                           json={"sender_profile_url": A, "receiver_profile_url": B})
    assert response.status_code == 401 and not path.exists()
    now[0] += 901
    assert client.post(PREFIX + "/users", json={"sender_profile_url": A, "receiver_profile_url": B}).status_code == 401
    assert not path.exists()


def test_local_origin_json_and_remote_client_guards_precede_creation(local):
    client, path, _, app = local
    assert client.post(PREFIX + "/admin-session", headers={"origin": "https://untrusted.invalid"},
                       json={"confirm_local_admin": True}).status_code == 403
    assert client.post(PREFIX + "/admin-session", content="{}",
                       headers={"content-type": "text/plain"}).status_code == 415
    with TestClient(app, base_url="http://127.0.0.1", client=("203.0.113.1", 50000), headers=ORIGIN) as remote:
        assert remote.get(PREFIX).status_code == 403
    assert not path.exists()
