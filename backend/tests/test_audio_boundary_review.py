"""ASR review can propose only a bounded center edit and must preserve evidence."""
from copy import deepcopy
from contextlib import contextmanager
import json
import struct
import wave

import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def legacy_single_window_controller(monkeypatch):
    monkeypatch.setattr(settings, 'audio_boundary_batch_enabled', False)
from app.services.audio_boundary import propose_boundary_splice, select_boundary_candidates
from app.services.audio_media import prepare_audio_window
from app.services.omni_service import AudioAnalysis, OmniService, OmniServiceError

LEFT = "春天的海风吹过安静山谷所有人一起迎接温暖"
RIGHT = "随后大家整理桌面准备开始今天精彩的实验"


def part(text, start=0, end=1):
    return {"start_seconds": start, "end_seconds": end, "text": text}


def proposal(old_left="美丽的花", old_right="正在开放", new="美丽的花朵正在开放"):
    return propose_boundary_splice([part(LEFT + old_left)], [part(old_right + RIGHT, 1, 2)],
                                  [part("外缘会被识别错" + LEFT + new + RIGHT + "外缘残句", 0, 2)])


def test_small_insertion_retains_outer_words_records_and_source_envelope():
    left = [part("最早记录必须逐字保留", 0, 1), part(LEFT + "美丽的花", 1, 2)]
    right = [part("正在开放" + RIGHT, 2, 3), part("最后记录也必须原样保留", 3, 4)]
    old = deepcopy(left + right)
    result = propose_boundary_splice(left, right, [part("错误外缘" + LEFT + "美丽的花朵正在开放" + RIGHT + "错误外缘")])
    assert result["accepted"]
    assert result["after_parts"][0] == old[0]
    assert result["after_parts"][-1] == old[-1]
    assert result["after_parts"][1] == part(LEFT + "美丽的花朵正在开放" + RIGHT, 1, 3)
    assert left + right == old
    assert result["before_parts"] == old
    assert result["acoustic_status"] == "unverified_acoustic"


@pytest.mark.parametrize("before,after", [("四四点三亿", "四点三亿"), ("两种", "三种"),
    ("〇点五", "一点五"), ("负半", "半"), ("12.3%", "12.3"), ("两公斤", "两公升"),
    ("３公斤", "４公斤"), ("壹份", "贰份"), ("三份", "三%份"), ("三份", "三‰份"), ("三度", "三℉度"),
    ("四台设备", "四套设备"), ("摄氏度范围", "华氏度范围")])
def test_numeric_or_unit_changes_are_not_automatically_adopted(before, after):
    middle = len(before) // 2
    result = proposal(before[:middle], before[middle:], after)
    assert not result["accepted"]


@pytest.mark.parametrize("before,after", [("不可以", "可以"), ("没有", "有"), ("未完成", "完成"),
                                            ("无影响", "有影响"), ("非正式", "正式"), ("API", "APIv2")])
def test_negation_and_ascii_changes_are_rejected(before, after):
    result = proposal(before[:1], before[1:], after)
    assert not result["accepted"]


def test_genuine_repetition_is_not_silently_deleted():
    assert not proposal("大家", "大家出发", "大家出发")["accepted"]


def test_raw_english_structure_and_negation_scope_are_not_repunctuated():
    assert not proposal("don'", "t接受", "dont接受")["accepted"]
    assert not proposal("目前还不", "可能接受", "目前还不可能接受")["accepted"]


def test_repeated_or_missing_or_reverse_anchors_cannot_authorize_edit():
    old_left, old_right = [part(LEFT + "美丽的花")], [part("正在开放" + RIGHT)]
    for text in (LEFT + "花朵" + LEFT + RIGHT, "美丽的花朵" + RIGHT,
                 RIGHT + "美丽的花朵" + LEFT):
        result = propose_boundary_splice(old_left, old_right, [part(text)])
        assert not result["accepted"]


def test_review_array_order_or_overlap_cannot_authorize_splice():
    left, right = [part(LEFT + "美丽的花", 0, 1)], [part("正在开放" + RIGHT, 1, 2)]
    for heard in ([part(LEFT + "美丽的花朵", 1, 2), part("正在开放" + RIGHT, 0, 1)],
                  [part(LEFT + "美丽的花朵", 0, 1.5), part("正在开放" + RIGHT, 1, 2)]):
        result = propose_boundary_splice(left, right, heard)
        assert not result["accepted"] and result["reason"] == "unordered_or_overlapping_review_records"


def test_candidate_budget_reserves_distinct_symptoms_without_fixed_timestamps():
    groups = [
        {"start_seconds": 0, "parts": [part("前文很长很长很长以后还有几个山", 0, 5)]},
        {"start_seconds": 5, "parts": [part("山外风景很美很美很美很美", 5, 10)]},
        {"start_seconds": 10, "parts": [part("一段原话仍然保留着", 10, 14), part("当", 14, 15)]},
        {"start_seconds": 15, "parts": [part("大家开始尝试其他任务", 15, 20)]},
        {"start_seconds": 20, "parts": [part("的继续故事", 20, 25)]},
    ]
    selected = select_boundary_candidates(groups, 2)
    assert {c["boundary_seconds"] for c in selected} == {15, 20}
    assert len(select_boundary_candidates(groups, 0)) == 0


def write_pcm(path, frames=40000, rate=1000):
    data = b"".join(struct.pack("<h", i % 32000) for i in range(frames))
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(data)
    return data


def test_review_window_is_exact_pcm_copy_and_source_unchanged(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    pcm = write_pcm(source)
    original = source.read_bytes()
    monkeypatch.setattr(settings, "audio_cache_dir", str(tmp_path / "cache"))
    with prepare_audio_window(source, 8.123, 24.128) as segment:
        with wave.open(str(segment.path), "rb") as audio:
            assert audio.readframes(audio.getnframes()) == pcm[8123 * 2:24128 * 2]
        assert segment.start == 8.123 and segment.duration == 16.005
        target = segment.path
    assert not target.exists()
    assert source.read_bytes() == original


def test_boundary_failure_retains_complete_original_and_records_extra_attempt(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    write_pcm(source)
    monkeypatch.setattr(settings, "audio_cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "audio_boundary_review_max_windows", 4)
    roots = [{"start_seconds": 0, "parts": [part(LEFT, 0, 19), part("当", 19, 20)]},
             {"start_seconds": 20, "parts": [part(RIGHT, 20, 40)]}]
    service = OmniService()
    def fail(segment):
        raise OmniServiceError("安全错误", code="api_error", raw_response="partial response")
    monkeypatch.setattr(service, "_analyze_audio_segment", fail)
    speech, audit = service._review_audio_boundaries(source, 40, roots)
    assert speech == roots[0]["parts"] + roots[1]["parts"]
    assert audit["recognition_attempts"] == audit["failed_recognition_attempts"] == 1
    assert audit["accepted_windows"] == 0
    assert audit["reviews"][0]["raw_response"] == "partial response"
    assert not source.with_name(source.name + ".analysis.json").exists()
    assert source.with_name(source.name + ".boundary-analysis.json").exists()


def test_review_cache_is_bound_to_current_original_text(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    write_pcm(source)
    monkeypatch.setattr(settings, "audio_cache_dir", str(tmp_path / "cache"))
    roots = [{"start_seconds": 0, "parts": [part(LEFT, 0, 19), part("当", 19, 20)]},
             {"start_seconds": 20, "parts": [part(RIGHT, 20, 40)]}]
    service, calls = OmniService(), []
    def recognize(segment):
        calls.append(segment.start)
        result = AudioAnalysis(transcript_segments=[part(LEFT + "当" + RIGHT, 0, 16)])
        return result, result.model_dump_json()
    monkeypatch.setattr(service, "_analyze_audio_segment", recognize)
    service._review_audio_boundaries(source, 40, roots)
    _, second = service._review_audio_boundaries(source, 40, roots)
    assert len(calls) == 1 and second["reused_windows"] == 1 and second["recognition_attempts"] == 0
    changed = deepcopy(roots)
    changed[0]["parts"][0]["text"] += "原稿重新识别后不同"
    service._review_audio_boundaries(source, 40, changed)
    assert len(calls) == 2


def test_optional_checkpoint_io_failure_does_not_fail_valid_original(tmp_path, monkeypatch):
    from app.services import omni_service as module
    source = tmp_path / "source.wav"
    write_pcm(source)
    roots = [{"start_seconds": 0, "parts": [part(LEFT, 0, 19), part("当", 19, 20)]},
             {"start_seconds": 20, "parts": [part(RIGHT, 20, 40)]}]
    def unavailable(*args, **kwargs):
        raise OSError("test-only I/O failure")
    monkeypatch.setattr(module, "AudioCheckpoint", unavailable)
    speech, audit = OmniService()._review_audio_boundaries(source, 40, roots)
    assert speech == roots[0]["parts"] + roots[1]["parts"]
    assert audit["reason"] == "review_checkpoint_unavailable"
    assert audit["recognition_attempts"] == audit["accepted_windows"] == 0


def test_provider_uncertainty_retains_original_even_when_anchors_match(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    write_pcm(source)
    monkeypatch.setattr(settings, "audio_cache_dir", str(tmp_path / "cache"))
    roots = [{"start_seconds": 0, "parts": [part(LEFT, 0, 19), part("当", 19, 20)]},
             {"start_seconds": 20, "parts": [part(RIGHT, 20, 40)]}]
    service = OmniService()
    result = AudioAnalysis(transcript_segments=[part(LEFT + "当" + RIGHT, 0, 16)], warnings=["有一处听不清"])
    monkeypatch.setattr(service, "_analyze_audio_segment", lambda segment: (result, result.model_dump_json()))
    speech, audit = service._review_audio_boundaries(source, 40, roots)
    assert speech == roots[0]["parts"] + roots[1]["parts"]
    assert audit["reviews"][0]["reason"] == "provider_uncertainty"


def test_root_review_order_matches_final_source_order_but_provider_raw_is_unchanged(tmp_path, monkeypatch):
    from app.services import omni_service as module
    from app.services.audio_media import AudioSegment
    source = tmp_path / "source.wav"
    write_pcm(source, frames=10000)
    monkeypatch.setattr(settings, "audio_cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "qwen_omni_api_key", "offline-test-only")
    monkeypatch.setattr(settings, "video_segment_seconds", 5)
    monkeypatch.setattr(settings, "audio_boundary_review_enabled", True)
    @contextmanager
    def prepare(path):
        yield 10, iter([AudioSegment(source, 0, 5), AudioSegment(source, 5, 5)])
    monkeypatch.setattr(module, "prepare_audio_segments", prepare)
    service = OmniService()
    original_raw = json.dumps({"transcript_segments": [part("稍后原话", 3, 4), part("较早原话", 0, 2)]})
    monkeypatch.setattr(service, "_analyze_audio_segment", lambda segment:
                        (AudioAnalysis.model_validate_json(original_raw), original_raw))
    captured = []
    def review(path, duration, roots):
        captured.extend(roots)
        return [p for root in roots for p in root["parts"]], {"reviews": [], "recognition_attempts": 0, "reused_windows": 0, "accepted_windows": 0}
    monkeypatch.setattr(service, "_review_audio_boundaries", review)
    analysis = service.analyze_audio(source)
    assert [p["text"] for p in captured[0]["parts"]] == ["较早原话", "稍后原话"]
    assert all(r["audio_response"] == original_raw for r in analysis.payload["raw_results"])


def test_analyze_audio_enters_boundary_review_with_default_configuration(tmp_path, monkeypatch):
    from app.core.config import Settings
    from app.services import omni_service as module
    from app.services.audio_media import AudioSegment
    enabled = Settings.model_fields["audio_boundary_review_enabled"].default
    limit = Settings.model_fields["audio_boundary_review_max_windows"].default
    assert enabled is True and limit == 4
    monkeypatch.setattr(settings, "audio_boundary_review_enabled", enabled)
    monkeypatch.setattr(settings, "audio_boundary_review_max_windows", limit)
    monkeypatch.setattr(settings, "video_segment_seconds", 60)
    monkeypatch.setattr(settings, "qwen_omni_api_key", "offline-test-only")
    source = tmp_path / "source.wav"
    write_pcm(source, frames=120000)
    @contextmanager
    def prepare(path):
        yield 120, iter([AudioSegment(source, 0, 60), AudioSegment(source, 60, 60)])
    monkeypatch.setattr(module, "prepare_audio_segments", prepare)
    service, calls = OmniService(), []
    recognized = AudioAnalysis(transcript_segments=[part("完全离线的原始听写", 0, 60)])
    monkeypatch.setattr(service, "_analyze_audio_segment", lambda segment: (recognized, recognized.model_dump_json()))
    def review(path, duration, roots):
        calls.append((path, duration, roots))
        return [p for root in roots for p in root["parts"]], {
            "reviews": [], "recognition_attempts": 0, "reused_windows": 0, "accepted_windows": 0}
    monkeypatch.setattr(service, "_review_audio_boundaries", review)
    analysis = service.analyze_audio(source)
    assert len(calls) == 1 and calls[0][1] == 120
    assert len(calls[0][2]) == 2
    assert "boundary_review" in analysis.payload


def test_successful_paid_response_survives_optional_cache_write_failure(tmp_path, monkeypatch):
    from app.services import omni_service as module
    source = tmp_path / "source.wav"
    write_pcm(source)
    monkeypatch.setattr(settings, "audio_cache_dir", str(tmp_path / "cache"))
    roots = [{"start_seconds": 0, "parts": [part(LEFT, 0, 19), part("当", 19, 20)]},
             {"start_seconds": 20, "parts": [part(RIGHT, 20, 40)]}]
    class Unwritable:
        def __init__(self, *args, **kwargs): pass
        def get(self, segment): return None
        def record_success(self, *args): raise OSError("test-only cache write failure")
    monkeypatch.setattr(module, "AudioCheckpoint", Unwritable)
    service = OmniService()
    result = AudioAnalysis(transcript_segments=[part(LEFT + "当" + RIGHT, 0, 16)])
    raw = result.model_dump_json()
    monkeypatch.setattr(service, "_analyze_audio_segment", lambda segment: (result, raw))
    speech, audit = service._review_audio_boundaries(source, 40, roots)
    assert speech == roots[0]["parts"] + roots[1]["parts"]
    assert audit["reviews"][0]["raw_response"] == raw
    assert audit["reviews"][0]["review_parts"]
    assert audit["reviews"][0]["after_parts"] == audit["reviews"][0]["before_parts"]


def test_boundary_budget_stops_new_requests_and_cleans_context(tmp_path, monkeypatch):
    from app.services import omni_service as module
    source = tmp_path / "source.wav"
    write_pcm(source, frames=100000)
    monkeypatch.setattr(settings, "audio_cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "audio_boundary_review_budget_seconds", 60)
    monkeypatch.setattr(settings, "audio_boundary_review_timeout_seconds", 20)
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    roots = [{"start_seconds": index * 20, "parts": [part(LEFT, index * 20, index * 20 + 19),
              part("当", index * 20 + 19, (index + 1) * 20)]} for index in range(5)]
    calls, service = [], OmniService()
    def recognize(segment):
        calls.append((segment.start, service._request_context.boundary_deadline))
        clock[0] += 21
        raise OmniServiceError("安全测试超时", code="boundary_review_timeout")
    monkeypatch.setattr(service, "_analyze_audio_segment", recognize)
    speech, audit = service._review_audio_boundaries(source, 100, roots)
    assert [deadline for _, deadline in calls] == [20, 41, 60]
    assert len(calls) == 3 and audit["recognition_attempts"] == 3
    assert audit["reviews"][-1]["reason"] == "stage_budget_exhausted"
    assert not hasattr(service._request_context, "boundary_deadline")
    assert speech == [p for root in roots for p in root["parts"]]


def test_request_scoped_short_timeout_has_no_sdk_retry_and_closes_stream(monkeypatch):
    from types import SimpleNamespace
    from app.services import omni_service as module
    clock, options, streams = [0.0], [], []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    class Stream:
        closed = False
        def __iter__(self):
            clock[0] = 21
            yield SimpleNamespace(choices=[])
        def close(self): self.closed = True
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def with_options(self, **kwargs):
            options.append(kwargs)
            return self
        def create(self, **kwargs):
            stream = Stream()
            streams.append(stream)
            return stream
        def close(self):
            raise AssertionError("Do not close the shared SDK client")
    service = OmniService()
    monkeypatch.setattr(service, "_get_client", lambda: Client())
    service._request_context.boundary_deadline = 20
    with pytest.raises(OmniServiceError) as error:
        service._request([])
    assert error.value.code == "boundary_review_timeout"
    assert options == [{"timeout": 20, "max_retries": 0}]
    assert streams[0].closed
    del service._request_context.boundary_deadline
    options.clear()
    clock[0] = 0
    with pytest.raises(OmniServiceError):
        service._request([])
    assert options == []  # Root calls retain their original SDK policy.


def test_expired_boundary_budget_never_creates_http_request(monkeypatch):
    from types import SimpleNamespace
    from app.services import omni_service as module
    monkeypatch.setattr(module.time, "monotonic", lambda: 10)
    class Client:
        def with_options(self, **kwargs): raise AssertionError("Expired budget must not send")
    service = OmniService()
    monkeypatch.setattr(service, "_get_client", lambda: Client())
    service._request_context.boundary_deadline = 10
    with pytest.raises(OmniServiceError) as error:
        service._request([])
    assert error.value.code == "boundary_review_timeout"
