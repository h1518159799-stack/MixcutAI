import io
import json
import sys
import threading
import unittest
import wave
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai_services import chat_completion, concatenate_tts_wavs, synthesize_tts


def wav_bytes(seconds=0.25, rate=24000):
    target = io.BytesIO()
    with wave.open(target, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\x00\x00" * int(seconds * rate))
    return target.getvalue()


def speech_wav(path, rate=24000):
    samples = array("h", [0] * int(rate * 0.2) + [2500] * int(rate * 0.2) + [0] * int(rate * 0.2))
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(samples.tobytes())


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/chat/completions"):
            body = json.dumps({"choices": [{"message": {"content": "mock rewritten copy"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.endswith("/audio/speech"):
            assert payload["voice"] == "mock_voice"
            body = wav_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


class ServiceAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_openai_compatible_chat(self):
        result = chat_completion({"provider": "openai_compatible", "base_url": self.base,
                                  "api_key": "test", "model": "mock", "timeout_seconds": 5,
                                  "is_enabled": 1}, [{"role": "user", "content": "rewrite"}])
        self.assertEqual(result, "mock rewritten copy")

    def test_openai_compatible_tts_wav(self):
        path, duration = synthesize_tts(
            {"provider": "openai_compatible", "base_url": self.base, "api_key": "test",
             "model": "mock-tts", "response_format": "wav", "timeout_seconds": 5},
            {"voice_code": "mock_voice"}, "hello", 999, {}, 1.0,
        )
        self.assertTrue(Path(path).exists())
        self.assertGreater(duration, 0.2)

    def test_segment_join_trims_boundary_silence_and_uses_short_gap(self):
        with TemporaryDirectory() as directory:
            first = Path(directory) / "first.wav"
            second = Path(directory) / "second.wav"
            output = Path(directory) / "joined.wav"
            speech_wav(first)
            speech_wav(second)
            _path, duration, timings = concatenate_tts_wavs(
                [str(first), str(second)], str(output), {"tts_segment_gap_ms": "50"}, return_timings=True
            )
        self.assertLess(duration, 0.6)
        self.assertAlmostEqual(timings[1]["start"] - timings[0]["end"], 0.05, places=2)


if __name__ == "__main__":
    unittest.main()
