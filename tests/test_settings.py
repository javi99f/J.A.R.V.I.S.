import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omar_ai_core import settings


class SettingsTests(unittest.TestCase):
    def test_gemini_is_the_only_required_key(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "OPENROUTER_API_KEY": ""}, clear=False):
            self.assertTrue(settings.is_configured())

    def test_example_placeholder_is_not_configured(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "your-gemini-api-key"}, clear=False):
            self.assertFalse(settings.is_configured())

    def test_env_parser_preserves_values_with_equals(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("TOKEN=left=right\n", encoding="utf-8")
            self.assertEqual(settings._parse_env_file(path)["TOKEN"], "left=right")

    def test_live_transport_environment_overrides_are_supported(self):
        with patch.dict(
            os.environ,
            {"LIVE_IP_MODE": "ipv4-first", "LIVE_USE_SYSTEM_PROXY": "0"},
            clear=False,
        ):
            self.assertEqual(settings.get_secret("LIVE_IP_MODE"), "ipv4-first")
            self.assertEqual(settings.get_secret("LIVE_USE_SYSTEM_PROXY"), "0")

    def test_audio_device_selection_is_persisted_without_losing_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("GEMINI_API_KEY=test-key\nINPUT_DEVICE=2\n", encoding="utf-8")
            with patch.object(settings, "ENV_FILE", path):
                settings.write_audio_devices(7, 11, "USB Microphone", "USB Speakers")
                saved = settings._parse_env_file(path)
                self.assertEqual(saved["GEMINI_API_KEY"], "test-key")
                self.assertEqual(saved["INPUT_DEVICE"], "7")
                self.assertEqual(saved["OUTPUT_DEVICE"], "11")
                self.assertEqual(saved["INPUT_DEVICE_NAME"], "USB Microphone")
                self.assertEqual(saved["OUTPUT_DEVICE_NAME"], "USB Speakers")
                settings.write_audio_devices(None, 11)
                saved = settings._parse_env_file(path)
                self.assertNotIn("INPUT_DEVICE", saved)
                self.assertNotIn("INPUT_DEVICE_NAME", saved)
                self.assertEqual(saved["OUTPUT_DEVICE"], "11")
                self.assertEqual(saved["OUTPUT_DEVICE_NAME"], "USB Speakers")


if __name__ == "__main__":
    unittest.main()
