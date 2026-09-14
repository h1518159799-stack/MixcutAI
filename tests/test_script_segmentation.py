import re
import unittest

from script_segmentation import DEFAULT_REWRITE_PROMPT, PROTECTED_TERMS, smart_segment_text


class SmartScriptSegmentationTests(unittest.TestCase):
    def test_default_rewrite_prompt_contains_required_rules(self):
        self.assertIn("保留原文核心事实", DEFAULT_REWRITE_PROMPT)
        self.assertIn("不使用标点符号", DEFAULT_REWRITE_PROMPT)
        self.assertIn("空格智能断句", DEFAULT_REWRITE_PROMPT)

    def test_user_example_preserves_words_and_content(self):
        source = "还要结合洗发水沐浴露护发素或护肤品的定位和功效共同设计"
        result = smart_segment_text(source)
        self.assertEqual(result.replace(" ", ""), source)
        self.assertTrue(all(len(part) <= 16 for part in result.split(" ")))
        for term in ("洗发水", "沐浴露", "护发素", "护肤品"):
            self.assertIn(term, result)
            self.assertNotIn(" ".join(term), result)

    def test_only_single_ascii_spaces_and_no_punctuation(self):
        result = smart_segment_text("源头工厂，支持OEM/ODM定制！！  免费打样。")
        self.assertNotRegex(result, r"[，。！？、：；/]")
        self.assertNotIn("  ", result)
        self.assertEqual(result.strip(), result)

    def test_long_text_is_segmented_before_overflow(self):
        source = "投入五十万元建立生产车间从原料采购研发到生产包装形成完整体系并且坚持严格质量管理"
        result = smart_segment_text(source)
        parts = result.split(" ")
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 16 for part in parts))
        self.assertGreaterEqual(sum(8 <= len(part) <= 14 for part in parts), len(parts) - 1)
        self.assertEqual(result.replace(" ", ""), source)

    def test_protected_terms_are_never_split(self):
        source = "产品定位产品功效使用效果使用方法核心成分都需要结合实际情况共同设计"
        result = smart_segment_text(source)
        for term in PROTECTED_TERMS[:9]:
            if term in source:
                self.assertIn(term, result)
        self.assertEqual(result.replace(" ", ""), source)

    def test_existing_semantic_cuts_are_preserved(self):
        self.assertEqual(smart_segment_text("你好，世界！ OEM/ODM  100% 😊"), "你好 世界 OEM ODM 100")


if __name__ == "__main__":
    unittest.main()
