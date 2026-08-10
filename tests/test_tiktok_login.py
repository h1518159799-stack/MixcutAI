import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import browser_profile_service
import commercial_api
import db
import tiktok_login_service as tiktok


class TikTokLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
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
        for item in self.patchers:
            item.start()
        db._init_db_impl()
        with tiktok._LOCK:
            tiktok._SESSIONS.clear()
            tiktok._LAST_RESULTS.clear()

    def tearDown(self):
        with tiktok._LOCK:
            for session in tiktok._SESSIONS.values():
                session.stop_event.set()
            tiktok._SESSIONS.clear()
            tiktok._LAST_RESULTS.clear()
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def _account(self, name="TikTok测试账号"):
        platform = commercial_api.row("SELECT * FROM platforms WHERE code='tiktok'")
        account_id = commercial_api.save_publishing_account({"platform_id": platform["id"], "account_name": name})
        return platform, commercial_api.row("SELECT * FROM publish_accounts WHERE id=?", (account_id,))

    def test_login_modes_use_current_official_pages(self):
        self.assertEqual(tiktok.login_url_for_mode("qr"), "https://www.tiktok.com/login/qrcode")
        self.assertEqual(tiktok.login_url_for_mode("account"), "https://www.tiktok.com/login/phone-or-email/email")
        self.assertEqual(tiktok.normalize_login_mode("unknown"), "qr")

    def test_qr_failure_is_actionable(self):
        state = tiktok.classify_tiktok_page(
            "https://www.tiktok.com/login/qrcode",
            "无法登录，请尝试其它登录方式。",
        )
        self.assertEqual(state[0], "qr_unavailable")
        self.assertEqual(state[1], "TIKTOK_QR_LOGIN_UNAVAILABLE")
        self.assertIn("账号登录", state[2])

    def test_verification_is_never_treated_as_success(self):
        state = tiktok.classify_tiktok_page(
            "https://www.tiktok.com/login/challenge",
            "Security verification",
        )
        self.assertEqual(state[0], "verification_required")
        cookies = [{"domain": ".tiktok.com", "name": "sessionid"}]
        self.assertFalse(tiktok.authenticated_page(cookies, "https://www.tiktok.com/login/challenge", 3))

    def test_auth_cookie_alone_is_not_enough(self):
        cookies = [{"domain": ".tiktok.com", "name": "sessionid"}]
        self.assertFalse(tiktok.authenticated_page(cookies, "https://www.tiktok.com/", 0))
        self.assertTrue(tiktok.authenticated_page(cookies, "https://www.tiktok.com/", 1))
        self.assertTrue(tiktok.authenticated_page(cookies, "https://www.tiktok.com/upload", 0))

    def test_each_account_has_an_independent_persistent_profile(self):
        _platform, first = self._account("TikTok一号")
        _platform, second = self._account("TikTok二号")
        self.assertNotEqual(first["browser_profile_path"], second["browser_profile_path"])
        self.assertTrue(Path(first["browser_profile_path"]).is_dir())
        self.assertTrue(Path(second["browser_profile_path"]).is_dir())

    def test_running_session_can_switch_login_mode_without_second_browser(self):
        _platform, account = self._account()
        session = tiktok.TikTokLoginSession(
            account_id=account["id"], profile_path=account["browser_profile_path"],
            browser_path="chrome.exe", mode="qr", requested_mode="qr",
        )
        session.thread = Mock(is_alive=lambda: True)
        with tiktok._LOCK:
            tiktok._SESSIONS[account["id"]] = session
        with patch.object(tiktok, "ensure_profile", return_value=Path(account["browser_profile_path"])), \
             patch.object(tiktok, "find_chromium", return_value=Path("chrome.exe")):
            result = tiktok.launch_tiktok_login(account["id"], account["browser_profile_path"], "account")
        self.assertEqual(session.requested_mode, "account")
        self.assertEqual(result["account_id"], account["id"])

    def test_login_state_and_capability_persist_without_oauth_confusion(self):
        platform, account = self._account()
        tiktok._mark_account(account["id"], "logged_in", "", username="tester", display_name="测试账号")
        saved = commercial_api.row("SELECT * FROM publish_accounts WHERE id=?", (account["id"],))
        self.assertEqual(saved["login_status"], "logged_in")
        self.assertEqual(saved["channel_id"], "tester")
        self.assertEqual(saved["channel_title"], "测试账号")
        self.assertEqual(saved["authorization_status"], "not_authorized")
        self.assertEqual(platform["capability_status"], "untested")

    def test_cancel_does_not_publish_or_fake_success(self):
        _platform, account = self._account()
        session = tiktok.TikTokLoginSession(
            account_id=account["id"], profile_path=account["browser_profile_path"],
            browser_path="chrome.exe", mode="qr", requested_mode="qr",
        )
        session.thread = Mock(is_alive=lambda: True)
        with tiktok._LOCK:
            tiktok._SESSIONS[account["id"]] = session
        result = tiktok.cancel_tiktok_login(account["id"])
        self.assertTrue(session.stop_event.is_set())
        self.assertNotEqual(result["status"], "logged_in")


if __name__ == "__main__":
    unittest.main()
