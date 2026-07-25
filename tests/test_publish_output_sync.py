import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import publish_queue


class PublishOutputSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "sync.db"
        self.existing_video = Path(self.temp_dir.name) / "existing.mp4"
        self.existing_video.write_bytes(b"video")
        conn = self.connect()
        try:
            conn.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE generation_tasks(
                    id INTEGER PRIMARY KEY,status TEXT,output_path TEXT
                );
                CREATE TABLE app_settings(
                    key TEXT PRIMARY KEY,value TEXT
                );
                CREATE TABLE publish_tasks(
                    id INTEGER PRIMARY KEY,status TEXT,video_path TEXT
                );
                CREATE TABLE publish_channels(
                    id INTEGER PRIMARY KEY,publish_task_id INTEGER,
                    FOREIGN KEY(publish_task_id) REFERENCES publish_tasks(id) ON DELETE CASCADE
                );
                """
            )
            conn.execute(
                "INSERT INTO app_settings(key,value) VALUES('output_dir',?)",
                (self.temp_dir.name,),
            )
            missing = str(Path(self.temp_dir.name) / "deleted.mp4")
            conn.executemany(
                "INSERT INTO generation_tasks(id,status,output_path) VALUES(?,?,?)",
                [(1, "completed", str(self.existing_video)), (2, "completed", missing)],
            )
            conn.executemany(
                "INSERT INTO publish_tasks(id,status,video_path) VALUES(?,?,?)",
                [
                    (1, "pending", missing),
                    (2, "queued", missing),
                    (3, "failed", missing),
                    (4, "publishing", missing),
                    (5, "success", missing),
                    (6, "pending", str(self.existing_video)),
                ],
            )
            conn.executemany(
                "INSERT INTO publish_channels(id,publish_task_id) VALUES(?,?)",
                [(index, index) for index in range(1, 7)],
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def test_sync_removes_only_unpublished_jobs_whose_video_is_missing(self):
        with patch.object(publish_queue, "connect", new=self.connect):
            result = publish_queue.sync_missing_publish_videos()

        conn = self.connect()
        try:
            tasks = [row[0] for row in conn.execute("SELECT id FROM publish_tasks ORDER BY id")]
            channels = [row[0] for row in conn.execute("SELECT publish_task_id FROM publish_channels ORDER BY id")]
            generation_tasks = [row[0] for row in conn.execute("SELECT id FROM generation_tasks ORDER BY id")]
        finally:
            conn.close()

        self.assertEqual(tasks, [4, 5, 6])
        self.assertEqual(channels, [4, 5, 6])
        self.assertEqual(generation_tasks, [1, 2])
        self.assertEqual(result["scanned_outputs"], 1)
        self.assertEqual(result["removed_publish_tasks"], 3)
        self.assertEqual(result["skipped_publishing_tasks"], 1)


if __name__ == "__main__":
    unittest.main()
