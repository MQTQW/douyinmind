"""Download complete videos and prepare bounded, audio-preserving MP4 segments."""
from __future__ import annotations

import math
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from yt_dlp import YoutubeDL

from app.core.config import settings
from app.services.douyin_media import DouyinMediaError, download_douyin_media, is_douyin_video_url
from app.services.media_service import (
    DOUYIN_REFERER, USER_AGENT, MediaPipelineError,
    _export_cookiefile, _resolve_ffmpeg_path,
)


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    has_audio: bool


@dataclass(frozen=True)
class VideoSegment:
    path: Path
    start: float
    duration: float
    has_audio: bool
    audio_path: Path | None = None


def download_video(video_url: str, item_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", item_id):
        raise MediaPipelineError("视频编号无效")
    folder = Path(settings.video_cache_dir)
    folder.mkdir(parents=True, exist_ok=True)
    if is_douyin_video_url(video_url):
        try:
            return download_douyin_media(video_url, item_id, folder, _resolve_ffmpeg_path(), require_audio=False)
        except DouyinMediaError as exc:
            raise MediaPipelineError(str(exc)) from None
    for extension in ("mp4", "mkv", "webm", "mov"):
        existing = folder / f"{item_id}.{extension}"
        if existing.is_file() and existing.stat().st_size > 0:
            return existing
    options = {
        "outtmpl": str(folder / f"{item_id}.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "noplaylist": True, "quiet": True, "no_warnings": True,
        "retries": 3, "ffmpeg_location": _resolve_ffmpeg_path(),
    }
    if "douyin.com" in video_url:
        options["cookiefile"] = str(_export_cookiefile())
        options["http_headers"] = {"Referer": DOUYIN_REFERER, "User-Agent": USER_AGENT}
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(video_url, download=True)
            path = Path(downloader.prepare_filename(info))
    except Exception:
        # Do not copy provider URLs, cookies, or headers into errors/logs.
        raise MediaPipelineError("视频下载失败，请检查抖音登录状态、视频是否可访问，再重试。") from None
    if not path.is_file() or path.stat().st_size == 0:
        raise MediaPipelineError("未获得完整视频文件")
    return path


def probe_video(path: Path) -> VideoInfo:
    result = subprocess.run(
        [_resolve_ffmpeg_path(), "-hide_banner", "-i", str(path)],
        capture_output=True, timeout=30,
    )
    description = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", description)
    if not match or "Video:" not in description:
        raise MediaPipelineError("无法读取视频时长或画面，请检查视频文件。")
    hours, minutes, seconds = map(float, match.groups())
    duration = hours * 3600 + minutes * 60 + seconds
    if not math.isfinite(duration) or duration <= 0 or duration > 7200:
        raise MediaPipelineError("视频时长无效或超过两小时，请先拆分视频。")
    return VideoInfo(duration=duration, has_audio="Audio:" in description)


@contextmanager
def prepare_video_segments(path: Path) -> Iterator[tuple[VideoInfo, Iterator[VideoSegment]]]:
    info = probe_video(path)
    parent = Path(settings.video_cache_dir)
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="segments-", dir=parent) as folder:
        def segments() -> Iterator[VideoSegment]:
            segment_seconds = settings.video_segment_seconds
            count = math.ceil(info.duration / segment_seconds)
            for index in range(count):
                start = index * segment_seconds
                duration = min(segment_seconds, info.duration - start)
                target = Path(folder) / f"part-{index:04d}.mp4"
                command = [
                    _resolve_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", str(start), "-i", str(path), "-t", str(duration),
                    "-map", "0:v:0", "-an",
                    "-vf", "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                    "-r", "12", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "23", "-maxrate", "600k", "-bufsize", "1200k",
                    "-movflags", "+faststart", str(target),
                ]
                result = subprocess.run(command, capture_output=True, timeout=180)
                if result.returncode or not target.is_file() or not target.stat().st_size:
                    raise MediaPipelineError("视频分段处理失败，请检查音视频处理工具。")
                # Base64 expands bytes by 4/3. Stay below the provider's 10 MB bound.
                if target.stat().st_size > 5_000_000:
                    raise MediaPipelineError("视频片段过大，请减小 VIDEO_SEGMENT_SECONDS 后重试。")
                audio_target = Path(folder) / f"part-{index:04d}.wav" if info.has_audio else None
                if audio_target:
                    audio_result = subprocess.run([
                        _resolve_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", str(start), "-i", str(path), "-t", str(duration),
                        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio_target),
                    ], capture_output=True, timeout=180)
                    if audio_result.returncode or not audio_target.is_file() or not audio_target.stat().st_size:
                        raise MediaPipelineError("无法提取完整音轨，未将视频作为识别成功处理。")
                try:
                    yield VideoSegment(target, start, duration, info.has_audio, audio_target)
                finally:
                    target.unlink(missing_ok=True)
                    if audio_target:
                        audio_target.unlink(missing_ok=True)
        yield info, segments()
