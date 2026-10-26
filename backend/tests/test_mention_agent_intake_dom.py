"""Agent intake on synthetic blank pages and pure public-data fixtures only.

No persistent browser, account, project database, model or live network is used.
"""
from datetime import datetime, timezone
import html
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from app.mention_agent.intake import make_envelope, should_receive_notice
from app.mention_receiver import browser as receiver_browser


SENDER = "https://www.douyin.com/user/bound-sender-fixture"
RECEIVER = "https://www.douyin.com/user/assistant-fixture"
OTHER = "https://www.douyin.com/user/stranger-fixture"
VIDEO = "7999999000000000011"
COMMENT = "7999999000000000099"
QUESTION = "我库里的机会成本是什么意思"
TEXT = "@测试助手 " + QUESTION


def binding(**overrides):
    return {
        "binding_id": "binding-fixture", "instance_id": "desktop-fixture",
        "sender_profile_url": SENDER, "receiver_profile_url": RECEIVER,
        "knowledge_base_id": 7, "public_reply_allowed": True, "active": True,
        **overrides,
    }


def notice(**overrides):
    return {
        "source_profile_url": SENDER, "exact_text": TEXT,
        "semantic_fingerprint": "/public-fixture-cover.jpg", **overrides,
    }


def verified_detail():
    evidence = {
        "sender_profile_url": SENDER, "receiver_profile_url": RECEIVER,
        "command_text": TEXT, "unique_count": 1, "video_id": VIDEO,
        "native_comment_id": None, "real_mention": True,
        "sender_verified": True, "video_verified": True,
    }
    return {
        "ok": True, **evidence, "evidence": evidence.copy(),
        "source_url": "https://www.douyin.com/video/" + VIDEO,
        "diagnostics": {"command_match_count": 1, "visible_video_ids": [VIDEO], "id_conflict": False},
    }


def comment(*, owner=SENDER, receiver=RECEIVER, fake_mention=False, attributes="", nested=""):
    mention = ("<span>@测试助手</span>" if fake_mention else
               '<a href="' + html.escape(receiver, quote=True) + '">@测试助手</a>')
    return (
        '<article data-e2e="comment-item" ' + attributes + '>'
        '<a href="' + html.escape(owner, quote=True) + '">虚构作者</a>'
        '<div class="body">' + mention + " " + html.escape(QUESTION) + "</div>"
        + nested + "</article>"
    )


def video(video_id=VIDEO):
    return '<section data-e2e="detail-video-info" data-e2e-aweme-id="' + video_id + '">虚构视频</section>'


def document(body):
    return (
        '<!doctype html><html><meta charset="utf-8"><style>'
        'body{margin:0;font:16px/1.5 sans-serif}article,section,.body{display:block;min-height:24px;padding:4px}'
        '</style><body>' + body + '</body></html>'
    )


class IntakeContractTests(unittest.TestCase):
    def test_bound_plain_questions_and_statements_need_no_import_words_or_question_mark(self):
        for text in (TEXT, "@测试助手 帮我解释复利", "@测试助手 能告诉我怎么加入学习群吗"):
            with self.subTest(text=text):
                self.assertTrue(should_receive_notice(notice(exact_text=text), binding()))
        self.assertTrue(should_receive_notice(notice(), SimpleNamespace(**binding())))
        self.assertTrue(should_receive_notice(notice(), binding(public_reply_allowed=False)))

    def test_stranger_inactive_empty_and_unbound_notices_are_excluded(self):
        for value, scoped in (
            (notice(source_profile_url=OTHER), binding()),
            (notice(), binding(active=False)),
            (notice(exact_text=" \t "), binding()),
            (notice(), binding(binding_id="")),
            (notice(), binding(knowledge_base_id=None)),
            (notice(), binding(sender_profile_url=RECEIVER)),
            (notice(source_profile_url="https://www.douyin.com/user/self"), binding()),
            (None, binding()),
        ):
            with self.subTest(notice=value, scoped=scoped):
                self.assertFalse(should_receive_notice(value, scoped))

    def test_plain_question_has_explicit_ui_quality_and_null_native_id(self):
        result = make_envelope(notice(), verified_detail(), binding())
        self.assertEqual(result["comment_text"], TEXT)
        self.assertEqual(result["video_id"], VIDEO)
        self.assertEqual(result["reference_quality"], "verified_ui")
        self.assertIsNone(result["native_comment_id"])
        self.assertEqual(result["source_mode"], "live_ui")
        self.assertEqual(result["evidence"]["unique_count"], 1)
        self.assertIs(result["evidence"]["real_mention"], True)

    def test_time_does_not_change_ids_and_provenance_cannot_collide_with_live_commands(self):
        times = [datetime(2026, 10, 21, hour, tzinfo=timezone.utc) for hour in (1, 2)]
        results = []
        for now in times:
            with patch("app.mention_agent.intake.datetime") as clock:
                clock.now.return_value = now
                results.append(make_envelope(notice(), verified_detail(), binding()))
        self.assertEqual(results[0]["command_id"], results[1]["command_id"])
        self.assertEqual(results[0]["reference_id"], results[1]["reference_id"])
        self.assertNotEqual(results[0]["evidence"]["observed_at_utc"], results[1]["evidence"]["observed_at_utc"])
        for source_mode in ("replay", "isolated"):
            isolated = make_envelope(notice(), verified_detail(), binding(), source_mode=source_mode)
            self.assertEqual(isolated["source_mode"], source_mode)
            self.assertNotEqual(isolated["command_id"], results[0]["command_id"])
            self.assertEqual(isolated["reference_id"], results[0]["reference_id"])
        with self.assertRaises(ValueError):
            make_envelope(notice(), verified_detail(), binding(), source_mode="sample_promoted_to_live")

    def test_no_legacy_preview_or_incomplete_detail_becomes_an_agent_envelope(self):
        legacy = {"video_id": VIDEO, "command_text": TEXT, "status": "received", "preview_only": True}
        for detail in (legacy, {**legacy, "ok": True}, {**verified_detail(), "evidence": None}):
            with self.subTest(detail=detail), self.assertRaises(ValueError):
                make_envelope(notice(), detail, binding())

    def test_explicit_evidence_flags_and_unique_integer_are_required(self):
        for field in ("real_mention", "sender_verified", "video_verified"):
            for location in ("detail", "evidence"):
                value = verified_detail()
                (value if location == "detail" else value["evidence"])[field] = False
                with self.subTest(field=field, location=location), self.assertRaises(ValueError):
                    make_envelope(notice(), value, binding())
        for count in (0, 2, "1", True):
            value = verified_detail()
            value["unique_count"] = value["evidence"]["unique_count"] = count
            with self.subTest(count=count), self.assertRaises(ValueError):
                make_envelope(notice(), value, binding())

    def test_source_body_video_and_binding_cannot_be_replaced(self):
        mutations = [
            lambda d: d.update(sender_profile_url=OTHER),
            lambda d: d["evidence"].update(receiver_profile_url=OTHER),
            lambda d: d["evidence"].update(command_text=TEXT + "变更"),
            lambda d: d.update(command_text=TEXT + "变更"),
            lambda d: d.update(video_id=""),
            lambda d: d.update(video_id=int(VIDEO)),
            lambda d: d["evidence"].update(video_id="7999999000000000022"),
            lambda d: d.update(source_url="https://www.douyin.com/video/7999999000000000022"),
            lambda d: d["diagnostics"].update(id_conflict=True),
        ]
        for mutate in mutations:
            value = verified_detail()
            mutate(value)
            with self.subTest(detail=value), self.assertRaises(ValueError):
                make_envelope(notice(), value, binding())
        with self.assertRaises(ValueError):
            make_envelope(notice(source_profile_url=OTHER), verified_detail(), binding())
        with self.assertRaises(ValueError):
            make_envelope(notice(semantic_fingerprint=""), verified_detail(), binding())


class AgentIntakeDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)
        except PlaywrightError as exc:
            cls.playwright.stop()
            raise unittest.SkipTest("已安装 Edge 不能启动：" + type(exc).__name__) from None

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1100, "height": 1000},
                                                service_workers="block")
        self.requests = []

        def abort(route):
            self.requests.append(route.request.url)
            route.abort()

        self.context.route("**/*", abort)
        self.page = self.context.new_page()

    def tearDown(self):
        url = self.page.url
        self.context.close()
        self.assertEqual(url, "about:blank")
        self.assertEqual(self.requests, [], "offline fixtures must not attempt any network request")

    def extract(self, body):
        self.page.set_content(document(body))
        return self.page.evaluate(receiver_browser.DETAIL_JS,
                                  {"sender": SENDER, "receiver": RECEIVER, "wanted": TEXT})

    def reject(self, body):
        result = self.extract(body)
        self.assertFalse(result.get("ok"), result)
        with self.assertRaises(ValueError):
            make_envelope(notice(), result, binding())
        return result

    def test_bound_ordinary_question_produces_public_evidence_without_import_keywords(self):
        detail = self.extract(comment() + video())
        self.assertTrue(detail["ok"], detail)
        self.assertEqual(detail["sender_profile_url"], SENDER)
        self.assertEqual(detail["receiver_profile_url"], RECEIVER)
        self.assertEqual(detail["evidence"]["command_text"], TEXT)
        self.assertEqual(make_envelope(notice(), detail, binding())["comment_text"], TEXT)

    def test_typed_at_text_wrong_receiver_and_wrong_sender_are_rejected(self):
        for source in (comment(fake_mention=True), comment(receiver=OTHER), comment(owner=OTHER)):
            with self.subTest(source=source):
                self.reject(source + video())

    def test_duplicate_same_body_comments_are_ambiguous(self):
        result = self.reject(comment() + comment() + video())
        self.assertEqual(result["diagnostics"]["command_match_count"], 2)

    def test_root_sender_cannot_borrow_another_persons_nested_mention(self):
        root = ('<article data-e2e="comment-item"><a href="' + SENDER
                + '">已绑定A</a><div>普通根评论</div>' + comment(owner=OTHER) + '</article>')
        result = self.reject(root + video())
        self.assertEqual(result["diagnostics"]["command_match_count"], 0)

    def test_nested_comment_with_its_own_correct_author_is_not_lost(self):
        root = ('<article data-e2e="comment-item"><a href="' + OTHER
                + '">其他根作者</a><div>根评论</div>' + comment() + '</article>')
        detail = self.extract(root + video())
        self.assertTrue(detail["ok"], detail)
        self.assertEqual(detail["unique_count"], 1)
        self.assertEqual(make_envelope(notice(), detail, binding())["sender_profile_url"], SENDER)

    def test_missing_or_conflicting_visible_video_never_produces_envelope(self):
        self.reject(comment())
        self.reject(comment() + video() + video("7999999000000000022"))

    def test_public_named_native_id_is_optional_but_arbitrary_data_id_is_not_used(self):
        detail = self.extract(comment(attributes='data-comment-id="' + COMMENT + '"') + video())
        envelope = make_envelope(notice(), detail, binding())
        self.assertEqual(envelope["native_comment_id"], COMMENT)
        self.assertEqual(envelope["reference_quality"], "native")
        detail = self.extract(comment(attributes='data-id="' + COMMENT + '"') + video())
        envelope = make_envelope(notice(), detail, binding())
        self.assertIsNone(envelope["native_comment_id"])
        self.assertEqual(envelope["reference_quality"], "verified_ui")
        self.reject(comment(attributes='data-comment-id="' + COMMENT
                    + '" data-e2e-comment-id="7999999000000000088"') + video())

    def test_synthetic_replay_evidence_keeps_nonlive_mode(self):
        detail = self.extract(comment() + video())
        result = make_envelope(notice(), detail, binding(), source_mode="replay")
        self.assertEqual(result["source_mode"], "replay")
        self.assertNotEqual(result["command_id"], make_envelope(notice(), detail, binding())["command_id"])


if __name__ == "__main__":
    unittest.main()
