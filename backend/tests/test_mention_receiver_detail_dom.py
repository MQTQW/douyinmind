"""Run the product's detail extractors against synthetic, offline DOM fixtures.

The installed Edge runs headlessly with a fresh non-persistent context per test.
Every request is aborted. No account, storage state, profile or live page is used.
"""
import html
import unittest

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from app.mention_receiver import browser as receiver_browser


SENDER = "https://www.douyin.com/user/sender-fixture"
RECEIVER = "https://www.douyin.com/user/receiver-fixture"
OTHER = "https://www.douyin.com/user/other-fixture"
VIDEO_ID = "7999999000000000011"
OTHER_VIDEO_ID = "7999999000000000022"
COMMAND_SUFFIX = " 帮我把这个视频加入“虚构测试库”，测试编号 DM-FIXTURE-01。"
WANTED = "@测试助手" + COMMAND_SUFFIX


def comment(*, sender=SENDER, receiver=RECEIVER):
    return ('<article data-e2e="comment-item">'
            '<a href="' + html.escape(sender, quote=True) + '">虚构发送者</a>'
            '<div class="command"><a href="' + html.escape(receiver, quote=True)
            + '">@测试助手</a>' + html.escape(COMMAND_SUFFIX) + '</div></article>')


def legacy_description(video_id=VIDEO_ID):
    return ('<section data-e2e="video-desc">虚构旧版视频标题 '
            '<a href="https://www.douyin.com/search/fixture?aweme_id='
            + video_id + '">#虚构话题</a></section>')


def detail_info(video_id=VIDEO_ID):
    return ('<section data-e2e="detail-video-info" data-e2e-aweme-id="' + video_id
            + '"><span>虚构新版视频标题</span></section>')


def player(video_id=VIDEO_ID):
    return ('<section data-e2e="player-container" class="fixture-player video_'
            + video_id + ' another-class">虚构播放区域</section>')


def active_feed(video_id=VIDEO_ID, *, token_id=None, info_id=None):
    token_id = token_id or video_id
    info_id = info_id or video_id
    return ('<div data-e2e="feed-active-video" data-e2e-vid="' + video_id
            + '" class="fixture-active video_' + token_id + '">'
            '<div data-e2e="video-info" data-e2e-aweme-id="' + info_id
            + '"><span>虚构弹窗信息</span>'
            '<div data-e2e="video-desc">虚构弹窗纯文案标题</div></div></div>')


def document(body):
    return ('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><style>
      body { margin: 0; font: 16px/1.5 sans-serif; }
      article, section { display: block; padding: 8px; min-height: 24px; }
      .command { min-height: 24px; }
      </style><body>''' + body + '</body></html>')


class ReceiverDetailDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            # Use the already-installed browser; never install or download one.
            cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)
        except PlaywrightError as exc:
            cls.playwright.stop()
            raise unittest.SkipTest(
                "已安装 Edge 无法以 headless 启动（" + type(exc).__name__ + "）；未下载组件") from None

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1100, "height": 1000},
                                                service_workers="block")
        self.requests = []

        def abort_request(route):
            self.requests.append(route.request.url)
            route.abort()

        self.context.route("**/*", abort_request)
        self.page = self.context.new_page()
        self.parameters = {"sender": SENDER, "wanted": WANTED, "receiver": RECEIVER}

    def tearDown(self):
        current_url = self.page.url
        self.context.close()
        self.assertEqual(current_url, "about:blank", "the fixture must not navigate to any website")
        self.assertEqual(self.requests, [], "the synthetic fixture must not attempt a network request")

    def extract(self, body):
        self.page.set_content(document(body))
        return self.page.evaluate(receiver_browser.DETAIL_JS, self.parameters)

    def assert_video(self, result, video_id=VIDEO_ID):
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["video_id"], video_id)
        self.assertEqual(result["source_url"], "https://www.douyin.com/video/" + video_id)
        self.assertEqual(result["command_text"], WANTED)
        self.assertEqual(result["sender_display_name"], "虚构发送者")

    def assert_rejected(self, body):
        result = self.extract(body)
        self.assertFalse(result.get("ok"), result)
        self.assertFalse(self.page.evaluate(receiver_browser.DETAIL_READY_JS, self.parameters))
        return result

    def test_legacy_video_description_with_topic_video_id(self):
        result = self.extract(comment() + legacy_description())
        self.assert_video(result)
        self.assertIn("虚构旧版视频标题", result["video_title"])

    def test_new_detail_info_without_any_topic_link(self):
        result = self.extract(comment() + detail_info())
        self.assert_video(result)
        self.assertIn("虚构新版视频标题", result["video_title"])
        self.assertEqual(self.page.locator('[data-e2e="video-desc"]').count(), 0)

    def test_player_container_video_class_is_a_fallback_without_description(self):
        result = self.extract(comment() + player())
        self.assert_video(result)
        self.assertEqual(result["video_title"], "")
        self.assertEqual(self.page.locator('[data-e2e="video-desc"], [data-e2e="detail-video-info"]').count(), 0)

    def test_active_feed_popup_with_video_info_and_plain_caption_has_no_topic_dependency(self):
        result = self.extract(comment() + active_feed())
        self.assert_video(result)
        self.assertEqual(result["video_title"], "虚构弹窗纯文案标题")
        self.assertEqual(self.page.locator('[data-e2e="video-desc"] a').count(), 0)
        self.assert_video(self.page.evaluate(receiver_browser.DETAIL_READY_JS, self.parameters))

    def test_active_feed_attribute_class_or_inner_info_conflicts_are_rejected(self):
        for source in (active_feed(token_id=OTHER_VIDEO_ID), active_feed(info_id=OTHER_VIDEO_ID)):
            with self.subTest(source=source):
                result = self.assert_rejected(comment() + source)
                self.assertTrue(result["diagnostics"]["id_conflict"])

    def test_conflicting_visible_video_ids_are_rejected(self):
        for conflicting_sources in (
                detail_info() + player(OTHER_VIDEO_ID),
                legacy_description() + detail_info(OTHER_VIDEO_ID)):
            with self.subTest(sources=conflicting_sources):
                self.assert_rejected(comment() + conflicting_sources)

    def test_comment_from_wrong_sender_is_rejected(self):
        self.assert_rejected(comment(sender=OTHER) + detail_info())

    def test_comment_mentions_wrong_receiver_is_rejected(self):
        self.assert_rejected(comment(receiver=OTHER) + detail_info())

    def test_two_same_sender_receiver_and_body_comments_are_ambiguous(self):
        self.assert_rejected(comment() + comment() + detail_info())

    def test_ready_waits_for_comment_and_video_information_arriving_in_batches(self):
        self.page.set_content(document('<main id="fixture"></main>'))
        self.assertFalse(self.page.evaluate(receiver_browser.DETAIL_READY_JS, self.parameters))
        self.page.evaluate("body => document.querySelector('#fixture').innerHTML = body", comment())
        self.assertFalse(self.page.evaluate(receiver_browser.DETAIL_READY_JS, self.parameters))
        self.page.evaluate("""body => {
          setTimeout(() => document.querySelector('#fixture').insertAdjacentHTML('beforeend', body), 80);
        }""", detail_info())
        snapshot = self.page.wait_for_function(receiver_browser.DETAIL_READY_JS, arg=self.parameters,
                                               timeout=3000).json_value()
        self.assert_video(snapshot)


if __name__ == "__main__":
    unittest.main()
