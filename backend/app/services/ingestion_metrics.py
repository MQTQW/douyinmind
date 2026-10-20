"""Durable processing timings, separate from transcripts and model credentials."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import time
import uuid

from app.core.config import settings

logger = logging.getLogger(__name__)


def _video_directory(video_id: str, root: Path | None = None) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", video_id):
        return None
    return (root or Path(settings.ingestion_metrics_dir)) / video_id


class IngestionRun:
    """One attempt. Monotonic elapsed times exclude the queue and previous runs."""

    def __init__(self, video_id: str, title: str, task_id: str, mode: str) -> None:
        folder = _video_directory(video_id)
        self.started = time.perf_counter()
        self.stage_started: float | None = None
        self.finished = False
        run_id = uuid.uuid4().hex
        self.path = folder / f"{run_id}.json" if folder else None
        self.data = {"schema_version": 1, "run_id": run_id, "video_id": video_id,
                     "title": title, "task_id": task_id, "analysis_mode": mode,
                     "started_at": self._now(), "ended_at": None, "status": "running",
                     "current_stage": None, "total_processing_seconds": None,
                     "queue_wait_seconds": None, "stages": {},
                     "timing_scope": "本次实际处理耗时，不含排队、先前尝试和后续问答"}
        self._save()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def _save(self) -> None:
        # Observability must not turn a successfully indexed video into failure.
        if self.path is None:
            return
        temporary = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(self.data, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            logger.warning("处理耗时记录未能保存：video=%s", self.data["video_id"])
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def start_stage(self, name: str) -> None:
        if self.finished:
            return
        if self.data["current_stage"] is not None:
            self.end_stage()
        self.stage_started = time.perf_counter()
        self.data["current_stage"] = name
        self.data["stages"][name] = {"started_at": self._now(), "ended_at": None,
                                      "seconds": None, "status": "running"}
        self._save()

    def end_stage(self, status: str = "done") -> None:
        name = self.data["current_stage"]
        if name is None or self.stage_started is None:
            return
        self.data["stages"][name].update(ended_at=self._now(), status=status,
                                        seconds=round(max(0, time.perf_counter() - self.stage_started), 6))
        self.data["current_stage"] = None
        self.stage_started = None
        self._save()

    def finish(self, status: str, *, error_type: str | None = None, details: dict | None = None) -> None:
        if self.finished:
            return
        self.end_stage("failed" if status == "failed" else "done")
        self.finished = True
        self.data.update(status=status, ended_at=self._now(),
                         total_processing_seconds=round(max(0, time.perf_counter() - self.started), 6))
        if error_type:
            self.data["error_type"] = error_type  # No exception body, signed URL or request headers.
        if details:
            self.data["details"] = details
        self._save()


def list_ingestion_runs(video_id: str, root: Path | None = None) -> list[dict]:
    folder = _video_directory(video_id, root)
    if folder is None or not folder.is_dir():
        return []
    runs = []
    for path in folder.glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if (isinstance(item, dict) and item.get("schema_version") == 1
                    and item.get("video_id") == video_id and isinstance(item.get("started_at"), str)
                    and item.get("status") in {"running", "done", "failed"}
                    and isinstance(item.get("stages"), dict)):
                json.dumps(item, allow_nan=False)
                runs.append(item)
        except (OSError, ValueError, UnicodeError):
            continue
    return sorted(runs, key=lambda item: item["started_at"])


def latest_ingestion_run(video_id: str) -> dict | None:
    runs = list_ingestion_runs(video_id)
    return runs[-1] if runs else None
