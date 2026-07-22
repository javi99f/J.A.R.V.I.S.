import unittest
from unittest.mock import patch

import omar_ai_core.tools.computer_control as computer_control_module
from omar_ai_core.tools.computer_control import (
    ComputerControlError,
    _search_default_browser,
    click_named_ui_control,
    click_ui_control,
    find_ui_controls,
    hotkey_requires_confirmation,
    inspect_ui_controls,
    is_blocked_app_name,
    normalize_keys,
    resolve_app,
    type_into_ui_control,
)


class _FakeRect:
    left, top, right, bottom = 10, 20, 210, 60


class _FakeInfo:
    def __init__(self, name, control_type, *, password=False):
        self.name = name
        self.control_type = control_type
        self.automation_id = ""
        self.class_name = "FakeControl"
        self.is_password = password


class _FakeControl:
    def __init__(self, name, control_type, *, password=False):
        self.element_info = _FakeInfo(name, control_type, password=password)
        self.invoked = False
        self.clicked = False
        self.typed = None
        self.focused = False

    def window_text(self):
        return self.element_info.name

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def rectangle(self):
        return _FakeRect()

    def invoke(self):
        self.invoked = True

    def click_input(self):
        self.clicked = True

    def set_edit_text(self, value):
        self.typed = value

    def set_focus(self):
        self.focused = True


class _FakeWindow:
    def __init__(self, controls):
        self.controls = controls

    def descendants(self):
        return self.controls


class _FakeDesktop:
    def __init__(self, controls):
        self.controls = controls

    def window(self, handle):
        self.handle = handle
        return _FakeWindow(self.controls)


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

    def test_inspect_ui_returns_semantic_controls_and_refs(self):
        controls = [
            _FakeControl("Helpful context", "Text"),
            _FakeControl("Connect", "Button"),
            _FakeControl("Remote address", "Edit"),
        ]
        with (
            patch.object(
                computer_control_module,
                "active_window",
                return_value={"handle": 42, "title": "AnyDesk", "process": "anydesk.exe"},
            ),
            patch.object(computer_control_module, "_uia_desktop", return_value=_FakeDesktop(controls)),
        ):
            result = inspect_ui_controls()
        self.assertEqual(result["active_window"], "AnyDesk")
        self.assertEqual(result["controls"][0]["name"], "Connect")
        self.assertEqual(result["controls"][0]["ref"], "ui1")
        self.assertEqual(result["controls"][1]["name"], "Remote address")

    def test_click_control_uses_ref_from_latest_semantic_snapshot(self):
        connect = _FakeControl("Connect", "Button")
        active = {"handle": 42, "title": "AnyDesk", "process": "anydesk.exe"}
        with (
            patch.object(computer_control_module, "active_window", return_value=active),
            patch.object(computer_control_module, "_uia_desktop", return_value=_FakeDesktop([connect])),
            patch.object(computer_control_module, "_guard_interaction"),
        ):
            inspect_ui_controls()
            result = click_ui_control({"ref": "ui1"})
        self.assertTrue(connect.invoked)
        self.assertIn("verify", result["next_step"].lower())

    def test_find_ui_searches_the_complete_browser_tree(self):
        controls = [
            _FakeControl(f"Navigation item {index}", "Button") for index in range(140)
        ]
        controls.append(_FakeControl("Si antes te hubiera conocido - official video", "Hyperlink"))
        active = {"handle": 52, "title": "Google - Chrome", "process": "chrome.exe"}
        with (
            patch.object(computer_control_module, "active_window", return_value=active),
            patch.object(computer_control_module, "_uia_desktop", return_value=_FakeDesktop(controls)),
        ):
            result = find_ui_controls("si antes te hubiera conocido")
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["controls"][0]["control_type"], "Hyperlink")

    def test_click_named_activates_an_exact_semantic_match(self):
        play = _FakeControl("Reproducir", "Button")
        active = {"handle": 62, "title": "Video - Chrome", "process": "chrome.exe"}
        with (
            patch.object(computer_control_module, "active_window", return_value=active),
            patch.object(computer_control_module, "_uia_desktop", return_value=_FakeDesktop([play])),
            patch.object(computer_control_module, "_guard_interaction"),
        ):
            result = click_named_ui_control({"query": "Reproducir"})
        self.assertTrue(play.invoked)
        self.assertEqual(result["matched_control"]["name"], "Reproducir")

    def test_browser_search_uses_the_windows_default_browser(self):
        with patch.object(computer_control_module.webbrowser, "open", return_value=True) as opened:
            result = _search_default_browser("música relajante")
        opened.assert_called_once()
        url = opened.call_args.args[0]
        self.assertTrue(url.startswith("https://www.google.com/search?q="))
        self.assertIn("m%C3%BAsica+relajante", url)
        self.assertIn("default browser", result["status"].lower())

    def test_type_into_control_targets_named_field_without_coordinates(self):
        field = _FakeControl("Search", "Edit")
        active = {"handle": 7, "title": "Browser", "process": "chrome.exe"}
        with (
            patch.object(computer_control_module, "active_window", return_value=active),
            patch.object(computer_control_module, "_uia_desktop", return_value=_FakeDesktop([field])),
            patch.object(computer_control_module, "_guard_interaction"),
        ):
            inspect_ui_controls()
            result = type_into_ui_control(
                {"ref": "ui1", "text": "search phrase", "submit": False}
            )
        self.assertEqual(field.typed, "search phrase")
        self.assertTrue(field.focused)
        self.assertEqual(result["method"], "set_edit_text")

    def test_password_controls_are_blocked(self):
        field = _FakeControl("Password", "Edit", password=True)
        active = {"handle": 9, "title": "Login", "process": "chrome.exe"}
        with (
            patch.object(computer_control_module, "active_window", return_value=active),
            patch.object(computer_control_module, "_uia_desktop", return_value=_FakeDesktop([field])),
        ):
            inspect_ui_controls()
            with self.assertRaises(ComputerControlError):
                type_into_ui_control({"ref": "ui1", "text": "secret"})

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
