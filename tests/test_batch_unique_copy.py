import unittest
import sys
import types
from unittest.mock import MagicMock, call, patch

video_scan_stub = types.ModuleType("video_scan")
for function_name in ("make_thumbnail", "probe_video", "quick_hash", "scan_video_files"):
    setattr(video_scan_stub, function_name, lambda *args, **kwargs: None)
sys.modules.setdefault("video_scan", video_scan_stub)

import commercial_api


class BatchUniqueCopyTests(unittest.TestCase):
    def test_duplicate_batch_copy_is_rewritten_again_before_generation(self):
        connection = MagicMock()
        context_manager = MagicMock()
        context_manager.__enter__.return_value = connection
        context_manager.__exit__.return_value = False
        context = {"batch_id": 9, "total_count": 3, "index_no": 2, "template_id": 200}

        with (
            patch.object(commercial_api, "row", return_value=context),
            patch.object(commercial_api, "rows", return_value=[{"index_no": 1, "template_id": 100}]),
            patch.object(commercial_api, "connect", return_value=context_manager),
            patch.object(
                commercial_api,
                "template_script_snapshot",
                side_effect=[("same", ["旧文案"]), ("same", ["旧文案"]), ("new", ["新文案"])],
            ),
            patch.object(commercial_api, "rewrite_template_shots") as rewrite,
            patch.object(commercial_api, "start_generate", return_value={"status": "running"}) as start,
        ):
            result = commercial_api.start_batch_generation(55)

        self.assertEqual(rewrite.call_count, 2)
        start.assert_called_once_with(55)
        self.assertEqual(result["status"], "running")

    def test_first_batch_item_also_rewrites_before_generation(self):
        connection = MagicMock()
        context_manager = MagicMock()
        context_manager.__enter__.return_value = connection
        context_manager.__exit__.return_value = False
        context = {"batch_id": 9, "total_count": 3, "index_no": 1, "template_id": 201}

        with (
            patch.object(commercial_api, "row", return_value=context),
            patch.object(commercial_api, "rows", return_value=[]),
            patch.object(commercial_api, "connect", return_value=context_manager),
            patch.object(commercial_api, "template_script_snapshot", return_value=("unique", ["独立文案"])),
            patch.object(commercial_api, "rewrite_template_shots") as rewrite,
            patch.object(commercial_api, "start_generate", return_value={"status": "running"}) as start,
        ):
            commercial_api.start_batch_generation(56)

        rewrite.assert_called_once()
        start.assert_called_once_with(56)


if __name__ == "__main__":
    unittest.main()
