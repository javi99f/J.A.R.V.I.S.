import json
import tempfile
import unittest
from pathlib import Path

from omar_ai_core.self_test import run_self_test


class PackagedSelfTestTests(unittest.TestCase):
    def test_offline_self_test_covers_core_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "self-test.json"
            report = run_self_test(output, include_audio=False)
            self.assertTrue(report["ok"], report)
            self.assertTrue(output.is_file())
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(
                {"memory", "planning", "wakeword", "runtime", "ui_imports"}.issubset(
                    saved["checks"]
                )
            )


if __name__ == "__main__":
    unittest.main()
