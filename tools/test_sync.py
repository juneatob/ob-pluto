#!/usr/bin/env python3

from __future__ import annotations

import unittest

from sync_wechat import WeChatPageParser, normalize_wechat_url


class SyncTests(unittest.TestCase):
    def test_normalize_slug_link(self) -> None:
        self.assertEqual(
            normalize_wechat_url("https://mp.weixin.qq.com/s/abc_DEF?scene=1#wechat_redirect"),
            "https://mp.weixin.qq.com/s/abc_DEF",
        )

    def test_parse_normal_article(self) -> None:
        parser = WeChatPageParser()
        parser.feed(
            '<meta property="og:title" content="测试标题">'
            '<span id="js_name">百味鸡OB Pluto</span>'
            '<div id="js_content"><p>第一段</p><p><strong>第二段</strong></p>'
            '<img data-src="https://mmbiz.qpic.cn/a/640?wx_fmt=png"></div>'
        )
        self.assertEqual(parser.meta["og:title"], "测试标题")
        self.assertEqual(parser.meta["js_name"], "百味鸡OB Pluto")
        self.assertIn("第一段", parser.markdown())
        self.assertIn("**第二段**", parser.markdown())
        self.assertEqual(len(parser.images), 1)


if __name__ == "__main__":
    unittest.main()
