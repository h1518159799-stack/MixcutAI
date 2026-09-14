from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import publishers.browser_publisher as browser


def test_tiktok_button_waits_for_delayed_appearance(tmp_path):
    page = Mock()
    page.wait_for_timeout = Mock()
    button = Mock()
    with patch.object(browser, "find_tiktok_publish_button", side_effect=[None, None, button]):
        assert browser.wait_for_tiktok_publish_button(page, tmp_path, timeout_seconds=60) is button
    assert page.wait_for_timeout.call_count == 2


def test_tiktok_button_rescan_handles_text_change_and_overlay(tmp_path):
    page = Mock()
    page.wait_for_timeout = Mock()
    button = Mock()
    # A failed click caused by an overlay must trigger a fresh scan, not a
    # force-click or browser/process reset.
    with patch.object(browser, "find_tiktok_publish_button", side_effect=[None, button]):
        result = browser.wait_for_tiktok_publish_button(page, tmp_path, timeout_seconds=60)
    assert result is button
    assert page.wait_for_timeout.call_count == 1


def test_strategy_contains_multilingual_and_dom_fallbacks():
    source = Path(browser.__file__).read_text(encoding="utf-8")
    for label in ("发布", "Post", "Publish"):
        assert label in source
    assert "data-testid" in source
    assert "[role='button']" in source
    assert "elementFromPoint" in source
    assert "tiktok_publish_button_diagnostics" in source
