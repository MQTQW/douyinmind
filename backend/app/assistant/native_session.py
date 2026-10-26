"""Read/write an explicitly app-owned Windows-user DPAPI session capsule.

This is not a browser credential extractor.  An old authorized capsule can be
read only when the trusted local factory supplies that exact sealed path.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import time
import uuid

from .native_transport import SecretSession, decimal_id

_STORAGE = Path(__file__).resolve().parents[1] / "storage"
_PRIVATE = _STORAGE / "assistant" / "native-receiver" / "private"


class NativeSessionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _guard_path(path, root, *, existing=False):
    path, root = Path(path).absolute(), Path(root).absolute()
    if path.resolve() != path or root.resolve() != root or not path.is_relative_to(root):
        raise NativeSessionError("vault_path_not_owned")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise NativeSessionError("vault_path_not_regular")
    if existing and not path.exists():
        raise NativeSessionError("authorization_not_provisioned")
    return path


class WindowsUserDPAPI:
    @staticmethod
    def _transform(payload, entropy, *, decrypt=False):
        if os.name != "nt":
            raise NativeSessionError("windows_dpapi_required")
        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
        source, additional = ctypes.create_string_buffer(payload), ctypes.create_string_buffer(entropy)
        src = Blob(len(payload), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
        ent = Blob(len(entropy), ctypes.cast(additional, ctypes.POINTER(ctypes.c_ubyte)))
        out = Blob()
        library = ctypes.WinDLL("crypt32", use_last_error=True)
        function = library.CryptUnprotectData if decrypt else library.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
        try:
            if not function(ctypes.byref(src), None, ctypes.byref(ent), None, None, 1, ctypes.byref(out)):
                raise NativeSessionError("capsule_unprotect_failed" if decrypt else "capsule_protect_failed")
            return ctypes.string_at(out.data, out.size)
        finally:
            ctypes.memset(source, 0, len(payload))
            if out.data:
                ctypes.memset(out.data, 0, out.size)
                free(out.data)

    def protect(self, data, entropy):
        return self._transform(data, entropy)

    def unprotect(self, data, entropy):
        return self._transform(data, entropy, decrypt=True)


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise NativeSessionError("capsule_invalid")
        result[key] = value
    return result


class NativeSessionVault:
    def __init__(self, expected_uid, *, private_root=None, path=None,
                 legacy_capsule_path=None, protector=None, clock=None):
        self.expected_uid = decimal_id(expected_uid, code="invalid_expected_uid")
        self.private_root = Path(private_root or _PRIVATE).absolute()
        self.path = _guard_path(path or self.private_root / "b-session.dpapi", self.private_root)
        self.legacy_capsule_path = None
        if legacy_capsule_path is not None:
            # Compatibility is limited to a supplied sealed capsule under this
            # app's storage, never a browser DB, directory scan or old ledger.
            candidate = _guard_path(legacy_capsule_path, _STORAGE)
            if candidate.suffix.lower() != ".dpapi":
                raise NativeSessionError("vault_path_not_owned")
            self.legacy_capsule_path = candidate
        self._protector = protector or WindowsUserDPAPI()
        self._clock = clock or time.time
        self._entropy = ("DouyinMind.native-readonly.v1:" + self.expected_uid).encode("ascii")

    def __repr__(self):
        return "<NativeSessionVault app-owned redacted>"

    def load(self):
        root = _STORAGE if self.legacy_capsule_path is not None else self.private_root
        path = _guard_path(self.legacy_capsule_path or self.path, root, existing=True)
        if path.stat().st_size > 131072:
            raise NativeSessionError("capsule_invalid")
        try:
            raw = self._protector.unprotect(path.read_bytes(), self._entropy)
            if type(raw) is not bytes or len(raw) > 131072:
                raise NativeSessionError("capsule_invalid")
            value = json.loads(raw, object_pairs_hook=_strict_pairs)
            if (type(value) is not dict or set(value) != {"version", "uid", "cookie", "ua", "expires_at"}
                    or type(value["version"]) is not int or value["version"] != 1 or value["uid"] != self.expected_uid):
                raise NativeSessionError("capsule_invalid")
            session = SecretSession(value["cookie"], value["ua"], value["uid"], value["expires_at"])
        except NativeSessionError:
            raise
        except Exception:
            raise NativeSessionError("capsule_invalid") from None
        if session.expires_at <= self._clock():
            raise NativeSessionError("authorization_expired")
        return session

    def save(self, session):
        """Called only by an explicit normal app provisioning operation."""
        if type(session) is not SecretSession or session.expected_uid != self.expected_uid:
            raise NativeSessionError("capsule_identity_mismatch")
        if session.expires_at <= self._clock():
            raise NativeSessionError("authorization_expired")
        payload = json.dumps({"version": 1, "uid": self.expected_uid,
                              "cookie": session._cookie_header, "ua": session._user_agent,
                              "expires_at": session.expires_at}, separators=(",", ":")).encode()
        sealed = self._protector.protect(payload, self._entropy)
        if type(sealed) is not bytes or len(sealed) > 131072:
            raise NativeSessionError("capsule_invalid")
        _guard_path(self.path, self.private_root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(".session-" + uuid.uuid4().hex + ".tmp")
        try:
            descriptor = os.open(_guard_path(temporary, self.private_root), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(sealed)
                output.flush()
                os.fsync(output.fileno())
            _guard_path(self.path, self.private_root)
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                _guard_path(temporary, self.private_root).unlink()
