import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
