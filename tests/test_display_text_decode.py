import unittest

from db import decode_legacy_display_text


class DisplayTextDecodeTests(unittest.TestCase):
    def test_decodes_literal_unicode_once(self):
        self.assertEqual(decode_legacy_display_text(r"AI \u670d\u52a1"), "AI 服务")

    def test_repairs_legacy_gbk_latin1_text(self):
        self.assertEqual(decode_legacy_display_text("Edge TTS ±¾µØ·þÎñ"), "Edge TTS 本地服务")

    def test_keeps_normal_multilingual_text(self):
        self.assertEqual(decode_legacy_display_text("Spanish | Elvira Female"), "Spanish | Elvira Female")


if __name__ == "__main__":
    unittest.main()
