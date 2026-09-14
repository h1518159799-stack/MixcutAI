import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import db


class DatabaseMigrationTests(unittest.TestCase):
    def test_backup_and_restore_preserve_original_database(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mixcut.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE sample(value TEXT)")
                conn.execute("INSERT INTO sample VALUES('original')")
                conn.execute("PRAGMA user_version=1")
                conn.commit()
            backup = db.create_migration_backup(path, 5)
            self.assertTrue(backup and backup.exists())
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("UPDATE sample SET value='broken'")
                conn.commit()
            db.restore_migration_backup(backup, path)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT value FROM sample").fetchone()[0], "original")
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_current_schema_does_not_create_redundant_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mixcut.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("PRAGMA user_version=5")
                conn.commit()
            self.assertIsNone(db.create_migration_backup(path, 5))

    def test_v25_migrates_only_known_legacy_production_urls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "data" / "mixcut.db"
            path.parent.mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE app_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)")
                conn.execute(
                    "INSERT INTO app_settings VALUES('license_server_url',?,?)",
                    ("https://47-107-104-106.sslip.io", "old"),
                )
                conn.execute(
                    "INSERT INTO app_settings VALUES('update_manifest_url',?,?)",
                    ("https://update.xn--fiq959cg4n10q.com/updates/update.json", "old"),
                )
                conn.execute("PRAGMA user_version=24")
                conn.commit()
            patches = (
                patch.object(db, "DB_PATH", path),
                patch.object(db, "DATA_DIR", root / "data"),
                patch.object(db, "THUMB_DIR", root / "data" / "thumbnails"),
                patch.object(db, "VOICE_DIR", root / "data" / "voices"),
                patch.object(db, "CONFIG_DIR", root / "config"),
            )
            for item in patches:
                item.start()
            try:
                db.init_db()
                with db.connect() as conn:
                    values = dict(conn.execute(
                        "SELECT key,value FROM app_settings WHERE key IN ('license_server_url','update_manifest_url')"
                    ).fetchall())
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
                self.assertEqual(values["license_server_url"], "https://license.ghwc.cn")
                self.assertEqual(values["update_manifest_url"], "https://update.ghwc.cn/update.json")
            finally:
                for item in reversed(patches):
                    item.stop()


if __name__ == "__main__":
    unittest.main()
