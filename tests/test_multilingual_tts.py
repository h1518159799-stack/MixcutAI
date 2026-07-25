import unittest

from language_service import detect_text_language, voice_matches_language
from subtitle_service import split_text


class MultilingualTtsTests(unittest.TestCase):
    def test_detects_supported_languages(self):
        self.assertEqual(detect_text_language("我们是一家专业工厂"), "zh-CN")
        self.assertEqual(detect_text_language("We are a professional factory"), "en-US")
        self.assertEqual(detect_text_language("Somos una fábrica de productos"), "es-ES")
        self.assertEqual(detect_text_language("Chúng tôi là nhà máy sản phẩm"), "vi-VN")

    def test_voice_locale_matching(self):
        self.assertTrue(voice_matches_language("en-US-AriaNeural", "en-US"))
        self.assertTrue(voice_matches_language("vi-VN-HoaiMyNeural", "vi-VN"))
        self.assertFalse(voice_matches_language("zh-CN-XiaoxiaoNeural", "en-US"))

    def test_latin_subtitles_group_words_instead_of_splitting_every_word(self):
        text = "We are a professional factory providing custom product development services"
        chunks = split_text(text)
        self.assertGreater(len(chunks), 1)
        self.assertLess(len(chunks), len(text.split()))
        self.assertEqual(" ".join(chunks), text)


if __name__ == "__main__":
    unittest.main()
