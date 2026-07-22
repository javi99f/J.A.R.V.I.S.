import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omar_ai_core import history


class HistoryTests(unittest.TestCase):
    def test_conversation_and_diagnostics_can_be_recovered(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            history_file = root / "jarvis-history.log"
            runtime_file = root / "jarvis-runtime.log"
            desktop_file = root / "jarvis.log"
            with (
                patch.object(history, "HISTORY_FILE", history_file),
                patch.object(history, "RUNTIME_LOG_FILE", runtime_file),
                patch.object(history, "DESKTOP_LOG_FILE", desktop_file),
            ):
                history.append_history("You: abre Spotify")
                history.append_history("Jarvis: Abriendo Spotify.")
                history.append_history("ERR: fallo de prueba")
                runtime_file.write_text("Traceback de prueba", encoding="utf-8")

                saved = history.read_history()
                self.assertIn("You: abre Spotify", saved)
                self.assertIn("Jarvis: Abriendo Spotify.", saved)
                self.assertIn("ERR: fallo de prueba", saved)
                self.assertIn("Traceback de prueba", history.read_diagnostics())


if __name__ == "__main__":
    unittest.main()
