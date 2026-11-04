"""Opaque fingerprints from temporary public sources, never real keys or SDKs."""
import hashlib
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from app.core import runtime_identity as identity


VERSIONS = {"openai": "1.0", "httpx": "2.0", "starlette": "3.0"}


def clone(tmp_path, name="CloneA"):
    project = tmp_path / name
    app = project / "backend" / "app"
    app.mkdir(parents=True)
    (app / "main.py").write_text("value = 'old'\n", encoding="utf-8")
    (project / "backend" / "pyproject.toml").write_text('[project]\nname="synthetic"\n', encoding="utf-8")
    (project / "backend" / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return project


def capture(project, **kwargs):
    return identity.capture_runtime_identity(project, dependency_versions=kwargs.get("versions", VERSIONS))


def test_two_local_clones_have_different_opaque_project_ids_and_same_public_code(tmp_path):
    first, second = clone(tmp_path), clone(tmp_path, "CloneB")
    a, b = capture(first), capture(second)
    assert a["project_id"] != b["project_id"]
    assert a["backend_code_id"] == b["backend_code_id"] and a["dependency_id"] == b["dependency_id"]
    assert identity.valid_identity(a) is True and set(a) == set(identity.IDENTITY_FIELDS)
    serialized = json.dumps(a)
    assert str(first) not in serialized and "openai" not in serialized and "starlette" not in serialized


def test_normalization_matches_the_frontend_protocol_on_both_platform_rules(tmp_path):
    project = clone(tmp_path)
    resolved = str(project.resolve()).replace("\\", "/").rstrip("/")
    assert identity.normalized_project_path(project, platform_name="nt") == resolved.lower()
    assert identity.normalized_project_path(project, platform_name="posix") == resolved
    assert identity.project_identifier(project, platform_name="nt") == hashlib.sha256(resolved.lower().encode()).hexdigest()


@pytest.mark.parametrize("target", ["app/main.py", "pyproject.toml", "uv.lock"])
def test_public_content_changes_invalidate_even_if_mtime_and_size_are_preserved(tmp_path, target):
    project = clone(tmp_path)
    before = capture(project)
    path = project / "backend" / target
    info = path.stat()
    original = path.read_bytes()
    changed = original.replace(b"old", b"new") if b"old" in original else bytes([original[0] ^ 1]) + original[1:]
    path.write_bytes(changed)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    after = capture(project)
    assert before["backend_code_id"] != after["backend_code_id"]
    assert before["project_id"] == after["project_id"] and before["config_revision"] == after["config_revision"]


def test_source_addition_and_deletion_change_the_code_manifest(tmp_path):
    project = clone(tmp_path)
    before = capture(project)
    extra = project / "backend" / "app" / "additional.py"
    extra.write_text("value = 2\n", encoding="utf-8")
    assert capture(project)["backend_code_id"] != before["backend_code_id"]
    extra.unlink()
    assert capture(project)["backend_code_id"] == before["backend_code_id"]


def test_all_installed_versions_including_transitive_packages_affect_identity(tmp_path):
    project = clone(tmp_path)
    before = capture(project)
    after = capture(project, versions={**VERSIONS, "starlette": "3.1"})
    assert before["dependency_id"] != after["dependency_id"]
    assert before["backend_code_id"] == after["backend_code_id"]
    assert capture(project, versions=dict(reversed(list(VERSIONS.items())))) == before


def test_production_distribution_enumeration_uses_only_name_and_version(tmp_path, monkeypatch):
    project = clone(tmp_path)
    monkeypatch.setattr(identity.importlib.metadata, "distributions", lambda: [
        SimpleNamespace(metadata={"Name": key}, version=value) for key, value in VERSIONS.items()])
    assert identity.capture_runtime_identity(project) == capture(project)


def test_config_revision_reads_metadata_only_and_private_storage_is_not_hashed(tmp_path, monkeypatch):
    project = clone(tmp_path)
    env = project / "backend" / ".env"
    env.write_text("PRIVATE_SYNTHETIC_KEY=do-not-read\n", encoding="utf-8")
    private = project / "backend" / "app" / "storage"
    private.mkdir()
    (private / "private.py").write_text("synthetic_secret = 'do-not-read'\n", encoding="utf-8")
    original_open = Path.open
    opened = []
    def checked_open(path, *args, **kwargs):
        assert path != env and private not in path.parents, "identity must never read private files"
        opened.append(path)
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", checked_open)
    before = capture(project)
    info = env.stat()
    os.utime(env, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
    after = capture(project)
    assert before["config_revision"] != after["config_revision"]
    assert before["backend_code_id"] == after["backend_code_id"]
    assert all(".env" != path.name for path in opened)
    assert "do-not-read" not in json.dumps(after)


def test_new_env_presence_changes_revision_without_public_code_change(tmp_path):
    project = clone(tmp_path)
    before = capture(project)
    (project / "backend" / ".env").write_text("SYNTHETIC=not-a-real-key", encoding="utf-8")
    after = capture(project)
    assert before["config_revision"] != after["config_revision"]
    assert before["backend_code_id"] == after["backend_code_id"]


def test_guard_copy_layout_without_backend_manifests_does_not_break_capture(tmp_path, monkeypatch):
    file = tmp_path / "copy" / "app" / "core" / "runtime_identity.py"
    file.parent.mkdir(parents=True)
    file.write_text("# public copied fixture\n", encoding="utf-8")
    monkeypatch.setattr(identity, "__file__", str(file))
    value = identity.capture_runtime_identity(dependency_versions=VERSIONS)
    assert identity.valid_identity(value) is True


@pytest.mark.parametrize("unsafe", ["link", "hardlink"])
def test_source_file_safety_failure_is_fixed_and_does_not_read_the_file(tmp_path, monkeypatch, unsafe):
    project = clone(tmp_path)
    target = project / "backend" / "app" / "main.py"
    original = Path.lstat
    def metadata(path):
        if path == target:
            return SimpleNamespace(st_mode=stat.S_IFLNK if unsafe == "link" else stat.S_IFREG,
                                   st_nlink=1 if unsafe == "link" else 2, st_file_attributes=0)
        return original(path)
    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(identity.RuntimeIdentityError, match="^runtime_source_unavailable$"):
        capture(project)


def test_unavailable_metadata_raises_fixed_code_without_package_or_path_details(tmp_path, monkeypatch):
    project = clone(tmp_path)
    def unavailable():
        raise OSError("private distribution path and provider key")
    monkeypatch.setattr(identity.importlib.metadata, "distributions", unavailable)
    with pytest.raises(identity.RuntimeIdentityError, match="^runtime_identity_unavailable$"):
        identity.capture_runtime_identity(project)


def test_missing_or_changed_identity_cannot_match_a_captured_server(tmp_path):
    project = clone(tmp_path)
    captured = capture(project)
    assert identity.identity_matches({"status": "ok", "service": "DouyinRAG"}, captured) is False
    assert identity.identity_matches(captured, captured) is True
    for field in identity.IDENTITY_FIELDS:
        changed = {**captured, field: "f" * 64 if captured[field] != "f" * 64 else "e" * 64}
        assert identity.identity_matches(captured, changed) is False


@pytest.mark.parametrize("field", [
    "DATABASE_URL", "CHROMA_PERSIST_DIR", "AUDIO_CACHE_DIR", "VIDEO_CACHE_DIR", "INGESTION_JOB_DB_PATH",
    "PLAYWRIGHT_USER_DATA_DIR", "PLAYWRIGHT_BROWSERS_PATH", "KNOWLEDGE_ANALYSIS_MODE", "LLM_MODEL",
    "LLM_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL", "QWEN_OMNI_MODEL", "QWEN_OMNI_BASE_URL",
])
def test_nonsecret_process_config_overrides_change_revision_without_env_file_changes(tmp_path, field):
    project = clone(tmp_path)
    first = identity.capture_runtime_identity(project, dependency_versions=VERSIONS,
        process_environment={field: "synthetic-original-value"})
    second = identity.capture_runtime_identity(project, dependency_versions=VERSIONS,
        process_environment={field: "synthetic-temporary-override"})
    assert first["config_revision"] != second["config_revision"]
    assert all(first[key] == second[key] for key in ("project_id", "backend_code_id", "dependency_id"))
    assert "synthetic-temporary-override" not in json.dumps(second)


def test_process_config_names_follow_pydantic_case_insensitive_last_value_semantics(tmp_path):
    project = clone(tmp_path)
    def revision(environment):
        return identity.capture_runtime_identity(project, dependency_versions=VERSIONS,
            process_environment=environment)["config_revision"]
    assert revision({"DATABASE_URL": "same"}) == revision({"database_url": "same"})
    assert revision({"DATABASE_URL": "first", "database_url": "second"}) == revision({"DATABASE_URL": "second"})


def test_secret_environment_values_are_never_read_or_exposed(tmp_path):
    project = clone(tmp_path)
    class TrackedEnvironment(dict):
        def __getitem__(self, key):
            assert key.lower() in identity._PROCESS_CONFIG, "a secret/irrelevant environment value was read"
            return super().__getitem__(key)
    environment = TrackedEnvironment(DATABASE_URL="sqlite:///synthetic.db", OPENAI_API_KEY="private-one",
                                     DASHSCOPE_API_KEY="private-two", SOME_COOKIE="private-three")
    first = identity.capture_runtime_identity(project, dependency_versions=VERSIONS, process_environment=environment)
    environment["OPENAI_API_KEY"] = "private-changed"
    second = identity.capture_runtime_identity(project, dependency_versions=VERSIONS, process_environment=environment)
    assert first == second
    assert "private-" not in json.dumps(second)


def test_launcher_only_process_variables_do_not_change_backend_config_revision(tmp_path):
    project = clone(tmp_path)
    first = identity.capture_runtime_identity(project, dependency_versions=VERSIONS, process_environment={})
    second = identity.capture_runtime_identity(project, dependency_versions=VERSIONS,
        process_environment={"DOUYINMIND_API_TARGET": "http://synthetic-other-target", "PYTHONIOENCODING": "utf-8"})
    assert first == second


def test_missing_and_explicit_default_process_config_may_conservatively_differ(tmp_path):
    project = clone(tmp_path)
    absent = identity.capture_runtime_identity(project, dependency_versions=VERSIONS, process_environment={})
    explicit = identity.capture_runtime_identity(project, dependency_versions=VERSIONS,
        process_environment={"KNOWLEDGE_ANALYSIS_MODE": "audio"})
    assert absent["config_revision"] != explicit["config_revision"]
