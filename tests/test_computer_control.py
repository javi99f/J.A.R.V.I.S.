import unittest
from unittest.mock import patch

from omar_ai_core.tools.computer_control import (
    ComputerControlError,
    hotkey_requires_confirmation,
    is_blocked_app_name,
    normalize_keys,
    resolve_app,
)


class ComputerControlSafetyTests(unittest.TestCase):
    def test_system_tools_and_arbitrary_paths_are_blocked(self):
        for name in ("PowerShell", "Registry Editor", r"C:\Windows\regedit.exe"):
            self.assertTrue(is_blocked_app_name(name))
            with self.assertRaises(ComputerControlError):
                resolve_app(name)

    def test_normal_application_names_are_allowed(self):
        self.assertFalse(is_blocked_app_name("Google Chrome"))
        self.assertFalse(is_blocked_app_name("Bloc de notas"))

    def test_dangerous_shortcuts_require_confirmation(self):
        self.assertTrue(hotkey_requires_confirmation(["alt", "f4"]))
        self.assertTrue(hotkey_requires_confirmation("shift+delete"))
        self.assertFalse(hotkey_requires_confirmation(["ctrl", "l"]))

    def test_key_aliases_are_normalized(self):
        self.assertEqual(normalize_keys("Control + Escape"), ["ctrl", "esc"])

    def test_computer_tool_is_exposed_only_on_desktop(self):
        from omar_ai_core import runtime

        with patch.object(runtime, "is_desktop_mode", return_value=True):
            desktop_names = {
                item["name"] for item in runtime._available_tool_declarations()
            }
        with patch.object(runtime, "is_desktop_mode", return_value=False):
            pi_names = {item["name"] for item in runtime._available_tool_declarations()}
        self.assertIn("computer_control", desktop_names)
        self.assertNotIn("computer_control", pi_names)


if __name__ == "__main__":
    unittest.main()
