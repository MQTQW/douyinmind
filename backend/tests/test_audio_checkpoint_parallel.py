"""Concurrent recognition must durably retain every validated audio span."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

from app.services.audio_checkpoint import AudioCheckpoint
from app.services.audio_media import AudioSegment


def _analysis(text):
    return {"transcript_segments": [{"start_seconds": 0, "end_seconds": 5, "text": text}],
            "warnings": []}


def test_concurrent_distinct_roots_retain_all_successes_failures_and_independent_reads(tmp_path):
    source = tmp_path / "complete.wav"
    source.write_bytes(b"complete original soundtrack")
    fingerprint = {"model": "test", "segment_seconds": 5}
    checkpoint = AudioCheckpoint(source, fingerprint)
    start = threading.Barrier(9)

    def recognize(index):
        start.wait(timeout=10)
        segment = AudioSegment(source, index * 5, 5)
        checkpoint.record_failure(segment, "invalid_json", f"bad-{index}")
        checkpoint.record_success(segment, _analysis(f"完整原话-{index}"), f"good-{index}")
        checkpoint.record_failure(segment, "output_incomplete", f"later-bad-{index}")
        # Concurrent callers receive independent mutable results.
        loaded = checkpoint.get(segment)
        loaded["analysis"]["transcript_segments"].clear()
        assert checkpoint.get(segment)["analysis"] == _analysis(f"完整原话-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(recognize, index) for index in range(8)]
        start.wait(timeout=10)
        for future in futures:
            future.result(timeout=20)

    reloaded = AudioCheckpoint(source, fingerprint)
    state = json.loads(checkpoint.path.read_text(encoding="utf-8"))
    assert len(state["segments"]) == 8
    for index in range(8):
        segment = AudioSegment(source, index * 5, 5)
        assert reloaded.get(segment) == {"analysis": _analysis(f"完整原话-{index}"),
                                        "raw_response": f"good-{index}"}
        stored = next(item for item in state["segments"].values()
                      if item["start_seconds"] == index * 5)
        assert [(attempt["outcome"], attempt["raw_response"]) for attempt in stored["attempts"]] == [
            ("failure", f"bad-{index}"), ("success", f"good-{index}"),
            ("failure", f"later-bad-{index}"),
        ]
    assert list(tmp_path.glob("*.tmp")) == []


def test_concurrent_attempts_on_same_span_do_not_overwrite_each_other(tmp_path):
    source = tmp_path / "source.wav"
    source.write_bytes(b"complete soundtrack")
    checkpoint = AudioCheckpoint(source, {"model": "test"})
    segment = AudioSegment(source, 0, 5)
    checkpoint.record_success(segment, _analysis("已验证的完整原话"), "original-success")
    start = threading.Barrier(9)

    def fail(index):
        start.wait(timeout=10)
        checkpoint.record_failure(segment, "invalid_json", f"failed-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fail, index) for index in range(8)]
        start.wait(timeout=10)
        for future in futures:
            future.result(timeout=20)

    reloaded = AudioCheckpoint(source, {"model": "test"})
    assert reloaded.get(segment)["raw_response"] == "original-success"
    state = json.loads(checkpoint.path.read_text(encoding="utf-8"))
    attempts = next(iter(state["segments"].values()))["attempts"]
    assert len(attempts) == 9
    assert {item["raw_response"] for item in attempts[1:]} == {f"failed-{index}" for index in range(8)}
