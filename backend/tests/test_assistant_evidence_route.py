"""All files/providers/HTTP are isolated; never opens a real profile/index/model."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.assistant import backend_knowledge as transport
from app.assistant import evidence_routes as routes
from app.assistant.queue import AssistantQueue
from app.assistant.registry import Registry
from app.db.base import Base
from app.mention_agent.contracts import Binding
from app.models.entities import FavoriteVideo, KnowledgeBase, KnowledgeBaseVideo, VideoCache


VIDEO = "7999999999999999901"
FOREIGN = "7999999999999999902"
A = "https://www.douyin.com/user/evidence-fixture-A"
B = "https://www.douyin.com/user/evidence-fixture-B"
PREFIX = "/api/assistant/evidence/query"
ORIGIN = {"origin": "http://127.0.0.1:5173"}


def source(video=VIDEO, chunk="1"):
    return {"source_id": "资料1", "platform_item_id": video, "chunk_id": video + ":" + chunk,
            "title": "Synthetic transcript", "text": "语音文字稿：\n[00:01–00:03] This is real fixture body.",
            "url": "https://www.douyin.com/video/" + video, "source_type": "speech", "time_range": "00:01–00:03"}


def result(items=None, kb=6):
    return {"status": "ok", "knowledge_base_id": kb, "knowledge_base_name": "Fixture library",
            "evidence": [source()] if items is None else items, "retrieval_mode": "hybrid"}


@pytest.fixture
def fixture(tmp_path):
    database = tmp_path / "business.sqlite3"
    engine = create_engine("sqlite:///" + str(database))
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([KnowledgeBase(id=6, name="Fixture library", name_key="fixture"),
                    KnowledgeBase(id=7, name="Other", name_key="other"),
                    VideoCache(platform_item_id=VIDEO, title="New cache without favorite", status="done"),
                    VideoCache(platform_item_id=FOREIGN, title="Other source", status="done")])
        db.flush()
        db.add_all([KnowledgeBaseVideo(knowledge_base_id=6, platform_item_id=VIDEO),
                    KnowledgeBaseVideo(knowledge_base_id=7, platform_item_id=FOREIGN)])
        db.commit()
    registry = Registry(tmp_path / "registry.sqlite3", catalog_reader=lambda: [{"id": 6, "name": "Fixture library"}, {"id": 7, "name": "Other"}])
    binding = Binding("fixture-binding", "fixture-instance", A, B, 6, False, True)
    migrated = registry.import_legacy(binding, verified_by="fixture-admin", allow_import=False)
    authorization = registry.authorization_snapshot(binding.binding_id, binding.instance_id, 6, "query")
    envelope = {"command_id": "command:" + "1" * 64, "reference_id": "reference:" + "2" * 64,
        "reference_quality": "verified_ui", "native_comment_id": None, "sender_profile_url": A,
        "receiver_profile_url": B, "video_id": VIDEO, "comment_text": "这条视频解释什么？", "source_mode": "isolated",
        "evidence": {"real_mention": True, "sender_verified": True, "video_verified": True, "unique_count": 1,
                     "observed_at_utc": datetime.now(timezone.utc).isoformat()}}
    queue = AssistantQueue(tmp_path / "queue.sqlite3")
    task = queue.enqueue(binding, envelope, kind="query", user_id=migrated["user_id"], device_id=migrated["device_id"], authorization=authorization)
    claimed = queue.claim_next("execute", run_id="fixture-run")
    calls, state = [], {"hook": None, "result": result([source(), source(FOREIGN)])}

    def factory(database_arg, kb, *, focus_video_id=None):
        def query(question):
            calls.append((database_arg, kb, focus_video_id, question))
            if state["hook"]:
                state["hook"]()
            return deepcopy(state["result"])
        return SimpleNamespace(query=query)

    control = SimpleNamespace(snapshot=lambda: {"run_id": "fixture-run", "desired_state": state.get("desired", "running")})
    service = routes.EvidenceService(queue, registry, database, knowledge_factory=factory, control=control)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[routes.get_service] = lambda: service
    headers = {**ORIGIN, transport.EXECUTION_CLAIM_HEADER: claimed["execution_token"]}
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000), headers=headers) as client:
        yield SimpleNamespace(database=database, engine=engine, registry=registry, migrated=migrated, queue=queue,
            binding=binding, task=task, claim=claimed, client=client, calls=calls, state=state, service=service, app=app)
    engine.dispose()


def body(fixture, **extra):
    return {"task_id": fixture.task["id"], "question": "Explain current video", **extra}


def helper(fixture, kb=6, focus=None):
    return transport.BackendKnowledgeTools(fixture.database, kb, fixture.task["id"], focus,
        execution_token=fixture.claim["execution_token"])


def test_backend_uses_frozen_query_scope_and_original_video_focus(fixture):
    response = fixture.client.post(PREFIX, json=body(fixture))
    assert response.status_code == 200
    assert response.json()["knowledge_base_id"] == 6
    assert [item["platform_item_id"] for item in response.json()["evidence"]] == [VIDEO]
    assert fixture.claim["execution_token"] not in response.text
    assert fixture.calls[0][1:3] == (6, VIDEO)
    with Session(fixture.engine) as db:
        assert db.scalar(select(func.count()).select_from(FavoriteVideo)) == 0


@pytest.mark.parametrize("extra", [{"knowledge_base_id": 7}, {"binding_id": "other"}, {"url": "https://evil.invalid"}, {"database": "other.db"}])
def test_client_cannot_supply_library_binding_address_or_path(fixture, extra):
    assert fixture.client.post(PREFIX, json=body(fixture, **extra)).status_code == 422
    assert fixture.calls == []


def test_mismatched_focus_is_rejected_before_provider(fixture):
    assert fixture.client.post(PREFIX, json=body(fixture, focus_video_id=FOREIGN)).status_code == 403
    assert fixture.calls == []


@pytest.mark.parametrize("value", ["", "true", "f" * 47, "g" * 48, "f" * 49])
def test_missing_or_malformed_execution_claim_never_calls_provider(fixture, value):
    response = fixture.client.post(PREFIX, json=body(fixture), headers={transport.EXECUTION_CLAIM_HEADER: value})
    assert response.status_code == 403 and fixture.calls == []


def test_old_request_cannot_borrow_same_task_new_retry_claim(fixture):
    old = fixture.claim["execution_token"]
    fixture.queue.finish(fixture.task["id"], old, "execute", {"state": "needs_reconcile"})
    fixture.queue.apply_reconciliation(fixture.task["id"], {"state": "queued", "model_started": False,
        "ingestion_submitted": False, "send_token_present": False, "safe_to_execute": True,
        "binding_active": True, "public_reply_allowed": False})
    fixture.queue.resume(fixture.task["id"])
    newer = fixture.queue.claim_next("execute", run_id="fixture-run")
    assert newer["execution_token"] != old
    assert fixture.client.post(PREFIX, json=body(fixture)).status_code == 403
    assert fixture.calls == []
    assert fixture.client.post(PREFIX, json=body(fixture),
        headers={transport.EXECUTION_CLAIM_HEADER: newer["execution_token"]}).status_code == 200
    assert len(fixture.calls) == 1


@pytest.mark.parametrize("mode", ["unknown", "queued", "save", "expired", "stopped"])
def test_only_current_executing_query_claim_can_retrieve(fixture, mode):
    request = body(fixture)
    if mode == "unknown":
        request["task_id"] = "0" * 32
    elif mode == "stopped":
        fixture.state["desired"] = "stopped"
    else:
        with fixture.queue._tx() as db:
            if mode == "queued":
                db.execute("UPDATE assistant_tasks SET state='queued'")
            elif mode == "save":
                db.execute("UPDATE assistant_tasks SET kind='save'")
            else:
                db.execute("UPDATE assistant_tasks SET lease_until=0")
    assert fixture.client.post(PREFIX, json=request).status_code in {404, 409}
    assert fixture.calls == []


@pytest.mark.parametrize("when", ["before", "during"])
def test_revoke_or_regrant_never_releases_old_scope(fixture, when):
    def revoke_and_regrant():
        fixture.registry.revoke_grant(fixture.migrated["grant"]["grant_id"])
        fixture.registry.grant_library(fixture.migrated["user_id"], fixture.migrated["device_id"], 6,
            can_query=True, can_import=False, public_reply_allowed=False, is_default=True)
    if when == "before":
        revoke_and_regrant()
    else:
        fixture.state["hook"] = revoke_and_regrant
    assert fixture.client.post(PREFIX, json=body(fixture)).status_code == 403
    assert len(fixture.calls) == (0 if when == "before" else 1)


@pytest.mark.parametrize("change", ["finished", "claim", "stop"])
def test_after_query_checks_claim_and_control_again(fixture, change):
    def changed():
        if change == "stop":
            fixture.state["desired"] = "stopped"
        else:
            with fixture.queue._tx() as db:
                if change == "finished":
                    db.execute("UPDATE assistant_tasks SET state='needs_review'")
                else:
                    db.execute("UPDATE assistant_tasks SET execution_token=?", ("e" * 48,))
    fixture.state["hook"] = changed
    assert fixture.client.post(PREFIX, json=body(fixture)).status_code in {403, 409}
    assert len(fixture.calls) == 1


def test_pause_does_not_cancel_already_claimed_query(fixture):
    fixture.state["desired"] = "paused"
    assert fixture.client.post(PREFIX, json=body(fixture)).status_code == 200


def test_same_service_sees_fresh_cache_membership_and_filters_metadata(fixture):
    with Session(fixture.engine) as db:
        row = db.get(KnowledgeBaseVideo, (6, VIDEO)); db.delete(row); db.commit()
    assert fixture.client.post(PREFIX, json=body(fixture)).json()["evidence"] == []
    with Session(fixture.engine) as db:
        db.add(KnowledgeBaseVideo(knowledge_base_id=6, platform_item_id=VIDEO)); db.commit()
    assert fixture.client.post(PREFIX, json=body(fixture)).json()["evidence"][0]["platform_item_id"] == VIDEO
    fixture.state["result"] = result([source() | {"text": "视频标题：Synthetic transcript"}])
    assert fixture.client.post(PREFIX, json=body(fixture)).json()["evidence"] == []


def test_provider_exception_is_fixed_and_has_no_response_evidence(fixture):
    def fail():
        raise RuntimeError("fictional credential or provider URL must not escape")
    fixture.state["hook"] = fail
    response = fixture.client.post(PREFIX, json=body(fixture))
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "evidence_query_failed"}}


@pytest.mark.parametrize("change", [{"knowledge_base_id": 7}, {"knowledge_base_name": "Different library"}])
def test_provider_cannot_change_server_library_identity(fixture, change):
    fixture.state["result"].update(change)
    response = fixture.client.post(PREFIX, json=body(fixture))
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "evidence_result_invalid"}}


@pytest.mark.parametrize("headers", [{"origin": "https://evil.invalid"}, {"origin": "null"}, {"host": "evil.invalid"}, {"content-type": "text/plain"}])
def test_origin_host_and_json_checks_precede_evidence(fixture, headers):
    assert fixture.client.post(PREFIX, content=json.dumps(body(fixture)), headers=headers).status_code in {403, 415}
    assert fixture.calls == []


def test_nonlocal_client_is_rejected(fixture):
    with TestClient(fixture.app, base_url="http://127.0.0.1", client=("203.0.113.2", 50000), headers=ORIGIN) as remote:
        assert remote.post(PREFIX, json=body(fixture)).status_code == 403
    assert fixture.calls == []


def test_bounded_body_question_and_duplicate_keys(fixture):
    assert fixture.client.post(PREFIX, content=b"x" * (routes.MAX_REQUEST_BYTES + 1), headers={"content-type": "application/json"}).status_code == 413
    assert fixture.client.post(PREFIX, json=body(fixture, question="x" * 2001)).status_code == 422
    assert fixture.client.post(PREFIX, json=body(fixture, question="   ")).status_code == 422
    duplicate = '{"task_id":"' + fixture.task["id"] + '","question":"one","question":"two"}'
    assert fixture.client.post(PREFIX, content=duplicate, headers={"content-type": "application/json"}).status_code == 422
    assert fixture.calls == []


class Response:
    def __init__(self, value, *, url=transport.EVIDENCE_URL, content_type="application/json"):
        self.data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.url, self.headers = url, {"Content-Type": content_type}
        self.read_limits = []
    def getcode(self): return 200
    def geturl(self): return self.url
    def read(self, limit): self.read_limits.append(limit); return self.data[:limit]
    def __enter__(self): return self
    def __exit__(self, *args): pass


def fake_http(monkeypatch, responses):
    calls, handlers = [], []
    def build(*values):
        handlers.extend(values)
        def open_(request, timeout):
            calls.append((request, timeout))
            value = responses.pop(0)
            if isinstance(value, Exception): raise value
            return value
        return SimpleNamespace(open=open_)
    monkeypatch.setattr(transport.urllib.request, "build_opener", build)
    return calls, handlers


def test_helper_describe_is_sql_only_and_http_target_proxy_body_are_fixed(fixture, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://evil.invalid:9090")
    monkeypatch.setattr(transport.LocalKnowledgeTools, "query", lambda *args: pytest.fail("helper opened local retrieval"))
    calls, handlers = fake_http(monkeypatch, [Response(result())])
    tools = helper(fixture, focus=VIDEO)
    before = hashlib.sha256(fixture.database.read_bytes()).hexdigest()
    assert tools.describe()["id"] == 6 and calls == []
    assert tools.query("Explain")["evidence"]
    request, timeout = calls[0]
    assert request.full_url == transport.EVIDENCE_URL and request.method == "POST"
    assert json.loads(request.data) == {"task_id": fixture.task["id"], "question": "Explain", "focus_video_id": VIDEO}
    assert request.get_header(transport.EXECUTION_CLAIM_HEADER.capitalize()) == fixture.claim["execution_token"]
    assert fixture.claim["execution_token"] not in request.full_url
    assert fixture.claim["execution_token"].encode() not in request.data
    assert timeout == transport.HTTP_TIMEOUT_SECONDS
    assert handlers[0].proxies == {} and isinstance(handlers[1], transport.NoEvidenceRedirect)
    assert hashlib.sha256(fixture.database.read_bytes()).hexdigest() == before


def test_helper_source_ids_remain_stable_across_server_request_local_labels(fixture, monkeypatch):
    fake_http(monkeypatch, [Response(result([source(chunk="1")])), Response(result([source(chunk="2")])),
                            Response(result([source(chunk="2"), source(chunk="1")]))])
    tools = helper(fixture)
    assert tools.query("one")["evidence"][0]["source_id"] == "资料1"
    assert tools.query("two")["evidence"][0]["source_id"] == "资料2"
    assert [item["source_id"] for item in tools.query("again")["evidence"]] == ["资料2", "资料1"]


def test_redirect_handler_never_creates_another_request():
    with pytest.raises(transport.BackendEvidenceError, match="redirect_rejected"):
        transport.NoEvidenceRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.invalid")


@pytest.mark.parametrize("response", [Response(result(kb=7)), Response(result(), url="http://evil.invalid"),
    Response(result(), content_type="text/plain"), Response(b"x" * (transport.MAX_RESPONSE_BYTES + 1)),
    Response(b'{"status":"ok","status":"empty"}'), TimeoutError("synthetic timeout")])
def test_helper_bad_scope_redirect_response_or_timeout_has_one_attempt(fixture, monkeypatch, response):
    calls, _ = fake_http(monkeypatch, [response])
    tools = helper(fixture)
    with pytest.raises(transport.BackendEvidenceError): tools.query("Explain")
    assert len(calls) == 1


def test_helper_rejects_overlong_question_before_http(fixture, monkeypatch):
    calls, _ = fake_http(monkeypatch, [])
    tools = helper(fixture)
    with pytest.raises(ValueError): tools.query("x" * 2001)
    assert calls == []
    with pytest.raises(TypeError):
        transport.BackendKnowledgeTools(fixture.database, 6, fixture.task["id"], url="https://evil.invalid")


def test_helper_roundtrip_uses_server_evidence_without_local_chroma(fixture, monkeypatch):
    def build(*handlers):
        def open_(request, timeout):
            reply = fixture.client.post(PREFIX, content=request.data, headers=dict(request.header_items()))
            assert reply.status_code == 200
            return Response(reply.json())
        return SimpleNamespace(open=open_)
    monkeypatch.setattr(transport.urllib.request, "build_opener", build)
    tools = helper(fixture, focus=VIDEO)
    assert tools.describe()["id"] == 6
    received = tools.query("Explain only the original current video")
    assert received["knowledge_base_id"] == 6
    assert [item["platform_item_id"] for item in received["evidence"]] == [VIDEO]
    assert received["evidence"][0]["source_id"] == "资料1"
    assert len(fixture.calls) == 1
