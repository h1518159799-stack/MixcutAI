import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import commercial_api
import db


class TitleTemplateScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "config").mkdir(parents=True, exist_ok=True)
        self.patchers = [
            patch.object(db, "DB_PATH", root / "data" / "mixcut.db"),
            patch.object(db, "DATA_DIR", root / "data"),
            patch.object(db, "THUMB_DIR", root / "data" / "thumbnails"),
            patch.object(db, "VOICE_DIR", root / "data" / "voices"),
            patch.object(db, "CONFIG_DIR", root / "config"),
        ]
        for item in self.patchers:
            item.start()
        db._init_db_impl()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def test_domestic_and_overseas_are_saved_independently(self):
        commercial_api.save_title_template({
            "template_scope": "domestic",
            "config": {"title_main": "DOMESTIC", "title_font_size": "100", "title_color": "#111111", "subtitle_margin_bottom": "400"},
        })
        commercial_api.save_title_template({
            "template_scope": "overseas",
            "config": {"title_main": "OVERSEAS", "title_font_size": "130", "title_color": "#EEEEEE", "subtitle_margin_bottom": "650"},
        })
        domestic = commercial_api.title_template_config("domestic")
        overseas = commercial_api.title_template_config("overseas")
        self.assertEqual((domestic["title_main"], domestic["title_font_size"], domestic["title_color"], domestic["subtitle_margin_bottom"]), ("DOMESTIC", "100", "#111111", "400"))
        self.assertEqual((overseas["title_main"], overseas["title_font_size"], overseas["title_color"], overseas["subtitle_margin_bottom"]), ("OVERSEAS", "130", "#EEEEEE", "650"))

    def test_activation_projects_only_selected_scope_to_render_settings(self):
        commercial_api.save_title_template({"template_scope": "domestic", "config": {"title_font_size": "100"}})
        commercial_api.save_title_template({"template_scope": "overseas", "config": {"title_font_size": "130"}})
        commercial_api.activate_title_template("domestic")
        values = commercial_api.settings()
        self.assertEqual(values["title_style_preset"], "domestic")
        self.assertEqual(values["title_font_size"], "100")
        commercial_api.activate_title_template("overseas")
        values = commercial_api.settings()
        self.assertEqual(values["title_style_preset"], "overseas")
        self.assertEqual(values["title_font_size"], "130")

    def test_restart_and_reset_do_not_cross_overwrite(self):
        commercial_api.save_title_template({"template_scope": "domestic", "config": {"title_font_size": "100", "subtitle_color": "#AAAAAA"}})
        commercial_api.save_title_template({"template_scope": "overseas", "config": {"title_font_size": "130", "subtitle_color": "#BBBBBB"}})
        db._init_db_impl()  # application restart / idempotent migration
        self.assertEqual(commercial_api.title_template_config("domestic")["title_font_size"], "100")
        self.assertEqual(commercial_api.title_template_config("overseas")["title_font_size"], "130")
        commercial_api.reset_title_template("domestic")
        self.assertEqual(commercial_api.title_template_config("domestic")["title_font_size"], "118")
        self.assertEqual(commercial_api.title_template_config("overseas")["title_font_size"], "130")
        self.assertEqual(commercial_api.title_template_config("overseas")["subtitle_color"], "#BBBBBB")

    def test_legacy_global_values_migrate_once_to_domestic(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM app_settings WHERE key LIKE 'domestic_%' OR key LIKE 'overseas_%' OR key='title_template_scope_migration_v2'")
            conn.execute("UPDATE app_settings SET value='旧版国内标题' WHERE key='title_main'")
            conn.execute("UPDATE app_settings SET value='101' WHERE key='title_font_size'")
        db._init_db_impl()
        domestic = commercial_api.title_template_config("domestic")
        self.assertEqual(domestic["title_main"], "旧版国内标题")
        self.assertEqual(domestic["title_font_size"], "101")
        with db.connect() as conn:
            conn.execute("UPDATE app_settings SET value='999' WHERE key='title_font_size'")
        db._init_db_impl()
        self.assertEqual(commercial_api.title_template_config("domestic")["title_font_size"], "101")

    def test_frontend_uses_explicit_scope_and_readable_strategy_cards(self):
        source = (Path(__file__).parents[1] / "ui-commercial" / "src" / "main.jsx").read_text(encoding="utf-8")
        css = (Path(__file__).parents[1] / "ui-commercial" / "src" / "studio-unified.css").read_text(encoding="utf-8")
        self.assertIn("template_scope:preset", source)
        self.assertIn("保存为${preset", source)
        self.assertIn("严格避免重复", source)
        self.assertIn("height:auto!important", css)
        self.assertIn("overflow:visible!important", css)
        self.assertIn("grid-template-columns:repeat(2", css)
        self.assertIn("moveTitleBlock", source)
        self.assertIn("整体上下位置", source)
        self.assertIn("title-position-range", css)


if __name__ == "__main__":
    unittest.main()
