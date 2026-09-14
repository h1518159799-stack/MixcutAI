import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import publish_queue
import publish_safety
from publish_safety import PublishGuardError, map_publish_exception, preflight_douyin_publish_check
from publishers.browser_publisher import extract_douyin_work_items, match_douyin_work


VALID_VIDEO = {"duration": 10.0, "size": 100, "format": "mp4", "codec": "h264", "width": 1080, "height": 1920}


class DouyinPublishRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patchers = [
            patch.object(db, "DB_PATH", self.root / "data" / "mixcut.db"),
            patch.object(db, "DATA_DIR", self.root / "data"),
            patch.object(db, "THUMB_DIR", self.root / "data" / "thumbs"),
            patch.object(db, "VOICE_DIR", self.root / "data" / "voices"),
            patch.object(db, "CONFIG_DIR", self.root / "config"),
            patch.object(db, "OUTPUT_DIR", self.root / "outputs"),
            patch.object(publish_queue, "DATA_DIR", self.root / "data"),
        ]
        for item in self.patchers:
            item.start()
        (self.root / "config").mkdir(parents=True)
        db._init_db_impl()
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"video")
        self.profile = self.root / "profile"
        self.profile.mkdir()
        with db.connect() as conn:
            platform = conn.execute("SELECT * FROM platforms WHERE code='douyin'").fetchone()
            conn.execute("UPDATE platforms SET capability_status='passed' WHERE id=?", (platform["id"],))
            stamp = db.now()
            conn.execute(
                """INSERT INTO publish_accounts(platform_id,account_name,browser_profile_path,login_status,status,created_at,updated_at)
                   VALUES(?,?,?,'logged_in','active',?,?)""",
                (platform["id"], "测试抖音", str(self.profile), stamp, stamp),
            )
            self.account_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO publish_tasks(video_path,title,status,total_channels,created_at,updated_at) VALUES(?,?,'queued',1,?,?)",
                (str(self.video), "测试标题", stamp, stamp),
            )
            self.task_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """INSERT INTO publish_channels(publish_task_id,account_id,platform_id,status,created_at,updated_at)
                   VALUES(?,?,?,'queued',?,?)""",
                (self.task_id, self.account_id, platform["id"], stamp, stamp),
            )
            self.channel_id = int(conn.execute("SELECT last_insert_rowid() ").fetchone()[0])

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def test_work_list_payload_provides_real_id_and_url(self):
        items = extract_douyin_work_items({"aweme_list": [{
            "aweme_id": "123456789", "item_title": "测试标题",
            "share_url": "https://www.iesdouyin.com/share/video/123456789/", "create_time": 100,
        }]})
        work = match_douyin_work(items, "测试标题")
        self.assertEqual(work["id"], "123456789")
        self.assertIn("123456789", work["url"])

    def test_work_list_duration_is_normalized_to_seconds(self):
        items = extract_douyin_work_items({"aweme_list": [{
            "aweme_id": "duration-id", "item_title": "duration-title",
            "create_time": 100, "duration": 33000,
        }]})
        self.assertEqual(items[0]["duration"], 33.0)

    def test_work_match_rejects_same_title_with_wrong_duration(self):
        items = [
            {"id": "old", "title": "same-title", "url": "", "create_time": 20, "duration": 12.0},
            {"id": "right", "title": "same-title", "url": "", "create_time": 21, "duration": 33.0},
        ]
        self.assertEqual(
            match_douyin_work(items, "same-title", expected_duration=33.2)["id"],
            "right",
        )

    def test_work_match_excludes_baseline_and_honours_time(self):
        items = [
            {"id": "old", "title": "测试标题", "url": "old-url", "create_time": 10},
            {"id": "new", "title": "测试标题", "url": "new-url", "create_time": 20},
        ]
        self.assertEqual(match_douyin_work(items, "测试标题", excluded_ids={"old"})["id"], "new")
        self.assertIsNone(match_douyin_work(items, "测试标题", created_after=30))

    def test_missing_real_id_maps_to_manual_confirmation(self):
        error = map_publish_exception(RuntimeError("平台页面已显示发布完成，但未取得真实作品ID或作品链接"))
        self.assertEqual(error.code, "MANUAL_CONFIRM_REQUIRED")

    def test_preflight_rejects_login_profile_and_video_errors(self):
        with db.connect() as conn:
            platform = dict(conn.execute("SELECT * FROM platforms WHERE code='douyin'").fetchone())
            account = dict(conn.execute("SELECT * FROM publish_accounts WHERE id=?", (self.account_id,)).fetchone())
        with patch.object(publish_safety, "_probe_video", return_value=VALID_VIDEO), patch.object(publish_safety, "default_network_check"):
            result = preflight_douyin_publish_check(platform, account, str(self.video), {"title": "测试标题", "cover_path": ""})
            self.assertEqual(result["status"], "ready")
            with self.assertRaises(PublishGuardError) as login_error:
                preflight_douyin_publish_check(platform, {**account, "login_status": "login_required"}, str(self.video), {"title": "测试标题"})
            self.assertEqual(login_error.exception.code, "AUTH_EXPIRED")
            with self.assertRaises(PublishGuardError) as file_error:
                preflight_douyin_publish_check(platform, account, str(self.root / "missing.mp4"), {"title": "测试标题"})
            self.assertEqual(file_error.exception.code, "FILE_NOT_FOUND")

    def test_verified_backend_result_becomes_succeeded(self):
        class FakePublisher:
            def publish(self, video, account, metadata):
                metadata["state_callback"]("uploading")
                metadata["state_callback"]("waiting_confirmation")
                return {"verified": True, "backend_confirmed": True, "id": "9988", "url": "https://www.douyin.com/video/9988"}
        channel = {"id": self.channel_id, "publish_task_id": self.task_id, "account_id": self.account_id, "platform_code": "douyin"}
        with patch.object(publish_queue, "get_publisher", return_value=FakePublisher()), \
             patch.object(publish_queue, "preflight_douyin_publish_check", return_value={"status": "ready"}):
            publish_queue.process_douyin_channel(channel)
        with db.connect() as conn:
            row = conn.execute("SELECT status,remote_post_id,error_code FROM publish_channels WHERE id=?", (self.channel_id,)).fetchone()
            task = conn.execute("SELECT status FROM publish_tasks WHERE id=?", (self.task_id,)).fetchone()
        self.assertEqual((row["status"], row["remote_post_id"], row["error_code"]), ("succeeded", "9988", ""))
        self.assertEqual(task["status"], "succeeded")

    def test_failure_records_code_log_stage_and_does_not_fake_success(self):
        class FakePublisher:
            def publish(self, video, account, metadata):
                metadata["state_callback"]("uploading")
                raise RuntimeError("locator missing")
        channel = {"id": self.channel_id, "publish_task_id": self.task_id, "account_id": self.account_id, "platform_code": "douyin"}
        with patch.object(publish_queue, "get_publisher", return_value=FakePublisher()), \
             patch.object(publish_queue, "preflight_douyin_publish_check", return_value={"status": "ready"}):
            publish_queue.process_douyin_channel(channel)
        with db.connect() as conn:
            row = conn.execute("SELECT status,error_code,technical_log_id,result_evidence_json,remote_post_id FROM publish_channels WHERE id=?", (self.channel_id,)).fetchone()
        evidence = json.loads(row["result_evidence_json"])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "ELEMENT_NOT_FOUND")
        self.assertTrue(row["technical_log_id"].startswith("PUB-"))
        self.assertEqual(evidence["diagnosis"]["stage"], "uploading")
        self.assertEqual(row["remote_post_id"], "")


if __name__ == "__main__":
    unittest.main()
