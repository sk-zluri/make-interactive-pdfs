"""Run the dependency-free browser polling lifecycle tests when Node is available."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


class BrowserPollingLifecycleTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_polling_lifecycle(self) -> None:
        test_file = Path(__file__).with_name("app_polling_test.mjs")
        completed = subprocess.run(
            [shutil.which("node") or "node", "--test", str(test_file)],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"{completed.stdout}\n{completed.stderr}".strip(),
        )


if __name__ == "__main__":
    unittest.main()
