"""Cold-render notification regressions on synthetic, offline browser DOM.

The product JavaScript is executed unchanged. Every test has an empty,
non-persistent Edge context; data-URL fixture images require no network.
"""
import html
import unittest
from urllib.parse import quote, urlsplit

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from app.mention_receiver import browser as receiver_browser


def image_source(label):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32">'
           '<title>' + label + '</title><rect width="32" height="32" fill="green"/></svg>')
    return "data:image/svg+xml," + quote(svg, safe="")


def cover_image(index):
    return ('<img class="cover" alt="虚构封面" width="32" height="32" src="'
            + html.escape(image_source("cover-" + str(index)), quote=True) + '">')


def notification(index, *, cover=True, profile_image=False):
    sender = "https://www.douyin.com/user/sender-fixture-" + str(index)
    avatar = ('<img alt="虚构头像" width="32" height="32" src="'
              + html.escape(image_source("avatar-" + str(index)), quote=True) + '">') if profile_image else ""
    return ('<div class="notice-row" id="notice-' + str(index) + '"><a href="' + sender + '">'
            + avatar + '虚构发送者' + str(index) + '</a><span>在评论中提到了你</span>'
            '<pre>@虚构助手 帮我把这个视频加入“虚构知识库”，测试编号 DM-FIXTURE-'
            + str(index) + '。</pre>' + (cover_image(index) if cover else "") + '</div>')


def document(rows, *, end=False):
    return ('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><style>
      body { margin: 0; font: 14px/1.4 sans-serif; }
      .notice-row { display: flex; align-items: center; gap: 10px; min-height: 72px; }
      .notice-row > a { min-width: 110px; }
      pre { margin: 0; white-space: pre-wrap; }
      img { display: inline-block; width: 32px; height: 32px; }
      </style><body><main id="notifications">''' + rows
            + ('<div>暂时没有更多了</div>' if end else '') + '</main></body></html>')


class ReceiverNoticeDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
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

    def tearDown(self):
        current_url = self.page.url
        self.context.close()
        self.assertEqual(current_url, "about:blank")
        self.assertEqual(self.requests, [], "fixture images and links must not request a real website")

    def scan(self):
        return self.page.evaluate(receiver_browser.NOTICE_JS)

    def ready(self):
        return self.page.evaluate(receiver_browser.NOTICE_READY_JS)

    def test_cover_arriving_after_text_is_awaited_and_returned_in_ready_snapshot(self):
        self.page.set_content(document(notification(1, cover=False)))
        early = self.scan()
        self.assertEqual(len(early["items"]), 1)
        self.assertEqual(early["items"][0]["semantic_fingerprint"], "")
        self.assertFalse(self.ready())
        self.page.evaluate("""markup => {
          setTimeout(() => document.querySelector('#notice-1').insertAdjacentHTML('beforeend', markup), 80);
        }""", cover_image(1))
        snapshot = self.page.wait_for_function(receiver_browser.NOTICE_READY_JS, timeout=3000).json_value()
        self.assertEqual(len(snapshot["items"]), 1)
        self.assertEqual(snapshot["items"][0]["exact_text"], early["items"][0]["exact_text"])
        self.assertEqual(snapshot["items"][0]["source_profile_url"], early["items"][0]["source_profile_url"])
        self.assertEqual(snapshot["items"][0]["semantic_fingerprint"], urlsplit(image_source("cover-1")).path)

    def test_known_end_marker_does_not_make_a_coverless_candidate_ready(self):
        self.page.set_content(document(notification(1, cover=False), end=True))
        value = self.scan()
        self.assertTrue(value["known_end_or_empty"])
        self.assertEqual(len(value["items"]), 1)
        self.assertEqual(value["items"][0]["semantic_fingerprint"], "")
        self.assertFalse(self.ready())

    def test_profile_link_image_is_not_a_video_cover(self):
        self.page.set_content(document(notification(1, cover=False, profile_image=True), end=True))
        self.assertEqual(self.page.locator('a[href*="/user/"] img').count(), 1)
        value = self.scan()
        self.assertEqual(len(value["items"]), 1)
        self.assertEqual(value["items"][0]["semantic_fingerprint"], "")
        self.assertFalse(self.ready())

    def test_three_complete_visible_mentions_return_one_complete_snapshot(self):
        self.page.set_content(document("".join(notification(index) for index in (1, 2, 3)), end=True))
        snapshot = self.ready()
        self.assertIsInstance(snapshot, dict)
        self.assertEqual(len(snapshot["items"]), 3)
        self.assertEqual(snapshot["visible_mention_count"], 3)
        self.assertEqual(snapshot["potential_mention_count"], 3)
        self.assertTrue(snapshot["known_end_or_empty"])
        self.assertEqual([item["semantic_fingerprint"] for item in snapshot["items"]],
                         [urlsplit(image_source("cover-" + str(index))).path for index in (1, 2, 3)])

    def test_one_coverless_row_among_complete_mentions_keeps_entire_snapshot_unready(self):
        rows = notification(1) + notification(2, cover=False) + notification(3)
        self.page.set_content(document(rows, end=True))
        value = self.scan()
        self.assertEqual(len(value["items"]), 3)
        self.assertEqual([bool(item["semantic_fingerprint"]) for item in value["items"]], [True, False, True])
        self.assertFalse(self.ready())


if __name__ == "__main__":
    unittest.main()
