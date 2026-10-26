"""Timing metadata only: temporary ledgers/business DB, fake run reader and worker."""
import copy
import hashlib
import json

import pytest

from app.assistant.queue import AssistantQueue, QueueError, normalized_result
from app.services.ingestion_metrics import list_ingestion_runs
from tests.test_assistant_queue import binding, envelope
from tests.test_mention_ingestion_service import context, VIDEO, OTHER


def processing(**changes):
    return {"run_id": "fixture-run-1", "request_id": "fixture-request-1", "video_id": VIDEO,
            "stages_seconds": {"download": 0, "recognition": 1.25, "indexing": 2}, **changes}


def details(timings):
    return {"state": "needs_review", "details": {"timings": timings}}


def test_timings_preserve_optional_missing_and_explicit_zero():
    assert "timings" not in normalized_result({"state": "needs_review", "details": {}})["details"]
    assert normalized_result(details({}))["details"]["timings"] == {}
    value = {"execution_ms": 0, "model_ms": 12, "retrieval_ms": 3, "video_processing": processing()}
    assert normalized_result(details(value))["details"]["timings"] == value


@pytest.mark.parametrize("field", ["execution_ms", "model_ms", "retrieval_ms"])
@pytest.mark.parametrize("value", [True, False, -1, 1.2, "3", None, float("inf"), float("nan")])
def test_milliseconds_reject_non_integer_or_invalid(field, value):
    with pytest.raises(QueueError, match="invalid_task_timings"):
        normalized_result(details({field: value}))


@pytest.mark.parametrize("value", [True, False, -1, "2", None, float("inf"), float("nan"), 10**400])
def test_video_seconds_reject_unmeasured_or_nonfinite(value):
    with pytest.raises(QueueError, match="invalid_task_timings"):
        normalized_result(details({"video_processing": processing(stages_seconds={"download": value})}))


@pytest.mark.parametrize("value", [
    {"api_key": "fixture"},
    {"video_processing": processing(extra="not-allowed")},
    {"video_processing": processing(stages_seconds={"queue": 1})},
    {"video_processing": processing(run_id="")},
    {"video_processing": processing(request_id=True)},
    {"video_processing": processing(video_id="not-a-video")},
    {"video_processing": {"run_id": "missing-other-required-fields"}},
])
def test_timings_strict_whitelist_and_identity_shape(value):
    with pytest.raises(QueueError, match="invalid_task_timings"):
        normalized_result(details(value))


def test_timings_survive_queue_reopen_without_schema_migration(tmp_path):
    path = tmp_path / "queue.sqlite3"
    queue = AssistantQueue(path)
    task = queue.enqueue(binding(), envelope())
    claim = queue.claim_next(run_id="fixture-run")
    measured = {"execution_ms": 42, "video_processing": processing(stages_seconds={"recognition": 1.5})}
    queue.finish(task["id"], claim["execution_token"], "execute", details(measured))
    reopened = AssistantQueue(path)
    assert reopened.get_task(task["id"])["details"]["timings"] == measured
    assert "model_ms" not in reopened.public_task(reopened.get_task(task["id"]), detail=True)["details"]["timings"]


def telemetry(context, **changes):
    return {"run_id": "fixture-run-1", "video_id": VIDEO, "task_id": context.payload["request_id"],
            "title": "fixture-title-must-not-project", "stages": {
                "download": {"seconds": 0}, "recognition": {"seconds": 2.5},
                "indexing": {"seconds": 1.0}, "unknown": {"seconds": 999}}, **changes}


def submitted(context):
    assert "video_processing" not in context.service.submit(context.payload)
    return context.payload["request_id"]


def test_receipt_projects_only_exact_request_video_and_stage_numbers(context):
    request_id = submitted(context)
    read_videos = []
    def reader(video_id):
        read_videos.append(video_id)
        return [telemetry(context), telemetry(context, task_id="later-other-request", run_id="latest-other-run")]
    context.service.run_reader = reader
    result = context.service.get(request_id)
    assert result["video_processing"] == {
        "run_id": "fixture-run-1", "request_id": request_id, "video_id": VIDEO,
        "stages_seconds": {"download": 0, "recognition": 2.5, "indexing": 1.0}}
    assert read_videos == [VIDEO]
    assert "fixture-title" not in str(result)
    assert result["state"] == "accepted"
    assert len(context.worker.jobs) == 1 and context.calls == []


@pytest.mark.parametrize("case", ["none", "different_request", "wrong_video", "duplicate", "bad_id", "no_stages", "reader_error"])
def test_receipt_never_borrows_ambiguous_missing_or_other_run(context, case):
    request_id = submitted(context)
    options = {
        "none": [], "different_request": [telemetry(context, task_id="some-other-request")],
        "wrong_video": [telemetry(context, video_id=OTHER)],
        "duplicate": [telemetry(context), telemetry(context, run_id="another-run")],
        "bad_id": [telemetry(context, run_id="")], "no_stages": [telemetry(context, stages={})],
    }
    def reader(video_id):
        if case == "reader_error":
            raise RuntimeError("fixture-private-error")
        return options[case]
    context.service.run_reader = reader
    assert "video_processing" not in context.service.get(request_id)
    assert len(context.worker.jobs) == 1 and context.calls == []


@pytest.mark.parametrize("value", [True, False, -1, "2", None, float("inf"), float("nan"), 10**400])
def test_receipt_omits_invalid_stage_without_losing_other_measured_stages(context, value):
    request_id = submitted(context)
    context.service.run_reader = lambda video_id: [telemetry(context, stages={
        "download": {"seconds": 1.5}, "recognition": {"seconds": value}})]
    assert context.service.get(request_id)["video_processing"]["stages_seconds"] == {"download": 1.5}


def test_receipt_reader_does_not_mutate_telemetry_or_business_data(context):
    request_id = submitted(context)
    run = telemetry(context)
    old = copy.deepcopy(run)
    before = hashlib.sha256(context.database.read_bytes()).hexdigest()
    context.service.run_reader = lambda video_id: [run]
    first = context.service.get(request_id)
    assert context.service.get(request_id) == first
    assert run == old and hashlib.sha256(context.database.read_bytes()).hexdigest() == before
    assert len(context.worker.jobs) == 1 and context.calls == []


def test_original_telemetry_reader_uses_only_fixture_files_and_keeps_them_unchanged(context, tmp_path):
    request_id = submitted(context)
    assert context.service.run_reader is None
    root = tmp_path / "fixture-metrics"
    folder = root / VIDEO
    folder.mkdir(parents=True)
    current = telemetry(context, schema_version=1, status="done", started_at="2026-01-01T00:00:00+00:00")
    latest_other = telemetry(context, schema_version=1, status="done", started_at="2026-02-01T00:00:00+00:00",
                             run_id="later-other-run", task_id="some-other-request")
    for name, run in (("current.json", current), ("other.json", latest_other)):
        (folder / name).write_text(json.dumps(run), encoding="utf-8")
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in folder.iterdir()}
    context.service.run_reader = lambda video_id: list_ingestion_runs(video_id, root=root)
    result = context.service.get(request_id)
    assert result["video_processing"]["run_id"] == "fixture-run-1"
    assert result["video_processing"]["request_id"] == request_id
    assert before == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in folder.iterdir()}
    assert len(context.worker.jobs) == 1 and context.calls == []
