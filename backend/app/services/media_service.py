"""
媒体下载服务模块

负责下载抖音视频的音频流：
1. 优先方案：yt-dlp 直接下载（使用 Playwright 登录态 Cookie）
2. 处理：ffmpeg 提取音频并转码为 mp3
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

from yt_dlp import YoutubeDL

from app.core.config import settings
from app.services.douyin_media import DouyinMediaError, download_douyin_media, is_douyin_video_url

logger = logging.getLogger(__name__)

# 抖音请求头
DOUYIN_REFERER = "https://www.douyin.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class MediaPipelineError(RuntimeError):
    """媒体处理异常"""
    pass


def _resolve_ffmpeg_path() -> str:
    """
    查找 ffmpeg 可执行文件路径

    依次尝试：
    1. PATH 环境变量
    2. 缓存旁与项目中已准备的工具
    3. Windows 常见安装路径

    :return: ffmpeg 可执行文件路径
    :raises MediaPipelineError: 未找到 ffmpeg
    """
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return ffmpeg_path

    common_paths = [
        Path(settings.audio_cache_dir).parent / "tools" / "ffmpeg.exe",
        # Moving only the audio cache must not hide the project's installed
        # tool. Resolve from this module rather than the current directory.
        Path(__file__).resolve().parents[1] / "storage" / "tools" / "ffmpeg.exe",
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
        Path.home() / "ffmpeg" / "bin" / "ffmpeg.exe",
    ]
    for candidate in common_paths:
        if candidate.is_file():
            return str(candidate)

    raise MediaPipelineError(
        "未找到 ffmpeg。请安装 ffmpeg 并加入 PATH，"
        "或将其路径添加到系统环境变量中。"
        "下载地址：https://www.gyan.dev/ffmpeg/builds/"
    )


def _export_cookiefile() -> Path:
    """
    从 Playwright 登录态导出 yt-dlp 格式的 Cookie 文件

    :return: Cookie 文件路径
    :raises MediaPipelineError: 登录态文件不存在
    """
    state_path = Path(settings.playwright_user_data_dir) / "state.json"
    if not state_path.exists():
        raise MediaPipelineError(
            "未找到登录状态文件，请先扫码登录抖音"
        )

    with state_path.open("r", encoding="utf-8") as f:
        state = json.load(f)

    cookies = state.get("cookies", []) if isinstance(state, dict) else []
    cookie_dir = Path(settings.audio_cache_dir)
    cookie_dir.mkdir(parents=True, exist_ok=True)
    cookie_file = cookie_dir / "douyin_cookies.txt"

    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated from Playwright storage state",
    ]
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").strip()
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if not domain or not name:
            continue

        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(cookie.get("path") or "/")
        secure = "TRUE" if bool(cookie.get("secure")) else "FALSE"
        expires_raw = cookie.get("expires")
        try:
            expires = (
                int(float(expires_raw))
                if expires_raw and float(expires_raw) > 0
                else 0
            )
        except (TypeError, ValueError):
            expires = 0

        lines.append(
            "\t".join([
                domain,
                include_subdomains,
                path,
                secure,
                str(expires),
                name,
                value,
            ])
        )

    cookie_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("已导出 Cookie 文件: %s", cookie_file)
    return cookie_file


def _valid_wav(path: Path) -> bool:
    """Read bounded chunks to verify the entire cached PCM recording."""
    try:
        with wave.open(str(path), "rb") as recording:
            if recording.getparams()[:3] != (1, 2, 16000) or recording.getcomptype() != "NONE":
                return False
            expected = recording.getnframes() * 2
            if not 0 < expected <= 10800 * 16000 * 2:
                return False
            actual = 0
            while chunk := recording.readframes(16000 * 60):
                actual += len(chunk)
            return actual == expected
    except (OSError, EOFError, wave.Error):
        return False


class _QuietDownloaderLogger:
    # yt-dlp errors can include signed URLs. The caller provides safe errors.
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


def download_audio(video_url: str, platform_item_id: str) -> Path:
    """Download the complete soundtrack and atomically save a verified WAV.

    Douyin uses the existing logged-in webpage instead of yt-dlp's unsigned
    detail endpoint. Other supported sites continue to use yt-dlp.
    """
    audio_dir = Path(settings.audio_cache_dir)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", platform_item_id):
        raise MediaPipelineError("视频编号无效")
    audio_dir.mkdir(parents=True, exist_ok=True)
    cached = audio_dir / f"{platform_item_id}.wav"
    if cached.is_file() and _valid_wav(cached):
        return cached
    ffmpeg = _resolve_ffmpeg_path()
    with tempfile.TemporaryDirectory(prefix=f"audio-{platform_item_id}-", dir=audio_dir) as folder:
        temporary = Path(folder)
        if is_douyin_video_url(video_url):
            try:
                source = download_douyin_media(video_url, platform_item_id, audio_dir / "source_media", ffmpeg)
            except DouyinMediaError as exc:
                raise MediaPipelineError(str(exc)) from None
        else:
            options = {
                "outtmpl": str(temporary / "source.%(ext)s"), "format": "bestaudio/best",
                "noplaylist": True, "quiet": True, "no_warnings": True,
                "retries": 3, "socket_timeout": 30, "ffmpeg_location": ffmpeg,
                "logger": _QuietDownloaderLogger(),
            }
            try:
                with YoutubeDL(options) as downloader:
                    info = downloader.extract_info(video_url, download=True)
                    source = Path(downloader.prepare_filename(info))
                if not source.is_file() or not source.stat().st_size:
                    raise MediaPipelineError("未获得完整音频文件。")
            except Exception:
                raise MediaPipelineError("音频下载失败，请确认视频仍可访问后重试。") from None
        output = temporary / "complete.wav"
        try:
            result = subprocess.run([
                ffmpeg, "-hide_banner", "-loglevel", "error", "-xerror", "-y", "-i", str(source),
                "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(output),
            ], capture_output=True, timeout=300)
        except Exception:
            raise MediaPipelineError("完整音轨提取超时或音视频处理工具不可用。") from None
        if result.returncode or not _valid_wav(output):
            raise MediaPipelineError("未获得完整可读取的音轨，未将视频作为入库成功处理。")
        output.replace(cached)
    logger.info("完整音频已下载并核验: %s", platform_item_id)
    return cached
