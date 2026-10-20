from io import BytesIO
from types import SimpleNamespace
from urllib.request import Request
import shutil
import subprocess
import wave

import pytest

from app.core.config import settings
from app.services import douyin_media as media, media_service, video_media
from app.services.browser_profile import persistent_profile_lock
from app.services.douyin_collector import DouyinCollector


def test_detail_requires_target_id_and_uses_video_instead_of_background_music():
    target = "123456"
    detail = {"aweme_detail": {
        "aweme_id": target,
        "video": {"play_addr": {"url_list": ["https://v3.douyinvod.com/full.mp4?unit=1"]}},
        "music": {"play_url": {"url_list": ["https://v3.douyinvod.com/background.mp3"]}},
    }}
    assert media._detail_media_urls(detail, target) == ["https://v3.douyinvod.com/full.mp4?unit=1"]
    assert not media._detail_media_urls(detail, "999999")
    assert not media.is_douyin_video_url("https://www.douyin.com/video/123456", "999999")
    assert not media._allowed_media_url("https://douyinvod.com.evil.example/media.mp4")
    assert not media._allowed_media_url("blob:https://www.douyin.com/123")


def test_browser_cookie_scope_is_not_forwarded_to_video_cdn():
    jar = media._cookie_jar([{"domain": ".douyin.com", "name": "unit_session", "value": "unit-value", "path": "/", "expires": -1, "secure": True}])
    same_domain = Request("https://www.douyin.com/video/123456")
    jar.add_cookie_header(same_domain)
    assert same_domain.get_header("Cookie") == "unit_session=unit-value"
    cdn = Request("https://v3.douyinvod.com/full.mp4")
    jar.add_cookie_header(cdn)
    assert cdn.get_header("Cookie") is None


class FakeResponse(BytesIO):
    def __init__(self, content, *, status=200, declared=None, content_range=None):
        super().__init__(content)
        self.status = status
        self.headers = {"Content-Type": "video/mp4", "Content-Length": str(declared if declared is not None else len(content))}
        if content_range:
            self.headers["Content-Range"] = content_range


@pytest.mark.parametrize("partial", [False, True])
def test_stream_rejects_missing_bytes_and_partial_ranges(tmp_path, monkeypatch, partial):
    response = (FakeResponse(b"half", status=206, content_range="bytes 0-3/8") if partial
                else FakeResponse(b"half", declared=8))
    monkeypatch.setattr(media, "build_opener", lambda *args: SimpleNamespace(open=lambda *args, **kwargs: response))
    with pytest.raises(media.DouyinMediaError, match="部分视频|不完整"):
        media._stream_media("https://v3.douyinvod.com/full.mp4", tmp_path / "temporary.mp4", [], "unit agent")


def test_stream_enforces_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MAX_MEDIA_BYTES", 3)
    response = FakeResponse(b"large")
    monkeypatch.setattr(media, "build_opener", lambda *args: SimpleNamespace(open=lambda *args, **kwargs: response))
    with pytest.raises(media.DouyinMediaError, match="大小上限"):
        media._stream_media("https://v3.douyinvod.com/full.mp4", tmp_path / "temporary.mp4", [], "unit agent")


def test_failed_download_never_becomes_a_valid_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "_browser_playback", lambda *args: (["https://v3.douyinvod.com/full.mp4"], [], "unit agent"))
    def fail(url, target, *args):
        target.write_bytes(b"partial")
        raise media.DouyinMediaError("视频下载不完整，请稍后重试。")
    monkeypatch.setattr(media, "_stream_media", fail)
    with pytest.raises(media.DouyinMediaError, match="不完整"):
        media.download_douyin_media("https://www.douyin.com/video/123456", "123456", tmp_path, "unit-ffmpeg")
    assert not list(tmp_path.iterdir())


def test_logout_does_not_remove_a_profile_used_by_download():
    collector = DouyinCollector()
    with persistent_profile_lock:
        success, message = collector.logout()
    assert not success and "浏览器" in message


def test_douyin_video_download_reuses_the_browser_service(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "video_cache_dir", str(tmp_path))
    monkeypatch.setattr(video_media, "_resolve_ffmpeg_path", lambda: "unit-ffmpeg")
    calls = []
    def browser_download(*args, **kwargs):
        calls.append((args, kwargs))
        return tmp_path / "123456.mp4"
    monkeypatch.setattr(video_media, "download_douyin_media", browser_download)
    monkeypatch.setattr(video_media, "YoutubeDL", lambda *args: pytest.fail("Douyin should not use the unsigned detail endpoint"))
    assert video_media.download_video("https://www.douyin.com/video/123456", "123456") == tmp_path / "123456.mp4"
    assert calls[0][1] == {"require_audio": False}


def test_real_browser_media_audio_conversion_retains_full_soundtrack_and_repairs_cache(tmp_path, monkeypatch):
    try:
        ffmpeg = media_service._resolve_ffmpeg_path()
    except media_service.MediaPipelineError:
        pytest.skip("FFmpeg is not installed")
    source = tmp_path / "fixture.mp4"
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=blue:s=160x90:d=4",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source),
    ], check=True, capture_output=True, timeout=30)
    cache = tmp_path / "audio"
    cache.mkdir()
    (cache / "123456.wav").write_bytes(b"corrupt cache")
    (cache / "123456.part").write_bytes(b"partial download must never be used")
    monkeypatch.setattr(settings, "audio_cache_dir", str(cache))
    monkeypatch.setattr(media_service, "_resolve_ffmpeg_path", lambda: ffmpeg)
    monkeypatch.setattr(media, "_browser_playback", lambda *args: (["https://v3.douyinvod.com/full.mp4"], [], "unit agent"))
    monkeypatch.setattr(media, "_stream_media", lambda url, target, *args: shutil.copyfile(source, target))
    monkeypatch.setattr(media_service, "YoutubeDL", lambda *args: pytest.fail("No yt-dlp request should be made"))
    result = media_service.download_audio("https://www.douyin.com/video/123456", "123456")
    assert media_service._valid_wav(result)
    with wave.open(str(result), "rb") as recording:
        assert recording.getparams()[:3] == (1, 2, 16000)
        assert recording.getnframes() / 16000 == pytest.approx(4, abs=.1)
    monkeypatch.setattr(media_service, "download_douyin_media", lambda *args: pytest.fail("Valid cache must be reused"))
    assert media_service.download_audio("https://www.douyin.com/video/123456", "123456") == result
    assert (cache / "123456.part").read_bytes() == b"partial download must never be used"


def test_truncated_wav_cache_is_rejected(tmp_path):
    path = tmp_path / "truncated.wav"
    with wave.open(str(path), "wb") as recording:
        recording.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        recording.writeframes(b"\0\0" * 16000)
    with path.open("r+b") as recording:
        recording.truncate(100)
    assert not media_service._valid_wav(path)
