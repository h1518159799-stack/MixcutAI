import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_publish_service
import ai_services
import db


class PublishAIUnifiedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "config").mkdir(parents=True)
        self.patchers = [
            patch.object(db, "DB_PATH", root / "data" / "mixcut.db"),
            patch.object(db, "DATA_DIR", root / "data"),
            patch.object(db, "CONFIG_DIR", root / "config"),
            patch.object(ai_publish_service, "DATA_DIR", root / "data"),
        ]
        for patcher in self.patchers:
            patcher.start()
        db._init_db_impl()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def _insert_config(self, api_key="stored-value", base_url="https://gateway.example/v1"):
        stamp = db.now()
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO ai_service_configs
                   (name,provider,base_url,api_key,model,timeout_seconds,is_enabled,is_default,created_at,updated_at)
                   VALUES(?,?,?,?,?,45,1,1,?,?)""",
                ("统一AI", "openai_compatible", base_url, api_key, "model-live", stamp, stamp),
            )

    def test_active_config_is_decrypted_only_on_backend(self):
        self._insert_config("dpapi:ciphertext")
        with patch.object(ai_services, "runtime_service_config", side_effect=lambda value: {**value, "api_key": "real-secret"}):
            config = ai_services.get_active_text_ai_config()
        self.assertEqual(config["api_key"], "real-secret")
        self.assertEqual(config["base_url"], "https://gateway.example/v1")
        self.assertEqual(config["model"], "model-live")

    def test_empty_key_reports_not_configured(self):
        self._insert_config("")
        with self.assertRaises(ai_services.ServiceError) as raised:
            ai_services.get_active_text_ai_config()
        self.assertEqual(str(raised.exception), "AI_TEXT_SERVICE_NOT_CONFIGURED")

    def test_provider_errors_are_converted_to_safe_chinese(self):
        cases = [
            ("HTTP 401: invalid_api_key", "AI_INVALID_KEY", "AI密钥无效"),
            ("<urlopen error connection refused>", "AI_ENDPOINT_UNREACHABLE", "接口地址无法访问"),
            ("request timed out", "AI_TIMEOUT", "请求超时"),
            ("HTTP 429: rate limit", "AI_RATE_LIMITED", "请求频率过高"),
        ]
        for raw, code, message in cases:
            error = ai_publish_service._friendly_service_error(ai_services.ServiceError(raw))
            self.assertEqual(error.code, code)
            self.assertIn(message, error.user_message)
            self.assertNotIn(raw, error.user_message)

    def test_hashtags_are_normalized_and_deduplicated(self):
        self.assertEqual(
            ai_publish_service.clean_hashtags("##洗发水 #工厂 #洗发水  OEM，#OEM"),
            ["洗发水", "工厂", "OEM"],
        )

    def test_frontend_does_not_send_api_key_and_uses_confirmation(self):
        source = (Path(__file__).parents[1] / "ui-commercial" / "src" / "PublishDraftCard.jsx").read_text(encoding="utf-8")
        request_block = source[source.index('request("/api/publish/ai-content"'):source.index('request("/api/publish/ai-content"') + 450]
        self.assertNotIn("api_key", request_block.lower())
        self.assertIn("publish-ai-compare", source)
        self.assertIn("应用建议", source)
        self.assertNotIn("AI优化全部", source)
        self.assertIn("if(aiBusy)return", source)


if __name__ == "__main__":
    unittest.main()
