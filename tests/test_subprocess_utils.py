import os
import subprocess
import unittest

from subprocess_utils import background_process_kwargs


class BackgroundProcessTests(unittest.TestCase):
    def test_windows_background_flags_are_enabled(self):
        options = background_process_kwargs()
        if os.name == "nt":
            self.assertTrue(options["creationflags"] & subprocess.CREATE_NO_WINDOW)
            self.assertTrue(options["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW)
            self.assertEqual(options["startupinfo"].wShowWindow, subprocess.SW_HIDE)
        else:
            self.assertEqual(options, {})

    def test_background_process_still_captures_output(self):
        command = ["cmd", "/c", "echo", "background-ok"] if os.name == "nt" else ["sh", "-c", "echo background-ok"]
        completed = subprocess.run(command, capture_output=True, text=True, **background_process_kwargs())
        self.assertEqual(completed.returncode, 0)
        self.assertIn("background-ok", completed.stdout)


if __name__ == "__main__":
    unittest.main()
