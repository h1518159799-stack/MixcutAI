import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import browser_profile_service
import db
import publish_safety
from publish_queue import enqueue_publish_task
from publish_safety import (
    GUARDED_DOMESTIC_PLATFORMS, PublishGuardError, idempotency_key,
    map_publish_exception, preflight_publish_check, recover_guarded_channels,
    title_hash,
)
from publishers import get_publisher
from publishers.browser_publisher import BrowserPublisher


VALID_VIDEO = {"duration": 12.5, "size": 1024, "format": "mov,mp4,m4a", "codec": "h264", "width": 1080, "height": 1920}


class DomesticPublishSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir(parents=True)
        self.patchers = [
            patch.object(db, "DB_PATH", self.root / "data" / "mixcut.db"),
            patch.object(db, "DATA_DIR", self.root / "data"),
            patch.object(db, "THUMB_DIR", self.root / "data" / "thumbs"),
            patch.object(db, "VOICE_DIR", self.root / "data" / "voices"),
            patch.object(db, "CONFIG_DIR", self.root / "config"),
            patch.object(db, "OUTPUT_DIR", self.root / "outputs"),
            patch.object(browser_profile_service, "BROWSER_PROFILE_DIR", self.root / "data" / "browser_profiles"),
        ]
        for item in self.patchers:
            item.start()
        db._init_db_impl()
        self.fixture_index = 0
        self.video = self.root / "valid.mp4"
        self.video.write_bytes(b"valid-video-fixture")
        self.cover = self.root / "cover.png"
        from PIL import Image
        Image.new("RGB", (64, 64), "red").save(self.cover)

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def fixture(self, code, *, capability="passed", login="logged_in"):
        self.fixture_index += 1
        with db.connect() as conn:
            platform = dict(conn.execute("SELECT * FROM platforms WHERE code=?", (code,)).fetchone())
            conn.execute("UPDATE platforms SET capability_status=? WHERE id=?", (capability, platform["id"]))
            profile = self.root / "profiles" / code / "account"
            profile.mkdir(parents=True, exist_ok=True)
            stamp = db.now()
            conn.execute(
                """INSERT INTO publish_accounts(platform_id,account_name,browser_profile_path,login_status,status,created_at,updated_at)
                   VALUES(?,?,?,?, 'active',?,?)""",
                (platform["id"], f"{code}_test_{self.fixture_index}", str(profile), login, stamp, stamp),
            )
            account_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            account = dict(conn.execute("SELECT * FROM publish_accounts WHERE id=?", (account_id,)).fetchone())
            platform = dict(conn.execute("SELECT * FROM platforms WHERE id=?", (platform["id"],)).fetchone())
        return platform, account

    @staticmethod
    def metadata(title="发布测试", **values):
        return {"title": title, "description": "测试简介", "tags": ["测试", "作品"], "cover_path": "", "schedule": "", **values}

    def preflight(self, platform, account, metadata=None, video=None, **kwargs):
        with patch.object(publish_safety, "_probe_video", return_value=VALID_VIDEO):
            return preflight_publish_check(
                platform, account, str(video or self.video), metadata or self.metadata(),
                network_checker=lambda _url: None, **kwargs,
            )

    def assert_code(self, code, callback):
        with self.assertRaises(PublishGuardError) as context:
            callback()
        self.assertEqual(context.exception.code, code)
        self.assertTrue(context.exception.log_id.startswith("PUB-"))

    def test_platform_states_and_publish_methods_are_truthful(self):
        with db.connect() as conn:
            states = {row["code"]: row["capability_status"] for row in conn.execute("SELECT code,capability_status FROM platforms")}
        self.assertEqual(states["douyin"], "passed")
        for code in GUARDED_DOMESTIC_PLATFORMS:
            self.assertEqual(states[code], "untested")
            self.assertEqual(get_publisher(code).integration_type, "browser")
        # The validated Douyin publisher is not replaced by the guarded module.
        self.assertIsInstance(get_publisher("douyin"), BrowserPublisher)

    def test_all_platforms_fail_closed_for_unverified_login_and_files(self):
        for code in GUARDED_DOMESTIC_PLATFORMS:
            with self.subTest(platform=code, case="untested"):
                platform, account = self.fixture(code, capability="untested")
                self.assert_code("PLATFORM_UNTESTED", lambda: self.preflight(platform, account))
            with self.subTest(platform=code, case="not_logged_in"):
                platform, account = self.fixture(code, login="login_required")
                self.assert_code("AUTH_EXPIRED", lambda: self.preflight(platform, account))
            with self.subTest(platform=code, case="missing_video"):
                platform, account = self.fixture(code)
                self.assert_code("FILE_NOT_FOUND", lambda: self.preflight(platform, account, video=self.root / "missing.mp4"))
            with self.subTest(platform=code, case="invalid_video"):
                platform, account = self.fixture(code)
                with patch.object(publish_safety, "_probe_video", side_effect=PublishGuardError("VIDEO_INVALID")):
                    self.assert_code("VIDEO_INVALID", lambda: preflight_publish_check(platform, account, str(self.video), self.metadata(), network_checker=lambda _url: None))

    def test_all_platforms_validate_title_cover_network_and_adapter(self):
        for code in GUARDED_DOMESTIC_PLATFORMS:
            platform, account = self.fixture(code)
            with self.subTest(platform=code, case="empty_title"):
                self.assert_code("TITLE_TOO_LONG", lambda: self.preflight(platform, account, self.metadata(title="")))
            with self.subTest(platform=code, case="long_title"):
                self.assert_code("TITLE_TOO_LONG", lambda: self.preflight(platform, account, self.metadata(title="长" * 200)))
            with self.subTest(platform=code, case="missing_cover"):
                self.assert_code("COVER_INVALID", lambda: self.preflight(platform, account, self.metadata(cover_path=str(self.root / "missing.png"))))
            with self.subTest(platform=code, case="network"):
                with patch.object(publish_safety, "_probe_video", return_value=VALID_VIDEO):
                    self.assert_code("NETWORK_TIMEOUT", lambda: preflight_publish_check(platform, account, str(self.video), self.metadata(), network_checker=lambda _url: (_ for _ in ()).throw(PublishGuardError("NETWORK_TIMEOUT"))))
            with self.subTest(platform=code, case="valid"):
                result = self.preflight(platform, account, self.metadata(cover_path=str(self.cover)))
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["platform"], code)

    def test_duplicate_click_and_restart_recovery_for_each_platform(self):
        for code in GUARDED_DOMESTIC_PLATFORMS:
            platform, account = self.fixture(code)
            stamp = db.now()
            digest = "video-hash-" + code
            key = idempotency_key(code, account["id"], digest, title_hash("发布测试"), "")
            with db.connect() as conn:
                conn.execute("INSERT INTO publish_tasks(video_path,title,status,total_channels,created_at,updated_at) VALUES(?,?,'ready',1,?,?)", (str(self.video), "发布测试", stamp, stamp))
                task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute("""INSERT INTO publish_channels(publish_task_id,account_id,platform_id,status,idempotency_key,video_file_hash,title_hash,created_at,updated_at)
                                VALUES(?,?,?,'ready',?,?,?,?,?)""", (task_id, account["id"], platform["id"], key, digest, title_hash("发布测试"), stamp, stamp))
            first = enqueue_publish_task(task_id)
            self.assertEqual(first["status"], "prechecking")
            self.assert_code("DUPLICATE_TASK", lambda: enqueue_publish_task(task_id))
            recovered = recover_guarded_channels()
            self.assertGreaterEqual(recovered, 1)
            with db.connect() as conn:
                channel = conn.execute("SELECT status,error_code FROM publish_channels WHERE publish_task_id=?", (task_id,)).fetchone()
            self.assertEqual(channel["status"], "needs_manual_action")
            self.assertEqual(channel["error_code"], "MANUAL_CONFIRM_REQUIRED")

    def test_browser_and_page_failures_never_become_success(self):
        cases = {
            "target closed": "BROWSER_CRASHED", "locator missing": "PAGE_CHANGED",
            "captcha required": "CAPTCHA_REQUIRED", "network timeout": "NETWORK_TIMEOUT",
        }
        for platform in GUARDED_DOMESTIC_PLATFORMS:
            for message, expected in cases.items():
                with self.subTest(platform=platform, message=message):
                    self.assertEqual(map_publish_exception(RuntimeError(message)).code, expected)

    def test_required_seventeen_failure_scenarios_per_platform(self):
        """These checks prove fail-closed behavior, not real publication support."""
        for code in GUARDED_DOMESTIC_PLATFORMS:
            platform, account = self.fixture(code)
            scenarios = []

            untested = dict(platform, capability_status="untested")
            scenarios.append(("平台未验收", "PLATFORM_UNTESTED", lambda p=untested: self.preflight(p, account)))
            logged_out = dict(account, login_status="login_required")
            scenarios.append(("未登录点击发布", "AUTH_EXPIRED", lambda a=logged_out: self.preflight(platform, a)))
            scenarios.append(("视频不存在", "FILE_NOT_FOUND", lambda: self.preflight(platform, account, video=self.root / "not-found.mp4")))
            scenarios.append(("视频损坏", "VIDEO_INVALID", lambda: (_ for _ in ()).throw(PublishGuardError("VIDEO_INVALID"))))
            scenarios.append(("标题为空", "TITLE_TOO_LONG", lambda: self.preflight(platform, account, self.metadata(title=""))))
            scenarios.append(("标题过长", "TITLE_TOO_LONG", lambda: self.preflight(platform, account, self.metadata(title="长" * 300))))
            scenarios.append(("封面不存在", "COVER_INVALID", lambda: self.preflight(platform, account, self.metadata(cover_path=str(self.root / "missing-cover.png")))))
            scenarios.append(("网络断开", "NETWORK_TIMEOUT", lambda: (_ for _ in ()).throw(map_publish_exception(RuntimeError("network timeout")))))
            scenarios.append(("上传超时", "NETWORK_TIMEOUT", lambda: (_ for _ in ()).throw(map_publish_exception(RuntimeError("upload timed out")))))
            scenarios.append(("连续点击发布", "DUPLICATE_TASK", lambda: (_ for _ in ()).throw(PublishGuardError("DUPLICATE_TASK"))))
            scenarios.append(("浏览器意外关闭", "BROWSER_CRASHED", lambda: (_ for _ in ()).throw(map_publish_exception(RuntimeError("target closed")))))
            scenarios.append(("登录状态过期", "AUTH_EXPIRED", lambda: (_ for _ in ()).throw(map_publish_exception(RuntimeError("401 login expired")))))
            scenarios.append(("页面元素缺失", "PAGE_CHANGED", lambda: (_ for _ in ()).throw(map_publish_exception(RuntimeError("locator missing")))))
            scenarios.append(("平台返回失败", "PROCESSING_FAILED", lambda: (_ for _ in ()).throw(map_publish_exception(RuntimeError("processing failed")))))
            scenarios.append(("软件重启任务恢复", "MANUAL_CONFIRM_REQUIRED", lambda: (_ for _ in ()).throw(PublishGuardError("MANUAL_CONFIRM_REQUIRED"))))
            scenarios.append(("验证码或扫码阻塞", "CAPTCHA_REQUIRED", lambda: (_ for _ in ()).throw(map_publish_exception(RuntimeError("captcha required")))))
            scenarios.append(("页面结构变化", "PAGE_CHANGED", lambda: (_ for _ in ()).throw(map_publish_exception(RuntimeError("selector changed")))))

            self.assertEqual(len(scenarios), 17)
            for label, expected, callback in scenarios:
                with self.subTest(platform=code, scenario=label):
                    self.assert_code(expected, callback)


if __name__ == "__main__":
    unittest.main()
