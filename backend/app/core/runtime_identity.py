"""Opaque local-copy/runtime fingerprints without loading SDKs or secret values.

The backend captures these once at import. A launcher computes them afresh in
the selected backend environment; matching is local process identification,
not an authentication credential or an assertion about platform delivery.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys


IDENTITY_FIELDS = ("project_id", "backend_code_id", "dependency_id", "config_revision")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_EXCLUDED = {"storage", "exports", "__pycache__", ".venv", "node_modules", "dist",
             "cache", ".pytest_cache", ".git", "logs", "tmp", "temp"}
_REPARSE = 0x400
_PROCESS_CONFIG = frozenset(name.lower() for name in (
    "DATABASE_URL", "CHROMA_PERSIST_DIR", "AUDIO_CACHE_DIR", "VIDEO_CACHE_DIR",
    "INGESTION_JOB_DB_PATH", "PLAYWRIGHT_USER_DATA_DIR", "PLAYWRIGHT_BROWSERS_PATH",
    "KNOWLEDGE_ANALYSIS_MODE", "LLM_MODEL", "LLM_BASE_URL", "EMBEDDING_MODEL",
    "EMBEDDING_BASE_URL", "QWEN_OMNI_MODEL", "QWEN_OMNI_BASE_URL",
))


class RuntimeIdentityError(RuntimeError):
    """Fixed diagnostics only; source paths and package metadata are not public."""


def normalized_project_path(project_root, *, platform_name=None):
    value = str(Path(project_root).resolve()).replace("\\", "/")
    if (os.name if platform_name is None else platform_name) == "nt":
        value = value.lower()
    return value.rstrip("/")


def _digest(value):
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def project_identifier(project_root, *, platform_name=None):
    return hashlib.sha256(normalized_project_path(project_root, platform_name=platform_name).encode("utf-8")).hexdigest()


def _regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & _REPARSE or path.resolve() != path):
        raise RuntimeIdentityError("runtime_source_unavailable")


def _source_manifest(backend_root):
    app_root = backend_root / "app"
    files = []
    if app_root.exists():
        app_info = app_root.lstat()
        if (not stat.S_ISDIR(app_info.st_mode) or getattr(app_info, "st_file_attributes", 0) & _REPARSE
                or app_root.resolve() != app_root):
            raise RuntimeIdentityError("runtime_source_unavailable")
        for directory, directories, names in os.walk(app_root, followlinks=False):
            parent = Path(directory)
            keep = []
            for name in directories:
                if name.startswith(".") or name in _EXCLUDED:
                    continue
                path = parent / name
                info = path.lstat()
                if (not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE
                        or path.resolve() != path):
                    raise RuntimeIdentityError("runtime_source_unavailable")
                keep.append(name)
            directories[:] = sorted(keep)
            for name in names:
                if name.startswith(".") or not name.endswith(".py"):
                    continue
                path = parent / name
                _regular(path)
                files.append((path.relative_to(backend_root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    else:
        files.append(("app/", "missing"))
    for name in ("pyproject.toml", "uv.lock"):
        path = backend_root / name
        if not path.exists():
            files.append((name, "missing"))
        else:
            _regular(path)
            files.append((name, hashlib.sha256(path.read_bytes()).hexdigest()))
    return sorted(files)


def _dependency_records(dependency_versions):
    if dependency_versions is None:
        pairs = [(distribution.metadata.get("Name"), distribution.version)
                 for distribution in importlib.metadata.distributions()]
    else:
        pairs = list(dependency_versions.items())
    records = []
    for name, version in pairs:
        if (type(name) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) is None
                or type(version) is not str or not version.strip() or len(version) > 256
                or any(ord(char) < 32 for char in version)):
            raise RuntimeIdentityError("runtime_dependencies_unavailable")
        records.append((re.sub(r"[-_.]+", "-", name).lower(), version))
    return sorted(records)


def _config_revision(backend_root, process_environment=None):
    # Never open/read .env; only its existence/size/mtime metadata contributes.
    try:
        info = (backend_root / ".env").stat()
    except FileNotFoundError:
        metadata = {"exists": False}
    else:
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeIdentityError("runtime_config_metadata_unavailable")
        metadata = {"exists": True, "size": info.st_size, "mtime_ns": info.st_mtime_ns}
    # Pydantic Settings is case-insensitive here. Iterate names only: reading
    # every environment value would also touch API keys/cookies unnecessarily.
    # For duplicate case variants, the last value wins just as parse_env_vars.
    environment = os.environ if process_environment is None else process_environment
    selected = {}
    for name in environment:
        normalized = name.lower()
        if normalized in _PROCESS_CONFIG:
            value = environment[name]
            if type(value) is not str:
                raise RuntimeIdentityError("runtime_config_metadata_unavailable")
            selected[normalized] = value
    return _digest({"schema": "env-metadata-and-process-v2", "metadata": metadata,
                    "nonsecret_process_config": selected})


def capture_runtime_identity(project_root=None, *, dependency_versions=None, process_environment=None):
    """Return only four opaque hashes; missing guard-copy files are supported.

    dependency_versions is a pure test injection. Production fingerprints every
    installed distribution name/version, including transitive dependencies.
    """
    try:
        if project_root is None:
            backend_root = Path(__file__).resolve().parents[2]
            project_root = backend_root.parent
        else:
            project_root = Path(project_root).resolve()
            backend_root = project_root / "backend"
        records = _dependency_records(dependency_versions)
        return {"project_id": project_identifier(project_root),
            "backend_code_id": _digest({"schema": "backend-public-code-v1", "sources": _source_manifest(backend_root)}),
            "dependency_id": _digest({"schema": "installed-distributions-v1", "packages": records,
                "python": list(sys.version_info[:3]), "implementation": sys.implementation.name,
                "pointer_bits": struct.calcsize("P") * 8}),
            "config_revision": _config_revision(backend_root, process_environment)}
    except RuntimeIdentityError:
        raise
    except Exception:
        raise RuntimeIdentityError("runtime_identity_unavailable") from None


def valid_identity(value):
    return isinstance(value, dict) and all(type(value.get(key)) is str and _HEX.fullmatch(value[key])
                                          for key in IDENTITY_FIELDS)


def identity_matches(actual, expected):
    return valid_identity(actual) and valid_identity(expected) and all(actual[key] == expected[key] for key in IDENTITY_FIELDS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path)
    args = parser.parse_args()
    try:
        identity = capture_runtime_identity(args.project_root)
    except RuntimeIdentityError:
        print(json.dumps({"error": "runtime_identity_unavailable"}))
        return 1
    print(json.dumps(identity, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
