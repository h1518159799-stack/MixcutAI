from __future__ import annotations

import io
import socket
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import ai_services
import commercial_api


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def http_error(url: str, code: int, payload: bytes = b"{}"):
    return urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(payload))


class AIServiceConnectionTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "id": 2,
            "name": "API",
            "provider": "openai_compatible",
            "base_url": "http://127.0.0.1:53417/v1/",
            "api_key": "secret-value",
            "model": "gpt-5.6-luna",
            "timeout_seconds": 60,
            "is_enabled": 1,
        }

    @patch("ai_services.socket.create_connection")
    @patch("ai_services.urllib.request.urlopen")
    def test_models_success_normalizes_url_and_reports_latency(self, urlopen, create_connection):
        create_connection.return_value.__enter__.return_value = object()
        urlopen.side_effect = [
            FakeResponse(b'{"data":[{"id":"gpt-5.6-luna"}]}'),
            FakeResponse(b'{"choices":[{"message":{"content":"OK"}}]}'),
        ]
        result = ai_services.test_ai_service(self.config)
        request = urlopen.call_args_list[0].args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:53417/v1/models")
        self.assertEqual(result["model_validation"], "通过")
        self.assertEqual(result["test_method"], "models+chat")
        self.assertGreaterEqual(result["latency_ms"], 1)
        self.assertNotIn("secret-value", str(result))

    @patch("ai_services.socket.create_connection")
    @patch("ai_services.urllib.request.urlopen")
    def test_models_unsupported_falls_back_to_small_chat(self, urlopen, create_connection):
        create_connection.return_value.__enter__.return_value = object()
        urlopen.side_effect = [
            http_error("http://127.0.0.1:53417/v1/models", 404),
            FakeResponse(b'{"choices":[{"message":{"content":"OK"}}]}'),
        ]
        result = ai_services.test_ai_service(self.config)
        second_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(second_request.full_url, "http://127.0.0.1:53417/v1/chat/completions")
        self.assertIn(b'"max_tokens": 4', second_request.data)
        self.assertEqual(result["test_method"], "chat")

    @patch("ai_services.socket.create_connection")
    @patch("ai_services.urllib.request.urlopen")
    def test_auth_error_is_normalized(self, urlopen, create_connection):
        create_connection.return_value.__enter__.return_value = object()
        urlopen.side_effect = http_error("http://host/v1/models", 401, b'{"error":"invalid_api_key"}')
        with self.assertRaises(ai_services.AIRequestError) as raised:
            ai_services.test_ai_service(self.config)
        self.assertEqual(raised.exception.code, "AI_AUTH_FAILED")
        self.assertNotIn("secret-value", str(raised.exception))

    @patch("ai_services.socket.create_connection")
    def test_missing_local_port_fails_fast(self, create_connection):
        create_connection.side_effect = ConnectionRefusedError("refused")
        with self.assertRaises(ai_services.AIRequestError) as raised:
            ai_services.test_ai_service(self.config)
        self.assertEqual(raised.exception.code, "AI_CONNECTION_FAILED")

    @patch("ai_services.socket.create_connection")
    @patch("ai_services.urllib.request.urlopen")
    def test_model_not_found_is_distinct(self, urlopen, create_connection):
        create_connection.return_value.__enter__.return_value = object()
        urlopen.return_value = FakeResponse(b'{"data":[{"id":"another-model"}]}')
        with self.assertRaises(ai_services.AIRequestError) as raised:
            ai_services.test_ai_service(self.config)
        self.assertEqual(raised.exception.code, "AI_MODEL_NOT_FOUND")
        self.assertIn("gpt-5.6-luna", str(raised.exception))

    @patch("ai_services.socket.create_connection")
    @patch("ai_services.urllib.request.urlopen")
    def test_timeout_is_normalized(self, urlopen, create_connection):
        create_connection.return_value.__enter__.return_value = object()
        urlopen.side_effect = socket.timeout("timed out")
        with self.assertRaises(ai_services.AIRequestError) as raised:
            ai_services.test_ai_service(self.config)
        self.assertEqual(raised.exception.code, "AI_TIMEOUT")

    @patch("ai_services.socket.create_connection")
    @patch("ai_services.urllib.request.urlopen")
    def test_http_500_is_provider_error(self, urlopen, create_connection):
        create_connection.return_value.__enter__.return_value = object()
        urlopen.side_effect = http_error("http://host/v1/models", 500, b'{"error":"internal"}')
        with self.assertRaises(ai_services.AIRequestError) as raised:
            ai_services.test_ai_service(self.config)
        self.assertEqual(raised.exception.code, "AI_PROVIDER_ERROR")
        self.assertEqual(raised.exception.status_code, 500)

    @patch("commercial_api.runtime_config")
    def test_current_form_uses_saved_key_without_persisting(self, runtime_config):
        runtime_config.return_value = dict(self.config)
        resolved = commercial_api.resolve_ai_test_config({"config": {
            "id": 2,
            "base_url": "https://changed.example/v1/",
            "model": "new-model",
            "api_key": "",
        }})
        self.assertEqual(resolved["api_key"], "secret-value")
        self.assertEqual(resolved["base_url"], "https://changed.example/v1/")
        self.assertEqual(resolved["model"], "new-model")
        runtime_config.assert_called_once_with("ai_service_configs", 2)

    @patch("commercial_api.runtime_config")
    def test_new_key_overrides_saved_key_and_mask_never_does(self, runtime_config):
        runtime_config.return_value = dict(self.config)
        new_value = commercial_api.resolve_ai_test_config({"config": {"id": 2, "api_key": "new-secret"}})
        masked = commercial_api.resolve_ai_test_config({"config": {"id": 2, "api_key": "••••••••••••••••"}})
        self.assertEqual(new_value["api_key"], "new-secret")
        self.assertEqual(masked["api_key"], "secret-value")

    def test_frontend_has_loading_finally_and_current_form_endpoint(self):
        source = (Path(__file__).parents[1] / "ui-commercial" / "src" / "AiServicesPanel.jsx").read_text(encoding="utf-8")
        self.assertIn('setTesting(true)', source)
        self.assertIn('finally{setTesting(false)}', source)
        self.assertIn('/api/ai-services/test-config', source)
        self.assertIn('body:{config:form}', source)
        self.assertIn('testing?"测试中...":"测试连接"', source)

    def test_bailian_key_entry_uses_backend_route(self):
        source = (Path(__file__).parents[1] / "ui-commercial" / "src" / "AiServicesPanel.jsx").read_text(encoding="utf-8")
        self.assertIn("open-bailian-key", source)
        self.assertIn("获取KEY", source)
        self.assertIn("前往阿里百炼获取 API Key", source)


if __name__ == "__main__":
    unittest.main()
