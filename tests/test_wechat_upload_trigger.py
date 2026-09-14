from unittest.mock import MagicMock, patch

import pytest

from publishers.domestic_guarded import GuardedDomesticBrowserPublisher as Publisher
from publish_safety import PublishGuardError


@pytest.mark.parametrize("started", [False, True])
def test_injection_observes_start_before_completion_wait(tmp_path, started):
    page = MagicMock()
    page.url = "https://channels.weixin.qq.com/platform/post/create"
    page.locator.return_value.count.return_value = 1
    selected = page.locator.return_value.first
    selected.is_visible.return_value = False
    selected.evaluate.return_value = [{"name": "video.mp4", "size": 42}]
    capture = MagicMock()
    idle = [{"url": page.url, "signals": [], "text": ""}]
    active = [{"url": page.url, "signals": [{"selector": "text=取消上传"}], "text": "取消上传"}]
    snapshots = [idle, active if started else idle] + [idle] * 29
    with patch.object(Publisher, "_roots", return_value=[page]), patch.object(
        Publisher, "_upload_start_snapshot", side_effect=snapshots
    ):
        if started:
            Publisher._inject_wechat_video(page, tmp_path / "video.mp4", tmp_path, capture)
        else:
            with pytest.raises(PublishGuardError) as error:
                Publisher._inject_wechat_video(page, tmp_path / "video.mp4", tmp_path, capture)
            assert error.value.code == "VIDEO_UPLOAD_NOT_STARTED"
    selected.set_input_files.assert_called_once()
    assert page.wait_for_timeout.call_count == 30
    capture.assert_called_once_with(page, "01_upload_after_30s")
    assert (tmp_path / "upload-trigger.jsonl").is_file()
