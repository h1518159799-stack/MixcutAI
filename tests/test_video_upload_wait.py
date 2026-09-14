from unittest.mock import MagicMock, patch

from publishers.domestic_guarded import GuardedDomesticBrowserPublisher
from publish_safety import PublishGuardError


def test_upload_waits_until_overlay_is_gone_and_preview_exists():
    page = MagicMock()
    pub = GuardedDomesticBrowserPublisher("wechat_channels")
    visible_values = iter([object(), None, None, None, None, object()])
    body_values = iter(["上传中", ""])
    with patch.object(GuardedDomesticBrowserPublisher, "_body_contains", side_effect=lambda *_: next(body_values, "")), patch.object(GuardedDomesticBrowserPublisher, "_visible", side_effect=lambda *_: next(visible_values, None)):
        pub.wait_video_upload_complete(page, timeout=2)
    page.wait_for_timeout.assert_called_once_with(1000)


def test_upload_wait_timeout_has_explicit_code():
    page = MagicMock()
    pub = GuardedDomesticBrowserPublisher("wechat_channels")
    with patch.object(GuardedDomesticBrowserPublisher, "_body_contains", return_value="上传中"), patch.object(GuardedDomesticBrowserPublisher, "_visible", return_value=object()):
        try:
            pub.wait_video_upload_complete(page, timeout=-1)
        except PublishGuardError as exc:
            assert exc.code == "VIDEO_UPLOAD_TIMEOUT"
        else:
            raise AssertionError("expected VIDEO_UPLOAD_TIMEOUT")
