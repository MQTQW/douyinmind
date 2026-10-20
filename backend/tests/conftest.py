"""Keep durable diagnostic artifacts from unit tests away from user storage."""
import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def isolated_ingestion_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ingestion_metrics_dir", str(tmp_path / "ingestion_runs"))
    monkeypatch.setattr(settings, "long_video_test_keep_audio", False)
