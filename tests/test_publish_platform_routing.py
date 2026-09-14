import pytest
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import commercial_api
import db
from platforms import get_adapter
from publishers import get_publisher
from publish_queue import (
    create_publish_task, enqueue_publish_task, next_queued_channel,
    platform_label, process_guarded_domestic_channel,
)


EXPECTED_ADAPTERS = {
    "douyin": "DouyinBrowserAdapter",
    "wechat_channels": "WechatChannelsPostFlowPublisher",
    "xiaohongshu": "XiaohongshuBrowserAdapter",
    "bilibili": "BilibiliBrowserAdapter",
    "tiktok": "TikTokBrowserAdapter",
    "facebook": "FacebookPublisher",
}


@pytest.mark.parametrize("code,adapter_name", EXPECTED_ADAPTERS.items())
def test_each_platform_resolves_to_its_own_adapter(code, adapter_name):
    assert type(get_adapter(code)).__name__ == adapter_name or type(get_publisher(code)).__name__ == adapter_name


@pytest.mark.parametrize("code,label", [("douyin", "抖音"), ("wechat_channels", "视频号"), ("xiaohongshu", "小红书"), ("bilibili", "B站"), ("tiktok", "TikTok"), ("facebook", "Facebook Page")])
def test_platform_failure_label_is_not_hardcoded_to_douyin(code, label):
    assert platform_label(code) == label


def test_unknown_platform_does_not_resolve_to_douyin():
    with pytest.raises(ValueError):
        get_publisher("missing_platform")


class PublishRoutingAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        self.patchers = [
            patch.object(db, "DB_PATH", root / "data" / "mixcut.db"),
            patch.object(db, "DATA_DIR", root / "data"),
            patch.object(db, "THUMB_DIR", root / "data" / "thumbnails"),
            patch.object(db, "VOICE_DIR", root / "data" / "voices"),
            patch.object(db, "CONFIG_DIR", root / "config"),
            patch.object(db, "OUTPUT_DIR", root / "outputs"),
        ]
        for item in self.patchers:
            item.start()
        (root / "config").mkdir(parents=True)
        db._init_db_impl()
        self.video = root / "acceptance.mp4"
        self.video.write_bytes(b"test-video")
        with db.connect() as conn:
            stamp = db.now()
            conn.execute("INSERT INTO generation_tasks(task_name,status,output_path,created_at,updated_at) VALUES(?, 'completed', ?, ?, ?)", ("路由验收", str(self.video), stamp, stamp))
            self.video_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def _account(self, code):
        with db.connect() as conn:
            platform_id = int(conn.execute("SELECT id FROM platforms WHERE code=?", (code,)).fetchone()[0])
            conn.execute("UPDATE platforms SET capability_status='passed' WHERE id=?", (platform_id,))
        account_id = commercial_api.save_publishing_account({"platform_id": platform_id, "account_name": f"路由-{code}"})
        with db.connect() as conn:
            conn.execute("UPDATE publish_accounts SET login_status='logged_in',authorization_status='authorized' WHERE id=?", (account_id,))
        return account_id

    def test_six_platforms_create_independent_routing_records(self):
        expected = {
            "douyin": "BrowserPublisher", "wechat_channels": "WechatChannelsPostFlowPublisher",
            "xiaohongshu": "XiaohongshuBrowserAdapter", "bilibili": "BilibiliBrowserAdapter",
            "tiktok": "TikTokBrowserAdapter", "facebook": "FacebookAdapter",
        }
        with patch("publish_queue.preflight_publish_check", return_value={"idempotency_key": "k", "video_file_hash": "v", "title_hash": "t"}):
            for code, adapter_name in expected.items():
                account_id = self._account(code)
                task_id = create_publish_task({"generation_task_id": self.video_id, "account_ids": [account_id], "platform": code, "title": "路由验收"})
                with db.connect() as conn:
                    task = conn.execute("SELECT platform FROM publish_tasks WHERE id=?", (task_id,)).fetchone()
                    channel = conn.execute("SELECT selected_platform,account_platform,publisher_adapter FROM publish_channels WHERE publish_task_id=?", (task_id,)).fetchone()
                self.assertEqual(task["platform"], code)
                self.assertEqual((channel["selected_platform"], channel["account_platform"], channel["publisher_adapter"]), (code, code, adapter_name))

    def test_missing_platform_and_account_mismatch_are_rejected(self):
        douyin = self._account("douyin")
        with self.assertRaisesRegex(ValueError, "缺少发布平台参数"):
            create_publish_task({"generation_task_id": self.video_id, "account_ids": [douyin], "title": "缺平台"})
        with self.assertRaisesRegex(ValueError, "当前账号未绑定视频号，请重新登录绑定"):
            create_publish_task({"generation_task_id": self.video_id, "account_ids": [douyin], "platform": "wechat_channels", "title": "错配"})

    def test_postflow_processing_survives_restart_and_later_confirms(self):
        from publish_queue import recheck_postflow_processing
        from publish_safety import recover_guarded_channels
        from unittest.mock import Mock
        import json
        account_id = self._account('wechat_channels')
        with patch('publish_queue.preflight_publish_check', return_value={'idempotency_key':'k','video_file_hash':'v','title_hash':'t'}):
            task_id = create_publish_task({'generation_task_id':self.video_id,'account_ids':[account_id], 'platform':'wechat_channels','title':'confirmation test'})
            enqueue_publish_task(task_id)
            publisher = Mock()
            publisher.publish.return_value = {'success':False,'submitted':True,'processing':True,'verified':False,
                'status':'processing','confirmation_job':'test-job.json','submitted_at':1000,'error_code':''}
            with patch('publish_queue.get_publisher',return_value=publisher):
                process_guarded_domestic_channel(next_queued_channel())
                with db.connect() as conn:
                    self.assertEqual(conn.execute('SELECT status FROM publish_tasks WHERE id=?',(task_id,)).fetchone()['status'],'processing')
                recover_guarded_channels()
                with db.connect() as conn:
                    self.assertEqual(conn.execute('SELECT status FROM publish_channels WHERE publish_task_id=?',(task_id,)).fetchone()['status'],'processing')
                publisher.confirm.return_value = {'success':True,'submitted':True,'verified':True,'id':'123',
                    'url':'https://channels.weixin.qq.com/platform/post/detail?id=123','status':'published'}
                recheck_postflow_processing()
            publisher.publish.assert_called_once()
        with db.connect() as conn:
            row = conn.execute('SELECT status,remote_post_id,remote_url FROM publish_channels WHERE publish_task_id=?',(task_id,)).fetchone()
            self.assertEqual(row['status'],'succeeded')
            self.assertEqual(row['remote_post_id'],'123')
            self.assertTrue(row['remote_url'])

    def test_wechat_worker_rejects_corrupted_account_binding_before_adapter(self):
        account_id = self._account("wechat_channels")
        with patch("publish_queue.preflight_publish_check", return_value={"idempotency_key": "k", "video_file_hash": "v", "title_hash": "t"}):
            task_id = create_publish_task({
                "generation_task_id": self.video_id,
                "account_ids": [account_id],
                "platform": "wechat_channels",
                "title": "绑定检查",
            })
        enqueue_publish_task(task_id)
        channel = next_queued_channel()
        channel["account_platform"] = "douyin"
        process_guarded_domestic_channel(channel)
        with db.connect() as conn:
            result = conn.execute(
                "SELECT error_message FROM publish_channels WHERE publish_task_id=?",
                (task_id,),
            ).fetchone()
        self.assertIn("当前账号未绑定视频号", result["error_message"])
        self.assertNotIn("发布账号不存在", result["error_message"])

    def test_retry_keeps_original_platform(self):
        expected = ("douyin", "wechat_channels", "xiaohongshu", "bilibili", "tiktok", "facebook")
        with patch("publish_queue.preflight_publish_check", return_value={"idempotency_key": "k", "video_file_hash": "v", "title_hash": "t"}):
            for code in expected:
                account_id = self._account(code)
                task_id = create_publish_task({"generation_task_id": self.video_id, "account_ids": [account_id], "platform": code, "title": "重试验收"})
                with db.connect() as conn:
                    conn.execute("UPDATE publish_tasks SET status='failed' WHERE id=?", (task_id,))
                    conn.execute("UPDATE publish_channels SET status='failed' WHERE publish_task_id=?", (task_id,))
                enqueue_publish_task(task_id)
                with db.connect() as conn:
                    row = conn.execute("SELECT platform FROM publish_tasks WHERE id=?", (task_id,)).fetchone()
                self.assertEqual(row["platform"], code)

    def test_fresh_database_account_id_is_not_platform_id(self):
        from unittest.mock import Mock
        account_id = self._account('wechat_channels')
        seen = []
        def check(platform, account, *args, **kwargs):
            self.assertEqual(platform['id'], account['platform_id'])
            seen.append((account['id'], platform['id']))
            return {'idempotency_key':'k','video_file_hash':'v','title_hash':'t'}
        with patch('publish_queue.preflight_publish_check', side_effect=check):
            task = create_publish_task({'generation_task_id':self.video_id, 'account_ids':[account_id], 'platform':'视频号', 'title':'fresh account'})
            enqueue_publish_task(task)
            publisher = Mock()
            publisher.publish.return_value = {'success':False,'submitted':True,'processing':True,'verified':False,'status':'processing','error_code':''}
            with patch('publish_queue.get_publisher', return_value=publisher):
                process_guarded_domestic_channel(next_queued_channel())
            publisher.publish.assert_called_once()
        self.assertGreaterEqual(len(seen), 2)
        self.assertNotEqual(*seen[-1])
