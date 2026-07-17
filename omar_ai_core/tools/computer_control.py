"""Constrained local Windows computer control for the desktop edition.

This module intentionally exposes UI primitives instead of a shell.  It cannot
run commands, edit the registry, change Windows settings, or launch arbitrary
paths.  High-impact actions require both an explicit model flag and a native
confirmation dialog that the user must accept locally.
"""

from __future__ import annotations

import ctypes
import io
import os
import re
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path

from PIL import ImageGrab


CRITICAL_PROCESSES = {
    "cmd.exe",
    "control.exe",
    "cscript.exe",
    "diskpart.exe",
    "mmc.exe",
    "msconfig.exe",
    "msiexec.exe",
    "powershell.exe",
    "pwsh.exe",
    "reg.exe",
    "regedit.exe",
    "rundll32.exe",
    "systemsettings.exe",
    "taskmgr.exe",
    "wscript.exe",
}
GUARDED_PROCESSES = {"explorer.exe"}
BLOCKED_APP_TERMS = {
    "command prompt",
    "control panel",
    "disk management",
    "powershell",
    "registry",
    "regedit",
    "services",
    "system configuration",
    "task scheduler",
    "terminal",
    "uninstall",
    "windows tools",
}
HIGH_RISK_HOTKEYS = {
    frozenset({"alt", "f4"}),
    frozenset({"ctrl", "w"}),
    frozenset({"ctrl", "shift", "w"}),
    frozenset({"shift", "delete"}),
}

VK = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "esc": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "delete": 0x2E,
    "win": 0x5B,
}
VK.update({f"f{i}": 0x6F + i for i in range(1, 13)})
VK.update({chr(code).lower(): code for code in range(ord("A"), ord("Z") + 1)})
VK.update({str(number): ord(str(number)) for number in range(10)})


class ComputerControlError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScreenFrame:
    jpeg: bytes
    width: int
    height: int
    origin_x: int
    origin_y: int
    active_title: str
    active_process: str


def _normal(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def is_blocked_app_name(name: object) -> bool:
    normalized = _normal(name)
    if not normalized or any(mark in normalized for mark in ("/", "\\", ":")):
        return True
    return any(term in normalized for term in BLOCKED_APP_TERMS)


def normalize_keys(keys: object) -> list[str]:
    if isinstance(keys, str):
        raw = re.split(r"[+,\s]+", keys)
    elif isinstance(keys, (list, tuple)):
        raw = [str(item) for item in keys]
    else:
        raw = []
    aliases = {
        "control": "ctrl",
        "escape": "esc",
        "return": "enter",
        "windows": "win",
        "pgup": "pageup",
        "pgdn": "pagedown",
        "del": "delete",
    }
    return [aliases.get(_normal(key), _normal(key)) for key in raw if _normal(key)]


def hotkey_requires_confirmation(keys: object) -> bool:
    normalized = frozenset(normalize_keys(keys))
    return normalized in HIGH_RISK_HOTKEYS or normalized == frozenset({"ctrl", "alt", "delete"})


def _require_windows() -> None:
    if sys.platform != "win32":
        raise ComputerControlError("Computer control is available only on Windows.")


def _user32():
    _require_windows()
    return ctypes.windll.user32


def _automation():
    _require_windows()
    import pyautogui

    pyautogui.PAUSE = 0.04
    pyautogui.FAILSAFE = True
    return pyautogui


def active_window() -> dict[str, object]:
    _require_windows()
    user32 = _user32()
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    title_buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title_buffer, length + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process_name = ""
    if pid.value:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid.value)
        if handle:
            try:
                size = wintypes.DWORD(32768)
                path_buffer = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(
                    handle, 0, path_buffer, ctypes.byref(size)
                ):
                    process_name = Path(path_buffer.value).name.lower()
            finally:
                kernel32.CloseHandle(handle)
    return {
        "title": title_buffer.value,
        "process": process_name,
        "pid": int(pid.value),
    }


def capture_screen_jpeg() -> ScreenFrame:
    _require_windows()
    image = ImageGrab.grab(all_screens=True).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=60, optimize=True)
    info = active_window()
    user32 = _user32()
    return ScreenFrame(
        jpeg=buffer.getvalue(),
        width=image.width,
        height=image.height,
        origin_x=int(user32.GetSystemMetrics(76)),
        origin_y=int(user32.GetSystemMetrics(77)),
        active_title=str(info["title"]),
        active_process=str(info["process"]),
    )


def _start_menu_apps() -> dict[str, Path]:
    roots = []
    appdata = os.getenv("APPDATA")
    programdata = os.getenv("PROGRAMDATA")
    if appdata:
        roots.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    if programdata:
        roots.append(Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    apps: dict[str, Path] = {}
    for root in roots:
        if not root.exists():
            continue
        for shortcut in root.rglob("*.lnk"):
            display = shortcut.stem.strip()
            if display and not is_blocked_app_name(display):
                apps.setdefault(_normal(display), shortcut)
    return apps


def list_safe_apps(limit: int = 80) -> list[str]:
    return [path.stem for _, path in sorted(_start_menu_apps().items())[: max(1, min(limit, 120))]]


def _known_app_targets() -> dict[str, str]:
    home = Path.home()
    local = Path(os.getenv("LOCALAPPDATA", home / "AppData" / "Local"))
    program_files = Path(os.getenv("ProgramFiles", r"C:\Program Files"))
    program_files_x86 = Path(os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    windows = Path(os.getenv("WINDIR", r"C:\Windows"))
    candidates = {
        "notepad": [windows / "System32" / "notepad.exe"],
        "bloc de notas": [windows / "System32" / "notepad.exe"],
        "calculator": [windows / "System32" / "calc.exe"],
        "calculadora": [windows / "System32" / "calc.exe"],
        "paint": [windows / "System32" / "mspaint.exe"],
        "explorer": [windows / "explorer.exe"],
        "explorador": [windows / "explorer.exe"],
        "edge": [program_files_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe"],
        "chrome": [
            program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
            program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe",
            local / "Google" / "Chrome" / "Application" / "chrome.exe",
        ],
        "firefox": [program_files / "Mozilla Firefox" / "firefox.exe"],
    }
    found: dict[str, str] = {}
    for alias, paths in candidates.items():
        for path in paths:
            if path.exists():
                found[alias] = str(path)
                break
    return found


def resolve_app(name: object) -> tuple[str | None, list[str]]:
    query = _normal(name)
    if is_blocked_app_name(query):
        raise ComputerControlError("That application is blocked by desktop safety mode.")
    known = _known_app_targets()
    if query in known:
        return known[query], []
    shortcuts = _start_menu_apps()
    if query in shortcuts:
        return str(shortcuts[query]), []
    names = list(shortcuts)
    partial = sorted((item for item in names if query in item), key=lambda item: (len(item), item))
    if len(partial) == 1:
        return str(shortcuts[partial[0]]), []
    if len(partial) > 1:
        return None, [shortcuts[item].stem for item in partial[:8]]
    close = get_close_matches(query, names, n=5, cutoff=0.72)
    if len(close) == 1:
        return str(shortcuts[close[0]]), []
    return None, [shortcuts[item].stem for item in close]


def _launch_app(name: object) -> str:
    target, alternatives = resolve_app(name)
    if not target:
        if alternatives:
            return "Application name is ambiguous. Matches: " + ", ".join(alternatives)
        return f"No safe installed application matched '{name}'. Use list_apps to inspect available names."
    os.startfile(target)  # type: ignore[attr-defined]
    return f"Opened {Path(target).stem}."


def _screen_point(x: object, y: object) -> tuple[int, int]:
    try:
        px, py = int(x), int(y)
    except (TypeError, ValueError) as exc:
        raise ComputerControlError("x and y must be integer screenshot coordinates.") from exc
    user32 = _user32()
    width, height = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    if not (0 <= px < width and 0 <= py < height):
        raise ComputerControlError(f"Point ({px}, {py}) is outside the {width}x{height} desktop image.")
    return px + user32.GetSystemMetrics(76), py + user32.GetSystemMetrics(77)


def _confirm_locally(description: str) -> bool:
    result = _user32().MessageBoxW(
        None,
        f"Jarvis solicita permiso para:\n\n{description}\n\n¿Permitir esta acción?",
        "Confirmación de seguridad de Jarvis",
        0x00000004 | 0x00000020 | 0x00040000,
    )
    return result == 6


def _guard_interaction(parameters: dict, description: str, *, inherently_sensitive: bool = False) -> None:
    info = active_window()
    process = str(info["process"]).lower()
    if process in CRITICAL_PROCESSES:
        raise ComputerControlError(
            f"Interaction with {process} is blocked to protect Windows settings and system tools."
        )
    sensitive = inherently_sensitive or bool(parameters.get("sensitive")) or process in GUARDED_PROCESSES
    if not sensitive:
        return
    if not bool(parameters.get("confirmed")):
        raise ComputerControlError(
            "This action is sensitive and requires explicit user confirmation. Ask first, then retry with confirmed=true."
        )
    if not _confirm_locally(description):
        raise ComputerControlError("The user denied the action in the local confirmation dialog.")


def _key_code(key: object) -> int:
    normalized = normalize_keys([key])
    if not normalized or normalized[0] not in VK:
        raise ComputerControlError(f"Unsupported key: {key}")
    return VK[normalized[0]]


def _key_event(vk: int, down: bool) -> None:
    event = _INPUT(type=1, ki=_KEYBDINPUT(vk, 0, 0 if down else 0x0002, 0, 0))
    _send_inputs(event)


def _press_key(key: object, presses: int = 1) -> None:
    normalized = normalize_keys([key])
    if not normalized or normalized[0] not in VK:
        raise ComputerControlError(f"Unsupported key: {key}")
    _automation().press(normalized[0], presses=max(1, min(int(presses), 20)), interval=0.04)


def _hotkey(keys: object) -> None:
    normalized = normalize_keys(keys)
    if not normalized or len(normalized) > 4:
        raise ComputerControlError("A hotkey must contain between one and four supported keys.")
    for key in normalized:
        _key_code(key)
    _automation().hotkey(*normalized, interval=0.04)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("ki", _KEYBDINPUT),
        ("mi", _MOUSEINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


def _send_inputs(*events: _INPUT) -> None:
    if not events:
        return
    inputs = (_INPUT * len(events))(*events)
    user32 = _user32()
    user32.SendInput.argtypes = (
        wintypes.UINT,
        ctypes.POINTER(_INPUT),
        ctypes.c_int,
    )
    user32.SendInput.restype = wintypes.UINT
    sent = user32.SendInput(len(inputs), inputs, ctypes.sizeof(_INPUT))
    if sent != len(inputs):
        raise ComputerControlError("Windows rejected keyboard input.")


def _release_modifiers() -> None:
    automation = _automation()
    for key in ("ctrl", "alt", "shift", "win"):
        automation.keyUp(key)


def _type_text(text: object, interval: float = 0.01) -> None:
    value = str(text or "")
    if not value:
        raise ComputerControlError("text cannot be empty.")
    if len(value) > 2000:
        raise ComputerControlError("A single typing action is limited to 2000 characters.")
    _release_modifiers()
    delay = max(0.0, min(float(interval), 0.1))
    for character in value:
        if character == "\n":
            _automation().press("enter")
            continue
        if character == "\t":
            _automation().press("tab")
            continue
        if 0x20 <= ord(character) <= 0x7E:
            _automation().write(character, interval=delay)
            continue
        codepoint = ord(character)
        if codepoint > 0xFFFF:
            units = character.encode("utf-16-le")
            scans = [int.from_bytes(units[index : index + 2], "little") for index in range(0, len(units), 2)]
        else:
            scans = [codepoint]
        for scan in scans:
            down = _INPUT(type=1, ki=_KEYBDINPUT(0, scan, 0x0004, 0, 0))
            up = _INPUT(type=1, ki=_KEYBDINPUT(0, scan, 0x0004 | 0x0002, 0, 0))
            _send_inputs(down, up)
        if delay:
            time.sleep(delay)


def computer_control(parameters: dict, player=None) -> object:
    """Execute one constrained desktop action and return an evidence string/dict."""
    del player
    _require_windows()
    action = _normal(parameters.get("action")).replace("-", "_").replace(" ", "_")

    if action == "list_apps":
        return {"safe_apps": list_safe_apps(int(parameters.get("limit", 80)))}
    if action == "open_app":
        return _launch_app(parameters.get("app"))
    if action in {"active_window", "window_status"}:
        return active_window()
    if action == "wait":
        seconds = max(0.0, min(float(parameters.get("seconds", 1.0)), 5.0))
        time.sleep(seconds)
        return f"Waited {seconds:.1f} seconds."

    if action in {"click", "double_click", "right_click", "move", "drag"}:
        point = _screen_point(parameters.get("x"), parameters.get("y"))
        _guard_interaction(
            parameters,
            f"{action.replace('_', ' ')} at screen position {parameters.get('x')}, {parameters.get('y')}",
            inherently_sensitive=action == "drag",
        )
        user32 = _user32()
        if action == "move":
            _automation().moveTo(*point, duration=0.15)
        elif action == "drag":
            end = _screen_point(parameters.get("end_x"), parameters.get("end_y"))
            automation = _automation()
            automation.moveTo(*point, duration=0.15)
            automation.dragTo(*end, duration=0.45, button="left")
        else:
            automation = _automation()
            if action == "right_click":
                automation.rightClick(*point)
            elif action == "double_click":
                automation.doubleClick(*point, interval=0.1)
            else:
                automation.click(*point)
        return f"Completed {action} at screenshot coordinates ({parameters.get('x')}, {parameters.get('y')})."

    if action == "scroll":
        amount = max(-20, min(20, int(parameters.get("amount", 0))))
        _guard_interaction(parameters, f"scroll {amount} steps")
        _automation().scroll(amount)
        return f"Scrolled {amount} steps."

    if action == "type_text":
        text = str(parameters.get("text") or "")
        _guard_interaction(
            parameters,
            f"type {len(text)} characters into {active_window().get('title') or 'the active window'}",
        )
        _type_text(text, float(parameters.get("interval", 0.01)))
        return f"Typed {len(text)} characters."

    if action == "press_key":
        key = parameters.get("key")
        risky = _normal(key) == "delete"
        _guard_interaction(parameters, f"press {key}", inherently_sensitive=risky)
        _release_modifiers()
        _press_key(key, int(parameters.get("presses", 1)))
        return f"Pressed {key}."

    if action == "hotkey":
        keys = parameters.get("keys")
        normalized = normalize_keys(keys)
        if frozenset(normalized) == frozenset({"win", "r"}) or frozenset(normalized) == frozenset({"win", "x"}):
            raise ComputerControlError("That Windows system shortcut is always blocked.")
        risky = hotkey_requires_confirmation(normalized)
        _guard_interaction(parameters, f"press {' + '.join(normalized)}", inherently_sensitive=risky)
        _release_modifiers()
        _hotkey(normalized)
        return f"Pressed {' + '.join(normalized)}."

    raise ComputerControlError(
        "Unknown action. Use observe, list_apps, open_app, active_window, click, double_click, "
        "right_click, move, drag, scroll, type_text, press_key, hotkey, or wait."
    )
