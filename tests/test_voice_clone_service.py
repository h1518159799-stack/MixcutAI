import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import ai_services
from voice_clone_service import VoiceCloneError, minimax_endpoint, upload_clone_audio


class VoiceCloneServiceTests(unittest.TestCase):
    def test_minimax_endpoint_accepts_root_or_v1_base(self):
        self.assertEqual(minimax_endpoint("https://api.minimaxi.com", "t2a_v2"), "https://api.minimaxi.com/v1/t2a_v2")
        self.assertEqual(minimax_endpoint("https://api.minimaxi.com/v1", "/voice_clone"), "https://api.minimaxi.com/v1/voice_clone")

    def test_clone_upload_rejects_unsupported_files_before_network(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sample.txt"
            path.write_text("bad", encoding="utf-8")
            with self.assertRaises(VoiceCloneError):
                upload_clone_audio({"api_key": "test"}, str(path))

    def test_minimax_tts_provider_decodes_audio_and_returns_duration(self):
        with tempfile.TemporaryDirectory() as root:
            voice_dir = Path(root)

            def fake_convert(_source, output, _settings):
                with wave.open(str(output), "wb") as stream:
                    stream.setnchannels(1)
                    stream.setsampwidth(2)
                    stream.setframerate(24000)
                    stream.writeframes(b"\x00\x00" * 2400)

            response = {"data": {"audio": "49443304"}, "base_resp": {"status_code": 0, "status_msg": "success"}}
            with patch.object(ai_services, "VOICE_DIR", voice_dir), patch.object(
                ai_services, "_json_request", return_value=response
            ) as request_mock, patch.object(ai_services, "_convert_to_wav", side_effect=fake_convert):
                path, duration = ai_services.synthesize_tts(
                    {"provider": "minimax", "base_url": "https://api.minimaxi.com", "api_key": "key", "model": "speech-2.8-hd"},
                    {"voice_code": "MixcutClone_demo"}, "hello world", 77, {}, 1.0,
                )
            self.assertTrue(Path(path).is_file())
            self.assertGreater(duration, 0)
            self.assertIn("/v1/t2a_v2", request_mock.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
