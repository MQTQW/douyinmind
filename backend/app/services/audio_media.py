"""Prepare the complete soundtrack without downloading or encoding visual frames."""
from __future__ import annotations

import math
import re
import subprocess
import tempfile
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from app.core.config import settings
from app.services.media_service import MediaPipelineError, _resolve_ffmpeg_path


@dataclass(frozen=True)
class AudioSegment:
    path: Path
    start: float
    duration: float


@contextmanager
def prepare_audio_window(path: Path, start: float, end: float) -> Iterator[AudioSegment]:
    """Copy an exact PCM interval for optional review; never trim root audio."""
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise MediaPipelineError("补听窗口的时间范围无效。")
    with wave.open(str(path), "rb") as source:
        params = source.getparams()
        first, last = round(start * params.framerate), round(end * params.framerate)
        if params.comptype != "NONE" or not (0 <= first < last <= params.nframes):
            raise MediaPipelineError("补听需要完整 PCM 音频，已保留原文字稿。")
        source.setpos(first)
        frames = source.readframes(last - first)
    if len(frames) != (last - first) * params.nchannels * params.sampwidth:
        raise MediaPipelineError("补听音频不完整，已保留原文字稿。")
    parent = Path(settings.audio_cache_dir)
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="boundary-", dir=parent) as folder:
        target = Path(folder) / "window.wav"
        with wave.open(str(target), "wb") as output:
            output.setparams(params)
            output.writeframes(frames)
        yield AudioSegment(target, first / params.framerate, (last - first) / params.framerate)


@contextmanager
def split_audio_segment(segment: AudioSegment) -> Iterator[list[AudioSegment]]:
    """Split the actual PCM samples in half without dropping or duplicating audio."""
    with wave.open(str(segment.path), "rb") as source:
        params = source.getparams()
        frames = source.readframes(source.getnframes())
    frame_size = params.nchannels * params.sampwidth
    count = len(frames) // frame_size
    if params.comptype != "NONE" or count != params.nframes or count < 2:
        raise MediaPipelineError("重试音频片段不完整，无法安全拆分。")
    middle = count // 2
    with tempfile.TemporaryDirectory(prefix="retry-", dir=segment.path.parent) as folder:
        parts = []
        for index, (first, last) in enumerate(((0, middle), (middle, count))):
            target = Path(folder) / f"part-{index}.wav"
            with wave.open(str(target), "wb") as output:
                output.setparams(params)
                output.writeframes(frames[first * frame_size:last * frame_size])
            parts.append(AudioSegment(target, segment.start + first / params.framerate,
                                      (last - first) / params.framerate))
        yield parts


def probe_audio(path: Path) -> float:
    try:
        with wave.open(str(path)) as audio:
            duration = audio.getnframes() / audio.getframerate()
    except (wave.Error, EOFError):
        result = subprocess.run([_resolve_ffmpeg_path(), "-hide_banner", "-i", str(path)],
            capture_output=True, timeout=30)
        description = result.stderr.decode("utf-8", errors="replace")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", description)
        if not match or "Audio:" not in description:
            raise MediaPipelineError("无法读取音频文件，请检查视频是否包含音轨。")
        hours, minutes, seconds = map(float, match.groups())
        duration = hours * 3600 + minutes * 60 + seconds
    if not math.isfinite(duration) or duration <= 0 or duration > 10800:
        raise MediaPipelineError("音频时长无效或超过三小时，请先拆分。")
    return duration


@contextmanager
def prepare_audio_segments(path: Path) -> Iterator[tuple[float, Iterator[AudioSegment]]]:
    duration = probe_audio(path)
    parent = Path(settings.audio_cache_dir)
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="segments-", dir=parent) as folder:
        def segments():
            for index in range(math.ceil(duration / settings.video_segment_seconds)):
                start = index * settings.video_segment_seconds
                length = min(settings.video_segment_seconds, duration - start)
                target = Path(folder) / f"part-{index:04d}.wav"
                result = subprocess.run([
                    _resolve_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", str(start), "-i", str(path), "-t", str(length),
                    "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target),
                ], capture_output=True, timeout=180)
                if result.returncode or not target.is_file() or not target.stat().st_size:
                    raise MediaPipelineError("音频分段处理失败，未将其作为完整识别结果保存。")
                # Consumers may still be reading earlier segments in parallel.
                # The enclosing context owns these files until all workers join.
                yield AudioSegment(target, start, length)
        yield duration, segments()
