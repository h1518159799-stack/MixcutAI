import json
import base64
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import license_module
from admin_tools.issue_license import issue


MACHINE_A = "MX-AAAA-BBBB-CCCC-DDDD-EEEE-FFFF"
MACHINE_B = "MX-1111-2222-3333-4444-5555-6666"


class LicenseModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.license_file = root / "license.dat"
        self.clock_file = root / "license_clock.json"
        self.private_key_file = root / "test_private_key.pem"
        private_key = Ed25519PrivateKey.generate()
        self.private_key_file.write_bytes(private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ))
        public_b64 = base64.b64encode(private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )).decode("ascii")
        self.patchers = [
            patch.object(license_module, "LICENSE_FILE", self.license_file),
            patch.object(license_module, "CLOCK_FILE", self.clock_file),
            patch.object(license_module, "_cache_payload", lambda *args, **kwargs: None),
            patch.object(license_module, "_settings", lambda: {}),
            patch.object(license_module, "_read_clock_state", lambda: json.loads(self.clock_file.read_text()) if self.clock_file.exists() else {}),
            patch.object(license_module, "_write_clock_state", lambda state: self.clock_file.write_text(json.dumps(state))),
            patch.object(license_module, "PUBLIC_KEY_B64", public_b64),
        ]
        for item in self.patchers:
            item.start()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def activate(self, license_type="permanent", expire_date=""):
        token = issue(MACHINE_A, license_type, expire_date, self.private_key_file)
        return token, license_module.activate_license(token, MACHINE_A)

    def test_machine_id_is_stable_and_formatted(self):
        first = license_module.machine_id()
        self.assertEqual(first, license_module.machine_id())
        self.assertRegex(first, r"^MX-(?:[A-F0-9]{4}-){5}[A-F0-9]{4}$")

    def test_signed_license_activates_and_restarts_offline(self):
        _, state = self.activate()
        self.assertTrue(state.valid)
        restarted = license_module.check_license(MACHINE_A, allow_online=False)
        self.assertTrue(restarted.valid)
        self.assertEqual(restarted.license_type, "permanent")

    def test_tampered_signature_is_rejected(self):
        token = issue(MACHINE_A, "permanent", private_key_path=self.private_key_file)
        prefix, payload, signature = token.split(".")
        raw = bytearray(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
        raw[0] ^= 1
        altered_signature = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
        altered = f"{prefix}.{payload}.{altered_signature}"
        with self.assertRaisesRegex(ValueError, "签名"):
            license_module.activate_license(altered, MACHINE_A)

    def test_other_machine_is_rejected(self):
        token = issue(MACHINE_B, "permanent", private_key_path=self.private_key_file)
        with self.assertRaisesRegex(ValueError, "其他机器"):
            license_module.activate_license(token, MACHINE_A)

    def test_expired_signed_license_is_rejected(self):
        token = issue(MACHINE_A, "month", "2020-01-01", self.private_key_file)
        with self.assertRaisesRegex(ValueError, "到期"):
            license_module.activate_license(token, MACHINE_A)

    def test_clock_rollback_is_detected(self):
        self.activate()
        future = datetime.now(timezone.utc) + timedelta(days=2)
        self.clock_file.write_text(json.dumps({"last_seen_utc": future.isoformat().replace("+00:00", "Z")}))
        state = license_module.check_license(MACHINE_A, current_time=datetime.now(timezone.utc), allow_online=False)
        self.assertFalse(state.valid)
        self.assertEqual(state.status, "clock_rollback")

    def test_database_cache_without_license_dat_is_not_trusted(self):
        state = license_module.check_license(MACHINE_A, allow_online=False)
        self.assertFalse(state.valid)
        self.assertEqual(state.status, "missing")

    def test_server_outage_allows_configured_offline_grace(self):
        self.activate("month", (datetime.now(timezone.utc).date() + timedelta(days=20)).isoformat())
        now = datetime.now(timezone.utc)
        self.clock_file.write_text(json.dumps({
            "last_seen_utc": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "last_server_check_utc": (now - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        }))
        with patch.object(license_module, "_settings", lambda: {"license_server_url": "https://license.example", "license_offline_grace_days": "3"}), \
             patch.object(license_module, "_server_verify", side_effect=ConnectionError("offline")):
            state = license_module.check_license(MACHINE_A, current_time=now)
        self.assertTrue(state.valid)

    def test_server_outage_rejects_after_offline_grace(self):
        self.activate("month", (datetime.now(timezone.utc).date() + timedelta(days=20)).isoformat())
        now = datetime.now(timezone.utc)
        self.clock_file.write_text(json.dumps({
            "last_seen_utc": (now - timedelta(days=5)).isoformat().replace("+00:00", "Z"),
            "last_server_check_utc": (now - timedelta(days=5)).isoformat().replace("+00:00", "Z"),
        }))
        with patch.object(license_module, "_settings", lambda: {"license_server_url": "https://license.example", "license_offline_grace_days": "3"}), \
             patch.object(license_module, "_server_verify", side_effect=ConnectionError("offline")):
            state = license_module.check_license(MACHINE_A, current_time=now)
        self.assertFalse(state.valid)
        self.assertEqual(state.status, "offline_grace_expired")

    def test_short_offline_tokens_are_signed_machine_bound_and_precise(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        for kind, duration in (("day1", timedelta(hours=24)), ("day7", timedelta(days=7))):
            token = issue(MACHINE_A, kind, private_key_path=self.private_key_file)
            payload = license_module.verify_signed_token(token)
            self.assertEqual(payload["type"], kind)
            self.assertIn("activated_at", payload)
            expires = license_module._parse_utc(payload["expire_date"])
            activated = license_module._parse_utc(payload["activated_at"])
            self.assertEqual(expires - activated, duration)
            self.assertLess(abs((activated - now).total_seconds()), 5)
            self.license_file.unlink(missing_ok=True)
            state = license_module.activate_license(token, MACHINE_A)
            self.assertTrue(state.valid)
            self.assertEqual(state.license_type, kind)
            self.assertGreater(state.seconds_left, 0)
            self.license_file.unlink(missing_ok=True)
            with self.assertRaisesRegex(ValueError, "其他机器"):
                license_module.activate_license(token, MACHINE_B)

    def test_short_license_expires_at_exact_utc_instant(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        expiry = now + timedelta(minutes=30)
        token = issue(MACHINE_A, "day1", expiry.isoformat().replace("+00:00", "Z"), self.private_key_file)
        license_module.save_license_token(token)
        before = license_module.check_license(MACHINE_A, current_time=expiry - timedelta(seconds=1), allow_online=False)
        at_expiry = license_module.check_license(MACHINE_A, current_time=expiry, allow_online=False)
        self.assertTrue(before.valid)
        self.assertFalse(at_expiry.valid)
        self.assertEqual(at_expiry.status, "expired")

    def test_tampered_short_tokens_are_rejected(self):
        for kind in ("day1", "day7"):
            token = issue(MACHINE_A, kind, private_key_path=self.private_key_file)
            prefix, payload, signature = token.split(".")
            raw = bytearray(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            raw[-2] ^= 1
            altered_payload = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
            with self.assertRaisesRegex(ValueError, "签名"):
                license_module.activate_license(f"{prefix}.{altered_payload}.{signature}", MACHINE_A)

    def test_legacy_types_remain_supported(self):
        future = (datetime.now(timezone.utc).date() + timedelta(days=20)).isoformat()
        for kind, expiry in (("month", future), ("year", future), ("permanent", "")):
            self.license_file.unlink(missing_ok=True)
            token = issue(MACHINE_A, kind, expiry, self.private_key_file)
            state = license_module.activate_license(token, MACHINE_A)
            self.assertTrue(state.valid)
            self.assertEqual(state.license_type, kind)


if __name__ == "__main__":
    unittest.main()
