"""验证搜索结果契约、保守整理和单次去重，不使用真实服务。"""

import copy
import importlib
import unittest


class SearchResultTests(unittest.TestCase):
    """用混合有效与无效结果检查处理边界。"""

    def normalize(self, response):
        """调用待实现的结果处理入口。"""
        module = importlib.import_module("seekra.search.retrieval")
        self.assertTrue(hasattr(module, "normalize_search_results"), "尚未实现结果校验")
        return module.normalize_search_results("查询", response)

    def test_normalization_and_exact_duplicates(self):
        """仅去掉同 URL 同正文，保留不同正文及 URL 变体。"""
        raw = {"results": [
            {"url": " https://example.org/a?x=1 ", "title": " 标题 ", "content": " 正文\r\n第二行 "},
            {"url": "https://example.org/a?x=1", "title": "另一标题", "content": "正文\n第二行"},
            {"url": "https://example.org/a?x=1", "content": "不同正文"},
            {"url": "https://www.example.org/a?x=1", "title": None, "content": "正文\n第二行"},
        ]}
        original = copy.deepcopy(raw)
        result = self.normalize(raw)
        self.assertEqual(raw, original)
        self.assertEqual(len(result["results"]), 3)
        self.assertEqual(result["results"][0], {"title": "标题", "url": "https://example.org/a?x=1", "content": "正文\n第二行"})
        self.assertEqual(result["results"][1]["title"], "https://example.org/a?x=1")
        self.assertEqual(result["processing"]["removed"], {"duplicate": 1})
        self.assertEqual(self.normalize(raw), result)

    def test_invalid_items_do_not_discard_good_items(self):
        """坏条目按首个失败原因计数，不影响有效条目。"""
        raw = {"results": [None,
            {"url": "javascript:alert(1)", "content": "正文"},
            {"url": "https://example.org", "content": " \r\n"},
            {"url": "https://example.org", "content": 3},
            {"url": "https://example.org", "title": [], "content": "正文"},
            {"url": "https://example.org", "title": "", "content": "有效"}]}
        result = self.normalize(raw)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["title"], "https://example.org")
        self.assertEqual(sum(result["processing"]["removed"].values()), 5)
        self.assertNotIn("error", result)

    def test_empty_filtered_and_malformed_are_distinct(self):
        """正常无命中、全被过滤和响应格式异常不能混为一谈。"""
        self.assertNotIn("error", self.normalize({"results": []}))
        self.assertIn("error", self.normalize({"results": [{}]}))
        for raw in (None, [], {}, {"results": None}, {"results": "bad"}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.normalize(raw)

    def test_unusable_urls_are_removed(self):
        """仅接收有主机的 HTTP(S) URL，不做网络探测或域名合并。"""
        for url in ("", "/relative", "https://", "https://bad host/a", "https://x:bad/a"):
            with self.subTest(url=url):
                self.assertIn("error", self.normalize({"results": [{"url": url, "content": "正文"}]}))
