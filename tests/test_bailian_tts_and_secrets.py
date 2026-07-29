import io
import json
import sys
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aliyun_bailian_tts import BailianTtsError, list_voices, synthesize
from config_secret_store import decrypt_secret, encrypt_secret, public_service_config
from ai_services import synthesize_tts


def wav_bytes(seconds=0.2, rate=24000):
    target = io.BytesIO()
    with wave.open(target, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\x00\x00" * int(seconds * rate))
    return target.getvalue()


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class BailianAndSecretTests(unittest.TestCase):
    def test_bailian_synthesis_downloads_returned_audio(self):
        payload = json.dumps({"output": {"audio": {"url": "https://audio.example/result.wav"}}}).encode()
        with TemporaryDirectory() as directory, patch(
            "aliyun_bailian_tts.urllib.request.urlopen",
            side_effect=[FakeResponse(payload), FakeResponse(wav_bytes())],
        ):
            output = Path(directory) / "preview.wav"
            result = synthesize(
                {"api_key": "test-key", "base_url": "https://dashscope.example/SpeechSynthesizer", "model": "qwen-audio-3.0-tts-flash"},
                "你好", "longanhuan_v3.6", str(output), audio_format="wav",
            )
            self.assertEqual(Path(result), output)
            self.assertGreater(output.stat().st_size, 100)

    def test_bailian_builtin_voice_list(self):
        voices = list_voices()
        self.assertTrue(any(item["voice_code"] == "longanhuan_v3.6" for item in voices))

    def test_bailian_is_connected_to_existing_tts_pipeline(self):
        def fake_synthesize(_config, _text, _voice, output, **_options):
            Path(output).write_bytes(wav_bytes())
            return output

        with patch("ai_services.synthesize_bailian", side_effect=fake_synthesize):
            path, duration = synthesize_tts(
                {"provider": "aliyun_bailian", "api_key": "test", "model": "qwen-audio-3.0-tts-flash", "response_format": "wav"},
                {"voice_code": "longanhuan_v3.6"}, "正式配音流程测试", 98765,
                {"aliyun_bailian_sample_rate": "24000", "tts_volume": "1.0"}, 1.0,
            )
        try:
            self.assertGreater(duration, 0.1)
            self.assertTrue(Path(path).is_file())
        finally:
            Path(path).unlink(missing_ok=True)

    def test_bailian_requires_https(self):
        with self.assertRaisesRegex(BailianTtsError, "HTTPS"):
            synthesize({"api_key": "x", "base_url": "http://example.test", "model": "qwen"}, "你好", "voice", "x.wav")

    def test_provider_secret_is_encrypted_and_never_exposed(self):
        encrypted = encrypt_secret("sk-test-secret")
        self.assertTrue(encrypted.startswith("dpapi:"))
        self.assertNotIn("sk-test-secret", encrypted)
        self.assertEqual(decrypt_secret(encrypted), "sk-test-secret")
        public = public_service_config({"id": 1, "api_key": encrypted})
        self.assertEqual(public["api_key"], "")
        self.assertTrue(public["api_key_configured"])
        self.assertEqual(public["api_key_masked"], "••••••••••••••••")


if __name__ == "__main__":
    unittest.main()
