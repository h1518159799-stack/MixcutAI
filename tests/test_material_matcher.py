import random
import sqlite3
import unittest

from material_matcher import load_material_candidates, material_pool_diagnostics, select_material


def material(mid, *, usage=0, last="", width=1080, height=1920):
    return {
        "id": mid, "file_name": f"{mid}.mp4", "duration": 20,
        "width": width, "height": height,
        "usage_count": usage, "last_used_at": last,
    }


class MaterialSelectionTests(unittest.TestCase):
    def test_prefer_unused_is_strictly_ahead_of_used(self):
        selected, info = select_material(
            [material(1, usage=8), material(2, usage=0)], strategy="prefer_unused", rng=random.Random(1)
        )
        self.assertEqual(selected["id"], 2)
        self.assertEqual(info["selection_reason"], "priority_unused")

    def test_least_usage_wins_when_everything_was_used(self):
        selected, _ = select_material(
            [material(1, usage=8), material(2, usage=2), material(3, usage=4)],
            strategy="prefer_unused", rng=random.Random(2),
        )
        self.assertEqual(selected["id"], 2)

    def test_oldest_last_used_wins_inside_same_usage_count(self):
        selected, _ = select_material(
            [material(1, usage=2, last="2026-03-01"), material(2, usage=2, last="2026-01-01")],
            strategy="prefer_unused", rng=random.Random(2),
        )
        self.assertEqual(selected["id"], 2)

    def test_equal_tier_is_randomized(self):
        candidates = [material(1), material(2), material(3), material(4)]
        chosen = {
            select_material(candidates, strategy="prefer_unused", rng=random.Random(seed))[0]["id"]
            for seed in range(20)
        }
        self.assertGreater(len(chosen), 1)

    def test_avoid_repeat_excludes_prior_batch_ids(self):
        selected, info = select_material(
            [material(1), material(2)], strategy="avoid_repeat", avoid_ids={1}, rng=random.Random(1)
        )
        self.assertEqual(selected["id"], 2)
        self.assertFalse(info["batch_reuse_fallback"])

    def test_insufficient_pool_allows_explicit_fallback(self):
        selected, info = select_material(
            [material(1)], strategy="avoid_repeat", avoid_ids={1}, rng=random.Random(1)
        )
        self.assertEqual(selected["id"], 1)
        self.assertTrue(info["batch_reuse_fallback"])

    def test_current_video_exclusion_is_respected(self):
        selected, _ = select_material(
            [material(1), material(2)], strategy="random", exclude_ids={1}, rng=random.Random(1)
        )
        self.assertEqual(selected["id"], 2)

    def test_1080_vertical_is_not_suppressed_by_4k(self):
        candidates = [material(1, width=3840, height=2160), material(2, width=1080, height=1920)]
        info = select_material(candidates, strategy="prefer_unused", rng=random.Random(0))[1]
        self.assertEqual(info["quality_candidates"], 2)
        self.assertEqual(info["selection_tier_count"], 2)

    def test_random_strategy_does_not_apply_usage_weight(self):
        candidates = [material(1, usage=50), material(2, usage=0)]
        selected_ids = {
            select_material(candidates, strategy="random", rng=random.Random(seed))[0]["id"]
            for seed in range(20)
        }
        self.assertEqual(selected_ids, {1, 2})

    def test_random_strategy_still_avoids_material_already_used_in_batch(self):
        selected, _ = select_material(
            [material(1), material(2)], strategy="random", avoid_ids={1}, rng=random.Random(0)
        )
        self.assertEqual(selected["id"], 2)


class MaterialCandidateQueryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE materials(
              id INTEGER PRIMARY KEY,category_id INTEGER,file_name TEXT,file_path TEXT,
              duration REAL,width INTEGER,height INTEGER,status TEXT
            );
            CREATE TABLE generation_tasks(
              id INTEGER PRIMARY KEY,status TEXT,created_at TEXT,updated_at TEXT,finished_at TEXT
            );
            CREATE TABLE generation_shot_materials(
              id INTEGER PRIMARY KEY,task_id INTEGER,shot_id INTEGER,material_id INTEGER
            );
            INSERT INTO materials VALUES(1,9,'old-4k.mp4','a',20,3840,2160,'active');
            INSERT INTO materials VALUES(2,9,'new-1080.mp4','b',20,1080,1920,'active');
            INSERT INTO materials VALUES(3,9,'short.mp4','c',2,1080,1920,'active');
            INSERT INTO materials VALUES(4,8,'wrong-category.mp4','d',20,1080,1920,'active');
            INSERT INTO materials VALUES(5,9,'missing.mp4','e',20,1080,1920,'missing');
            INSERT INTO generation_tasks VALUES(10,'completed','','','2026-01-02');
            INSERT INTO generation_tasks VALUES(11,'failed','','','2026-01-03');
            INSERT INTO generation_shot_materials VALUES(1,10,1,1);
            INSERT INTO generation_shot_materials VALUES(2,11,1,2);
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_complete_pool_has_no_id_limit_or_resolution_preselection(self):
        rows = load_material_candidates(self.conn, 9, 5)
        self.assertEqual({item["id"] for item in rows}, {1, 2})

    def test_duration_category_and_status_are_hard_filters(self):
        rows = load_material_candidates(self.conn, 9, 5)
        self.assertNotIn(3, {item["id"] for item in rows})
        self.assertNotIn(4, {item["id"] for item in rows})
        self.assertNotIn(5, {item["id"] for item in rows})

    def test_only_successful_generation_increments_usage(self):
        rows = {item["id"]: item for item in load_material_candidates(self.conn, 9, 5)}
        self.assertEqual(rows[1]["usage_count"], 1)
        self.assertEqual(rows[2]["usage_count"], 0)
        self.assertEqual(rows[2]["last_used_at"], None)

    def test_newly_scanned_row_is_visible_without_cache_refresh(self):
        before = {item["id"] for item in load_material_candidates(self.conn, 9, 5)}
        self.conn.execute(
            "INSERT INTO materials VALUES(6,9,'just-scanned.mov','f',21,1080,1920,'active')"
        )
        self.conn.commit()
        after = {item["id"] for item in load_material_candidates(self.conn, 9, 5)}
        self.assertNotIn(6, before)
        self.assertIn(6, after)

    def test_newly_scanned_row_starts_with_zero_successful_usage(self):
        self.conn.execute(
            "INSERT INTO materials VALUES(6,9,'just-scanned.mov','f',21,1080,1920,'active')"
        )
        item = next(row for row in load_material_candidates(self.conn, 9, 5) if row["id"] == 6)
        self.assertEqual(item["usage_count"], 0)
        self.assertIsNone(item["last_used_at"])

    def test_diagnostic_counts_use_the_same_material_registry(self):
        info = material_pool_diagnostics(self.conn, 9, 5)
        self.assertEqual(info["registry_count"], 5)
        self.assertEqual(info["category_count"], 3)
        self.assertEqual(info["duration_count"], 2)
        self.assertEqual(info["valid_count"], 2)


if __name__ == "__main__":
    unittest.main()
