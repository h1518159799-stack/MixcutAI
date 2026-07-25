import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import browser_profile_service
import commercial_api
import db


class PublishAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile_root = self.root / "data" / "browser_profiles"
        (self.root / "config").mkdir(parents=True)
        self.patchers = [
            patch.object(db, "DB_PATH", self.root / "data" / "mixcut.db"),
            patch.object(db, "DATA_DIR", self.root / "data"),
            patch.object(db, "THUMB_DIR", self.root / "data" / "thumbnails"),
            patch.object(db, "VOICE_DIR", self.root / "data" / "voices"),
            patch.object(db, "CONFIG_DIR", self.root / "config"),
            patch.object(db, "OUTPUT_DIR", self.root / "outputs"),
            patch.object(browser_profile_service, "BROWSER_PROFILE_DIR", self.profile_root),
        ]
        for item in self.patchers:
            item.start()
        db._init_db_impl()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def test_platforms_are_database_driven(self):
        with db.connect() as conn:
            platforms = conn.execute("SELECT name,region,network_requirement,login_type,enabled FROM platforms ORDER BY id").fetchall()
        self.assertEqual(len(platforms), 10)
        self.assertEqual({row["name"] for row in platforms}, {"抖音", "小红书", "快手", "视频号", "B站", "TikTok", "YouTube", "Instagram Business", "Facebook Page", "X"})
        self.assertTrue(all(row["network_requirement"] == "该平台需要海外网络环境" for row in platforms if row["region"] == "overseas"))

    def test_account_gets_independent_profile_directory(self):
        platform = commercial_api.row("SELECT id FROM platforms WHERE code='douyin'")
        first = commercial_api.save_publishing_account({"platform_id": platform["id"], "account_name": "品牌主号"})
        second = commercial_api.save_publishing_account({"platform_id": platform["id"], "account_name": "品牌副号"})
        accounts = commercial_api.publishing_accounts()
        paths = {Path(item["browser_profile_path"]) for item in accounts}
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(path.is_dir() and self.profile_root in path.parents for path in paths))
        self.assertNotEqual(first, second)

    def test_auth_cookie_marks_account_logged_in(self):
        profile = self.profile_root / "bilibili" / "account-1"
        cookie_db = profile / "Default" / "Network" / "Cookies"
        cookie_db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(cookie_db)) as conn:
            conn.execute("CREATE TABLE cookies(host_key TEXT,name TEXT)")
            conn.execute("INSERT INTO cookies VALUES('.bilibili.com','SESSDATA')")
            conn.commit()
        result = browser_profile_service.detect_login_status(
            {"cookie_domains_json": '["bilibili.com"]', "login_cookie_names_json": '["SESSDATA"]'},
            {"browser_profile_path": str(profile)},
        )
        self.assertEqual(result["status"], "logged_in")
        self.assertEqual(result["auth_cookie_count"], 1)

    def test_login_launch_uses_the_account_profile(self):
        profile = self.profile_root / "youtube" / "account-9"
        fake_browser = self.root / "chrome.exe"
        fake_browser.write_bytes(b"")
        with patch.object(browser_profile_service, "find_chromium", return_value=fake_browser), \
             patch.object(browser_profile_service.subprocess, "Popen") as popen:
            result = browser_profile_service.launch_login(
                {"login_type": "browser_profile", "login_url": "https://accounts.google.com/"},
                {"browser_profile_path": str(profile)},
            )
        command = popen.call_args.args[0]
        self.assertIn(f"--user-data-dir={profile.resolve()}", command)
        self.assertEqual(result["profile_path"], str(profile.resolve()))

    def test_bundled_chromium_has_first_priority(self):
        bundled = self.root / "browser" / "chromium" / "chrome.exe"
        bundled.parent.mkdir(parents=True)
        bundled.write_bytes(b"chromium")
        system = self.root / "system-chrome.exe"
        system.write_bytes(b"chrome")
        with patch.object(browser_profile_service, "CHROMIUM_CANDIDATES", (bundled, system)):
            self.assertEqual(browser_profile_service.find_chromium(), bundled)


if __name__ == "__main__":
    unittest.main()
