import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# This unit test exercises database invalidation only.  Keep optional media/Pillow
# dependencies out of the test environment by replacing the scanner import.
video_scan_stub = types.ModuleType("video_scan")
for function_name in ("make_thumbnail", "probe_video", "quick_hash", "scan_video_files"):
    setattr(video_scan_stub, function_name, lambda *args, **kwargs: None)
sys.modules.setdefault("video_scan", video_scan_stub)

import commercial_api


class ScriptCacheInvalidationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.connections = []
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE edit_templates (
                    id INTEGER PRIMARY KEY, updated_at TEXT
                );
                CREATE TABLE template_shots (
                    id INTEGER PRIMARY KEY,
                    template_id INTEGER NOT NULL,
                    shot_name TEXT,
                    original_text TEXT DEFAULT '',
                    ai_text TEXT DEFAULT '',
                    material_category_id INTEGER,
                    mute_original_audio INTEGER DEFAULT 1,
                    enable_subtitle INTEGER DEFAULT 1,
                    enable_tts INTEGER DEFAULT 1,
                    tts_voice_id INTEGER,
                    sort_order INTEGER DEFAULT 0,
                    voice_file_path TEXT DEFAULT '',
                    voice_duration REAL DEFAULT 0,
                    estimated_duration REAL DEFAULT 0,
                    subtitle_file_path TEXT DEFAULT '',
                    subtitle_render_hash TEXT DEFAULT '',
                    voice_text_hash TEXT DEFAULT '',
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE generation_shot_materials (
                    id INTEGER PRIMARY KEY, task_id INTEGER, shot_id INTEGER, material_id INTEGER
                );
                INSERT INTO edit_templates(id,updated_at) VALUES(1,'old');
                INSERT INTO template_shots(
                    id,template_id,shot_name,original_text,ai_text,voice_file_path,
                    voice_duration,estimated_duration,subtitle_file_path,
                    voice_text_hash,created_at,updated_at
                ) VALUES(1,1,'shot','old original','old rewrite','old.wav',5,5,
                    'old.ass','old-hash','old','old');
                INSERT INTO generation_shot_materials(id,shot_id,material_id)
                    VALUES(1,1,99);
                """
            )

    def tearDown(self):
        for conn in self.connections:
            conn.close()
        self.temp_dir.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.connections.append(conn)
        return conn

    def test_editing_original_discards_stale_rewrite_and_generated_assets(self):
        payload = {
            "id": 1,
            "template_id": 1,
            "shot_name": "shot",
            "original_text": "new original",
            "ai_text": "old rewrite",
            "mute_original_audio": True,
            "enable_subtitle": True,
            "enable_tts": True,
        }
        with patch.object(commercial_api, "connect", self.connect):
            commercial_api.save_shot(payload)

        with self.connect() as conn:
            shot = dict(conn.execute("SELECT * FROM template_shots WHERE id=1").fetchone())
            relation_count = conn.execute(
                "SELECT COUNT(*) FROM generation_shot_materials WHERE shot_id=1"
            ).fetchone()[0]

        self.assertEqual(shot["original_text"], "new original")
        self.assertEqual(shot["ai_text"], "")
        self.assertEqual(shot["voice_file_path"], "")
        self.assertEqual(shot["voice_duration"], 0)
        self.assertEqual(shot["subtitle_file_path"], "")
        self.assertEqual(shot["subtitle_render_hash"], "")
        self.assertEqual(shot["voice_text_hash"], "")
        self.assertEqual(relation_count, 0)

    def test_batch_rewrite_parser_accepts_strict_json_and_code_fences(self):
        content = '```json\n[{"id":1,"ai_text":"first"},{"id":2,"ai_text":"second"}]\n```'
        result = commercial_api.parse_rewrite_batch(content, [1, 2])
        self.assertEqual(result, {1: "first", 2: "second"})

    def test_voice_cache_hash_includes_voice_and_speed(self):
        config = {"provider": "edge_tts", "model": "edge-tts", "response_format": "mp3"}
        voice_a = {"id": 1, "service_id": 1, "voice_code": "zh-CN-XiaoxiaoNeural"}
        voice_b = {"id": 2, "service_id": 1, "voice_code": "zh-CN-YunxiNeural"}
        first = commercial_api.voice_render_hash("same text", config, voice_a, 1.0)
        self.assertNotEqual(first, commercial_api.voice_render_hash("same text", config, voice_b, 1.0))
        self.assertNotEqual(first, commercial_api.voice_render_hash("same text", config, voice_a, 1.2))

    def test_rewrite_text_uses_spaces_as_only_segment_markers(self):
        result = commercial_api.normalize_rewrite_text("你好，世界！ OEM/ODM  100% 😊")
        self.assertEqual(result, "你好 世界 OEM ODM 100")
        self.assertNotRegex(result, r"[^\w\s\u4e00-\u9fff]")


if __name__ == "__main__":
    unittest.main()
