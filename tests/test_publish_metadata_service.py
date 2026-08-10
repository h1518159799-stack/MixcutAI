import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import publish_metadata_service as service


class PublishMetadataServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "config").mkdir(parents=True)
        self.patchers = [
            patch.object(db, "DB_PATH", root / "mixcut.db"),
            patch.object(db, "DATA_DIR", root / "data"),
            patch.object(db, "CONFIG_DIR", root / "config"),
            patch.object(service, "connect", db.connect),
        ]
        for patcher in self.patchers:
            patcher.start()
        db._init_db_impl()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def _task(self, created_at=None):
        stamp = created_at or db.now()
        output = Path(self.temp.name) / "video.mp4"
        output.write_bytes(b"video")
        with db.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO generation_tasks(task_name,status,output_path,created_at,updated_at)
                   VALUES(?, 'completed',?,?,?)""",
                ("发布元数据测试", str(output), stamp, stamp),
            )
            task_id = int(cursor.lastrowid)
        return {"id": task_id, "task_name": "发布元数据测试", "status": "completed", "output_exists": True, "created_at": stamp}

    def test_historical_task_is_not_auto_enrolled(self):
        task = self._task("2000-01-01 00:00:00")
        self.assertIsNone(service.ensure_new_draft(task))
        self.assertTrue(service.public_metadata(task["id"])["legacy_record"])

    def test_new_task_generates_once_and_persists(self):
        task = self._task()
        self.assertIsNotNone(service.ensure_new_draft(task))
        result = {
            "title": "工厂实力展示", "description": "简介", "hashtags": ["工厂", "产品"],
            "cover_text": "源头工厂", "ai_generated_time": db.now(),
        }
        with patch.object(service, "generate_publish_content", return_value=result) as generate:
            service._auto_generate_worker(task["id"])
            service._auto_generate_worker(task["id"])
        value = service.public_metadata(task["id"])
        self.assertEqual(value["ai_status"], "completed")
        self.assertEqual(value["title"], "工厂实力展示")
        self.assertEqual(value["hashtags"], "#工厂 #产品")
        self.assertEqual(generate.call_count, 1)

    def test_manual_field_is_not_overwritten_by_background_result(self):
        task = self._task()
        service.ensure_new_draft(task)
        service.save_draft_field(task["id"], "title", "用户标题", "manually_edited")
        with db.connect() as conn:
            conn.execute(
                "UPDATE publish_draft_metadata SET ai_status='not_started',auto_generation_attempted=0 WHERE generation_task_id=?",
                (task["id"],),
            )
        result = {
            "title": "AI标题", "description": "简介", "hashtags": ["产品"],
            "cover_text": "新品展示", "ai_generated_time": db.now(),
        }
        with patch.object(service, "generate_publish_content", return_value=result):
            service._auto_generate_worker(task["id"])
        value = service.public_metadata(task["id"])
        self.assertEqual(value["title"], "用户标题")
        self.assertEqual(value["title_state"], "manually_edited")
        self.assertEqual(value["ai_status"], "manually_edited")

    def test_each_manual_regeneration_only_changes_requested_field(self):
        task = self._task()
        service.ensure_new_draft(task)
        service.save_draft_field(task["id"], "title", "标题A", "completed")
        service.save_draft_field(task["id"], "hashtags", "#话题A", "completed")
        service.save_draft_field(task["id"], "cover_text", "封面A", "completed")
        service.save_draft_field(task["id"], "hashtags", "#话题B", "completed")
        value = service.public_metadata(task["id"])
        self.assertEqual(value["title"], "标题A")
        self.assertEqual(value["hashtags"], "#话题B")
        self.assertEqual(value["cover_text"], "封面A")


if __name__ == "__main__":
    unittest.main()
