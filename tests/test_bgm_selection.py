import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import video_engine


class BgmSelectionTests(unittest.TestCase):
    def test_missing_stale_bgm_record_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "bgm.db"
            valid = root / "valid.mp3"
            valid.write_bytes(b"test")

            def connect():
                conn = sqlite3.connect(database, factory=db.ClosingConnection)
                conn.row_factory = sqlite3.Row
                return conn

            with connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE app_settings(key TEXT PRIMARY KEY,value TEXT);
                    CREATE TABLE bgm_library(id INTEGER PRIMARY KEY,name TEXT,file_path TEXT,volume_default REAL);
                    INSERT INTO app_settings VALUES('bgm_enabled','1');
                    INSERT INTO app_settings VALUES('default_bgm_id','');
                    INSERT INTO app_settings VALUES('bgm_dir','');
                    INSERT INTO app_settings VALUES('ffprobe_path','');
                    INSERT INTO app_settings VALUES('bgm_volume','0.15');
                    """
                )
                conn.execute("INSERT INTO bgm_library VALUES(1,'stale',?,0.15)", (str(root / "missing.mp3"),))
                conn.execute("INSERT INTO bgm_library VALUES(2,'valid',?,0.15)", (str(valid),))

            with patch.object(video_engine, "connect", connect):
                selected = video_engine.load_default_bgm()

            self.assertEqual(selected["source"], str(valid))
            self.assertEqual(selected["name"], "valid")


if __name__ == "__main__":
    unittest.main()
