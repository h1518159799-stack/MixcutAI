import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import video_engine
import commercial_api


class TaskMaterialIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "task-materials.db"
        self.connections = []
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE template_shots (
                    id INTEGER PRIMARY KEY, template_id INTEGER, shot_name TEXT,
                    original_text TEXT DEFAULT '', ai_text TEXT DEFAULT '',
                    sort_order INTEGER, voice_file_path TEXT, voice_duration REAL,
                    enable_subtitle INTEGER, subtitle_file_path TEXT,
                    material_category_id INTEGER
                );
                CREATE TABLE materials (
                    id INTEGER PRIMARY KEY, file_path TEXT, file_name TEXT,
                    duration REAL, width INTEGER, height INTEGER,
                    category_id INTEGER, status TEXT
                );
                CREATE TABLE material_categories (id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE generation_shot_materials (
                    id INTEGER PRIMARY KEY, task_id INTEGER, shot_id INTEGER,
                    material_id INTEGER, voice_duration REAL,
                    material_start_time REAL, material_end_time REAL,
                    created_at TEXT, updated_at TEXT
                );
                CREATE TABLE generation_tasks (
                    id INTEGER PRIMARY KEY, status TEXT, created_at TEXT,
                    updated_at TEXT, finished_at TEXT
                );
                INSERT INTO template_shots VALUES(1,7,'shot','show new product','',1,'voice.wav',3,0,'',5);
                INSERT INTO materials VALUES(10,'old.mp4','old.mp4',10,1080,1920,5,'active');
                INSERT INTO materials VALUES(11,'new.mp4','new.mp4',10,1080,1920,5,'active');
                INSERT INTO material_categories VALUES(5,'factory');
                INSERT INTO generation_shot_materials VALUES(1,100,1,10,3,1,4,'','');
                INSERT INTO generation_shot_materials VALUES(2,101,1,11,3,5,8,'','');
                INSERT INTO generation_tasks VALUES(100,'completed','','','2026-01-01 10:00:00');
                INSERT INTO generation_tasks VALUES(101,'failed','','','2026-01-01 11:00:00');
                """
            )

    def tearDown(self):
        for connection in self.connections:
            connection.close()
        self.temp_dir.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.connections.append(conn)
        return conn

    def test_each_task_reads_its_own_material_relation(self):
        with patch.object(video_engine, "connect", self.connect):
            task_100 = video_engine.load_template_shots(7, 100)
            task_101 = video_engine.load_template_shots(7, 101)

        self.assertEqual(task_100[0]["material_id"], 10)
        self.assertEqual(task_100[0]["material_start_time"], 1)
        self.assertEqual(task_100[0]["original_text"], "show new product")
        self.assertEqual(task_101[0]["material_id"], 11)
        self.assertEqual(task_101[0]["material_start_time"], 5)

    def test_new_task_avoids_previous_task_material(self):
        with patch.object(commercial_api, "connect", self.connect):
            result = commercial_api.match_shot(
                1,
                task_id=102,
                avoid_material_ids={10},
                previous_match={"material_id": 10, "material_start_time": 1},
            )

        self.assertEqual(result["material_id"], 11)
        with self.connect() as conn:
            saved = conn.execute(
                "SELECT material_id FROM generation_shot_materials WHERE task_id=102 AND shot_id=1"
            ).fetchone()[0]
        self.assertEqual(saved, 11)


if __name__ == "__main__":
    unittest.main()
