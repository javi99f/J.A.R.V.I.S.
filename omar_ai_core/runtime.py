import asyncio
import contextlib
import socket
import os
import re
import threading
import sys
import time
import traceback
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd
from google import genai
from google.genai import types
from .display.hud import JarvisUI
from .memory.store import (
    forget_memory,
    format_memory_for_prompt,
    load_memory,
    remember,
    search_memories,
)
from .planning import PlanManager, format_plan_for_prompt, is_complex_request

from .tools.web_lookup import web_search as web_search_action
from .tools.pi_device import pi_controls
from .tools.home_control import home_control
from .tools.computer_control import capture_screen_jpeg, computer_control
from .settings import BASE_DIR, get_secret, is_desktop_mode, require_secret
from .state import listening as listening_state
from .audio.wakeword import WakeWordGate
from .updater import ReleaseInfo, UpdateManager


def get_base_dir():
    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return bundle / "omar_ai_core"
    return Path(__file__).resolve().parent


PACKAGE_DIR     = get_base_dir()
PROJECT_DIR     = BASE_DIR
PROMPT_PATH     = PACKAGE_DIR / "persona" / "system_prompt.txt"
RUNTIME_LOG_PATH = PROJECT_DIR / "jarvis-runtime.log"
LIVE_MODEL          = "gemini-3.1-flash-live-preview"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

WAKE_PATTERN = re.compile(
    r"\b(jarvis|assistant)\b.*\b(unmute|wake up|listen|start listening|resume listening|despierta|escucha|act[ií]vate)\b"
    r"|\b(unmute|wake up|listen|start listening|resume listening|despierta|escucha|act[ií]vate)\b.*\b(jarvis|assistant)\b",
    re.IGNORECASE,
)

LISTENING_MUTE_ACTIONS = {"listening_mute", "assistant_mute", "mute_listening", "stop_listening"}
LISTENING_UNMUTE_ACTIONS = {"listening_unmute", "assistant_unmute", "unmute_listening", "start_listening"}
SPEAKER_MUTE_ACTIONS = {"speaker_mute", "mute_speaker", "volume_mute"}
SPEAKER_UNMUTE_ACTIONS = {"speaker_unmute", "unmute_speaker", "volume_unmute"}
TRANSIENT_LIVE_MARKERS = (
    "operation was aborted",
    "connection closed",
    "connectionclosed",
    "keepalive ping timeout",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "network is unreachable",
    "temporary failure",
    "service unavailable",
    "try again",
)


def _get_api_key() -> str:
    return require_secret("GEMINI_API_KEY")


def _exception_leaves(error: BaseException) -> list[BaseException]:
    children = getattr(error, "exceptions", None)
    if children:
        leaves = []
        for child in children:
            leaves.extend(_exception_leaves(child))
        return leaves
    return [error]


def _live_error_summary(error: BaseException) -> str:
    summaries = []
    for leaf in _exception_leaves(error):
        text = " ".join(str(leaf).strip().split()) or type(leaf).__name__
        summary = f"{type(leaf).__name__}: {text[:180]}"
        if summary not in summaries:
            summaries.append(summary)
    return "; ".join(summaries[:3]) or type(error).__name__


def _is_transient_live_error(error: BaseException) -> bool:
    for leaf in _exception_leaves(error):
        if isinstance(leaf, (TimeoutError, ConnectionError, OSError, asyncio.TimeoutError)):
            continue
        module = type(leaf).__module__.casefold()
        name = type(leaf).__name__.casefold()
        text = str(leaf).casefold()
        if module.startswith("websockets") and "connectionclosed" in name:
            continue
        if module.startswith("google.genai") and name == "apierror":
            code = getattr(leaf, "code", None)
            if code in {408, 429, 500, 502, 503, 504, 1001, 1006, 1008, 1011}:
                continue
        if any(marker in text for marker in TRANSIENT_LIVE_MARKERS):
            continue
        return False
    return True


def _configured_audio_device(name: str) -> int | None:
    value = get_secret(name)
    try:
        selected = int(value) if value else None
    except (TypeError, ValueError):
        return None
    if selected is None:
        return None
    direction = "input" if name == "INPUT_DEVICE" else "output"
    saved_label = get_secret(f"{name}_NAME")
    if not saved_label:
        return selected
    try:
        # Import lazily because liquid_window is part of the desktop UI module
        # loaded by hud while this runtime module is being initialized.
        from .display.liquid_window import _canonical_audio_device, enumerate_audio_devices

        devices = sd.query_devices()
        candidates = enumerate_audio_devices(direction, devices, sd.query_hostapis())
        return _canonical_audio_device(selected, devices, candidates, saved_label)
    except Exception:
        # PortAudio can be temporarily unavailable while Windows is changing
        # endpoints.  The live refresh path will retry after the UI starts.
        return selected


def _audio_stream_format(
    device: int | None,
    direction: str,
    target_rate: int,
) -> tuple[int, int]:
    """Choose a format the selected Windows endpoint actually accepts."""
    info = sd.query_devices(device, direction)
    channel_key = "max_input_channels" if direction == "input" else "max_output_channels"
    max_channels = max(1, int(info[channel_key]))
    default_rate = int(round(float(info.get("default_samplerate") or target_rate)))
    rates = list(dict.fromkeys((default_rate, target_rate, 48000, 44100)))
    channels = list(dict.fromkeys((1, min(2, max_channels), max_channels)))
    checker = sd.check_input_settings if direction == "input" else sd.check_output_settings
    last_error: Exception | None = None
    for rate in rates:
        for channel_count in channels:
            try:
                checker(
                    device=device,
                    channels=channel_count,
                    dtype="int16",
                    samplerate=rate,
                )
                return rate, channel_count
            except Exception as exc:
                last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"No compatible {direction} audio format")


def _convert_pcm16(
    data: bytes,
    source_rate: int,
    target_rate: int,
    source_channels: int = 1,
    target_channels: int = 1,
) -> bytes:
    """Convert streaming PCM to the rate/channel layout required by an endpoint."""
    samples = np.frombuffer(data, dtype="<i2")
    if samples.size == 0:
        return b""
    source_channels = max(1, int(source_channels))
    usable = samples.size - (samples.size % source_channels)
    if usable <= 0:
        return b""
    mono = samples[:usable].reshape(-1, source_channels).astype(np.float32).mean(axis=1)
    if source_rate != target_rate and mono.size > 1:
        target_length = max(1, int(round(mono.size * target_rate / source_rate)))
        positions = np.arange(target_length, dtype=np.float32) * (source_rate / target_rate)
        mono = np.interp(positions, np.arange(mono.size, dtype=np.float32), mono)
    converted = np.clip(np.rint(mono), -32768, 32767).astype("<i2")
    if target_channels > 1:
        converted = np.repeat(converted[:, None], target_channels, axis=1).reshape(-1)
    return converted.tobytes()


def _restart_portaudio_backend() -> None:
    sd._terminate()
    sd._initialize()


def _configure_live_transport() -> None:
    """Harden Gemini Live's WebSocket connection on Raspberry Pi networks.

    The GenAI SDK doesn't currently expose WebSocket connection options on
    ``live.connect``.  Its default ten-second opening timeout is too short on
    some Pi/network combinations, and broken IPv6 routes can consume the
    entire timeout before IPv4 is attempted.  websockets 15+ also discovers
    system and environment proxies automatically; a proxy that works for
    ordinary HTTPS may still stall a WebSocket upgrade.  Keep the SDK
    implementation but supply conservative transport defaults before the
    client is created.
    """
    try:
        import google.genai.live as live_module

        original_connect = live_module.ws_connect
        if getattr(original_connect, "_jarvis_transport", False):
            return

        timeout = max(10.0, float(get_secret("LIVE_OPEN_TIMEOUT_SECONDS", "45")))
        ip_mode = get_secret("LIVE_IP_MODE", "").strip().lower()
        if not ip_mode:
            # Backwards compatibility with the first Pi image. "Force" now
            # means prefer IPv4, then fall back to the normal resolver; it no
            # longer removes every non-IPv4 route.
            legacy_force_ipv4 = get_secret("LIVE_FORCE_IPV4", "0").lower()
            ip_mode = (
                "ipv4-first"
                if legacy_force_ipv4 not in {"0", "false", "no", "off", ""}
                else ("auto" if is_desktop_mode() else "ipv4-first")
            )
        if ip_mode not in {
            "auto", "ipv4-first", "ipv6-first", "ipv4-only", "ipv6-only"
        }:
            ip_mode = "auto"
        use_system_proxy = get_secret(
            "LIVE_USE_SYSTEM_PROXY", "0" if not is_desktop_mode() else "1"
        ).lower() not in {"0", "false", "no", "off"}

        @contextlib.asynccontextmanager
        async def jarvis_ws_connect(*args, **kwargs):
            base_kwargs = dict(kwargs)
            base_kwargs.setdefault("open_timeout", timeout)
            if not use_system_proxy:
                # websockets.connect defaults to proxy=True since v15. A Pi
                # appliance on a normal LAN should connect directly; this
                # also guarantees that an address-family preference applies
                # to Google's host rather than to an auto-detected proxy.
                base_kwargs.setdefault("proxy", None)

            if "family" in base_kwargs:
                attempts = [base_kwargs]
            else:
                auto = dict(base_kwargs)
                ipv4 = {**base_kwargs, "family": socket.AF_INET}
                ipv6 = {**base_kwargs, "family": socket.AF_INET6}
                attempts = {
                    "auto": [auto, ipv4],
                    "ipv4-first": [ipv4, auto],
                    "ipv6-first": [ipv6, auto],
                    "ipv4-only": [ipv4],
                    "ipv6-only": [ipv6],
                }[ip_mode]

            websocket = None
            for index, attempt_kwargs in enumerate(attempts):
                try:
                    websocket = await original_connect(*args, **attempt_kwargs)
                    break
                except (TimeoutError, OSError) as exc:
                    if index == len(attempts) - 1:
                        raise
                    print(
                        "[JARVIS] Live handshake route failed "
                        f"({type(exc).__name__}); trying alternate route."
                    )

            if websocket is None:  # pragma: no cover - defensive invariant
                raise RuntimeError("No Gemini Live transport route was attempted.")
            try:
                yield websocket
            finally:
                await websocket.close()

        jarvis_ws_connect._jarvis_transport = True
        live_module.ws_connect = jarvis_ws_connect
    except Exception as exc:
        print(f"[JARVIS] Live transport defaults unavailable: {exc}")


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, a private Raspberry Pi voice assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results - always call the appropriate tool."
        )


def _normalized_action(args: dict) -> str:
    return str(args.get("action") or "").lower().strip().replace("-", "_").replace(" ", "_")


def _configured_thinking_level() -> types.ThinkingLevel:
    configured = get_secret("THINKING_LEVEL", "MEDIUM").strip().upper()
    return {
        "MINIMAL": types.ThinkingLevel.MINIMAL,
        "LOW": types.ThinkingLevel.LOW,
        "MEDIUM": types.ThinkingLevel.MEDIUM,
        "HIGH": types.ThinkingLevel.HIGH,
    }.get(configured, types.ThinkingLevel.MEDIUM)


def _is_wake_phrase(text: str) -> bool:
    return bool(WAKE_PATTERN.search(text or ""))
    
TOOL_DECLARATIONS = [
    {
        "name": "web_search",
        "description": (
            "Looks up live public information. Use only for current/latest facts, source links, news, "
            "prices, schedules, or recent public research. Do not use for ordinary conversation."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Search query"},
                "mode": {"type": "STRING", "description": "search or compare"},
                "items": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Items to compare"},
                "aspect": {"type": "STRING", "description": "Comparison aspect"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "social_insights",
        "description": (
            "Answers Instagram, IG, TikTok, and Zernio analytics questions for connected accounts. "
            "Use for followers, engagement, post performance, views, likes, comments, shares, reach, "
            "latest posts, last N posts, and account status."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "question": {"type": "STRING", "description": "The original social analytics question."},
                "platform": {"type": "STRING", "description": "instagram | tiktok | both"},
                "action": {"type": "STRING", "description": "ask | followers | accounts | latest_post | summary | recent_posts"},
                "days": {"type": "INTEGER", "description": "Days of analytics to inspect."},
                "post_count": {"type": "INTEGER", "description": "Number of recent posts requested."},
                "username": {"type": "STRING", "description": "Optional account username."},
            },
            "required": ["question"],
        },
    },
    {
        "name": "pi_controls",
        "description": (
            "Controls this Raspberry Pi assistant appliance only. Use listening_mute/listening_unmute "
            "when the user asks JARVIS to mute itself, stop listening, wake up, or listen again. "
            "Use speaker_mute/speaker_unmute only when the user asks to mute or unmute sound, volume, "
            "audio output, or speakers. Also controls volume, a configured Bluetooth speaker, and screen brightness. "
            "Do not use for room lights, desktop apps, files, keyboard, mouse, or general computer automation."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "listening_mute | listening_unmute | speaker_mute | speaker_unmute | "
                        "volume_set | volume_up | volume_down | brightness_set | brightness_up | "
                        "brightness_down | connect_speaker"
                    ),
                },
                "value": {"type": "STRING", "description": "Percent value for volume/brightness."},
                "description": {"type": "STRING", "description": "Original user command."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "home_control",
        "description": (
            "Controls Home Assistant smart-home lights and switches. Use this for room lights, lamps, "
            "bulbs, LED strips, desk lights, wall panels, monitor backlights, floor lamps, smart plugs, "
            "and Home Assistant entity status. Do not use pi_controls for room lights."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "turn_on | turn_off | toggle | status | list_entities | brightness_set",
                },
                "target": {
                    "type": "STRING",
                    "description": "Device/entity name, e.g. all lights, desk lamp, wall panel, monitor backlight.",
                },
                "domain": {"type": "STRING", "description": "light | switch | any"},
                "brightness": {"type": "INTEGER", "description": "Brightness percent for lights."},
                "description": {"type": "STRING", "description": "Original user command."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "jarvis_update",
        "description": (
            "Checks and installs official JARVIS Raspberry Pi updates from the configured GitHub "
            "repository. Use check when the user asks to search for updates. Use install only when "
            "the user explicitly confirms installation or clearly commands JARVIS to update itself. "
            "Never invent an available version and never install without explicit confirmation."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "check | install | status",
                },
                "confirmed": {
                    "type": "BOOLEAN",
                    "description": "True only if the user explicitly approved installation.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "computer_control",
        "description": (
            "Controls the local Windows desktop in constrained safety mode. Use observe before clicking "
            "and after any screen change; never guess coordinates. It opens searches and URLs in the Windows "
            "default browser, can list/open safe installed apps, inspect "
            "the active application's semantic controls, activate a control by ref, type into a named field, "
            "inspect the active window, click, move, drag, scroll, press keys, use safe hotkeys, and wait. For "
            "multi-step work inside an application, open it, wait, call inspect_ui, choose controls by their names "
            "and refs, use click_control or type_into_control, then inspect again after every navigation or state "
            "change. Prefer semantic controls over coordinates. Use observe and coordinate actions only when the "
            "needed control is not exposed through inspect_ui. Continue until the requested end state is verified. "
            "It cannot use terminals, PowerShell, the registry, Windows settings, installers, or arbitrary paths. "
            "Set sensitive=true for anything that sends/publishes content, purchases, deletes data, changes an "
            "account, closes unsaved work, uploads/shares a file, or could otherwise have an external consequence. "
            "Sensitive actions require explicit user approval and a local Windows confirmation dialog."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "observe | browser_search | open_url | inspect_ui | find_ui | click_named | "
                        "click_control | type_into_control | list_apps | open_app | active_window | click | "
                        "double_click | right_click | move | drag | scroll | type_text | press_key | hotkey | wait"
                    ),
                },
                "app": {"type": "STRING", "description": "Installed application display name for open_app."},
                "query": {
                    "type": "STRING",
                    "description": "Search terms for browser_search, find_ui, or click_named.",
                },
                "url": {
                    "type": "STRING",
                    "description": "Complete http/https URL for open_url.",
                },
                "ref": {
                    "type": "STRING",
                    "description": "Control ref returned by the latest inspect_ui call, e.g. ui4.",
                },
                "limit": {
                    "type": "INTEGER",
                    "description": "Maximum controls/apps to return, from 10 to 120.",
                },
                "x": {"type": "INTEGER", "description": "X coordinate in the most recent observed screenshot."},
                "y": {"type": "INTEGER", "description": "Y coordinate in the most recent observed screenshot."},
                "end_x": {"type": "INTEGER", "description": "Drag destination X coordinate."},
                "end_y": {"type": "INTEGER", "description": "Drag destination Y coordinate."},
                "amount": {"type": "INTEGER", "description": "Scroll steps, positive up and negative down."},
                "text": {"type": "STRING", "description": "Text to type into the active application."},
                "replace": {
                    "type": "BOOLEAN",
                    "description": "For type_into_control, replace the current field value; defaults to true.",
                },
                "submit": {
                    "type": "BOOLEAN",
                    "description": "For type_into_control, press Enter after typing.",
                },
                "key": {"type": "STRING", "description": "Single key name for press_key."},
                "keys": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Key names for hotkey, e.g. [ctrl, l].",
                },
                "presses": {"type": "INTEGER", "description": "Number of key presses, maximum 20."},
                "seconds": {"type": "NUMBER", "description": "Wait duration, maximum five seconds."},
                "sensitive": {
                    "type": "BOOLEAN",
                    "description": "True for any consequential action that requires local confirmation.",
                },
                "confirmed": {
                    "type": "BOOLEAN",
                    "description": "True only after the user explicitly approved this exact sensitive action.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "shutdown_jarvis",
        "description": "Shuts down the assistant when the user clearly asks to stop, quit, close, or end JARVIS.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "save_memory",
        "description": (
            "Silently saves a durable, non-sensitive user fact, preference, project, goal, relationship, or note. "
            "Never save passwords, API keys, authentication tokens, payment details, or one-time commands."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {"type": "STRING", "description": "identity | preferences | projects | relationships | wishes | notes"},
                "key": {"type": "STRING", "description": "Short snake_case key."},
                "value": {"type": "STRING", "description": "Concise value in English."},
                "importance": {"type": "NUMBER", "description": "Long-term relevance from 0.0 to 1.0."},
            },
            "required": ["category", "key", "value"],
        },
    },
    {
        "name": "recall_memory",
        "description": (
            "Retrieves relevant long-term memories when the user refers to previous preferences, projects, "
            "people, goals, or facts that are not already present in the current context."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "What should be recalled."},
                "limit": {"type": "INTEGER", "description": "Maximum memories to return, from 1 to 12."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "forget_memory",
        "description": "Deletes one stored memory only when the user explicitly asks JARVIS to forget it.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {"type": "STRING", "description": "Memory category."},
                "key": {"type": "STRING", "description": "Exact memory key to delete."},
            },
            "required": ["category", "key"],
        },
    },
    {
        "name": "task_plan",
        "description": (
            "Creates and maintains a checkable plan for genuinely complex multi-step tasks. Start before the first "
            "action, mark one step in_progress at a time, mark it completed only after verification, and complete "
            "the plan only when all requested work is verified. Do not use for simple questions or one-step commands."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "start | update | status | complete | cancel | blocked"},
                "goal": {"type": "STRING", "description": "The user's requested outcome."},
                "steps": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Two to twelve concrete, verifiable steps for start.",
                },
                "step": {"type": "INTEGER", "description": "One-based step number for update."},
                "status": {"type": "STRING", "description": "pending | in_progress | completed | blocked"},
                "note": {"type": "STRING", "description": "Short evidence, result, or blocking reason."},
            },
            "required": ["action"],
        },
    },
]


def _available_tool_declarations() -> list[dict]:
    if is_desktop_mode():
        disabled = {"pi_controls", "social_insights", "jarvis_update"}
        return [item for item in TOOL_DECLARATIONS if item.get("name") not in disabled]
    return [item for item in TOOL_DECLARATIONS if item.get("name") != "computer_control"]


class JarvisLive:

    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self.session        = None
        self.audio_in_queue = None
        self.out_queue      = None
        self.mic_raw_queue  = None
        self._loop          = None
        self._is_speaking   = False
        self._speaking_lock = threading.Lock()
        self._state_mtime   = 0.0
        self._last_state_check = 0.0
        self._pre_roll = deque(maxlen=12)
        self.plans = PlanManager()
        self.updates = UpdateManager()
        self._pending_update: ReleaseInfo | None = None
        self._restart_requested = False
        self._restart_fallback_started = False
        self._microphone_available: bool | None = None
        self._speaker_available: bool | None = None
        self._input_device = _configured_audio_device("INPUT_DEVICE")
        self._output_device = _configured_audio_device("OUTPUT_DEVICE")
        self._input_device_generation = 0
        self._output_device_generation = 0
        self._input_stream_open = False
        self._output_stream_open = False
        self._audio_backend_refreshing = False
        self._audio_backend_refresh_pending = False
        self._audio_backend_refresh_task = None
        self.wake_gate = WakeWordGate(
            mode=get_secret("WAKE_MODE", "wakeword").lower(),
            threshold=float(get_secret("WAKE_THRESHOLD", "0.55")),
            conversation_seconds=float(get_secret("CONVERSATION_TIMEOUT_SECONDS", "12")),
            voice_rms_threshold=int(get_secret("VOICE_RMS_THRESHOLD", "300")),
        )
        self.ui.on_text_command = self._on_text_command
        self.ui.on_manual_activate = self._manual_activate
        if is_desktop_mode() and hasattr(self.ui, "on_audio_devices_changed"):
            self.ui.on_audio_devices_changed = self._on_audio_devices_changed
        if is_desktop_mode() and hasattr(self.ui, "on_audio_refresh_requested"):
            self.ui.on_audio_refresh_requested = self._on_audio_refresh_requested
        self.ui.muted = listening_state.get_listening_muted(False)
        if self.wake_gate.mode == "wakeword" and not self.wake_gate.available:
            self.ui.write_log(f"ERR: Local wake word unavailable: {self.wake_gate.error}")
            self.ui.write_log("SYS: Privacy fallback active; use typed commands until it is repaired.")
        elif self.wake_gate.error:
            self.ui.write_log(f"WARN: {self.wake_gate.error}")

    def _on_audio_devices_changed(self, input_device, output_device) -> None:
        try:
            input_device = int(input_device) if input_device is not None else None
        except (TypeError, ValueError):
            input_device = None
        try:
            output_device = int(output_device) if output_device is not None else None
        except (TypeError, ValueError):
            output_device = None

        if input_device != self._input_device:
            self._input_device = input_device
            self._input_device_generation += 1
            self.ui.write_log("SYS: Dispositivo de entrada actualizado.")

        if output_device != self._output_device:
            self._output_device = output_device
            self._output_device_generation += 1
            self.ui.write_log("SYS: Dispositivo de salida actualizado.")
            if self._loop is not None and self.audio_in_queue is not None:
                self._loop.call_soon_threadsafe(self._request_output_stream_restart)

    def _request_output_stream_restart(self) -> None:
        if self.audio_in_queue is None:
            return
        while not self.audio_in_queue.empty():
            try:
                self.audio_in_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.audio_in_queue.put_nowait(None)

    def _on_audio_refresh_requested(self) -> None:
        self._audio_backend_refresh_pending = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._schedule_audio_backend_refresh)

    def _schedule_audio_backend_refresh(self) -> None:
        if self._audio_backend_refresh_task and not self._audio_backend_refresh_task.done():
            return
        self._audio_backend_refresh_task = asyncio.create_task(
            self._refresh_audio_backend()
        )

    async def _refresh_audio_backend(self) -> None:
        self._audio_backend_refreshing = True
        self._input_device_generation += 1
        self._output_device_generation += 1
        self._request_output_stream_restart()
        deadline = time.monotonic() + 2.0
        while (
            self._input_stream_open or self._output_stream_open
        ) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        try:
            if self._input_stream_open or self._output_stream_open:
                raise RuntimeError("audio streams did not close before refresh")
            await asyncio.to_thread(_restart_portaudio_backend)
            self._audio_backend_refresh_pending = False
            refresh_ui = getattr(self.ui, "refresh_audio_devices", None)
            if refresh_ui is not None:
                refresh_ui()
            self.ui.write_log("SYS: Lista de dispositivos de audio actualizada.")
        except Exception as exc:
            self.ui.write_log(f"ERR: No se pudo actualizar el audio: {exc}")
        finally:
            self._audio_backend_refreshing = False

    def _manual_activate(self):
        if self.ui.muted:
            self.set_listening_muted(False)
        self.wake_gate.activate()
        self.ui.set_state("LISTENING")
        self.ui.write_log("SYS: Conversation opened manually.")

    def _sync_external_listening_state(self):
        now = time.monotonic()
        if now - self._last_state_check < 0.5:
            return
        self._last_state_check = now
        try:
            mtime = listening_state.STATE_FILE.stat().st_mtime
        except FileNotFoundError:
            return
        except Exception:
            return
        if mtime <= self._state_mtime:
            return
        self._state_mtime = mtime
        muted = listening_state.get_listening_muted(self.ui.muted)
        if muted != self.ui.muted:
            self.ui.muted = muted
            self.ui.set_state("MUTED" if muted else "LISTENING")
            self.ui.write_log("SYS: Listening muted by control file." if muted else "SYS: Listening resumed by control file.")

    def _on_text_command(self, text: str):
        text = str(text or "").strip()
        if not text:
            return
        self.ui.write_log(f"You: {text}")
        if not self._loop or not self.session:
            self.ui.write_log("ERR: Gemini no esta conectado. Reintentando la conexion...")
            self.ui.set_state("STANDBY")
            return
        self.ui.set_state("THINKING")
        relevant_memory = format_memory_for_prompt(query=text, limit=8)
        active_plan = getattr(self, "plans", PlanManager()).load()
        plan_context = format_plan_for_prompt(active_plan)
        planning_instruction = ""
        if is_complex_request(text) and not plan_context:
            self.ui.write_log("SYS: Tarea compleja detectada; preparando un plan verificable.")
            planning_instruction = (
                "[INTERNAL COMPLEX-TASK INSTRUCTION]\n"
                "Before taking the first action, call task_plan with action=start and 2-12 concrete steps. "
                "Update each step as work progresses. Do not declare success until every step is verified.\n"
            )
        payload = (
            f"{planning_instruction}{plan_context}{relevant_memory}"
            f"[USER REQUEST]\n{text}"
        )
        future = asyncio.run_coroutine_threadsafe(
            self.session.send_realtime_input(text=payload), self._loop
        )
        future.add_done_callback(self._text_send_finished)

    def _text_send_finished(self, future):
        try:
            future.result()
        except Exception as exc:
            self.ui.write_log(f"ERR: No se pudo enviar la orden: {exc}")
            self.ui.set_state("STANDBY")

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            was_speaking = self._is_speaking
            self._is_speaking = value
        if value:
            if self.ui.muted:
                return
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            if was_speaking:
                self.wake_gate.extend_conversation()
            self.ui.set_state("LISTENING" if self.wake_gate.active else "STANDBY")
            if was_speaking and self._restart_requested:
                self._restart_requested = False
                threading.Thread(target=self._exit_for_update, daemon=True).start()

    @staticmethod
    def _exit_for_update():
        # Exit code 75 is handled by start_jarvis_pi.sh, which keeps the X
        # session alive and starts the newly installed code.
        time.sleep(1.0)
        os._exit(75)

    def _request_restart_after_response(self):
        self._restart_requested = True
        if self._restart_fallback_started:
            return
        self._restart_fallback_started = True

        def fallback():
            # A muted/broken speaker may never produce a speaking transition.
            # Do not leave a successfully installed update pending forever.
            time.sleep(30)
            if self._restart_requested:
                os._exit(75)

        threading.Thread(target=fallback, daemon=True).start()

    def set_listening_muted(self, value: bool, reason: str = "") -> str:
        listening_state.set_listening_muted(value)
        try:
            self._state_mtime = listening_state.STATE_FILE.stat().st_mtime
        except Exception:
            pass
        self.ui.muted = value
        if value:
            self.ui.set_state("MUTED")
            self.ui.write_log("SYS: Listening muted. Say 'JARVIS wake up' to resume.")
            return "Listening muted. Say 'JARVIS wake up' to resume."
        self.ui.set_state("LISTENING")
        self.ui.write_log("SYS: Listening resumed.")
        return "Listening resumed."

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_realtime_input(text=text),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} - {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _queue_mic_chunk(self, data: bytes):
        if not self.out_queue:
            return
        payload = {"data": data, "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}"}
        try:
            if self.out_queue.full():
                self.out_queue.get_nowait()
            self.out_queue.put_nowait(payload)
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass

    def _queue_raw_mic_chunk(self, data: bytes):
        if not self.mic_raw_queue:
            return
        try:
            if self.mic_raw_queue.full():
                self.mic_raw_queue.get_nowait()
            self.mic_raw_queue.put_nowait(data)
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass

    async def _process_mic_audio(self):
        """Keep room audio local until the wake word opens a conversation."""
        forwarding = self.wake_gate.active
        while True:
            data = await self.mic_raw_queue.get()
            self._pre_roll.append(data)
            if self.ui.muted:
                self.wake_gate.deactivate()
                continue

            detected, score = await asyncio.to_thread(self.wake_gate.process, data)
            if detected:
                self.ui.write_log(f"SYS: Hey Jarvis detected ({score:.2f}).")
                self.ui.set_state("LISTENING")
                for chunk in self._pre_roll:
                    self._queue_mic_chunk(chunk)
                self._pre_roll.clear()
                forwarding = True
                continue

            if self.wake_gate.active:
                if self.wake_gate.contains_voice(data):
                    self.wake_gate.extend_conversation()
                self._queue_mic_chunk(data)
                forwarding = True
            elif forwarding:
                self.ui.set_state("STANDBY")
                self.ui.write_log("SYS: Conversation closed. Say 'Hey Jarvis'.")
                forwarding = False

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        plan_str   = format_plan_for_prompt(getattr(self, "plans", PlanManager()).load())
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y - %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        parts = [time_ctx]
        if is_desktop_mode():
            parts.append(
                "[DESKTOP SAFETY MODE]\n"
                "You may control ordinary Windows applications with computer_control. Always observe the screen "
                "before clicking and observe again after navigation or a visual change. Never invent coordinates "
                "or claim success without visual/tool evidence. For multi-step work inside any application, keep "
                "working after open_app: wait for it, call inspect_ui, identify controls by name and ref, use "
                "click_control or type_into_control, and inspect again after each state change until the requested "
                "result is verified. Use find_ui when the active application or web page has too many controls, "
                "and click_named only when its match is unambiguous. Requests to search, open, show, watch, or play "
                "something on the computer are interaction tasks: use browser_search or open_url so they open in "
                "the Windows default browser; do not use factual web_search as a substitute. After browser_search, "
                "wait for the page, find and activate the relevant result, then keep inspecting and interacting. "
                "For requested media playback, success means playback is actually active, not merely that search "
                "results or a media page are visible. Activate the page's named play control and verify that it "
                "changed to pause/playing; if the player exposes no semantic control, observe it and use a verified "
                "coordinate click or the focused player's safe play/pause key, then observe again. "
                "Prefer these semantic controls over coordinates. Fall back to observe and "
                "coordinate actions only for controls that the application does not expose. Never try to open or "
                "control PowerShell, CMD, "
                "Terminal, Registry Editor, Control Panel, Windows Settings, installers, system administration, "
                "or arbitrary executable/file paths. Mark any consequential action as sensitive and ask for explicit "
                "approval before retrying it with confirmed=true; the user must also approve the native dialog. "
                "Examples include sending or publishing, purchases, deleting, account changes, uploads/shares, "
                "and closing unsaved work. Stop if the screen is ambiguous."
            )
        if mem_str:
            parts.append(mem_str)
        if plan_str:
            parts.append(plan_str)
        parts.append(sys_prompt)

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            thinking_config=types.ThinkingConfig(
                include_thoughts=False,
                thinking_level=_configured_thinking_level(),
            ),
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": _available_tool_declarations()}],
            session_resumption=types.SessionResumptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Charon"
                    )
                )
            ),
        )

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] Tool request: {name} {args}")
        action = _normalized_action(args)
        if self.ui.muted:
            print(f"[JARVIS] muted: ignored tool {name}/{action}")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "Assistant listening is muted. Only the exact wake phrase can resume listening."}
            )

        self.ui.set_state("THINKING")
        if name == "save_memory":
            category = args.get("category", "notes")
            key = args.get("key", "")
            value = args.get("value", "")
            importance = args.get("importance", 0.5)
            result = remember(key, value, category, importance) if key and value else "Memory requires a key and value."
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": result, "silent": True}
            )

        if name == "recall_memory":
            query = str(args.get("query") or "").strip()
            limit = min(12, max(1, int(args.get("limit") or 8)))
            matches = search_memories(query, limit=limit) if query else []
            result = [
                {
                    "category": item["category"],
                    "key": item["key"],
                    "value": item["value"],
                    "updated_at": item["updated_at"],
                }
                for item in matches
            ]
            return types.FunctionResponse(
                id=fc.id,
                name=name,
                response={"result": result or "No relevant memory was found."},
            )

        if name == "forget_memory":
            result = forget_memory(args.get("key", ""), args.get("category", "notes"))
            return types.FunctionResponse(id=fc.id, name=name, response={"result": result})

        if name == "task_plan":
            manager = getattr(self, "plans", None)
            if manager is None:
                manager = self.plans = PlanManager()
            try:
                if action == "start":
                    plan = manager.start(args.get("goal", ""), args.get("steps") or [])
                    self.ui.write_log(f"PLAN: {plan['goal']}")
                elif action == "update":
                    plan = manager.update(
                        args.get("step"), args.get("status", "in_progress"), args.get("note", "")
                    )
                    current = plan["steps"][int(args.get("step")) - 1]
                    self.ui.write_log(
                        f"PLAN: Paso {current['index']} {current['status']}: {current['text']}"
                    )
                elif action == "status":
                    plan = manager.load()
                elif action in {"complete", "cancel", "blocked"}:
                    final_status = {
                        "complete": "completed",
                        "cancel": "cancelled",
                        "blocked": "blocked",
                    }[action]
                    plan = manager.finish(final_status, args.get("note", ""))
                    self.ui.write_log(f"PLAN: {plan['status']}: {plan['goal']}")
                else:
                    raise ValueError("Unknown plan action.")
                result = plan or "There is no active plan."
            except (TypeError, ValueError) as exc:
                result = f"Plan update rejected: {exc}"
            return types.FunctionResponse(id=fc.id, name=name, response={"result": result})

        loop = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "web_search":
                r = await loop.run_in_executor(None, lambda: web_search_action(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "social_insights":
                if is_desktop_mode():
                    result = "Social account analytics are not included in the desktop edition."
                    return types.FunctionResponse(id=fc.id, name=name, response={"result": result})
                from importlib import import_module

                zernio_social = import_module("omar_ai_core.tools.social_metrics").zernio_social
                if not args.get("action"):
                    args["action"] = "ask"
                r = await loop.run_in_executor(None, lambda: zernio_social(parameters=args, player=self.ui))
                result = r or "No social analytics data was returned."

            elif name == "pi_controls":
                if is_desktop_mode():
                    result = "Computer controls are disabled in the desktop edition."
                    return types.FunctionResponse(id=fc.id, name=name, response={"result": result})
                if action in LISTENING_MUTE_ACTIONS:
                    result = self.set_listening_muted(True)
                    return types.FunctionResponse(
                        id=fc.id, name=name,
                        response={"result": result}
                    )
                if action in LISTENING_UNMUTE_ACTIONS:
                    result = self.set_listening_muted(False)
                    return types.FunctionResponse(
                        id=fc.id, name=name,
                        response={"result": result}
                    )
                if action in SPEAKER_MUTE_ACTIONS:
                    args["action"] = "mute"
                elif action in SPEAKER_UNMUTE_ACTIONS:
                    args["action"] = "unmute"
                r = await loop.run_in_executor(None, lambda: pi_controls(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "home_control":
                r = await loop.run_in_executor(None, lambda: home_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "computer_control":
                if not is_desktop_mode():
                    result = "Computer control is available only in the Windows desktop edition."
                    return types.FunctionResponse(id=fc.id, name=name, response={"result": result})
                from .display.visual_config import load_visual_settings

                if not load_visual_settings().computer_control_enabled:
                    result = "Computer control is disabled in Jarvis visual settings."
                elif action == "observe":
                    frame = await loop.run_in_executor(None, capture_screen_jpeg)
                    await self.session.send_realtime_input(
                        video=types.Blob(data=frame.jpeg, mime_type="image/jpeg")
                    )
                    result = {
                        "status": "Current desktop screenshot sent for visual inspection.",
                        "screenshot_size": [frame.width, frame.height],
                        "coordinate_origin": [frame.origin_x, frame.origin_y],
                        "active_window": frame.active_title,
                        "active_process": frame.active_process,
                    }
                else:
                    result = await loop.run_in_executor(
                        None, lambda: computer_control(parameters=args, player=self.ui)
                    )

            elif name == "jarvis_update":
                if is_desktop_mode():
                    result = "Remote self-updates are currently available only on the Raspberry Pi edition."
                    return types.FunctionResponse(id=fc.id, name=name, response={"result": result})
                update_action = action or "check"
                if update_action == "status":
                    status = self.updates.status()
                    result = (
                        f"JARVIS version {status.get('installed_version', 'unknown')}; "
                        f"update state: {status.get('status', 'idle')}."
                    )
                elif update_action == "check":
                    self.ui.write_log("SYS: Buscando actualizaciones seguras en GitHub...")
                    check = await loop.run_in_executor(None, self.updates.check_for_updates)
                    if check.available and check.release:
                        self._pending_update = check.release
                        notes = check.release.notes.strip()
                        summary = f" Changes: {notes[:500]}" if notes else ""
                        result = (
                            f"JARVIS {check.release.version} is available; the installed version is "
                            f"{check.current_version}.{summary} Ask the user to confirm before installing."
                        )
                    else:
                        self._pending_update = None
                        result = f"JARVIS is already up to date at version {check.current_version}."
                elif update_action == "install":
                    if not bool(args.get("confirmed")):
                        result = "Installation was not started. Ask the user for explicit confirmation first."
                    else:
                        release = self._pending_update
                        if release is None:
                            check = await loop.run_in_executor(None, self.updates.check_for_updates)
                            release = check.release if check.available else None
                        if release is None:
                            result = "JARVIS is already up to date; nothing was installed."
                        else:
                            self.ui.write_log(
                                f"SYS: Instalando JARVIS {release.version}; no desconectes la alimentación..."
                            )
                            installed = await loop.run_in_executor(
                                None, lambda: self.updates.install(release)
                            )
                            self._pending_update = None
                            self._request_restart_after_response()
                            result = (
                                f"JARVIS {installed.installed_version} was installed and validated. "
                                "Tell the user it will restart now."
                            )
                else:
                    result = "Unknown update action. Use check, install, or status."

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")
                self.speak("Goodbye, sir.")

                def _shutdown():
                    import time, os
                    time.sleep(1)
                    os._exit(0)

                threading.Thread(target=_shutdown, daemon=True).start()
            else:
                result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] Tool result: {name} - {str(result)[:80]}")

        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(audio=msg)

    async def _listen_audio(self):
        print("[JARVIS] Mic started")
        loop = asyncio.get_event_loop()
        generation = self._input_device_generation
        device = self._input_device
        stream_rate, stream_channels = _audio_stream_format(
            device, "input", SEND_SAMPLE_RATE
        )

        def callback(indata, frames, time_info, status):
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking
            if not jarvis_speaking:
                data = _convert_pcm16(
                    indata.tobytes(),
                    stream_rate,
                    SEND_SAMPLE_RATE,
                    stream_channels,
                    CHANNELS,
                )
                if not data:
                    return
                # Reuse the recognition PCM for visual analysis.  The UI does
                # not open a second microphone stream or compete for access.
                feed_visual = getattr(self.ui, "feed_input_audio", None)
                if feed_visual is not None:
                    feed_visual(data, SEND_SAMPLE_RATE)
                loop.call_soon_threadsafe(
                    self._queue_raw_mic_chunk,
                    data
                )

        try:
            with sd.InputStream(
                device=device,
                samplerate=stream_rate,
                channels=stream_channels,
                dtype="int16",
                blocksize=max(256, int(round(CHUNK_SIZE * stream_rate / SEND_SAMPLE_RATE))),
                callback=callback,
            ):
                print("[JARVIS] Mic stream open")
                self._input_stream_open = True
                while True:
                    if generation != self._input_device_generation:
                        return
                    self._microphone_available = True
                    self._sync_external_listening_state()
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] Mic error: {e}")
            raise
        finally:
            self._input_stream_open = False

    async def _receive_audio(self):
        print("[JARVIS] Receive started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():

                    if response.data and not self.ui.muted:
                        self.audio_in_queue.put_nowait(response.data)

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = sc.output_transcription.text.strip()
                            if txt and not self.ui.muted:
                                self.set_speaking(True)
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = sc.input_transcription.text.strip()
                            if txt:
                                in_buf.append(txt)
                                if self.ui.muted and _is_wake_phrase(txt):
                                    self.set_listening_muted(False)
                                    out_buf = []
                                    while self.audio_in_queue and not self.audio_in_queue.empty():
                                        try:
                                            self.audio_in_queue.get_nowait()
                                        except asyncio.QueueEmpty:
                                            break

                        if sc.turn_complete:
                            self.set_speaking(False)

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                if self.ui.muted:
                                    self.ui.write_log("SYS: Muted speech ignored.")
                                else:
                                    self.ui.write_log(f"You: {full_in}")
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out and not self.ui.muted:
                                self.ui.write_log(f"Jarvis: {full_out}")
                            out_buf = []

                            # Disabled for the Pi appliance runtime: automatic memory
                            # extraction was causing slow background OpenRouter calls
                            # after normal voice turns.

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] Tool call: {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )

        except Exception as e:
            print(f"[JARVIS] Receive channel closed: {_live_error_summary(e)}")
            if not _is_transient_live_error(e):
                traceback.print_exc()
            raise

    async def _listen_audio_resilient(self):
        """Keep Gemini's text channel alive when audio input is unavailable."""
        while True:
            if getattr(self, "_audio_backend_refreshing", False):
                await asyncio.sleep(0.05)
                continue
            try:
                await self._listen_audio()
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                first_failure = self._microphone_available is not False
                self._microphone_available = False
                print(f"[JARVIS] Microphone unavailable; text mode active: {exc}")
                if first_failure:
                    self.ui.write_log(
                        "WARN: Microphone unavailable. Text commands remain active; "
                        "connect or configure INPUT_DEVICE to restore voice input."
                    )
                # Retry quietly so hot-plugging a microphone restores voice
                # without restarting Jarvis, but never disconnect Gemini.
                await asyncio.sleep(5.0)

    async def _play_audio(self):
        print("[JARVIS] Playback started")
        loop = asyncio.get_event_loop()
        generation = self._output_device_generation
        device = self._output_device
        stream_rate, stream_channels = _audio_stream_format(
            device, "output", RECEIVE_SAMPLE_RATE
        )

        stream = sd.RawOutputStream(
            device=device,
            samplerate=stream_rate,
            channels=stream_channels,
            dtype="int16",
            blocksize=CHUNK_SIZE,
        )
        try:
            stream.start()
            self._output_stream_open = True
            self._speaker_available = True
            print(
                f"[JARVIS] Audio output ready: {sd.query_devices(stream.device)['name']} "
                f"({stream_rate} Hz, {stream_channels} ch)"
            )
            while True:
                chunk = await self.audio_in_queue.get()
                if chunk is None or generation != self._output_device_generation:
                    return
                if self.ui.muted:
                    while self.audio_in_queue and not self.audio_in_queue.empty():
                        try:
                            self.audio_in_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    continue
                print(f"[JARVIS] Playing Gemini audio ({len(chunk)} bytes initial chunk)")
                self.set_speaking(True)
                feed_visual = getattr(self.ui, "feed_output_audio", None)
                if feed_visual is not None:
                    feed_visual(chunk, RECEIVE_SAMPLE_RATE)
                device_chunk = _convert_pcm16(
                    chunk,
                    RECEIVE_SAMPLE_RATE,
                    stream_rate,
                    CHANNELS,
                    stream_channels,
                )
                await asyncio.to_thread(stream.write, device_chunk)
                while True:
                    try:
                        chunk = await asyncio.wait_for(self.audio_in_queue.get(), timeout=0.18)
                    except asyncio.TimeoutError:
                        self.set_speaking(False)
                        break
                    if chunk is None or generation != self._output_device_generation:
                        return
                    if self.ui.muted:
                        self.set_speaking(False)
                        break
                    if feed_visual is not None:
                        feed_visual(chunk, RECEIVE_SAMPLE_RATE)
                    device_chunk = _convert_pcm16(
                        chunk,
                        RECEIVE_SAMPLE_RATE,
                        stream_rate,
                        CHANNELS,
                        stream_channels,
                    )
                    await asyncio.to_thread(stream.write, device_chunk)
        except Exception as e:
            print(f"[JARVIS] Playback error: {e}")
            raise
        finally:
            self.set_speaking(False)
            self._output_stream_open = False
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()

    async def _play_audio_resilient(self):
        """Reopen audio output after device changes or temporary failures."""
        while True:
            if getattr(self, "_audio_backend_refreshing", False):
                await asyncio.sleep(0.05)
                continue
            try:
                await self._play_audio()
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                first_failure = self._speaker_available is not False
                self._speaker_available = False
                print(f"[JARVIS] Audio output unavailable: {exc}")
                if first_failure:
                    self.ui.write_log(
                        "WARN: Audio output unavailable. Select another output "
                        "device in Settings to restore sound."
                    )
                while self.audio_in_queue and not self.audio_in_queue.empty():
                    try:
                        self.audio_in_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                await asyncio.sleep(5.0)

    async def run(self):
        _configure_live_transport()
        client = genai.Client(
            api_key=_get_api_key(),
            http_options={"api_version": "v1beta"}
        )

        consecutive_failures = 0
        retry_delay = 3.0
        while True:
            session_started_at = None
            try:
                print("[JARVIS] Connecting...")
                # Keep ERROR stable while retrying in the background. The
                # previous ERROR -> THINKING -> ERROR cycle every three
                # seconds looked like a broken, flickering interface.
                if consecutive_failures == 0:
                    self.ui.set_state("THINKING")
                config = self._build_config()

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    session_started_at = time.monotonic()
                    self.session        = session
                    self._loop          = asyncio.get_event_loop()
                    self.audio_in_queue = asyncio.Queue()
                    self.out_queue      = asyncio.Queue(maxsize=10)
                    self.mic_raw_queue  = asyncio.Queue(maxsize=40)
                    if self._audio_backend_refresh_pending:
                        try:
                            await asyncio.to_thread(_restart_portaudio_backend)
                            self._audio_backend_refresh_pending = False
                            refresh_ui = getattr(self.ui, "refresh_audio_devices", None)
                            if refresh_ui is not None:
                                refresh_ui()
                        except Exception as exc:
                            self.ui.write_log(f"ERR: No se pudo inicializar el audio: {exc}")

                    print("[JARVIS] Connected.")
                    self.ui.set_state("LISTENING" if self.wake_gate.active else "STANDBY")
                    mode_msg = "continuous listening" if self.wake_gate.mode == "continuous" else "say 'Hey Jarvis'"
                    self.ui.write_log(f"SYS: JARVIS online; {mode_msg}.")

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio_resilient())
                    tg.create_task(self._process_mic_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio_resilient())
                    
            except Exception as e:
                summary = _live_error_summary(e)
                transient = _is_transient_live_error(e)
                connected_seconds = (
                    time.monotonic() - session_started_at if session_started_at is not None else 0.0
                )
                if connected_seconds >= 30.0:
                    consecutive_failures = 0
                    retry_delay = 3.0
                consecutive_failures += 1
                escalated = not transient or consecutive_failures >= 4
                print(f"[JARVIS] Live session interrupted: {summary}")
                if escalated:
                    traceback.print_exc()
                try:
                    with RUNTIME_LOG_PATH.open("a", encoding="utf-8") as handle:
                        level = "ERROR" if escalated else "WARN"
                        handle.write(
                            f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                            f"{level} live_session: {summary}\n"
                        )
                        if escalated:
                            traceback.print_exc(file=handle)
                except Exception:
                    pass
                if escalated:
                    self.ui.write_log(f"ERR: Gemini no pudo mantener la conexión: {summary[:160]}")
                    self.ui.set_state("ERROR")
                else:
                    self.ui.write_log(
                        "WARN: La conexión con Gemini se interrumpió; "
                        "JARVIS se reconectará automáticamente."
                    )
                    self.ui.set_state("STANDBY")

            self.set_speaking(False)
            self.session = None
            delay = retry_delay
            print(f"[JARVIS] Reconnecting in {delay:.0f}s...")
            await asyncio.sleep(delay)
            retry_delay = min(retry_delay * 2.0, 30.0)

def main():
    try:
        (PROJECT_DIR / "assistant.pid").write_text(str(os.getpid()), encoding="utf-8")
    except Exception as e:
        print(f"[JARVIS] PID write failed: {e}")

    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n[JARVIS] Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()


if __name__ == "__main__":
    main()
