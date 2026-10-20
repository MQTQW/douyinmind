"""Retain validated audio spans and response-only attempt diagnostics.

The caller owns recognition validation and adaptive child segments. Only a complete,
validated span belongs in ``record_success``. No request payload, audio data,
credentials, or provider exception representation is written by this module.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
from copy import deepcopy
from pathlib import Path

from app.services.audio_media import AudioSegment


class AudioCheckpoint:
    """A content- and configuration-bound, atomically replaced local sidecar."""

    SCHEMA_VERSION = 1

    def __init__(self, source: Path, fingerprint: dict, *, namespace: str = "analysis") -> None:
        self.source = Path(source)
        if namespace not in {"analysis", "boundary-analysis"}:
            raise ValueError("未知音频检查点类型")
        self.path = self.source.with_name(self.source.name + f".{namespace}.json")
        # Root segments may finish concurrently. A save must include every
        # preceding change, not a stale copy made by another recognition thread.
        self._lock = threading.RLock()
        # Only a digest of the caller's settings is retained. This also prevents a
        # future accidentally supplied sensitive setting from entering the file.
        encoded = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        self._fingerprint_sha256 = hashlib.sha256(encoded).hexdigest()
        digest = hashlib.sha256()
        with self.source.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        self._source_sha256 = digest.hexdigest()
        self._state = self._load()

    def _empty(self) -> dict:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "source_sha256": self._source_sha256,
            "fingerprint_sha256": self._fingerprint_sha256,
            "segments": {},
        }

    @staticmethod
    def _span(start: float, duration: float) -> tuple[str, float, float]:
        start, duration = float(start), float(duration)
        if not math.isfinite(start) or not math.isfinite(duration) or start < 0 or duration <= 0:
            raise ValueError("音频分段起点或时长无效")
        # Canonical numeric values make 0 and 0.0 identify the same root segment.
        start = 0.0 if start == 0 else start
        key = json.dumps([start, duration], separators=(",", ":"), allow_nan=False)
        return key, start, duration

    @staticmethod
    def _valid_result(value) -> bool:
        return (isinstance(value, dict) and isinstance(value.get("analysis"), dict)
                and isinstance(value.get("raw_response"), str))

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("schema_version") != self.SCHEMA_VERSION
                    or data.get("source_sha256") != self._source_sha256
                    or data.get("fingerprint_sha256") != self._fingerprint_sha256
                    or not isinstance(data.get("segments"), dict)):
                return self._empty()
            for key, segment in data["segments"].items():
                if not isinstance(segment, dict):
                    return self._empty()
                canonical, _, _ = self._span(segment["start_seconds"], segment["duration_seconds"])
                if canonical != key or not isinstance(segment.get("attempts"), list):
                    return self._empty()
                if "success" in segment and not self._valid_result(segment["success"]):
                    return self._empty()
                for attempt in segment["attempts"]:
                    if not isinstance(attempt, dict) or attempt.get("outcome") not in {"success", "failure"}:
                        return self._empty()
                    raw = attempt.get("raw_response")
                    if raw is not None and not isinstance(raw, str):
                        return self._empty()
                    if attempt["outcome"] == "failure" and not isinstance(attempt.get("error_code"), str):
                        return self._empty()
            # Reject non-JSON numeric values, including a hand-edited NaN payload.
            json.dumps(data, allow_nan=False)
            return data
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError):
            return self._empty()

    def get(self, segment: AudioSegment) -> dict | None:
        key, _, _ = self._span(segment.start, segment.duration)
        with self._lock:
            item = self._state["segments"].get(key, {})
            result = item.get("success")
            # Callers must still validate the analysis schema and its local times.
            return deepcopy(result) if result is not None else None

    def record_success(self, segment: AudioSegment, analysis: dict, raw_response: str) -> None:
        if not isinstance(analysis, dict) or not isinstance(raw_response, str):
            raise TypeError("成功结果必须包含分析对象和原始响应文字")
        result = {"analysis": deepcopy(analysis), "raw_response": raw_response}
        self._record(segment, {"outcome": "success", "raw_response": raw_response}, result)

    def record_failure(self, segment: AudioSegment, error_code: str, raw_response: str | None) -> None:
        if not isinstance(error_code, str) or not error_code.strip():
            raise ValueError("失败记录需要错误类型")
        if raw_response is not None and not isinstance(raw_response, str):
            raise TypeError("失败响应必须是文字或空值")
        self._record(segment, {"outcome": "failure", "error_code": error_code,
                               "raw_response": raw_response})

    def _record(self, segment: AudioSegment, attempt: dict, result: dict | None = None) -> None:
        key, start, duration = self._span(segment.start, segment.duration)
        with self._lock:
            updated = deepcopy(self._state)
            item = updated["segments"].setdefault(key, {
                "start_seconds": start, "duration_seconds": duration, "attempts": [],
            })
            item["attempts"].append(attempt)
            if result is not None:
                item["success"] = result
            self._save(updated)
            self._state = updated

    def _save(self, state: dict) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                    prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent,
                    delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
