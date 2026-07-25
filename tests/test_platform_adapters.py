import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import browser_profile_service
import browser_auth_service
import commercial_api
import db
import ai_publish_service
from oauth_service import authorization_status, start_authorization
from oauth_token_store import protect, unprotect
from platforms import adapter_catalog, get_adapter
from platforms.base import PublishNotImplementedError
from publish_service import publish
from publish_queue import create_publish_task, enqueue_publish_task, next_queued_channel
from publishers import get_publisher, publisher_catalog
from publishers.base import PublishingNotEnabledError
from publishers.youtube import YouTubePublisher


class PlatformAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        (root / "config").mkdir(parents=True)
        self.patchers = [
            patch.object(db, "DB_PATH", root / "data" / "mixcut.db"),
            patch.object(db, "DATA_DIR", root / "data"),
            patch.object(db, "THUMB_DIR", root / "data" / "thumbnails"),
            patch.object(db, "VOICE_DIR", root / "data" / "voices"),
            patch.object(db, "CONFIG_DIR", root / "config"),
            patch.object(db, "OUTPUT_DIR", root / "outputs"),
            patch.object(browser_profile_service, "BROWSER_PROFILE_DIR", root / "data" / "browser_profiles"),
        ]
        for patcher in self.patchers:
            patcher.start()
        db._init_db_impl()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def test_catalog_separates_api_and_browser_platforms(self):
        catalog = {item["code"]: item["integration_type"] for item in adapter_catalog()}
        for code in ("youtube", "facebook", "instagram", "x"):
            self.assertEqual(catalog[code], "api")
        for code in ("tiktok", "douyin", "xiaohongshu", "kuaishou", "wechat_channels"):
            self.assertEqual(catalog[code], "browser")

    def test_api_account_does_not_create_browser_profile(self):
        platform = commercial_api.row("SELECT * FROM platforms WHERE code='youtube'")
        account_id = commercial_api.save_publishing_account({
            "platform_id": platform["id"], "account_name": "YouTube Brand", "oauth_client_id": "public-client-id",
            "oauth_client_secret": "desktop-client-secret",
        })
        account = commercial_api.row("SELECT * FROM publish_accounts WHERE id=?", (account_id,))
        self.assertEqual(account["publishing_mode"], "api")
        self.assertEqual(account["browser_profile_path"], "")
        self.assertEqual(authorization_status(account_id)["status"], "not_authorized")
        api_config = __import__("json").loads(account["api_config_json"])
        self.assertNotIn("desktop-client-secret", account["api_config_json"])
        self.assertEqual(unprotect(api_config["client_secret_encrypted"]), "desktop-client-secret")

    def test_oauth_start_requires_client_id(self):
        platform = commercial_api.row("SELECT * FROM platforms WHERE code='x'")
        account_id = commercial_api.save_publishing_account({"platform_id": platform["id"], "account_name": "X Brand"})
        account = commercial_api.row("SELECT * FROM publish_accounts WHERE id=?", (account_id,))
        result = start_authorization(platform, account)
        self.assertEqual(result["status"], "configure_required")

    def test_browser_cookie_detection_and_status_writeback(self):
        self.assertTrue(browser_auth_service._is_authenticated(
            [{"domain": ".douyin.com", "name": "sessionid"}],
            ["douyin.com"], ["sessionid", "sessionid_ss"],
        ))
        platform = commercial_api.row("SELECT * FROM platforms WHERE code='douyin'")
        account_id = commercial_api.save_publishing_account({"platform_id": platform["id"], "account_name": "抖音主号"})
        browser_auth_service._mark_account(account_id, "logged_in")
        account = commercial_api.row("SELECT login_status,last_checked_at FROM publish_accounts WHERE id=?", (account_id,))
        self.assertEqual(account["login_status"], "logged_in")
        self.assertTrue(account["last_checked_at"])

    def test_unified_publish_is_reserved(self):
        with self.assertRaises(PublishNotImplementedError):
            publish("youtube", "video.mp4", "title", ["tag"], None)

    def test_publish_task_records_channels_queue_and_delete_policy(self):
        video = self.root / "finished.mp4"
        video.write_bytes(b"video")
        with db.connect() as conn:
            stamp = db.now()
            conn.execute(
                "INSERT INTO generation_tasks(task_name,status,output_path,created_at,updated_at) VALUES(?,'completed',?,?,?)",
                ("成片1", str(video), stamp, stamp),
            )
            generation_task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        platform = commercial_api.row("SELECT * FROM platforms WHERE code='douyin'")
        account_id = commercial_api.save_publishing_account({"platform_id": platform["id"], "account_name": "发布主号"})
        task_id = create_publish_task({
            "generation_task_id": generation_task_id,
            "account_ids": [account_id],
            "title": "测试标题",
            "description": "测试描述",
            "tags": "#工厂 #OEM 工厂",
            "scheduled_at": "2026-07-20T10:30",
            "delete_after_publish": True,
        })
        task = commercial_api.row("SELECT * FROM publish_tasks WHERE id=?", (task_id,))
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["delete_after_publish"], 1)
        self.assertEqual(task["scheduled_at"], "2026-07-20 10:30")
        self.assertEqual(__import__("json").loads(task["tags_json"]), ["工厂", "OEM"])
        enqueue_publish_task(task_id)
        self.assertEqual(commercial_api.row("SELECT status FROM publish_tasks WHERE id=?", (task_id,))["status"], "queued")
        self.assertEqual(commercial_api.row("SELECT status FROM publish_channels WHERE publish_task_id=?", (task_id,))["status"], "queued")
        self.assertIsNotNone(next_queued_channel())

    def test_youtube_publisher_requires_a_real_video_and_oauth(self):
        catalog = publisher_catalog()
        self.assertEqual({item["platform_code"] for item in catalog}, {"douyin", "tiktok", "youtube", "instagram", "facebook", "xiaohongshu", "kuaishou"})
        with self.assertRaises(ValueError):
            get_publisher("youtube").publish("video.mp4", {}, {})

    def test_untested_platform_cannot_enter_production_queue(self):
        video = self.root / "youtube-test.mp4"
        video.write_bytes(b"video")
        with db.connect() as conn:
            stamp = db.now()
            conn.execute(
                "INSERT INTO generation_tasks(task_name,status,output_path,created_at,updated_at) VALUES(?,'completed',?,?,?)",
                ("YouTube 验收", str(video), stamp, stamp),
            )
            generation_task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        platform = commercial_api.row("SELECT * FROM platforms WHERE code='youtube'")
        account_id = commercial_api.save_publishing_account({
            "platform_id": platform["id"], "account_name": "待验收 YouTube", "oauth_client_id": "client-id"
        })
        with self.assertRaisesRegex(ValueError, "平台尚未完成测试"):
            create_publish_task({
                "generation_task_id": generation_task_id,
                "account_ids": [account_id],
                "title": "不能进入正式发布",
            })

    def test_youtube_success_requires_processing_and_real_id(self):
        video = self.root / "youtube-ok.mp4"
        video.write_bytes(b"video")
        publisher = YouTubePublisher()
        final = {
            "snippet": {"title": "验收标题", "description": "验收简介", "tags": ["验收"], "channelId": "channel-1"},
            "status": {"privacyStatus": "private", "uploadStatus": "uploaded"},
            "processingDetails": {"processingStatus": "succeeded"},
        }
        with patch("publishers.youtube._access_token", return_value="token"), \
             patch.object(publisher, "_start_session", return_value="https://upload.example/session"), \
             patch.object(publisher, "_upload_chunks", return_value={"id": "real_video_id"}), \
             patch.object(publisher, "_set_thumbnail", return_value={"supported": True, "passed": True, "message": "封面已上传"}), \
             patch.object(publisher, "_wait_for_processing", return_value=final):
            result = publisher.publish(str(video), {"id": 1, "channel_id": "channel-1"}, {
                "title": "验收标题", "description": "验收简介", "tags": ["验收"],
                "visibility": "private", "cover_path": "cover.jpg",
            })
        self.assertTrue(result["verified"])
        self.assertEqual(result["id"], "real_video_id")
        self.assertEqual(result["processing_status"], "succeeded")
        self.assertEqual(result["url"], "https://www.youtube.com/watch?v=real_video_id")

    def test_ai_publish_content_uses_video_profile_and_platform_strategy(self):
        stamp = db.now()
        with db.connect() as conn:
            conn.execute("INSERT INTO account_profiles(company_name,industry,product_info,brand_intro,persona_info,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                         ("亿康源", "洗护OEM", "产品介绍：洗发水定制", "品牌名称：亿康源", "身份角色：工厂老板", stamp, stamp))
            profile_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO edit_templates(name,created_at,updated_at) VALUES(?,?,?)", ("发布测试", stamp, stamp))
            template_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO template_shots(template_id,shot_name,original_text,ai_text,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                         (template_id, "开场", "为什么品牌都在找源头工厂", "做品牌为什么要找源头工厂", 1, stamp, stamp))
            conn.execute("INSERT INTO generation_tasks(template_id,task_name,status,created_at,updated_at) VALUES(?,?,'completed',?,?)",
                         (template_id, "AI发布测试", stamp, stamp))
            video_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO ai_service_configs(name,provider,base_url,api_key,model,is_enabled,is_default,created_at,updated_at) VALUES(?,?,?,?,?,1,1,?,?)",
                         ("AI", "openai_compatible", "https://example.invalid/v1", "key", "model", stamp, stamp))
        response = '{"titles":["源头工厂怎么选","品牌代工避坑指南","OEM合作看这几点"],"description":"洗护产品定制经验分享","hashtags":["创业","OEM","工厂实力"],"cover_text":"代工避坑"}'
        with patch.object(ai_publish_service, "chat_completion", return_value=response) as completion:
            result = ai_publish_service.generate_publish_content(video_id, "douyin", profile_id)
        self.assertEqual(result["title"], "源头工厂怎么选")
        self.assertEqual(result["hashtags"], ["创业", "OEM", "工厂实力"])
        self.assertEqual(result["cover_text"], "代工避坑")
        prompt = completion.call_args.args[1][1]["content"]
        self.assertIn("洗护OEM", prompt)
        self.assertIn("做品牌为什么要找源头工厂", prompt)
        self.assertIn("抖音", prompt)

    @unittest.skipUnless(__import__("os").name == "nt", "DPAPI is Windows-only")
    def test_tokens_round_trip_through_windows_dpapi(self):
        encrypted = protect("access-token-value")
        self.assertNotIn("access-token-value", encrypted)
        self.assertEqual(unprotect(encrypted), "access-token-value")


if __name__ == "__main__":
    unittest.main()
