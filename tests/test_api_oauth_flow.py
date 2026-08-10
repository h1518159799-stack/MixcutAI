import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import commercial_api
import db
import oauth_service
from oauth_token_store import unprotect


class ApiOAuthFlowTests(unittest.TestCase):
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
            patch.object(oauth_service.webbrowser, "open", return_value=True),
            patch("oauth_callback_server.ensure_callback_server", return_value=True),
            patch("oauth_callback_server.callback_server_status", return_value={"available": True, "running": False}),
        ]
        for item in self.patchers:
            item.start()
        db._init_db_impl()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def account(self, code, name):
        platform = commercial_api.row("SELECT * FROM platforms WHERE code=?", (code,))
        account_id = commercial_api.save_publishing_account({"platform_id": platform["id"], "account_name": name})
        return platform, commercial_api.row("SELECT * FROM publish_accounts WHERE id=?", (account_id,))

    def configure(self, provider, with_secret=True):
        data = {"client_id": f"{provider}-client"}
        if with_secret:
            data["client_secret"] = f"{provider}-secret"
        return oauth_service.save_provider_config(provider, data)

    def test_each_provider_has_independent_callback_and_preflight(self):
        expected = {
            "youtube": "/api/oauth/youtube/callback",
            "facebook_page": "/api/oauth/facebook-page/callback",
            "instagram_business": "/api/oauth/instagram-business/callback",
            "x": "/api/oauth/x/callback",
        }
        for provider, suffix in expected.items():
            self.configure(provider, provider != "x")
            result = oauth_service.provider_preflight(provider)
            self.assertTrue(result["developer_configured"])
            self.assertTrue(result["callback_uri"].endswith(suffix))
            self.assertNotIn(f"{provider}-secret", str(result))

    def test_secret_is_encrypted_and_not_returned(self):
        result = self.configure("youtube")
        stored = commercial_api.row("SELECT * FROM oauth_provider_configs WHERE provider='youtube'")
        self.assertNotIn("youtube-secret", stored["client_secret_encrypted"])
        self.assertEqual(unprotect(stored["client_secret_encrypted"]), "youtube-secret")
        self.assertNotIn("youtube-secret", str(result))

    def test_youtube_desktop_json_import_is_validated_and_sanitized(self):
        with self.assertRaisesRegex(ValueError, "桌面应用"):
            oauth_service.import_google_oauth_payload({"web": {"client_id": "wrong"}})
        result = oauth_service.import_google_oauth_payload({
            "installed": {"client_id": "google-client", "client_secret": "google-secret"}
        }, "client.json")
        self.assertEqual(result["message"], "Google OAuth 配置已导入")
        self.assertNotIn("google-secret", str(result))
        stored = commercial_api.row("SELECT * FROM oauth_provider_configs WHERE provider='youtube'")
        self.assertEqual(unprotect(stored["client_secret_encrypted"]), "google-secret")

    def test_authorization_waits_for_account_selection_and_keeps_untested(self):
        self.configure("youtube")
        platform, account = self.account("youtube", "频道占位")
        with patch.object(oauth_service, "_discover_accounts", return_value=[
            {"id": "channel-123456", "name": "品牌频道", "avatar": "", "kind": "YouTube频道"},
            {"id": "channel-654321", "name": "产品频道", "avatar": "", "kind": "YouTube频道"},
        ]), patch.object(oauth_service.get_adapter("youtube"), "exchange_token", return_value={
            "access_token": "access-value", "refresh_token": "refresh-value", "expires_in": 3600,
        }):
            started = oauth_service.start_authorization(platform, account)
            result = oauth_service.complete_authorization(started["state"], "code", "youtube")
        self.assertEqual(result["status"], "selecting_account")
        self.assertEqual(len(result["candidates"]), 2)
        bound = oauth_service.select_oauth_account(started["state"], "channel-654321")
        self.assertEqual(bound["status"], "authorized")
        self.assertEqual(bound["publication_capability"], "untested")
        token = commercial_api.row("SELECT * FROM oauth_tokens WHERE account_id=?", (account["id"],))
        self.assertEqual(unprotect(token["access_token_encrypted"]), "access-value")

    def test_state_provider_mismatch_and_single_use_are_rejected(self):
        self.configure("x", False)
        platform, account = self.account("x", "X账号")
        started = oauth_service.start_authorization(platform, account)
        with self.assertRaisesRegex(ValueError, "平台不匹配"):
            oauth_service.complete_authorization(started["state"], "code", "youtube")
        with patch.object(oauth_service, "_discover_accounts", return_value=[{"id":"x-1","name":"X User","kind":"X账号"}]), \
             patch.object(oauth_service.get_adapter("x"), "exchange_token", return_value={"access_token":"x-token"}):
            oauth_service.complete_authorization(started["state"], "code", "x")
        with self.assertRaisesRegex(ValueError, "已经使用"):
            oauth_service.complete_authorization(started["state"], "code", "x")

    def test_cancel_and_duplicate_binding(self):
        self.configure("facebook_page")
        platform, first = self.account("facebook", "主页一")
        _, second = self.account("facebook", "主页二")
        with db.connect() as conn:
            conn.execute("UPDATE publish_accounts SET channel_id='page-1',authorization_status='authorized' WHERE id=?", (first["id"],))
        started = oauth_service.start_authorization(platform, second)
        oauth_service.cancel_authorization(started["state"])
        self.assertEqual(oauth_service.session_status(started["state"])["status"], "authorization_cancelled")

        started = oauth_service.start_authorization(platform, second)
        with patch.object(oauth_service, "_discover_accounts", return_value=[
            {"id": "page-1", "name": "重复主页", "kind": "Facebook主页"},
        ]), patch.object(oauth_service.get_adapter("facebook"), "exchange_token", return_value={
            "access_token": "page-token", "expires_in": 3600,
        }):
            oauth_service.complete_authorization(started["state"], "code", "facebook_page")
        with self.assertRaisesRegex(ValueError, "已经添加"):
            oauth_service.select_oauth_account(started["state"], "page-1")

    def test_instagram_rejects_account_without_business_profile(self):
        self.configure("instagram_business")
        platform, account = self.account("instagram", "Instagram占位")
        started = oauth_service.start_authorization(platform, account)
        with patch.object(oauth_service, "_discover_accounts", return_value=[]), \
             patch.object(oauth_service.get_adapter("instagram"), "exchange_token", return_value={"access_token": "meta-token"}):
            with self.assertRaisesRegex(RuntimeError, "专业账号"):
                oauth_service.complete_authorization(started["state"], "code", "instagram_business")
        status = oauth_service.session_status(started["state"])
        self.assertEqual(status["status"], "authorization_failed")
        self.assertNotIn("meta-token", str(status))

    def test_x_pkce_and_missing_refresh_token_warning(self):
        self.configure("x", False)
        platform, account = self.account("x", "X占位")
        started = oauth_service.start_authorization(platform, account)
        session = commercial_api.row("SELECT * FROM oauth_sessions WHERE state=?", (started["state"],))
        self.assertTrue(session["code_verifier"])
        with patch.object(oauth_service, "_discover_accounts", return_value=[
            {"id": "x-user-1", "name": "X User", "username": "mixcut", "kind": "X账号"},
        ]), patch.object(oauth_service.get_adapter("x"), "exchange_token", return_value={"access_token": "x-token", "expires_in": 7200}):
            oauth_service.complete_authorization(started["state"], "code", "x")
        result = oauth_service.select_oauth_account(started["state"], "x-user-1")
        self.assertFalse(result["refresh_available"])
        self.assertIn("需要重新登录", result["message"])

    def test_expired_token_is_reported_without_exposing_token(self):
        self.configure("youtube")
        _, account = self.account("youtube", "过期频道")
        expired = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        stamp = db.now()
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO oauth_tokens(account_id,provider,access_token_encrypted,refresh_token_encrypted,
                   token_type,scope,expires_at,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (account["id"], "youtube", "ciphertext", "refresh-ciphertext", "Bearer", "", expired, "authorized", stamp, stamp),
            )
        status = oauth_service.provider_preflight("youtube", account["id"])
        self.assertEqual(status["authorization_status"], "token_expired")
        self.assertEqual(status["next_action"], "reauthorize")
        self.assertNotIn("ciphertext", str(status))

    def test_deleting_account_cascades_local_tokens(self):
        self.configure("x", False)
        platform, account = self.account("x", "待删除")
        stamp = db.now()
        with db.connect() as conn:
            conn.execute("INSERT INTO oauth_tokens(account_id,provider,access_token_encrypted,created_at,updated_at) VALUES(?,?,?,?,?)", (account["id"], "x", "encrypted", stamp, stamp))
            conn.execute("DELETE FROM oauth_tokens WHERE account_id=?", (account["id"],))
            conn.execute("UPDATE publish_accounts SET status='deleted' WHERE id=?", (account["id"],))
        self.assertIsNone(commercial_api.row("SELECT * FROM oauth_tokens WHERE account_id=?", (account["id"],)))


if __name__ == "__main__":
    unittest.main()
