import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import browser_profile_service
import commercial_api
import db
import domestic_login_service as domestic


class DomesticLoginServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile_root = self.root / "data" / "browser_profiles"
        self.log_root = self.root / "data" / "logs"
        (self.root / "config").mkdir(parents=True)
        self.patchers = [
            patch.object(db, "DB_PATH", self.root / "data" / "mixcut.db"),
            patch.object(db, "DATA_DIR", self.root / "data"),
            patch.object(db, "THUMB_DIR", self.root / "data" / "thumbnails"),
            patch.object(db, "VOICE_DIR", self.root / "data" / "voices"),
            patch.object(db, "CONFIG_DIR", self.root / "config"),
            patch.object(db, "OUTPUT_DIR", self.root / "outputs"),
            patch.object(browser_profile_service, "BROWSER_PROFILE_DIR", self.profile_root),
            patch.object(domestic, "BROWSER_PROFILE_DIR", self.profile_root),
            patch.object(domestic, "LOG_DIR", self.log_root),
        ]
        for item in self.patchers:
            item.start()
        db._init_db_impl()
        domestic.reset_domestic_login_state_for_tests()

    def tearDown(self):
        domestic.reset_domestic_login_state_for_tests()
        time.sleep(0.02)
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def _account(self, code, name="测试账号"):
        platform = commercial_api.row("SELECT * FROM platforms WHERE code=?", (code,))
        account_id = commercial_api.save_publishing_account({"platform_id": platform["id"], "account_name": name})
        return platform, commercial_api.row("SELECT * FROM publish_accounts WHERE id=?", (account_id,))

    @staticmethod
    def _cookies(names, domain):
        return [{"name": name, "domain": domain} for name in names]

    def test_xiaohongshu_real_creator_session_is_authenticated(self):
        cookies = self._cookies(
            ["access-token-creator.xiaohongshu.com", "galaxy_creator_session_id"],
            ".xiaohongshu.com",
        )
        self.assertTrue(domestic.authenticated_platform_page(
            "xiaohongshu", cookies, "https://creator.xiaohongshu.com/new/home",
            "小红书创作服务平台", "发布笔记 首页 笔记管理 数据看板",
        ))

    def test_xiaohongshu_legacy_web_session_cookie_is_not_enough(self):
        cookies = self._cookies(["web_session"], ".xiaohongshu.com")
        self.assertFalse(domestic.authenticated_platform_page(
            "xiaohongshu", cookies, "https://creator.xiaohongshu.com/new/home", visible_text="发布笔记 笔记管理",
        ))

    def test_xiaohongshu_login_page_is_never_success(self):
        cookies = self._cookies(
            ["access-token-creator.xiaohongshu.com", "galaxy_creator_session_id"], ".xiaohongshu.com",
        )
        self.assertFalse(domestic.authenticated_platform_page(
            "xiaohongshu", cookies, "https://creator.xiaohongshu.com/login", visible_text="扫码登录",
        ))

    def test_wechat_authenticated_platform_route(self):
        cookies = self._cookies(["sessionid", "wxuin"], "channels.weixin.qq.com")
        self.assertTrue(domestic.authenticated_platform_page(
            "wechat_channels", cookies, "https://channels.weixin.qq.com/platform/post/list",
            visible_text="发表动态 内容管理 数据中心",
        ))

    def test_wechat_accepts_each_reliable_login_signal(self):
        route_only = self._cookies([], "channels.weixin.qq.com")
        self.assertTrue(domestic.authenticated_platform_page(
            "wechat_channels", route_only,
            "https://channels.weixin.qq.com/platform/post/list",
        ))
        cookie_only = self._cookies(["sessionid"], ".channels.weixin.qq.com")
        self.assertTrue(domestic.authenticated_platform_page(
            "wechat_channels", cookie_only,
            "https://channels.weixin.qq.com/",
        ))
        self.assertTrue(domestic.authenticated_platform_page(
            "wechat_channels", [], "https://channels.weixin.qq.com/",
            visible_text="视频管理",
        ))

    def test_wechat_login_shell_rejects_stale_single_cookie(self):
        cookies = self._cookies(["sessionid"], ".channels.weixin.qq.com")
        self.assertFalse(domestic.authenticated_platform_page(
            "wechat_channels", cookies, "https://channels.weixin.qq.com/login.html",
            visible_text="登录视频号助手",
        ))

    def test_wechat_confirmation_timeout_is_bounded(self):
        self.assertEqual(domestic.LOGIN_VERIFY_TIMEOUT_SECONDS, 60)

    def test_wechat_stale_cookies_on_login_page_are_not_success(self):
        cookies = self._cookies(["sessionid", "wxuin"], "channels.weixin.qq.com")
        self.assertFalse(domestic.authenticated_platform_page(
            "wechat_channels", cookies, "https://channels.weixin.qq.com/login.html",
            visible_text="登录视频号助手 加载失败 点击重试 内容管理 数据中心",
        ))

    def test_wechat_restart_probe_targets_authenticated_platform_route(self):
        self.assertEqual(
            domestic.PROBE_URLS["wechat_channels"],
            "https://channels.weixin.qq.com/platform",
        )
        self.assertNotEqual(
            domestic.PROBE_URLS["wechat_channels"],
            domestic.LOGIN_URLS["wechat_channels"],
        )

    def test_domestic_accounts_use_account_id_profile_path(self):
        for code in ("xiaohongshu", "wechat_channels"):
            _platform, account = self._account(code, f"{code}-账号")
            self.assertEqual(Path(account["browser_profile_path"]), self.profile_root / code / str(account["id"]))

    def test_legacy_profile_is_moved_without_deleting_session_data(self):
        legacy = self.profile_root / "xiaohongshu" / "旧账号名"
        legacy.mkdir(parents=True)
        (legacy / "profile-marker.txt").write_text("keep", encoding="utf-8")
        target = domestic.ensure_domestic_profile(81, "xiaohongshu", legacy)
        self.assertEqual(target, (self.profile_root / "xiaohongshu" / "81").resolve())
        self.assertEqual((target / "profile-marker.txt").read_text(encoding="utf-8"), "keep")

    def test_two_accounts_never_share_profile(self):
        _platform, first = self._account("xiaohongshu", "一号")
        _platform, second = self._account("xiaohongshu", "二号")
        self.assertNotEqual(first["browser_profile_path"], second["browser_profile_path"])

    def test_two_wechat_accounts_keep_separate_profiles(self):
        _platform, first = self._account("wechat_channels", "视频号一号")
        _platform, second = self._account("wechat_channels", "视频号二号")
        self.assertNotEqual(first["browser_profile_path"], second["browser_profile_path"])
        self.assertEqual(Path(first["browser_profile_path"]).name, str(first["id"]))
        self.assertEqual(Path(second["browser_profile_path"]).name, str(second["id"]))

    def test_wechat_relogin_and_restart_keep_canonical_binding(self):
        platform, account = self._account("wechat_channels", "重新登录账号")
        domestic._mark_account(account["id"], "login_pending", "", profile_path=account["browser_profile_path"])
        domestic._mark_account(account["id"], "logged_in", "", profile_path=account["browser_profile_path"], success=True)
        saved = commercial_api.row(
            """SELECT a.*,p.code platform_code FROM publish_accounts a
               JOIN platforms p ON p.id=a.platform_id WHERE a.id=?""",
            (account["id"],),
        )
        domestic.reset_domestic_login_state_for_tests()
        status = domestic.domestic_login_status(account["id"], saved)
        self.assertEqual(saved["platform_id"], platform["id"])
        self.assertEqual(saved["platform_code"], "wechat_channels")
        self.assertEqual(status["status"], "logged_in")
        self.assertEqual(status["profile_path"], account["browser_profile_path"])

    def test_http_start_returns_while_background_session_remains_alive(self):
        _platform, account = self._account("xiaohongshu")
        entered = threading.Event()

        def fake_runner(session):
            domestic._set_state(session, "waiting_login", "正在等待用户完成小红书登录", context_status="browser_open")
            entered.set()
            session.stop_event.wait(3)

        with patch.object(domestic, "find_chromium", return_value=Path("chrome.exe")), patch.object(domestic, "_run_login", side_effect=fake_runner):
            result = domestic.launch_domestic_login(account["id"], "xiaohongshu", account["browser_profile_path"])
            self.assertTrue(entered.wait(1))
            self.assertIn(result["status"], {"starting", "waiting_login"})
            self.assertTrue(domestic._SESSIONS[account["id"]].thread.is_alive())
            domestic.cancel_domestic_login(account["id"])

    def test_same_account_cannot_open_second_session(self):
        _platform, account = self._account("wechat_channels")

        def fake_runner(session):
            session.stop_event.wait(3)

        with patch.object(domestic, "find_chromium", return_value=Path("chrome.exe")), patch.object(domestic, "_run_login", side_effect=fake_runner):
            first = domestic.launch_domestic_login(account["id"], "wechat_channels", account["browser_profile_path"])
            second = domestic.launch_domestic_login(account["id"], "wechat_channels", account["browser_profile_path"])
            self.assertEqual(first["session_id"], second["session_id"])
            self.assertTrue(second["already_open"])
            domestic.cancel_domestic_login(account["id"])

    def test_cancel_sets_stop_event_and_preserves_profile(self):
        _platform, account = self._account("wechat_channels")
        session = domestic.BrowserLoginSession(
            session_id="cancel-test", account_id=account["id"], platform="wechat_channels",
            profile_path=account["browser_profile_path"], browser_path="chrome.exe",
        )
        session.thread = Mock(is_alive=lambda: True)
        with domestic._LOCK:
            domestic._SESSIONS[account["id"]] = session
        result = domestic.cancel_domestic_login(account["id"])
        self.assertTrue(session.stop_event.is_set())
        self.assertTrue(Path(account["browser_profile_path"]).is_dir())
        self.assertNotEqual(result["status"], "logged_in")

    def test_login_success_updates_db_without_changing_capability(self):
        platform, account = self._account("xiaohongshu")
        domestic._mark_account(account["id"], "logged_in", "", profile_path=account["browser_profile_path"], success=True)
        saved = commercial_api.row("SELECT * FROM publish_accounts WHERE id=?", (account["id"],))
        capability = commercial_api.row("SELECT capability_status FROM platforms WHERE id=?", (platform["id"],))
        self.assertEqual(saved["login_status"], "logged_in")
        self.assertTrue(saved["last_checked_at"])
        self.assertEqual(capability["capability_status"], "untested")

    def test_login_success_does_not_forge_oauth_or_publish_state(self):
        _platform, account = self._account("wechat_channels")
        domestic._mark_account(account["id"], "logged_in", "", profile_path=account["browser_profile_path"], success=True)
        saved = commercial_api.row("SELECT * FROM publish_accounts WHERE id=?", (account["id"],))
        self.assertEqual(saved["authorization_status"], "not_authorized")

    def test_session_status_survives_followup_requests(self):
        _platform, account = self._account("xiaohongshu")
        session = domestic.BrowserLoginSession(
            session_id="status-test", account_id=account["id"], platform="xiaohongshu",
            profile_path=account["browser_profile_path"], browser_path="chrome.exe", login_status="waiting_login",
        )
        session.thread = Mock(is_alive=lambda: True)
        with domestic._LOCK:
            domestic._SESSIONS[account["id"]] = session
        first = domestic.domestic_login_status(account["id"], account)
        second = domestic.domestic_login_status(account["id"], account)
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(second["status"], "waiting_login")

    def test_commercial_api_dispatches_only_target_platforms_to_new_manager(self):
        _platform, account = self._account("xiaohongshu")
        with patch.object(commercial_api, "launch_domestic_login", return_value={"status": "waiting_login"}) as launch:
            result = commercial_api.start_account_login(account["id"])
        launch.assert_called_once()
        self.assertEqual(result["status"], "waiting_login")

    def test_frontend_polls_and_refetches_after_success(self):
        source = Path("ui-commercial/src/PublishAccountsPage.jsx").read_text(encoding="utf-8")
        self.assertIn("BrowserPlatformLoginDialog", source)
        self.assertIn("login/status", source)
        self.assertIn("setInterval(poll,2000)", source)
        self.assertIn("await refresh(false)", source)

    def test_error_codes_have_clear_chinese_messages(self):
        expected = {
            "BROWSER_LAUNCH_FAILED", "PROFILE_LOCKED", "LOGIN_TIMEOUT", "LOGIN_VERIFY_TIMEOUT", "LOGIN_PAGE_CHANGED",
            "LOGIN_BROWSER_CLOSED", "LOGIN_VERIFY_FAILED", "NETWORK_ERROR",
        }
        self.assertEqual(expected, set(domestic.ERROR_MESSAGES))
        self.assertTrue(all(any("\u4e00" <= char <= "\u9fff" for char in message) for message in domestic.ERROR_MESSAGES.values()))


if __name__ == "__main__":
    unittest.main()
