import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

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
            self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM activation_logs WHERE machine_id=?", (machine,)).fetchone()[0], 3)

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


if __name__ == "__main__":
    unittest.main()

