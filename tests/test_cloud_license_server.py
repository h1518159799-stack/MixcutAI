import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class CloudLicenseServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        private = Ed25519PrivateKey.generate()
        key_path = root / "private.pem"
        key_path.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        pepper = root / "pepper"
        pepper.write_text("test-pepper-with-enough-entropy", encoding="ascii")
        secret = root / "session-secret"
        secret.write_text("a" * 96, encoding="ascii")
        os.environ.update({
            "MIXCUT_DATA_DIR": str(root / "data"),
            "MIXCUT_PRIVATE_KEY": str(key_path),
            "MIXCUT_PEPPER_FILE": str(pepper),
            "MIXCUT_SESSION_SECRET": str(secret),
        })
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server_deploy"))
        import license_server
        cls.server = license_server
        cls.server.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.client = self.server.app.test_client()

    def new_key(self, kind="month"):
        with self.server.connect() as conn:
            return self.server.create_card_keys(conn, kind, 1)[0]

    def post(self, path, payload):
        return self.client.post(path, json={"product_id": self.server.PRODUCT_ID, "app_version": "1.2.1", **payload})

    def test_required_schema_exists(self):
        with self.server.connect() as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"customers", "license_keys", "devices", "activation_logs"}.issubset(names))

    def test_activate_check_and_deactivate(self):
        key = self.new_key()
        machine = "MX-AAAA-BBBB-CCCC-DDDD-EEEE-FFFF"
        activate = self.post("/api/license/activate", {"key_code": key, "machine_id": machine, "device_name": "QA-PC", "os_info": "Windows"})
        self.assertEqual(activate.status_code, 200)
        token = activate.get_json()["data"]["license_token"]
        check = self.post("/api/license/check", {"license_token": token, "machine_id": machine})
        self.assertEqual(check.get_json()["data"]["status"], "active")
        deactivate = self.post("/api/license/deactivate", {"license_token": token, "machine_id": machine})
        self.assertEqual(deactivate.get_json()["data"]["status"], "deactivated")
        with self.server.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM devices WHERE machine_id=?", (machine,)).fetchone()[0], 1)
            masked = self.server.mask_machine_id(machine)
            self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM activation_logs WHERE machine_id=?", (masked,)).fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM activation_logs WHERE machine_id=?", (machine,)).fetchone()[0], 0)

    def test_frozen_license_is_rejected(self):
        key = self.new_key("year")
        with self.server.connect() as conn:
            conn.execute("UPDATE license_keys SET status='frozen' WHERE key_hash=?", (self.server.key_digest(key),))
        result = self.post("/api/license/activate", {"key_code": key, "machine_id": "MX-1111-2222-3333-4444-5555-6666"})
        self.assertEqual(result.status_code, 400)
        self.assertIn("冻结", result.get_json()["error"])

    def test_admin_login_and_dashboard(self):
        self.server.set_admin_password("admin", "Strong-Test-Password-2026")
        login = self.client.get("/admin/login")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', login.data).group(1).decode()
        response = self.client.post("/admin/login", data={"csrf_token": token, "username": "admin", "password": "Strong-Test-Password-2026"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("授权运营概览".encode(), response.data)

    def test_short_cards_start_on_first_activation_and_keep_expiry(self):
        machine = "MX-DAY1-AAAA-BBBB-CCCC-DDDD-EEEE"
        start = datetime(2026, 8, 11, 7, 30, tzinfo=timezone.utc)
        key = self.new_key("day1")
        with self.server.connect() as conn:
            issued = conn.execute("SELECT * FROM license_keys WHERE key_hash=?", (self.server.key_digest(key),)).fetchone()
            self.assertEqual(issued["status"], "issued")
            self.assertEqual(issued["duration_days"], 1)
            self.assertEqual(issued["activated_at"], "")
            self.assertEqual(issued["expire_date"], "")
        with patch.object(self.server, "utc_now", return_value=start):
            response = self.post("/api/license/activate", {"key_code": key, "machine_id": machine})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(data["type"], "day1")
        self.assertEqual(self.server.parse_utc(data["expire_date"]), start + timedelta(hours=24))
        original_expiry = data["expire_date"]
        token = data["license_token"]
        with patch.object(self.server, "utc_now", return_value=start + timedelta(hours=1)):
            checked = self.post("/api/license/check", {"license_token": token, "machine_id": machine})
        self.assertEqual(checked.status_code, 200)
        self.assertEqual(checked.get_json()["data"]["expire_date"], original_expiry)

    def test_day7_is_exact_seven_days_and_second_machine_is_rejected(self):
        first = "MX-DAY7-AAAA-BBBB-CCCC-DDDD-EEEE"
        second = "MX-DAY7-1111-2222-3333-4444-5555"
        start = datetime(2026, 8, 11, 7, 30, tzinfo=timezone.utc)
        key = self.new_key("day7")
        with patch.object(self.server, "utc_now", return_value=start):
            response = self.post("/api/license/activate", {"key_code": key, "machine_id": first})
        data = response.get_json()["data"]
        self.assertEqual(self.server.parse_utc(data["expire_date"]), start + timedelta(days=7))
        with patch.object(self.server, "utc_now", return_value=start + timedelta(seconds=1)):
            denied = self.post("/api/license/activate", {"key_code": key, "machine_id": second})
        self.assertEqual(denied.status_code, 400)
        self.assertIn("上限", denied.get_json()["error"])

    def test_short_card_expiry_freeze_and_reactivation_do_not_extend(self):
        machine = "MX-SHORT-AAAA-BBBB-CCCC-DDDD-EEEE"
        start = datetime(2026, 8, 11, 7, 30, tzinfo=timezone.utc)
        key = self.new_key("day1")
        with patch.object(self.server, "utc_now", return_value=start):
            activated = self.post("/api/license/activate", {"key_code": key, "machine_id": machine}).get_json()["data"]
        token = activated["license_token"]
        expiry = activated["expire_date"]
        with patch.object(self.server, "utc_now", return_value=start + timedelta(hours=1)):
            deactivated = self.post("/api/license/deactivate", {"license_token": token, "machine_id": machine})
        self.assertEqual(deactivated.status_code, 200)
        with patch.object(self.server, "utc_now", return_value=start + timedelta(hours=2)):
            reactivated = self.post("/api/license/activate", {"key_code": key, "machine_id": machine})
        self.assertEqual(reactivated.get_json()["data"]["expire_date"], expiry)
        with self.server.connect() as conn:
            row = conn.execute("SELECT id FROM license_keys WHERE key_hash=?", (self.server.key_digest(key),)).fetchone()
            conn.execute("UPDATE license_keys SET status='frozen' WHERE id=?", (row["id"],))
            frozen_expiry = conn.execute("SELECT expire_date FROM license_keys WHERE id=?", (row["id"],)).fetchone()[0]
            conn.execute("UPDATE license_keys SET status='active' WHERE id=?", (row["id"],))
            thawed_expiry = conn.execute("SELECT expire_date FROM license_keys WHERE id=?", (row["id"],)).fetchone()[0]
        self.assertEqual(frozen_expiry, thawed_expiry)
        with patch.object(self.server, "utc_now", return_value=start + timedelta(hours=24)):
            expired = self.post("/api/license/check", {"license_token": token, "machine_id": machine})
        self.assertEqual(expired.status_code, 400)
        self.assertIn("到期", expired.get_json()["error"])

    def test_admin_ui_and_cli_type_choices_include_short_cards(self):
        self.server.set_admin_password("short-admin", "Strong-Test-Password-2026")
        login = self.client.get("/admin/login")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', login.data).group(1).decode()
        response = self.client.post("/admin/login", data={"csrf_token": token, "username": "short-admin", "password": "Strong-Test-Password-2026"}, follow_redirects=True)
        self.assertIn('value="day1">1天卡'.encode(), response.data)
        self.assertIn('value="day7">7天卡'.encode(), response.data)
        self.assertEqual(tuple(self.server.VALID_TYPES), ("day1", "day7", "month", "year", "permanent"))
        for kind, label in (("day1", "1天卡"), ("day7", "7天卡")):
            csrf = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
            generated = self.client.post("/admin/licenses/generate", data={
                "csrf_token": csrf, "type": kind, "count": "1", "max_devices": "1", "customer_id": "",
            })
            self.assertEqual(generated.status_code, 200)
            self.assertIn("新卡密，请立即保存".encode(), generated.data)
            self.assertIn(label.encode(), generated.data)
            response = generated

    def test_legacy_check_constraint_migrates_without_changing_rows(self):
        legacy_path = Path(self.temp.name) / "legacy-licenses.db"
        import sqlite3
        conn = sqlite3.connect(legacy_path)
        conn.execute("""CREATE TABLE license_keys (
            id TEXT PRIMARY KEY,key_hash TEXT UNIQUE,key_hint TEXT,
            type TEXT CHECK(type IN ('month','year','permanent')),duration_days INTEGER,status TEXT,
            machine_id TEXT,activated_at TEXT,expire_date TEXT,unbind_count INTEGER,max_unbinds INTEGER,
            created_at TEXT,updated_at TEXT)""")
        conn.execute("INSERT INTO license_keys VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("legacy", "hash", "HINT", "month", 30, "active", "MX-LEGACY", "2026-01-01T00:00:00Z",
                      "2026-01-31", 0, 2, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        conn.commit(); conn.close()
        with patch.object(self.server, "DB_PATH", legacy_path):
            self.server.migrate_license_type_constraint()
        conn = sqlite3.connect(legacy_path)
        row = conn.execute("SELECT type,status,machine_id,activated_at,expire_date FROM license_keys WHERE id='legacy'").fetchone()
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='license_keys'").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()
        self.assertEqual(row, ("month", "active", "MX-LEGACY", "2026-01-01T00:00:00Z", "2026-01-31"))
        self.assertIn("'day1'", schema)
        self.assertEqual(integrity, "ok")


if __name__ == "__main__":
    unittest.main()
