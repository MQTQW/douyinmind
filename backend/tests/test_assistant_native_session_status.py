"""Production status projection and real lane/control classes, fake local inputs."""
import ast
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from app.assistant import native_lifecycle as candidate
from app.assistant import control as control_module
from app.assistant.control import AssistantControl
from app.assistant.native_lane import NativeLane, NativeLaneError
from app.assistant.native_session import NativeSessionVault
from tests.test_assistant_native_lifecycle import FakeAdapter, FakeContext, FakeNetwork, FakeProtector, UID, PROFILE
from app.assistant.queue import QueueError, encoded
from app.mention_receiver.browser import ReceiverInterventionRequired

HERE = Path(control_module.__file__).resolve().parent
RUN_ID = "a" * 32


def baseline():
    return {"state": "not_started", "reason_code": None, "generation": 0, "refresh_count": 0,
            "source": "none", "seal_state": "not_attempted", "transient_failures": 0,
            "last_transient_code": None, "retry_after_seconds": 0}


class NeverInspect:
    def __repr__(self): raise AssertionError("unknown private field inspected")
    __str__ = __repr__


class ProjectionTests(unittest.TestCase):
    def test_only_nine_public_fields_copied_without_unknown_value_access(self):
        value = baseline()
        for key in ("CookieHeader", "user_agent", "expires_at", "raw_response", "url", "secret"):
            value[key] = NeverInspect()
        public = candidate.project_native_session_status(value)
        self.assertEqual(public, baseline())
        self.assertEqual(set(public), set(baseline()))
        self.assertIsNot(public, value)
        encoded(public)

    def test_missing_or_non_dict_metadata_is_not_synthesized(self):
        for value in (None, {}, [], {"state": "ready"}):
            with self.subTest(kind=type(value).__name__), self.assertRaisesRegex(candidate.SessionLifecycleError, "invalid_native_session_status"):
                candidate.project_native_session_status(value)

    def test_known_fields_cannot_smuggle_raw_strings_or_objects(self):
        for key in ("state", "source", "seal_state", "reason_code", "last_transient_code"):
            value = baseline(); value[key] = NeverInspect()
            with self.subTest(key=key), self.assertRaisesRegex(candidate.SessionLifecycleError, "invalid_native_session_status"):
                candidate.project_native_session_status(value)

    def test_numeric_fields_are_strict_and_bounded(self):
        for key, value in (("generation", True), ("generation", -1), ("refresh_count", 10 ** 12),
                           ("transient_failures", 4), ("retry_after_seconds", 91), ("retry_after_seconds", 1.0)):
            status = baseline(); status[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(candidate.SessionLifecycleError, "invalid_native_session_status"):
                candidate.project_native_session_status(status)

    def test_fixed_pause_and_cooldown_reasons_match_their_states(self):
        value = baseline(); value.update(state="paused", reason_code="verification_required")
        self.assertEqual(candidate.project_native_session_status(value)["reason_code"], "verification_required")
        value = baseline(); value.update(state="cooldown", transient_failures=1, last_transient_code="network_error", retry_after_seconds=45)
        self.assertEqual(candidate.project_native_session_status(value)["state"], "cooldown")
        value["reason_code"] = "fixture_unrecognized_string"
        with self.assertRaisesRegex(candidate.SessionLifecycleError, "invalid_native_session_status"):
            candidate.project_native_session_status(value)


class MemoryQueue:
    def __init__(self, path):
        self.path = path
        self.value = {"state": "running", "desired_state": "running", "run_id": RUN_ID, "pid": 12345}
        self.recoveries = []
    def get_control(self): return deepcopy(self.value)
    def update_control(self, transform):
        result = transform(deepcopy(self.value))
        # Use the actual queue JSON/privacy validator, without any DB operation.
        self.value = json.loads(encoded(result))
        return deepcopy(self.value)
    def recover_interrupted(self, **options): self.recoveries.append(dict(options))


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.queue = MemoryQueue(self.root / "queue.sqlite3")
        self.module = control_module
        self.control = self.module.AssistantControl(self.queue, self.root,
            process_probe=lambda pid: False, command_factory=lambda: [sys.executable, "fixture-no-process"],
            popen_factory=lambda *args, **kwargs: types.SimpleNamespace(pid=22222))

    def test_heartbeat_optional_status_copies_and_discards_private_extras(self):
        value = baseline(); value["cookie"] = NeverInspect()
        result = self.control.heartbeat(RUN_ID, native_session_status=value)
        self.assertEqual(result["native_session_status"], baseline())
        self.assertIn("native_session_status_observed_at", result)
        value["generation"] = 999
        self.assertEqual(self.queue.value["native_session_status"]["generation"], 0)
        self.assertFalse((self.root / "queue.sqlite3").exists())

    def test_regular_heartbeat_does_not_retimestamp_old_observation(self):
        with mock.patch.object(self.module, "utc", side_effect=["fixture-beat-1", "fixture-observed-1", "fixture-beat-2"]):
            self.control.heartbeat(RUN_ID, native_session_status=baseline())
            result = self.control.heartbeat(RUN_ID)
        self.assertEqual(result["heartbeat_at"], "fixture-beat-2")
        self.assertEqual(result["native_session_status_observed_at"], "fixture-observed-1")

    def test_legacy_no_status_does_not_invent_a_native_source(self):
        result = self.control.heartbeat(RUN_ID)
        self.assertNotIn("native_session_status", result)
        self.assertNotIn("native_session_status_observed_at", result)

    def test_bad_status_or_wrong_run_leaves_control_unchanged(self):
        before = deepcopy(self.queue.value)
        invalid = baseline(); invalid["source"] = "invented-source"
        with self.assertRaisesRegex(QueueError, "invalid_native_session_status"):
            self.control.heartbeat(RUN_ID, native_session_status=invalid)
        self.assertEqual(self.queue.value, before)
        with self.assertRaisesRegex(QueueError, "runtime_owner_changed"):
            self.control.heartbeat("b" * 32, native_session_status=baseline())
        self.assertEqual(self.queue.value, before)

    def test_finish_keeps_last_observation_without_claiming_new_session_state(self):
        self.control.heartbeat(RUN_ID, native_session_status=baseline())
        old = deepcopy(self.queue.value["native_session_status"])
        observed = self.queue.value["native_session_status_observed_at"]
        result = self.control.finish(RUN_ID)
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(result["native_session_status"], old)
        self.assertEqual(result["native_session_status_observed_at"], observed)

    def test_new_normal_start_clears_previous_run_session_evidence(self):
        self.control.heartbeat(RUN_ID, native_session_status=baseline())
        result = self.control.start()  # Fake process and fake recovery only.
        self.assertNotEqual(result["run_id"], RUN_ID)
        self.assertNotIn("native_session_status", result)
        self.assertNotIn("native_session_status_observed_at", result)
        self.assertEqual(len(self.queue.recoveries), 1)


class ProductionLaneFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root, self.now = Path(self.temp.name).resolve(), [1000.0]
        self.vault = NativeSessionVault(UID, private_root=self.root / "private", protector=FakeProtector(), clock=lambda: self.now[0])
        self.context, self.network = FakeContext(self.now), FakeNetwork()
        self.adapter, self.empty = FakeAdapter(self.context), True
        capture = candidate.SameContextCapture(self.adapter, PROFILE, UID,
            draft_is_empty=lambda: self.empty, clock=lambda: self.now[0])
        self.lifecycle = candidate.NativeSessionLifecycle(expected_uid=UID, owned_vault=self.vault,
            context_capture=capture, network=self.network, clock=lambda: self.now[0])
        class ReceiverFixture:
            code, calls = None, 0
            def poll_once(inner):
                inner.calls += 1
                if inner.code:
                    error = RuntimeError(inner.code)
                    error.code = inner.code
                    raise error
                return []
        class StoreFixture:
            def __init__(inner): inner.pause_codes = []
            def safe_status(inner): return {"armed": True}
            def pause(inner, code): inner.pause_codes.append(code)
        self.receiver, self.store = ReceiverFixture(), StoreFixture()
        self.browser_lane = types.SimpleNamespace(adapter=self.adapter, registry=object(), queue=object(),
            conversation=object(), should_stop=lambda: False, last_baseline_created=False,
            last_detail_failure_count=0)
        self.lane = NativeLane(self.browser_lane, self.receiver, self.store, session_lifecycle=self.lifecycle)
    def doCleanups(self): self.temp.cleanup()


class LaneAndMainTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ProductionLaneFixture()
        self.addCleanup(self.fixture.doCleanups)

    def test_successful_lane_observation_is_actual_lifecycle_projection(self):
        result = self.fixture.lane.poll_native()
        status = result["native_session_status"]
        self.assertEqual(status, candidate.project_native_session_status(self.fixture.lifecycle.safe_status()))
        self.assertEqual((status["source"], status["seal_state"]), ("same_context", "sealed"))
        self.assertEqual(set(status), set(baseline()))

    def test_no_lifecycle_lane_keeps_old_result_shape(self):
        self.fixture.lane.session_lifecycle = None
        result = self.fixture.lane.poll_native()
        self.assertNotIn("native_session_status", result)
        self.assertEqual(self.fixture.context.cookie_calls, 0)

    def test_failure_carries_this_poll_safe_cooldown_or_pause(self):
        self.fixture.receiver.code = "network_error"
        with self.assertRaises(NativeLaneError) as error:
            self.fixture.lane.poll_native()
        self.assertEqual(error.exception.native_session_status["state"], "cooldown")
        self.assertEqual(error.exception.native_session_status["last_transient_code"], "network_error")
        self.assertEqual(set(error.exception.native_session_status), set(baseline()))

    def test_draft_failure_has_actual_fixed_reason_and_no_invented_owned_source(self):
        self.fixture.empty = False
        with self.assertRaises(ReceiverInterventionRequired) as error:
            self.fixture.lane.poll_native()
        value = error.exception.native_session_status
        self.assertEqual((value["state"], value["reason_code"], value["source"]),
                         ("paused", "session_existing_draft", "none"))
        self.assertEqual(self.fixture.context.cookie_calls, 0)

    def test_main_routes_only_current_observation_or_error_to_existing_heartbeat(self):
        source = (HERE / "__main__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "heartbeat"]
        status_calls = [call for call in calls if any(item.arg == "native_session_status" for item in call.keywords)]
        # Heartbeats may grow independently of this contract. Every forwarded
        # value must be a current projection, never the raw error or cached lane.
        self.assertTrue(status_calls)
        self.assertEqual({ast.unparse(item.value) for call in status_calls for item in call.keywords
            if item.arg=='native_session_status'}, {'status','session_status'})
        owner=next(node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name=='run_registered')
        parent={child:node for node in ast.walk(owner) for child in ast.iter_child_nodes(node)}
        def error_branch(node):
            while node in parent:
                node=parent[node]
                if isinstance(node,ast.ExceptHandler):return ast.unparse(node.type)
            return None
        # Native intervention, expected page failure, and unexpected failure all
        # forward only the safe status attached to that particular observation.
        branches={error_branch(call) for call in status_calls}
        self.assertTrue({'ReceiverInterventionRequired',
            '(ReceiverPageError, ReceiverDetailError, BrowserError)','Exception'} <= branches)
        assignments=[node for node in ast.walk(owner) if isinstance(node,ast.Assign)
            and any(isinstance(target,ast.Name) and target.id in {'status','session_status'} for target in node.targets)]
        self.assertEqual({ast.unparse(node.value) for node in assignments},{
            'verify_registered_receiver(adapter, lane)',
            "observation.get('native_session_status') if isinstance(observation, dict) else None",
            "getattr(error, 'native_session_status', None)"})
        self.assertTrue(any(not call.keywords for call in calls))
        self.assertNotIn("lane.last_native_session_status", source)
        self.assertIn("observation.get('native_session_status')", source)
        self.assertIn("getattr(error, 'native_session_status', None)", source)


if __name__ == "__main__": unittest.main()
