import io
import urllib.error
import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import ai_services
import commercial_api


CONFIG = {
    "provider": "openai_compatible",
    "base_url": "http://127.0.0.1:9/v1",
    "api_key": "secret-not-for-logs",
    "model": "test-model",
    "timeout_seconds": 60,
    "is_enabled": 1,
}
MESSAGES = [{"role": "user", "content": "rewrite"}]
SUCCESS = {"choices": [{"message": {"content": "rewritten"}}]}


class AIRewriteRetryTests(unittest.TestCase):
    def run_chat(self, side_effect):
        events = []
        with patch.object(ai_services, "_json_request", side_effect=side_effect) as request, \
             patch.object(ai_services.time, "sleep") as sleep:
            result = ai_services.chat_completion(CONFIG, MESSAGES, attempts=3, retry_callback=events.append)
        return result, events, request, sleep

    def test_normal_success(self):
        result, events, request, sleep = self.run_chat([SUCCESS])
        self.assertEqual(result, "rewritten")
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual([item["phase"] for item in events], ["attempt", "success"])

    def test_first_timeout_second_success(self):
        timeout = ai_services.AIRequestError("AI_TIMEOUT", "timeout", retryable=True)
        result, events, request, sleep = self.run_chat([timeout, SUCCESS])
        self.assertEqual(result, "rewritten")
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(2.0)
        self.assertEqual([item["attempt"] for item in events if item["phase"] == "attempt"], [1, 2])

    def test_two_timeouts_third_success(self):
        timeout = ai_services.AIRequestError("AI_TIMEOUT", "timeout", retryable=True)
        result, events, request, sleep = self.run_chat([timeout, timeout, SUCCESS])
        self.assertEqual(result, "rewritten")
        self.assertEqual(request.call_count, 3)
        self.assertEqual([item.args[0] for item in sleep.call_args_list], [2.0, 5.0])
        self.assertEqual([item["attempt"] for item in events if item["phase"] == "attempt"], [1, 2, 3])

    def test_three_timeouts_raise_normalized_error(self):
        timeout = ai_services.AIRequestError("AI_TIMEOUT", "timeout", retryable=True)
        events = []
        with patch.object(ai_services, "_json_request", side_effect=[timeout, timeout, timeout]) as request, \
             patch.object(ai_services.time, "sleep"):
            with self.assertRaises(ai_services.AIRequestError) as raised:
                ai_services.chat_completion(CONFIG, MESSAGES, attempts=3, retry_callback=events.append)
        self.assertEqual(raised.exception.code, "AI_TIMEOUT")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(events[-1]["phase"], "failed")

    def test_auth_error_is_not_retried(self):
        auth = ai_services.AIRequestError("AI_AUTH_FAILED", "bad key", retryable=False, status_code=401)
        with patch.object(ai_services, "_json_request", side_effect=auth) as request, \
             patch.object(ai_services.time, "sleep") as sleep:
            with self.assertRaises(ai_services.AIRequestError) as raised:
                ai_services.chat_completion(CONFIG, MESSAGES, attempts=3)
        self.assertEqual(raised.exception.code, "AI_AUTH_FAILED")
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_rate_limit_and_gateway_errors_are_retryable(self):
        for code in (429, 502, 503, 504):
            error = urllib.error.HTTPError("https://example.invalid", code, "error", {}, io.BytesIO(b"{}"))
            with patch.object(ai_services.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(ai_services.AIRequestError) as raised:
                    ai_services._json_request("https://example.invalid", {}, "key", 1)
            self.assertTrue(raised.exception.retryable)

    def test_raw_timeout_is_normalized(self):
        with patch.object(ai_services.urllib.request, "urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(ai_services.AIRequestError) as raised:
                ai_services._json_request("https://example.invalid", {}, "key", 1)
        self.assertEqual(raised.exception.code, "AI_TIMEOUT")
        self.assertTrue(raised.exception.retryable)

    def test_retry_events_never_contain_api_key(self):
        timeout = ai_services.AIRequestError("AI_TIMEOUT", "timeout", retryable=True)
        _, events, _, _ = self.run_chat([timeout, SUCCESS])
        self.assertNotIn(CONFIG["api_key"], repr(events))


class BatchRewriteFallbackTests(unittest.TestCase):
    def test_final_timeout_uses_original_copy(self):
        timeout = ai_services.AIRequestError("AI_TIMEOUT", "timeout", retryable=True)
        with patch.object(commercial_api, "rewrite_template_shots", side_effect=timeout), \
             patch.object(commercial_api, "fallback_template_to_original", return_value=("original", ["original"])), \
             patch.object(commercial_api, "heartbeat") as heartbeat:
            result = commercial_api.rewrite_batch_or_fallback(
                55, 10, variation="different", temperature=0.8
            )
        self.assertTrue(result["used_original"])
        self.assertEqual(result["error_code"], "AI_TIMEOUT")
        self.assertEqual(result["signature"], "original")
        self.assertIn("AI_TIMEOUT", heartbeat.call_args.kwargs["log"])

    def test_auth_failure_also_uses_original_without_retrying_batch(self):
        auth = ai_services.AIRequestError("AI_AUTH_FAILED", "bad key")
        with patch.object(commercial_api, "rewrite_template_shots", side_effect=auth) as rewrite, \
             patch.object(commercial_api, "fallback_template_to_original", return_value=("original", ["original"])):
            result = commercial_api.rewrite_batch_or_fallback(
                56, 11, variation="different", temperature=0.8
            )
        self.assertEqual(rewrite.call_count, 1)
        self.assertTrue(result["used_original"])
        self.assertEqual(result["error_code"], "AI_AUTH_FAILED")

    def test_missing_ai_config_uses_original_copy(self):
        with patch.object(
            commercial_api, "rewrite_template_shots",
            side_effect=ValueError("请先在设置中配置并启用 AI 服务"),
        ), patch.object(
            commercial_api, "fallback_template_to_original",
            return_value=("original", ["original"]),
        ):
            result = commercial_api.rewrite_batch_or_fallback(
                58, 13, variation="different", temperature=0.8
            )
        self.assertTrue(result["used_original"])
        self.assertEqual(result["error_code"], "AI_PROVIDER_ERROR")

    def test_unrelated_value_error_is_not_hidden(self):
        with patch.object(
            commercial_api, "rewrite_template_shots",
            side_effect=ValueError("template data corrupt"),
        ):
            with self.assertRaisesRegex(ValueError, "template data corrupt"):
                commercial_api.rewrite_batch_or_fallback(
                    59, 14, variation="different", temperature=0.8
                )

    def test_missing_original_text_keeps_a_real_ai_error(self):
        timeout = ai_services.AIRequestError("AI_TIMEOUT", "timeout", retryable=True)
        with patch.object(commercial_api, "rewrite_template_shots", side_effect=timeout), \
             patch.object(commercial_api, "rows", return_value=[{"id": 1, "original_text": ""}]):
            with self.assertRaises(ai_services.AIRequestError):
                commercial_api.rewrite_batch_or_fallback(
                    57, 12, variation="different", temperature=0.8
                )

    def test_retry_logger_records_every_attempt(self):
        callback = commercial_api.batch_ai_retry_logger(60)
        with patch.object(commercial_api, "heartbeat") as heartbeat, \
             patch.object(commercial_api, "append_task_log") as append:
            callback({"phase": "attempt", "attempt": 1, "total": 3})
            callback({"phase": "retry", "attempt": 1, "total": 3, "code": "AI_TIMEOUT", "delay": 2})
            callback({"phase": "attempt", "attempt": 2, "total": 3})
        self.assertEqual(heartbeat.call_count, 2)
        self.assertIn("1/3", heartbeat.call_args_list[0].kwargs["step"])
        self.assertIn("2/3", heartbeat.call_args_list[1].kwargs["step"])
        self.assertIn("2", append.call_args.args[1])

    def test_batch_generation_continues_after_ai_fallback(self):
        """A degraded AI step must still hand the item to normal generation."""
        context = {"batch_id": 9, "total_count": 3, "index_no": 1, "template_id": 41}
        generated = []

        def run_generation(task_id, before_generation=None, **_kwargs):
            self.assertIsNotNone(before_generation)
            before_generation()
            generated.append(task_id)
            return {"task_id": task_id, "status": "queued"}

        with patch.object(commercial_api, "row", return_value=context), \
             patch.object(commercial_api, "rows", return_value=[]), \
             patch.object(commercial_api, "heartbeat"), \
             patch.object(commercial_api, "check_cancelled"), \
             patch.object(commercial_api, "rewrite_batch_or_fallback", return_value={
                 "used_original": True,
                 "signature": "original",
                 "texts": ["original"],
                 "error_code": "AI_TIMEOUT",
             }) as rewrite, \
             patch.object(commercial_api, "start_generate", side_effect=run_generation):
            result = commercial_api.start_batch_generation(88)

        self.assertEqual(result["task_id"], 88)
        self.assertEqual(generated, [88])
        rewrite.assert_called_once()

    def test_three_batch_items_each_continue_when_ai_is_unavailable(self):
        """One provider outage must not discard the remaining batch children."""
        generated = []

        def context_for(task_id):
            return {"batch_id": 12, "total_count": 3, "index_no": task_id, "template_id": 100 + task_id}

        def run_generation(task_id, before_generation=None, **_kwargs):
            before_generation()
            generated.append(task_id)
            return {"task_id": task_id, "status": "queued"}

        with patch.object(commercial_api, "row", side_effect=lambda _sql, params: context_for(int(params[0]))), \
             patch.object(commercial_api, "rows", return_value=[]), \
             patch.object(commercial_api, "heartbeat"), \
             patch.object(commercial_api, "check_cancelled"), \
             patch.object(commercial_api, "rewrite_batch_or_fallback", return_value={
                 "used_original": True,
                 "signature": "original",
                 "texts": ["original"],
                 "error_code": "AI_TIMEOUT",
             }) as rewrite, \
             patch.object(commercial_api, "start_generate", side_effect=run_generation):
            for task_id in (1, 2, 3):
                commercial_api.start_batch_generation(task_id)

        self.assertEqual(generated, [1, 2, 3])
        self.assertEqual(rewrite.call_count, 3)


if __name__ == "__main__":
    unittest.main()
