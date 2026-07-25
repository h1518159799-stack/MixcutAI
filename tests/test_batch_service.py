import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import batch_service
import db


class BatchServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "batch.db"
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE edit_templates(
                    id INTEGER PRIMARY KEY,name TEXT,description TEXT DEFAULT '',output_width INTEGER DEFAULT 1080,
                    output_height INTEGER DEFAULT 1920,is_hidden INTEGER DEFAULT 0,source_template_id INTEGER,
                    created_at TEXT,updated_at TEXT
                );
                CREATE TABLE template_shots(
                    id INTEGER PRIMARY KEY,template_id INTEGER,shot_name TEXT DEFAULT '',original_text TEXT DEFAULT '',
                    ai_text TEXT DEFAULT '',material_category_id INTEGER,duration REAL DEFAULT 3,
                    estimated_duration REAL DEFAULT 0,mute_original_audio INTEGER DEFAULT 1,
                    enable_subtitle INTEGER DEFAULT 1,enable_tts INTEGER DEFAULT 1,voice_file_path TEXT DEFAULT '',
                    voice_duration REAL DEFAULT 0,voice_text_hash TEXT DEFAULT '',subtitle_file_path TEXT DEFAULT '',
                    subtitle_render_hash TEXT DEFAULT '',sort_order INTEGER,tts_voice_id INTEGER,created_at TEXT,updated_at TEXT
                );
                CREATE TABLE material_categories(id INTEGER PRIMARY KEY,name TEXT);
                CREATE TABLE shot_transitions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,from_shot_id INTEGER,to_shot_id INTEGER,
                    effect TEXT,duration REAL,enabled INTEGER,created_at TEXT,updated_at TEXT
                );
                CREATE TABLE materials(id INTEGER PRIMARY KEY,category_id INTEGER,status TEXT);
                CREATE TABLE generation_tasks(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,template_id INTEGER,task_name TEXT,status TEXT,
                    progress INTEGER DEFAULT 0,total_shots INTEGER DEFAULT 0,current_shot TEXT DEFAULT '',
                    output_path TEXT DEFAULT '',error_message TEXT DEFAULT '',created_at TEXT,updated_at TEXT
                );
                CREATE TABLE batch_tasks(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,template_id INTEGER,total_count INTEGER,
                    completed_count INTEGER DEFAULT 0,failed_count INTEGER DEFAULT 0,status TEXT,
                    material_strategy TEXT,category_ids_json TEXT,created_at TEXT,updated_at TEXT,finished_at TEXT DEFAULT ''
                );
                CREATE TABLE batch_task_items(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,batch_id INTEGER,task_id INTEGER,index_no INTEGER,status TEXT,
                    output_path TEXT DEFAULT '',error_message TEXT DEFAULT '',created_at TEXT,updated_at TEXT,
                    UNIQUE(batch_id,index_no)
                );
                INSERT INTO edit_templates(id,name,description,output_width,output_height,is_hidden,created_at,updated_at)
                    VALUES(7,'生产测试模板','',1080,1920,0,'','');
                INSERT INTO material_categories VALUES(3,'产品展示');
                INSERT INTO template_shots(id,template_id,shot_name,original_text,material_category_id,sort_order,created_at,updated_at)
                    VALUES(11,7,'镜头1','原始文案',3,1,'','');
                INSERT INTO materials VALUES(101,3,'active');
                INSERT INTO materials VALUES(102,3,'active');
                INSERT INTO materials VALUES(103,3,'active');
                """
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5, factory=db.ClosingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    def test_create_batch_creates_generation_items(self):
        with patch.object(batch_service, "connect", self.connect):
            batch_id = batch_service.create_batch_task(
                {"template_id": 7, "total_count": 3, "category_ids": [3], "material_strategy": "random"}
            )
        with self.connect() as conn:
            batch = conn.execute("SELECT * FROM batch_tasks WHERE id=?", (batch_id,)).fetchone()
            items = conn.execute("SELECT * FROM batch_task_items ORDER BY index_no").fetchall()
            tasks = conn.execute("SELECT * FROM generation_tasks ORDER BY id").fetchall()
        self.assertEqual(batch["total_count"], 3)
        self.assertEqual([item["index_no"] for item in items], [1, 2, 3])
        self.assertEqual(len(tasks), 3)
        self.assertEqual(len({task["template_id"] for task in tasks}), 3)
        with self.connect() as conn:
            hidden = conn.execute("SELECT COUNT(*) FROM edit_templates WHERE is_hidden=1").fetchone()[0]
            cloned = conn.execute("SELECT COUNT(*) FROM template_shots WHERE template_id<>7 AND ai_text='' ").fetchone()[0]
        self.assertEqual(hidden, 3)
        self.assertEqual(cloned, 3)

    def test_clone_copies_transition_to_cloned_shot_ids(self):
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO template_shots
                   (id,template_id,shot_name,original_text,material_category_id,sort_order,created_at,updated_at)
                   VALUES(12,7,'shot2','copy',3,2,'','')"""
            )
            conn.execute(
                """INSERT INTO shot_transitions
                   (from_shot_id,to_shot_id,effect,duration,enabled,created_at,updated_at)
                   VALUES(11,12,'slide_left',0.35,1,'','')"""
            )
            cloned_id = batch_service.clone_template_for_task(conn, 7, "clone", "2026-01-01 00:00:00")
        with self.connect() as conn:
            cloned_shots = conn.execute(
                "SELECT id FROM template_shots WHERE template_id=? ORDER BY sort_order,id", (cloned_id,)
            ).fetchall()
            transition = conn.execute(
                "SELECT * FROM shot_transitions WHERE from_shot_id=? AND to_shot_id=?",
                (cloned_shots[0]["id"], cloned_shots[1]["id"]),
            ).fetchone()
        self.assertIsNotNone(transition)
        self.assertEqual(transition["effect"], "slide_left")
        self.assertEqual(transition["enabled"], 1)

    def test_serial_processing_keeps_order_and_counts_failures(self):
        with patch.object(batch_service, "connect", self.connect):
            batch_id = batch_service.create_batch_task(
                {"template_id": 7, "total_count": 3, "category_ids": [3], "material_strategy": "random"}
            )
            with self.connect() as conn:
                task_ids = [r[0] for r in conn.execute("SELECT task_id FROM batch_task_items ORDER BY index_no")]
            call_order = []

            def generate(task_id):
                call_order.append(task_id)
                with self.connect() as conn:
                    if task_id == task_ids[1]:
                        conn.execute(
                            "UPDATE generation_tasks SET status='failed',error_message='test failure' WHERE id=?",
                            (task_id,),
                        )
                    else:
                        conn.execute(
                            "UPDATE generation_tasks SET status='completed',progress=100,output_path=? WHERE id=?",
                            (f"video-{task_id}.mp4", task_id),
                        )

            batch_service.process_batch(batch_id, generate, poll_interval=0.01)

        with self.connect() as conn:
            batch = conn.execute("SELECT * FROM batch_tasks WHERE id=?", (batch_id,)).fetchone()
            statuses = [r[0] for r in conn.execute("SELECT status FROM batch_task_items ORDER BY index_no")]
        self.assertEqual(call_order, sorted(call_order))
        self.assertEqual(statuses, ["completed", "failed", "completed"])
        self.assertEqual(batch["completed_count"], 2)
        self.assertEqual(batch["failed_count"], 1)
        self.assertEqual(batch["status"], "completed_with_errors")

    def test_avoid_repeat_rejects_insufficient_pool(self):
        with patch.object(batch_service, "connect", self.connect):
            with self.assertRaisesRegex(ValueError, "避免重复"):
                batch_service.create_batch_task(
                    {"template_id": 7, "total_count": 4, "category_ids": [3], "material_strategy": "avoid_repeat"}
                )

    def test_failed_item_retries_once_before_next_queued_item(self):
        with patch.object(batch_service, "connect", self.connect):
            batch_id = batch_service.create_batch_task(
                {"template_id": 7, "total_count": 2, "category_ids": [3], "material_strategy": "random"}
            )
            calls = []
            attempts = {}

            def generate(task_id):
                calls.append(task_id)
                attempts[task_id] = attempts.get(task_id, 0) + 1
                with self.connect() as conn:
                    if len(attempts) == 1 and attempts[task_id] == 1:
                        conn.execute("UPDATE generation_tasks SET status='failed',error_message='temporary' WHERE id=?", (task_id,))
                    else:
                        conn.execute("UPDATE generation_tasks SET status='completed',progress=100 WHERE id=?", (task_id,))

            batch_service.process_batch(batch_id, generate, poll_interval=0.01)

        self.assertEqual(calls[0], calls[1])
        self.assertNotEqual(calls[1], calls[2])
        with self.connect() as conn:
            batch = conn.execute("SELECT * FROM batch_tasks WHERE id=?", (batch_id,)).fetchone()
        self.assertEqual(batch["completed_count"], 2)
        self.assertEqual(batch["failed_count"], 0)


if __name__ == "__main__":
    unittest.main()
