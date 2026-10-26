"""Explicit local-admin registry actions; no automatic migration on GET.

The short in-memory management session records a local operator declaration.
It is not Douyin authentication or proof of ownership, and is not a remote
multi-user login system. No registry path or delivery URL is accepted.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import secrets
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.storage_guard import StorageGuardError, assert_no_orphan_sqlite_files
from .registry import DEFAULT_PATH, REQUIRED_MARKER_NAME, Registry, RegistryError, read_local_catalog
from .routes import local_request


BACKEND = Path(__file__).resolve().parents[2]
LEGACY_PATH = BACKEND / "app/storage/mention_agent/local-binding.json"
REGISTRY_PATH = DEFAULT_PATH
_registry = None
_registry_lock = threading.Lock()


def catalog_reader():
    from app.core.config import settings
    from app.mention_receiver.catalog import configured_database
    return read_local_catalog(configured_database(settings.database_url, BACKEND))


def get_registry(*, create=False):
    global _registry
    with _registry_lock:
        if not REGISTRY_PATH.exists() and not REGISTRY_PATH.is_symlink():
            # An unconfigured GET must not create a parent/lock file. Existing
            # sidecars, however, are an error, not a fresh setup or ownership.
            if REGISTRY_PATH.parent.is_dir():
                try:
                    assert_no_orphan_sqlite_files(REGISTRY_PATH)
                except StorageGuardError as error:
                    raise RegistryError(str(error)) from None
            marker = REGISTRY_PATH.parent / REQUIRED_MARKER_NAME
            if _registry is not None or marker.exists() or marker.is_symlink():
                raise RegistryError("registry_missing_after_initialization")
            if not create:
                return None
        if _registry is None:
            _registry = Registry(REGISTRY_PATH, catalog_reader=catalog_reader, allow_create=create,
                protected_paths=(LEGACY_PATH, BACKEND / "app/storage/mention_agent/agent.sqlite3",
                                 BACKEND / "app/storage/mention_agent/backend-ingestion.sqlite3"))
        return _registry


class AdminSessions:
    TTL_SECONDS = 900

    def __init__(self, clock=None):
        self.clock = clock or time.time
        self._sessions = {}
        self._lock = threading.Lock()

    @staticmethod
    def _context(request):
        return request.client.host, request.headers.get("origin", "")

    def issue(self, request):
        token = secrets.token_urlsafe(32)
        now = self.clock()
        record = {"admin_id": "local-admin-" + secrets.token_hex(8),
                  "expires": now + self.TTL_SECONDS, "context": self._context(request)}
        with self._lock:
            self._sessions = {key: value for key, value in self._sessions.items() if value["expires"] > now}
            self._sessions[hashlib.sha256(token.encode()).digest()] = record
        return {"admin_token": token, "admin_id": record["admin_id"],
                "expires_at_utc": datetime.fromtimestamp(record["expires"], timezone.utc).isoformat(),
                "scope": "local_admin_declaration_not_account_ownership_proof"}

    def authenticate(self, request):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer ") or len(header) > 512:
            return None
        digest = hashlib.sha256(header[7:].encode()).digest()
        with self._lock:
            record = self._sessions.get(digest)
            if not record or record["expires"] <= self.clock() or record["context"] != self._context(request):
                return None
            return record["admin_id"]


admin_sessions = AdminSessions()


def admin_request(request: Request):
    actor = admin_sessions.authenticate(request)
    if actor is None:
        raise HTTPException(401, detail={"code": "local_admin_session_required",
                                        "message": "请先在本人设备上确认进入本机管理。"})
    return actor


router = APIRouter(prefix="/assistant/registry", tags=["助手账号与设备"],
                   dependencies=[Depends(local_request)])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdminConfirmation(StrictBody):
    confirm_local_admin: bool = Field(strict=True)

    @field_validator("confirm_local_admin")
    @classmethod
    def confirmed(cls, value):
        if value is not True:
            raise ValueError("explicit_local_admin_confirmation_required")
        return value


class Confirmation(StrictBody):
    confirm: bool = Field(strict=True)

    @field_validator("confirm")
    @classmethod
    def confirmed(cls, value):
        if value is not True:
            raise ValueError("explicit_confirmation_required")
        return value


class MigrateRequest(Confirmation):
    allow_import: bool = Field(default=False, strict=True)


class UserRequest(StrictBody):
    sender_profile_url: str = Field(max_length=2048)
    receiver_profile_url: str = Field(max_length=2048)
    label: str = Field(default="新账号", max_length=128)


class DeviceRequest(StrictBody):
    label: str = Field(default="本机设备", max_length=128)
    instance_id: str | None = Field(default=None, max_length=128)
    is_default: bool = Field(default=False, strict=True)


class GrantRequest(StrictBody):
    aliases: list[str] = Field(default_factory=list, max_length=20)
    can_query: bool = Field(default=True, strict=True)
    can_import: bool = Field(default=False, strict=True)
    public_reply_allowed: bool = Field(default=False, strict=True)
    is_default: bool = Field(default=False, strict=True)


class DefaultDeviceRequest(StrictBody):
    device_id: str = Field(max_length=128)


class DefaultLibraryRequest(StrictBody):
    knowledge_base_id: int = Field(gt=0, strict=True)


class LocalOwnerAccessRequest(Confirmation):
    query_all_local: bool = Field(default=False,strict=True)
    catalog_read_allowed: bool = Field(default=False,strict=True)
    public_reply_allowed: bool = Field(default=False,strict=True)
    grant_current_libraries: bool = Field(default=False,strict=True)


def fail(error):
    messages = {
        "record_not_found": "这项账号、设备或授权不存在。",
        "local_owner_verified_device_required": "请先明确验证本人的本机账号和设备；远端不会继承本机全部资料权限。",
        "catalog_owner_confirmation_required": "登记当前全部库的保存权限，需要同时明确允许查看本人本机库目录。",
        "local_owner_scope_required": "请先明确允许全部资料查询或本机库目录，不会单独开启没有范围的公开许可。",
        "invalid_local_owner_access_schema": "本人本机全资料权限登记格式异常，已停止授权，不会自动修复或扩大范围。",
        "profile_already_registered": "这个公开账号组合已登记，请管理原记录，不会继承另一人的授权。",
        "device_instance_already_registered": "这个账号已经登记了该设备实例。",
        "remote_catalog_not_verified": "远端设备尚未接通，不能把本机同号库授给另一电脑。",
        "library_not_in_local_catalog": "该知识库不在当前本机候选清单中。",
        "legacy_library_missing": "原绑定的知识库在本机已不存在。",
        "legacy_migration_requires_empty_devices": "已有设备登记，请先核对迁移顺序，未自动改动现有授权。",
        "legacy_profile_already_registered": "原账号已作为新资料登记，请先核对，未自动替换验证状态。",
        "invalid_legacy_binding": "未找到可用的原本机绑定，不会创建演示授权。",
        "catalog_unavailable": "本机知识库候选暂不可读，未改动业务资料。",
        "registry_missing_or_empty": "授权登记库缺失或为空，未自动重建。",
        "registry_missing_after_initialization": "原授权登记库缺失，已停止执行，不会退回旧绑定或自动重建。",
        "registry_required_marker_missing": "授权保护标记缺失，已停止执行，请先恢复原授权状态。",
        "registry_required_marker_write_failed": "授权保护标记未能可靠保存，本次设置未完成，已停止执行。",
        "invalid_registry_required_marker": "授权保护标记无效，已停止执行，不会退回旧绑定。",
        "unsafe_registry_required_marker": "授权保护标记路径异常，已停止执行。",
        "orphan_sqlite_sidecar": "授权登记主文件缺失但仍有数据库恢复文件，已停止执行，请先恢复原授权状态。",
    }
    code = error.code if isinstance(error, RegistryError) else "registry_operation_failed"
    status = 404 if code == "record_not_found" else 422 if code.startswith("invalid_") else 409
    return HTTPException(status, detail={"code": code, "message": messages.get(code, "本机管理操作未完成，请核对资料与授权。")})


def perform(function):
    try:
        return function()
    except Exception as error:
        raise fail(error) from None


@router.get("")
def snapshot():
    try:
        registry = get_registry()
    except Exception as error:
        raise fail(error) from None
    if registry is None:
        try:
            candidates, error = catalog_reader(), None
        except Exception:
            candidates, error = [], "catalog_unavailable"
        return {"configured": False, "registry_id": None, "local_instance_id": None, "users": [],
                "local_catalog": candidates, "catalog_error": error,
                "verification_scope": "local_admin_or_pairing_not_platform_ownership_proof",
                "cross_device_status": "local_protocol_only_not_remote_verified"}
    return perform(lambda: {"configured": True, **registry.snapshot(allow_unavailable_catalog=True)})


@router.post("/admin-session")
def create_admin_session(body: AdminConfirmation, request: Request):
    return admin_sessions.issue(request)


@router.post("/migrate-legacy")
def migrate(body: MigrateRequest, actor=Depends(admin_request)):
    return perform(lambda: get_registry(create=True).import_legacy(
        LEGACY_PATH, verified_by=actor, allow_import=body.allow_import))


@router.post("/users")
def create_user(body: UserRequest, actor=Depends(admin_request)):
    return perform(lambda: get_registry(create=True).register_user(**body.model_dump()))


@router.post("/users/{user_id}/verify")
def verify_user(user_id: str, body: Confirmation, actor=Depends(admin_request)):
    return perform(lambda: get_registry(create=True).verify_user(user_id, verified_by=actor, method="local_admin"))


@router.post("/users/{user_id}/revoke")
def revoke_user(user_id: str, body: Confirmation, actor=Depends(admin_request)):
    return perform(lambda: get_registry(create=True).revoke_user(user_id))


@router.post("/users/{user_id}/devices")
def create_device(user_id: str, body: DeviceRequest, actor=Depends(admin_request)):
    return perform(lambda: get_registry(create=True).register_device(user_id, **body.model_dump()))


@router.post("/devices/{device_id}/verify")
def verify_device(device_id: str, body: Confirmation, actor=Depends(admin_request)):
    def apply():
        registry = get_registry(create=True)
        result = registry.verify_device(device_id, verified_by=actor, method="local_admin")
        return registry.mark_local_device_online(device_id) if result["is_local"] else result
    return perform(apply)


@router.post("/devices/{device_id}/revoke")
def revoke_device(device_id: str, body: Confirmation, actor=Depends(admin_request)):
    return perform(lambda: get_registry(create=True).revoke_device(device_id))


@router.put("/users/{user_id}/devices/{device_id}/libraries/{knowledge_base_id}")
def grant_library(user_id: str, device_id: str, knowledge_base_id: int, body: GrantRequest, actor=Depends(admin_request)):
    return perform(lambda: get_registry(create=True).grant_library(
        user_id, device_id, knowledge_base_id, **body.model_dump()))


@router.put("/users/{user_id}/default-device")
def default_device(user_id: str, body: DefaultDeviceRequest, actor=Depends(admin_request)):
    return perform(lambda: get_registry(create=True).set_default_device(user_id, body.device_id))


@router.put("/users/{user_id}/devices/{device_id}/default-library")
def default_library(user_id: str, device_id: str, body: DefaultLibraryRequest, actor=Depends(admin_request)):
    return perform(lambda: get_registry(create=True).set_default_library(user_id, device_id, body.knowledge_base_id))


@router.delete("/grants/{grant_id}")
def revoke_grant(grant_id: str, body: Confirmation, actor=Depends(admin_request)):
    return perform(lambda: get_registry(create=True).revoke_grant(grant_id))


@router.put("/users/{user_id}/devices/{device_id}/local-owner-access")
def local_owner_access(user_id: str, device_id: str, body: LocalOwnerAccessRequest, actor=Depends(admin_request)):
    return perform(lambda: get_registry(create=True).set_local_owner_access(user_id,device_id,
        **body.model_dump(exclude={"confirm"}),verified_by=actor))


@router.get("/users/{user_id}/devices/{device_id}/library-choices",dependencies=[Depends(admin_request)])
def library_choices(user_id: str, device_id: str):
    def read():
        registry=get_registry()
        if registry is None:
            raise RegistryError("record_not_found")
        return registry.library_choices(user_id,device_id)
    return perform(read)
