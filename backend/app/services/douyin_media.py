"""Read the target video's normal playback resource in the logged-in browser.

No API signatures or verification challenges are reproduced here. Playback URLs
and browser credentials remain in memory and never enter errors or the database.
"""
from __future__ import annotations

import http.cookiejar
import math
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from playwright.sync_api import sync_playwright

from app.core.config import settings
from app.services.browser_profile import persistent_profile_lock


class DouyinMediaError(RuntimeError):
    """Safe, user-facing errors without media URLs or browser credentials."""


MAX_MEDIA_BYTES = 512 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 180
MEDIA_DOMAINS = (
    "douyin.com", "douyinvod.com", "amemv.com", "snssdk.com",
    "bytecdn.cn", "bytedance.com", "bytefcdn.com",
)


def is_douyin_video_url(url: str, item_id: str | None = None) -> bool:
    parsed = urlparse(url)
    match = re.fullmatch(r"/video/(\d+)/?", parsed.path)
    return bool(parsed.scheme in {"http", "https"} and parsed.hostname in {"douyin.com", "www.douyin.com"}
                and match and (item_id is None or match.group(1) == item_id))


def _allowed_media_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and not parsed.username and not parsed.password and any(
        hostname == domain or hostname.endswith("." + domain) for domain in MEDIA_DOMAINS)


def _detail_media_urls(payload: object, item_id: str) -> list[str]:
    if not isinstance(payload, dict):
        return []
    detail = payload.get("aweme_detail")
    if not isinstance(detail, dict) or str(detail.get("aweme_id")) != item_id:
        return []
    video = detail.get("video")
    if not isinstance(video, dict):
        return []
    addresses = [video.get("play_addr"), video.get("play_addr_h264")]
    for bitrate in video.get("bit_rate") or []:
        if isinstance(bitrate, dict):
            addresses.append(bitrate.get("play_addr"))
    result = []
    for address in addresses:
        if isinstance(address, dict):
            for url in address.get("url_list") or []:
                if isinstance(url, str) and _allowed_media_url(url) and url not in result:
                    result.append(url)
    return result


def _cookie_jar(cookies: list[dict]) -> http.cookiejar.CookieJar:
    jar = http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(
        strict_ns_domain=http.cookiejar.DefaultCookiePolicy.DomainStrictNonDomain))
    for entry in cookies:
        domain = entry.get("domain") or ""
        expires = entry.get("expires")
        if not domain or not entry.get("name"):
            continue
        expiry = int(expires) if isinstance(expires, (int, float)) and expires > 0 else None
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=entry["name"], value=entry.get("value", ""),
            port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
            path=entry.get("path") or "/", path_specified=True,
            secure=bool(entry.get("secure")), expires=expiry, discard=expiry is None,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        ))
    return jar


class _MediaRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not _allowed_media_url(newurl):
            raise DouyinMediaError("视频播放地址跳转到不支持的媒体来源。")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _stream_media(url: str, target: Path, cookies: list[dict], user_agent: str) -> None:
    if not _allowed_media_url(url):
        raise DouyinMediaError("未取得可用的视频播放地址。")
    opener = build_opener(HTTPCookieProcessor(_cookie_jar(cookies)), _MediaRedirectHandler())
    request = Request(url, headers={
        "User-Agent": user_agent, "Referer": "https://www.douyin.com/", "Accept-Encoding": "identity",
    })
    start = time.monotonic()
    try:
        with opener.open(request, timeout=30) as response, target.open("wb") as output:
            status = getattr(response, "status", 200)
            if status not in {200, 206}:
                raise DouyinMediaError("抖音拒绝提供完整视频，请确认该视频仍可播放。")
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/" in content_type or "json" in content_type:
                raise DouyinMediaError("播放地址返回了验证或错误页面，未下载视频。")
            declared = response.headers.get("Content-Length")
            size = int(declared) if declared and declared.isdigit() else None
            if size is not None and size > MAX_MEDIA_BYTES:
                raise DouyinMediaError("视频文件超过本地下载大小上限。")
            if status == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range") or "")
                if not match or int(match.group(1)) != 0 or int(match.group(2)) + 1 != int(match.group(3)):
                    raise DouyinMediaError("播放地址只提供了部分视频，未作为完整下载保存。")
            downloaded = 0
            while True:
                if time.monotonic() - start > MAX_DOWNLOAD_SECONDS:
                    raise DouyinMediaError("完整视频下载超时，请稍后重试。")
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > MAX_MEDIA_BYTES:
                    raise DouyinMediaError("视频文件超过本地下载大小上限。")
                output.write(chunk)
            if not downloaded or (size is not None and downloaded != size):
                raise DouyinMediaError("视频下载不完整，请稍后重试。")
    except DouyinMediaError:
        raise
    except Exception:
        raise DouyinMediaError("视频媒体下载失败，请确认该视频可播放并重试。") from None


def validate_complete_media(path: Path, ffmpeg_path: str, *, require_audio: bool = True) -> float:
    """Probe and decode the whole media; reject corrupt or truncated cached files."""
    try:
        probe = subprocess.run([ffmpeg_path, "-hide_banner", "-i", str(path)], capture_output=True, timeout=30)
        description = probe.stderr.decode("utf-8", errors="replace")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", description)
        if not match or "Video:" not in description:
            raise DouyinMediaError("未获得可读取的完整视频文件。")
        hours, minutes, seconds = map(float, match.groups())
        duration = hours * 3600 + minutes * 60 + seconds
        if not math.isfinite(duration) or not 0 < duration <= 10800:
            raise DouyinMediaError("视频时长无效或超过三小时。")
        if require_audio and "Audio:" not in description:
            raise DouyinMediaError("该视频没有可识别的音轨。")
        decode = subprocess.run([
            ffmpeg_path, "-hide_banner", "-loglevel", "error", "-xerror", "-i", str(path),
            "-map", "0:a:0" if require_audio else "0:v:0", "-f", "null", "-",
        ], capture_output=True, timeout=300)
        if decode.returncode:
            raise DouyinMediaError("视频音轨不完整或无法解码，未作为成功下载保存。")
        return duration
    except DouyinMediaError:
        raise
    except Exception:
        raise DouyinMediaError("无法核验完整视频或音轨，请检查本地音视频处理工具。") from None


def _browser_playback(video_url: str, item_id: str) -> tuple[list[str], list[dict], str]:
    profile = Path(settings.playwright_user_data_dir)
    if not (profile / "state.json").is_file():
        raise DouyinMediaError("未找到抖音登录状态，请先扫码登录后重试。")
    if not persistent_profile_lock.acquire(timeout=60):
        raise DouyinMediaError("抖音登录或视频下载正在使用浏览器，请稍后重试。")
    try:
        # Keep browser selection consistent with the login collector; never
        # export credentials or synthesize verification/signature parameters.
        from app.services.douyin_collector import collector
        launch = collector._browser_launch_kwargs()
        launch["headless"] = True
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(str(profile), **launch)
            try:
                page = context.new_page()
                media_urls: list[str] = []
                def on_response(response):
                    if "/aweme/v1/web/aweme/detail/" not in response.url or not response.ok:
                        return
                    try:
                        for url in _detail_media_urls(response.json(), item_id):
                            if url not in media_urls:
                                media_urls.append(url)
                    except Exception:
                        pass
                page.on("response", on_response)
                page.goto(video_url, wait_until="domcontentloaded", timeout=45000)
                deadline = time.monotonic() + 35
                while time.monotonic() < deadline:
                    if not is_douyin_video_url(page.url, item_id):
                        raise DouyinMediaError("网页跳转后不再是所选视频，请确认视频仍可访问。")
                    if media_urls:
                        break
                    candidate = page.evaluate("""(itemId) => {
                        const active = document.querySelector('[data-e2e="feed-active-video"] video');
                        if (!active) return '';
                        const card = active.closest('[data-e2e="feed-active-video"]');
                        const tagged = active.closest('[data-aweme-id]');
                        if (tagged && tagged.getAttribute('data-aweme-id') === itemId)
                            return active.currentSrc || active.src || '';
                        // A visible recommendation is not sufficient: require
                        // the active video's description to identify the page.
                        const description = card?.querySelector('[data-e2e="video-desc"]')?.textContent?.trim();
                        const title = document.title.replace(/\\s*[-_|]\\s*抖音.*$/, '').trim();
                        const canonical = document.querySelector('link[rel="canonical"]')?.href;
                        if (description && title.length > 4 && description.includes(title)
                            && (!canonical || new URL(canonical).pathname === '/video/' + itemId))
                            return active.currentSrc || active.src || '';
                        return '';
                    }""", item_id)
                    if isinstance(candidate, str) and _allowed_media_url(candidate):
                        media_urls.append(candidate)
                        break
                    page.wait_for_timeout(500)
                if not media_urls:
                    raise DouyinMediaError("已登录网页未取得视频播放地址；请在抖音页面确认视频可播放，若有验证提示需手动完成后重试。")
                return media_urls, context.cookies(), page.evaluate("navigator.userAgent")
            finally:
                context.close()
    except DouyinMediaError:
        raise
    except Exception:
        raise DouyinMediaError("无法打开已登录的抖音视频页面，请确认登录和视频可访问后重试。") from None
    finally:
        persistent_profile_lock.release()


def download_douyin_media(video_url: str, item_id: str, target_dir: Path, ffmpeg_path: str,
                          *, require_audio: bool = True) -> Path:
    if not is_douyin_video_url(video_url, item_id):
        raise DouyinMediaError("视频地址与所选抖音视频编号不一致。")
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{item_id}.mp4"
    if target.is_file():
        try:
            validate_complete_media(target, ffmpeg_path, require_audio=require_audio)
            return target
        except DouyinMediaError:
            target.unlink(missing_ok=True)
    urls, cookies, user_agent = _browser_playback(video_url, item_id)
    last_error = None
    with tempfile.TemporaryDirectory(prefix="douyin-media-", dir=target_dir) as folder:
        temporary = Path(folder) / "complete.mp4"
        for url in urls[:4]:
            try:
                _stream_media(url, temporary, cookies, user_agent)
                validate_complete_media(temporary, ffmpeg_path, require_audio=require_audio)
                temporary.replace(target)
                return target
            except DouyinMediaError as exc:
                last_error = exc
                temporary.unlink(missing_ok=True)
    raise last_error or DouyinMediaError("未获得完整抖音视频文件。")
