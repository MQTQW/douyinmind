"""A relocated audio cache must still find the project's prepared tool."""
from pathlib import Path

from app.services import media_service


def test_cache_relocation_preserves_project_tool_and_path_priority(tmp_path, monkeypatch):
    app = tmp_path / "project" / "app"
    module = app / "services" / "media_service.py"
    module.parent.mkdir(parents=True)
    module.touch()
    prepared = app / "storage" / "tools" / "ffmpeg.exe"
    prepared.parent.mkdir(parents=True)
    prepared.write_bytes(b"fixture-not-executed")
    relocated = tmp_path / "other-volume" / "cache"
    monkeypatch.setattr(media_service, "__file__", str(module))
    monkeypatch.setattr(media_service.settings, "audio_cache_dir", str(relocated))
    monkeypatch.setattr(media_service.shutil, "which", lambda name: None)
    assert Path(media_service._resolve_ffmpeg_path()) == prepared

    # Explicit environment selection and an intentionally colocated tool keep
    # their original precedence over the project's fallback installation.
    local_tool = relocated.parent / "tools" / "ffmpeg.exe"
    local_tool.parent.mkdir(parents=True)
    local_tool.write_bytes(b"custom-fixture-not-executed")
    assert Path(media_service._resolve_ffmpeg_path()) == local_tool
    monkeypatch.setattr(media_service.shutil, "which", lambda name: "environment-ffmpeg")
    assert media_service._resolve_ffmpeg_path() == "environment-ffmpeg"
