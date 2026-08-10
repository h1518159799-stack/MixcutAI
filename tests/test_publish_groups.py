import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import commercial_api
import db
import publish_queue


class PublishGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patchers = [
            patch.object(db, "DB_PATH", self.root / "data" / "mixcut.db"),
            patch.object(db, "DATA_DIR", self.root / "data"),
            patch.object(db, "THUMB_DIR", self.root / "data" / "thumbnails"),
            patch.object(db, "VOICE_DIR", self.root / "data" / "voices"),
            patch.object(db, "CONFIG_DIR", self.root / "config"),
            patch.object(db, "OUTPUT_DIR", self.root / "outputs"),
        ]
        for item in self.patchers:
            item.start()
        (self.root / "config").mkdir(parents=True)
        db._init_db_impl()
        self.video = self.root / "test.mp4"
        self.video.write_bytes(b"mixcut-test-video")
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO generation_tasks(task_name,status,output_path,created_at,updated_at) VALUES(?, 'completed', ?, ?, ?)",
                ("矩阵验收视频", str(self.video), db.now(), db.now()),
            )
            self.video_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def platform(self, code, capability):
        with db.connect() as conn:
            conn.execute("UPDATE platforms SET capability_status=? WHERE code=?", (capability, code))
            return int(conn.execute("SELECT id FROM platforms WHERE code=?", (code,)).fetchone()[0])

    def account(self, code, name, capability="passed", login="logged_in"):
        account_id = commercial_api.save_publishing_account({
            "platform_id": self.platform(code, capability), "account_name": name,
        })
        with db.connect() as conn:
            conn.execute(
                "UPDATE publish_accounts SET login_status=?,authorization_status='authorized' WHERE id=?",
                (login, account_id),
            )
        return account_id

    def payload(self, group_id):
        return {
            "group_id": group_id, "generation_task_id": self.video_id,
            "title": "MixcutAI 矩阵发布验收", "description": "验收测试", "tags": ["MixcutAI"],
        }

    def test_create_edit_copy_delete_group_keeps_accounts(self):
        account_a = self.account("douyin", "主账号")
        account_b = self.account("douyin", "产品账号")
        group_id = commercial_api.save_account_group({"name": "品牌矩阵", "description": "主账号组", "account_ids": [account_a, account_b]})
        summary = commercial_api.publishing_account_groups()[0]
        self.assertEqual(summary["account_count"], 2)
        self.assertEqual(summary["publishable_count"], 2)
        commercial_api.save_account_group({"name": "品牌矩阵", "description": "已编辑", "account_ids": [account_a]}, group_id)
        copied = commercial_api.copy_account_group(group_id)
        self.assertNotEqual(copied, group_id)
        commercial_api.delete_account_group(group_id)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM publish_accounts WHERE status='active'").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM publish_account_group_members WHERE group_id=?", (group_id,)).fetchone()[0], 0)

    def test_duplicate_group_name_is_rejected(self):
        account_a = self.account("douyin", "主账号")
        account_b = self.account("douyin", "产品账号")
        commercial_api.save_account_group({"name": "同名组", "account_ids": [account_a]})
        with self.assertRaises(ValueError):
            commercial_api.save_account_group({"name": "同名组", "account_ids": [account_b]})

    def test_account_delete_cleans_members(self):
        account_a = self.account("douyin", "主账号")
        group_id = commercial_api.save_account_group({"name": "清理组", "account_ids": [account_a]})
        with db.connect() as conn:
            conn.execute("DELETE FROM publish_accounts WHERE id=?", (account_a,))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM publish_account_group_members WHERE group_id=?", (group_id,)).fetchone()[0], 0)

    def test_end_to_end_gate_stats_restart_and_idempotency(self):
        account_a = self.account("douyin", "品牌账号", "passed", "logged_in")
        account_b = self.account("xiaohongshu", "品牌账号", "untested", "logged_in")
        account_c = self.account("kuaishou", "工厂账号", "passed", "needs_login")
        account_d = self.account("bilibili", "产品账号", "failed", "logged_in")
        group_id = commercial_api.save_account_group({
            "name": "矩阵验收组", "account_ids": [account_a, account_b, account_c, account_d],
        })
        preview = commercial_api.group_publish_preflight(self.payload(group_id))
        self.assertEqual((preview["total_count"], preview["publishable_count"], preview["blocked_count"]), (4, 1, 3))
        result = commercial_api.create_group_publish_batch(self.payload(group_id))
        self.assertEqual(len(result["task_ids"]), 1)
        self.assertEqual(len(result["items"]), 4)
        skipped = {item["account_id"]: item for item in result["items"] if item["status"] == "skipped"}
        self.assertEqual(skipped[account_b]["error_code"], "PLATFORM_UNTESTED")
        self.assertEqual(skipped[account_c]["error_code"], "AUTH_EXPIRED")
        self.assertEqual(skipped[account_d]["error_code"], "PLATFORM_FAILED")
        batch = result["batch"]
        self.assertEqual((batch["total_count"], batch["queued_count"], batch["skipped_count"]), (4, 1, 3))

        task_id = result["task_ids"][0]
        with db.connect() as conn:
            conn.execute("UPDATE publish_channels SET status='success' WHERE publish_task_id=?", (task_id,))
        publish_queue._finish_task(task_id)
        with db.connect() as conn:
            batch = dict(conn.execute("SELECT * FROM publish_group_batches WHERE id=?", (result["batch_id"],)).fetchone())
        self.assertEqual(batch["status"], "partial_success")
        self.assertEqual((batch["succeeded_count"], batch["skipped_count"]), (1, 3))

        db._init_db_impl()  # simulate restart/migration pass
        restored = commercial_api._group_batch_result(result["batch_id"])
        self.assertEqual(len(restored["items"]), 4)
        duplicate = commercial_api.create_group_publish_batch(self.payload(group_id))
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["batch_id"], result["batch_id"])
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM publish_tasks WHERE group_batch_id=?", (result["batch_id"],)).fetchone()[0], 1)

    def test_failure_retry_only_failed_account(self):
        account_a = self.account("douyin", "成功账号")
        account_e = self.account("douyin", "失败账号")
        group_id = commercial_api.save_account_group({"name": "失败重试组", "account_ids": [account_a, account_e]})
        result = commercial_api.create_group_publish_batch(self.payload(group_id))
        items = {item["account_id"]: item for item in result["items"]}
        success_task = items[account_a]["task_id"]
        failed_task = items[account_e]["task_id"]
        with db.connect() as conn:
            conn.execute("UPDATE publish_channels SET status='success' WHERE publish_task_id=?", (success_task,))
            conn.execute("UPDATE publish_channels SET status='failed',error_message='模拟平台失败' WHERE publish_task_id=?", (failed_task,))
            conn.execute("UPDATE publish_tasks SET last_error='模拟平台失败',error_code='UPLOAD_FAILED' WHERE id=?", (failed_task,))
        publish_queue._finish_task(success_task)
        publish_queue._finish_task(failed_task)
        with db.connect() as conn:
            batch = dict(conn.execute("SELECT * FROM publish_group_batches WHERE id=?", (result["batch_id"],)).fetchone())
        self.assertEqual(batch["status"], "partial_success")
        with patch.object(publish_queue.PUBLISH_WORKER, "wake"):
            retry = publish_queue.retry_group_batch_failed(result["batch_id"])
        self.assertEqual(retry["retried_task_ids"], [failed_task])
        with db.connect() as conn:
            rows = {row["account_id"]: dict(row) for row in conn.execute("SELECT * FROM publish_group_batch_items WHERE batch_id=?", (result["batch_id"],))}
        self.assertEqual(rows[account_a]["status"], "success")
        self.assertEqual(rows[account_a]["retry_count"], 0)
        self.assertEqual(rows[account_e]["status"], "queued")
        self.assertEqual(rows[account_e]["retry_count"], 1)

    def test_all_blocked_is_persisted_as_all_skipped(self):
        account_b = self.account("xiaohongshu", "待验收账号", "untested", "logged_in")
        group_id = commercial_api.save_account_group({"name": "全部跳过组", "account_ids": [account_b]})
        result = commercial_api.create_group_publish_batch(self.payload(group_id))
        self.assertEqual(result["task_ids"], [])
        self.assertEqual(result["batch"]["status"], "all_skipped")
        self.assertEqual(result["batch"]["skipped_count"], 1)


if __name__ == "__main__":
    unittest.main()
