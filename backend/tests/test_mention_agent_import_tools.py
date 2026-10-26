"""Import-tool boundaries: fictional messages, temporary ledger, no HTTP/model.

These tests exercise the actual ImportTools and AgentEngine. Backend responses
are fixtures; passing them does not prove real media ingestion or indexing.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.mention_agent.contracts import Binding
from app.mention_agent.engine import AgentEngine
from app.mention_agent.imports import ImportTools, is_save_command
from app.mention_agent.store import AgentStore


LIBRARY = "虚构测试库"
SAVE_TEXT = '@虚构助手 帮我把这个视频加入“虚构测试库”知识库。'
FICTIONAL_ERROR = "fiction-only private backend error: api_key=DO-NOT-EXPOSE"


def fixture_binding():
    return Binding("import-tools-fixture", "fixture-desktop", "https://www.douyin.com/user/fake-sender",
                   "https://www.douyin.com/user/fake-receiver", 7, True)


def fixture_envelope(**changes):
    binding = fixture_binding()
    return {"command_id": "fixture-import-command", "reference_id": "fixture-import-reference",
            "reference_quality": "verified_ui", "native_comment_id": None,
            "sender_profile_url": binding.sender_profile_url, "receiver_profile_url": binding.receiver_profile_url,
            "video_id": "7999999000000000001", "comment_text": SAVE_TEXT, "source_mode": "live_ui",
            "evidence": {"real_mention": True, "sender_verified": True, "video_verified": True, "unique_count": 1,
                "observed_at_utc": datetime.now(timezone.utc).isoformat()}, **changes}


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += max(seconds, 0.001)


class FakeBackendClient:
    def __init__(self, binding=None, envelope=None):
        self.binding = binding or fixture_binding()
        self.envelope = envelope or fixture_envelope()
        self.posts = []
        self.gets = []
        self.submit_action = None
        self.status_actions = []
        self.default_status = "processing"

    def response(self, request_id, state="processing", **changes):
        return {"request_id": request_id, "binding_id": self.binding.binding_id,
                "instance_id": self.binding.instance_id, "video_id": self.envelope["video_id"],
                "knowledge_base_id": self.binding.knowledge_base_id, "state": state,
                "membership_exists": True, "cache_status": "done" if state == "succeeded" else "transcribing",
                "indexed_content_chunks": 2 if state == "succeeded" else 0, **changes}

    @staticmethod
    def _resolve(action, request_id):
        if isinstance(action, Exception):
            raise action
        return action(request_id) if callable(action) else deepcopy(action)

    def submit(self, body):
        self.posts.append(deepcopy(body))
        if self.submit_action is None:
            return self.response(body["request_id"])
        return self._resolve(self.submit_action, body["request_id"])

    def status(self, request_id):
        self.gets.append(request_id)
        if self.status_actions:
            return self._resolve(self.status_actions.pop(0), request_id)
        return self.response(request_id, self.default_status)


class FakeKnowledge:
    def describe(self):
        return {"id": 7, "name": LIBRARY, "total_count": 0, "ready_count": 0}

    def query(self, question):
        raise AssertionError("import tests must not invoke RAG")


class FakeModel:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments
        self.histories = []

    def complete(self, messages, tools):
        self.histories.append(deepcopy(messages))
        return SimpleNamespace(message={"role": "assistant", "tool_calls": [{"id": "fictional-call-1",
            "type": "function", "function": {"name": self.name, "arguments": json.dumps(self.arguments)}}]},
            usage={"input_tokens": 0, "output_tokens": 0}, finish_reason="tool_calls")


class ImportToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / "fictional-agent.sqlite3"
        self.store = AgentStore(self.path)
        self.binding = fixture_binding()
        self.store.put_binding(self.binding.to_dict())
        self.envelope = fixture_envelope()
        task = self.store.enqueue(self.binding.binding_id, self.envelope)
        self.claim = self.store.claim(task["task_id"], lease_seconds=1200)
        self.clock = FakeClock()
        self.backend = FakeBackendClient(self.binding, self.envelope)
        self.monotonic = patch("app.mention_agent.imports.time.monotonic", self.clock.monotonic)
        self.monotonic.start()
        self.addCleanup(self.monotonic.stop)

    def tools(self, *, store=None, envelope=None, should_stop=None, wait_seconds=0):
        return ImportTools(store or self.store, self.binding, self.claim["task_id"], self.claim["claim_token"],
                           envelope or self.envelope, LIBRARY, should_stop=should_stop, client=self.backend,
                           wait_seconds=wait_seconds, sleep=self.clock.sleep)

    def existing_success(self):
        job = self.store.ensure_ingestion(self.claim["task_id"], self.claim["claim_token"])
        return self.store.observe_ingestion(job["job_id"], {
            "backend_request_id": job["job_id"], "binding_id": self.binding.binding_id,
            "instance_id": self.binding.instance_id, "video_id": self.envelope["video_id"],
            "knowledge_base_id": 7, "backend_state": "succeeded", "membership_exists": True,
            "cache_status": "done", "indexed_content_chunks": 2})

    def test_explicit_save_grammar_positive_corpus(self):
        positives = [SAVE_TEXT, '把这个视频加入“虚构测试库”', '请帮我将当前视频导入到"虚构测试库"知识库！',
                     '帮我把本条视频上传到「虚构测试库」知识库', '请把这个视频保存到‘虚构测试库’。',
                     '@6 帮我把这个视频加入“虚构测试库”，测试编号 DM-SAVE-01。',
                     '  @助手 \n请帮我把这个视频加入“虚构测试库”知识库。  ']
        for text in positives:
            with self.subTest(text=text):
                self.assertTrue(is_save_command(text, LIBRARY))

    def test_explicit_save_grammar_negation_quotation_condition_and_other_targets(self):
        negatives = [
            '不要保存，只根据“虚构测试库”回答', '不要把这个视频加入“虚构测试库”',
            '如果合适，请把这个视频加入“虚构测试库”', '你能把这个视频加入“虚构测试库”吗？',
            '有人说“把这个视频加入“虚构测试库””，这句话什么意思', '“把这个视频加入“虚构测试库””',
            '请解释把这个视频加入“虚构测试库”的流程', '把这个视频加入“其他库”',
            '把这个视频加入“虚构测试库”和“其他库”', '把这个视频加入虚构测试库',
            '把另一个视频加入“虚构测试库”', '把这个视频加入“虚构测试库” https://example.invalid/x',
            '把这个视频加入“虚构测试库”，然后删除其他资料', '把这个视频加入“虚构测试库”吗',
            '帮我把这个视频加入“虚构测试库”；不要真的执行', '', None, 7,
        ]
        for text in negatives:
            with self.subTest(text=text):
                self.assertFalse(is_save_command(text, LIBRARY))

    def test_empty_or_invalid_library_name_does_not_authorize_save(self):
        for name in ("", " ", None, 7):
            with self.subTest(name=name):
                self.assertFalse(is_save_command('把这个视频加入“”', name))

    def test_regex_metacharacters_in_library_name_are_literal(self):
        self.assertTrue(is_save_command('把这个视频加入“测试[1].*”', '测试[1].*'))
        self.assertFalse(is_save_command('把这个视频加入“测试任意名字”', '测试[1].*'))

    def test_non_live_or_non_command_does_not_dispatch(self):
        for change in ({"source_mode": "replay"}, {"source_mode": "isolated"},
                       {"comment_text": '不要把这个视频加入“虚构测试库”'}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "explicit_save_command_required"):
                self.tools(envelope=self.envelope | change).save_current_video(7)
        self.assertEqual(self.backend.posts, [])
        self.assertEqual(self.store.list_ingestions(), [])

    def test_library_arguments_reject_bool_string_float_or_other_id(self):
        for value in (True, "7", 7.0, 8, None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "library_not_allowed"):
                self.tools().save_current_video(value)
        self.assertEqual(self.backend.posts, [])
        self.assertEqual(self.store.list_ingestions(), [])

    def test_one_post_across_repeated_tools_and_restarted_store(self):
        first = self.tools().save_current_video(7)
        self.assertEqual(first["status"], "processing")
        second = self.tools().save_current_video(7)
        restarted = self.tools(store=AgentStore(self.path)).save_current_video(7)
        self.assertEqual(first["import_job_id"], second["import_job_id"])
        self.assertEqual(first["import_job_id"], restarted["import_job_id"])
        self.assertEqual(len(self.backend.posts), 1)
        self.assertEqual(len(self.backend.gets), 2)
        self.assertEqual(set(self.backend.posts[0]), {"request_id", "binding_id", "instance_id", "video_id", "knowledge_base_id"})
        self.assertNotIn("title", self.backend.posts[0])
        self.assertNotIn("url", self.backend.posts[0])

    def test_reserved_crash_recovery_only_gets_without_second_post(self):
        job = self.store.ensure_ingestion(self.claim["task_id"], self.claim["claim_token"])
        self.store.reserve_ingestion(self.claim["task_id"], self.claim["claim_token"])
        self.backend.status_actions = [lambda request_id: self.backend.response(request_id, "succeeded")]
        result = self.tools(store=AgentStore(self.path)).save_current_video(7)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["import_job_id"], job["job_id"])
        self.assertEqual(self.backend.posts, [])
        self.assertEqual(self.backend.gets, [job["job_id"]])

    def test_lost_post_response_can_finish_by_get_without_repost(self):
        self.backend.submit_action = TimeoutError(FICTIONAL_ERROR)
        self.backend.status_actions = [lambda request_id: self.backend.response(request_id, "succeeded")]
        tools = self.tools(wait_seconds=6)
        result = tools.save_current_video(7)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(self.backend.posts), 1)
        self.assertEqual(len(self.backend.gets), 1)
        job = self.store.get_ingestion(result["import_job_id"])
        self.assertEqual(job["submission_outcome"], "submission_unknown")
        self.backend.default_status = "succeeded"
        proposal = tools.proposal()
        self.assertEqual(proposal["import_state"], "succeeded")
        self.assertNotIn(FICTIONAL_ERROR, json.dumps(proposal))
        self.assertNotIn("DO-NOT-EXPOSE", self.path.read_bytes().decode("utf-8", errors="ignore"))

    def test_unknown_post_and_missing_receipt_do_not_retry_until_fake_deadline(self):
        self.backend.submit_action = TimeoutError(FICTIONAL_ERROR)
        self.backend.status_actions = [None, None, None]
        tools = self.tools(wait_seconds=5)
        result = tools.save_current_video(7)
        self.assertEqual(result["status"], "submission_unknown")
        self.assertEqual(len(self.backend.posts), 1)
        self.assertEqual(len(self.backend.gets), 3)
        self.assertEqual(self.clock.sleeps, [2, 2, 1])
        self.assertEqual(tools.proposal()["import_state"], "submission_unknown")
        self.assertNotIn("已加入", tools.proposal()["text"])

    def test_repeated_save_calls_share_one_lifetime_wait_budget(self):
        tools = self.tools(wait_seconds=5)
        first = tools.save_current_video(7)
        self.assertEqual(first["status"], "processing")
        self.assertEqual(self.clock.now, 1005.0)
        self.assertEqual(self.clock.sleeps, [2, 2, 1])
        deadline = tools.wait_deadline
        initial_gets = len(self.backend.gets)
        # A new model tool-call ID must not turn into another full wait window.
        second = tools.save_current_video(7)
        third = tools.save_current_video(7)
        self.assertEqual(second["import_job_id"], first["import_job_id"])
        self.assertEqual(third["import_job_id"], first["import_job_id"])
        self.assertEqual(self.clock.now, 1005.0)
        self.assertEqual(tools.wait_deadline, deadline)
        self.assertEqual(self.clock.sleeps, [2, 2, 1])
        self.assertEqual(len(self.backend.posts), 1)
        # Repeated calls may each make one read-only reconciliation GET.
        self.assertEqual(len(self.backend.gets), initial_gets + 2)

    def test_ack_or_processing_with_three_content_fields_never_means_success(self):
        for index, state in enumerate(("pending", "accepted", "processing")):
            with self.subTest(state=state):
                observed = fixture_envelope(command_id="state-command-" + str(index), reference_id="state-reference-" + str(index),
                                            video_id="79999990000000000" + str(index + 10))
                task = self.store.enqueue(self.binding.binding_id, observed)
                claim = self.store.claim(task["task_id"])
                backend = FakeBackendClient(self.binding, observed)
                backend.submit_action = lambda request_id, state=state, backend=backend: backend.response(request_id, state,
                    membership_exists=True, cache_status="done", indexed_content_chunks=5)
                tools = ImportTools(self.store, self.binding, claim["task_id"], claim["claim_token"], observed, LIBRARY,
                                    client=backend, wait_seconds=0, sleep=self.clock.sleep)
                result = tools.save_current_video(7)
                self.assertEqual(result["status"], "accepted" if state == "pending" else state)
                proposed = tools.proposal()
                self.assertNotEqual(proposed["import_state"], "succeeded")
                self.assertNotIn("已加入", proposed["text"])
                self.assertEqual(len(backend.posts), 1)

    def test_failed_backend_uses_fixed_status_text_without_raw_error(self):
        self.backend.submit_action = lambda request_id: self.backend.response(request_id, "failed", cache_status="failed",
            error_message=FICTIONAL_ERROR, raw_exception={"password": "fiction-only"})
        tools = self.tools()
        result = tools.save_current_video(7)
        self.assertEqual(result["status"], "failed")
        proposed = tools.proposal()
        self.assertEqual(proposed["import_state"], "failed")
        self.assertIn("没有完成", proposed["text"])
        self.assertNotIn("DO-NOT-EXPOSE", json.dumps(result | proposed, ensure_ascii=False))
        self.assertNotIn("DO-NOT-EXPOSE", self.path.read_bytes().decode("utf-8", errors="ignore"))

    def test_backend_target_mismatch_cannot_confirm_success(self):
        self.backend.submit_action = lambda request_id: self.backend.response(request_id, "succeeded", video_id="7999999000000000099")
        tools = self.tools()
        with self.assertRaisesRegex(ValueError, "import_response_scope_mismatch"):
            tools.save_current_video(7)
        self.assertEqual(len(self.backend.posts), 1)
        self.assertEqual(self.store.list_ingestions()[0]["state"], "accepted")
        with self.assertRaisesRegex(ValueError, "import_status_required"):
            tools.proposal()

    def test_status_and_proposal_require_observation_and_never_post(self):
        tools = self.tools()
        with self.assertRaisesRegex(ValueError, "import_status_required"):
            tools.proposal()
        status = tools.get_import_status()
        self.assertEqual(status["status"], "queued")
        proposed = tools.proposal()
        self.assertEqual(proposed["kind"], "import_status")
        self.assertEqual(proposed["import_state"], "queued")
        self.assertEqual(proposed["source_ids"], [])
        self.assertEqual(proposed["sources"], [])
        self.assertEqual(self.backend.posts, [])
        self.assertEqual(self.backend.gets, [])

    def test_old_success_needs_fresh_get_and_current_membership_and_index(self):
        job = self.existing_success()
        failures = [lambda request_id: self.backend.response(request_id, "succeeded", membership_exists=False),
                    lambda request_id: self.backend.response(request_id, "succeeded", indexed_content_chunks=0),
                    lambda request_id: self.backend.response(request_id, "succeeded", cache_status="embedding"),
                    RuntimeError(FICTIONAL_ERROR), None,
                    lambda request_id: self.backend.response(request_id, "processing")]
        for action in failures:
            with self.subTest(action_type=type(action).__name__):
                self.backend.status_actions = [action, action]
                tools = self.tools()
                tools.get_import_status()
                with self.assertRaisesRegex(ValueError, "import_status_unverified"):
                    tools.proposal()
        self.assertEqual(self.backend.posts, [])
        self.assertEqual(self.store.get_ingestion(job["job_id"])["state"], "succeeded")

    def test_success_requires_another_get_at_proposal_time(self):
        job = self.existing_success()
        self.backend.status_actions = [lambda request_id: self.backend.response(request_id, "succeeded"),
                                       TimeoutError(FICTIONAL_ERROR)]
        tools = self.tools()
        self.assertEqual(tools.get_import_status()["status"], "succeeded")
        self.assertTrue(tools.confirmed_succeeded)
        with self.assertRaisesRegex(ValueError, "import_status_unverified"):
            tools.proposal()
        self.assertEqual(self.backend.gets, [job["job_id"], job["job_id"]])
        self.assertFalse(tools.confirmed_succeeded)

    def test_current_success_can_complete_real_store_import_status_proposal(self):
        self.backend.submit_action = lambda request_id: self.backend.response(request_id, "succeeded")
        self.backend.default_status = "succeeded"
        tools = self.tools()
        self.assertEqual(tools.save_current_video(7)["status"], "succeeded")
        proposed = tools.proposal()
        reply = self.store.complete(self.claim["task_id"], self.claim["claim_token"], proposed)
        self.assertEqual(reply["kind"], "import_status")
        self.assertEqual(reply["import_state"], "succeeded")
        self.assertEqual(reply["target"]["video_id"], self.envelope["video_id"])
        self.assertEqual(len(self.backend.posts), 1)
        self.assertEqual(len(self.backend.gets), 1)

    def test_stop_before_any_work_does_not_post_or_create_job(self):
        with self.assertRaisesRegex(ValueError, "import_run_stopped"):
            self.tools(should_stop=lambda: True).save_current_video(7)
        self.assertEqual(self.backend.posts, [])
        self.assertEqual(self.store.list_ingestions(), [])

    def test_stop_after_reservation_and_before_post_records_no_submit(self):
        checks = iter([False, True])
        result = self.tools(should_stop=lambda: next(checks)).save_current_video(7)
        self.assertEqual(result["status"], "failed_no_submit")
        self.assertEqual(self.backend.posts, [])
        job = self.store.get_ingestion(result["import_job_id"])
        self.assertEqual(job["submission_evidence"]["reason_code"], "run_stopped_before_dispatch")
        self.assertIsNone(self.store.reserve_ingestion(self.claim["task_id"], self.claim["claim_token"]))

    def test_engine_rejects_extra_target_fields_before_import_tools_execute(self):
        for key, value in (("video_id", "7999999000000000099"), ("binding_id", "other-user"),
                           ("instance_id", "other-device"), ("url", "https://example.invalid/"),
                           ("text", "伪造完成")):
            with self.subTest(key=key):
                model = FakeModel("save_current_video", {"knowledge_base_id": 7, key: value})
                audit = []
                decision = AgentEngine(model, FakeKnowledge(), max_rounds=1, ingestion=self.tools()).run(
                    self.binding, self.envelope, record=lambda kind, metadata: audit.append((kind, metadata)))
                self.assertIsNone(decision.proposal)
                self.assertEqual(decision.review_code, "model_round_budget_exceeded")
                self.assertTrue(any(kind == "tool_result" and item["status"] == "error" for kind, item in audit))
        self.assertEqual(self.backend.posts, [])
        self.assertEqual(self.store.list_ingestions(), [])

    def test_engine_rejects_freeform_status_reply_parameters(self):
        model = FakeModel("propose_import_status", {"text": "已经成功保存", "import_state": "succeeded"})
        decision = AgentEngine(model, FakeKnowledge(), max_rounds=1, ingestion=self.tools()).run(self.binding, self.envelope)
        self.assertIsNone(decision.proposal)
        self.assertEqual(self.backend.posts, [])


if __name__ == "__main__":
    unittest.main()
