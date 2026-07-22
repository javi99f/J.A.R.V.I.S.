from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path


def _check(name: str, action, report: dict) -> None:
    started = time.monotonic()
    try:
        details = action()
        report["checks"][name] = {
            "ok": True,
            "milliseconds": round((time.monotonic() - started) * 1000),
            "details": details,
        }
    except Exception as exc:
        report["checks"][name] = {
            "ok": False,
            "milliseconds": round((time.monotonic() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }


def run_self_test(output_path: Path, *, include_audio: bool = True) -> dict:
    report = {
        "ok": False,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "checks": {},
    }

    def check_memory():
        from omar_ai_core.memory import store

        with tempfile.TemporaryDirectory() as temporary:
            original_db, original_legacy = store.MEMORY_DB_PATH, store.MEMORY_PATH
            try:
                root = Path(temporary)
                store.MEMORY_DB_PATH = root / "jarvis-memory.db"
                store.MEMORY_PATH = root / "long_term.json"
                result = store.remember("self_test", "packaged memory works", "notes", 0.8)
                matches = store.search_memories("packaged memory", limit=2)
                if result != "Remembered." or not matches:
                    raise RuntimeError("memory could not be stored and retrieved")
                return {"database": "sqlite", "retrieved": matches[0]["key"]}
            finally:
                store.MEMORY_DB_PATH, store.MEMORY_PATH = original_db, original_legacy

    def check_planning():
        from omar_ai_core.planning import PlanManager

        with tempfile.TemporaryDirectory() as temporary:
            manager = PlanManager(Path(temporary) / "plan.json")
            manager.start("Self test", ["Create plan", "Verify plan"])
            manager.update(1, "completed", "created")
            manager.update(2, "completed", "verified")
            plan = manager.finish("completed")
            return {"status": plan["status"], "steps": len(plan["steps"])}

    def check_wakeword():
        from omar_ai_core.audio.wakeword import WakeWordGate

        gate = WakeWordGate(mode="wakeword")
        if not gate.available:
            raise RuntimeError(gate.error or "wake word model unavailable")
        detected, score = gate.process(bytes(gate.FRAME_SAMPLES * 2))
        return {
            "backend": type(gate._model).__name__,
            "silence_detected": detected,
            "silence_score": score,
        }

    def check_runtime():
        from google.genai import types
        from omar_ai_core import runtime

        names = {item["name"] for item in runtime.TOOL_DECLARATIONS}
        required = {
            "computer_control", "task_plan", "save_memory", "recall_memory", "developer_mode"
        }
        missing = sorted(required - names)
        if missing:
            raise RuntimeError(f"missing tools: {', '.join(missing)}")
        level = runtime._configured_thinking_level()
        if level not in {types.ThinkingLevel.MEDIUM, types.ThinkingLevel.HIGH}:
            raise RuntimeError(f"unexpected thinking level: {level}")
        return {"thinking_level": level.value, "tools": len(names)}

    def check_ui():
        from omar_ai_core.display.liquid_window import (
            DeveloperDialog,
            MemoryDialog,
            VisualSettingsPanel,
        )

        return {
            "developer_dialog": DeveloperDialog.__name__,
            "memory_dialog": MemoryDialog.__name__,
            "settings": VisualSettingsPanel.__name__,
        }

    def check_audio():
        import sounddevice as sd
        from omar_ai_core.runtime import _audio_stream_format

        defaults = (int(sd.default.device[0]), int(sd.default.device[1]))
        results = {}
        for direction, device, target_rate in (
            ("input", defaults[0], 16000),
            ("output", defaults[1], 24000),
        ):
            rate, channels = _audio_stream_format(device, direction, target_rate)
            info = sd.query_devices(device, direction)
            if direction == "input":
                stream = sd.RawInputStream(
                    device=device, samplerate=rate, channels=channels, dtype="int16", blocksize=512
                )
                stream.start()
                data, _overflowed = stream.read(512)
                stream.stop()
                stream.close()
                transferred = len(data)
            else:
                stream = sd.RawOutputStream(
                    device=device, samplerate=rate, channels=channels, dtype="int16", blocksize=512
                )
                stream.start()
                silence = bytes(512 * channels * 2)
                stream.write(silence)
                stream.stop()
                stream.close()
                transferred = len(silence)
            results[direction] = {
                "device": info["name"],
                "rate": rate,
                "channels": channels,
                "bytes": transferred,
            }
        return results

    _check("memory", check_memory, report)
    _check("planning", check_planning, report)
    _check("wakeword", check_wakeword, report)
    _check("runtime", check_runtime, report)
    _check("ui_imports", check_ui, report)
    if include_audio:
        _check("audio", check_audio, report)

    report["ok"] = all(check["ok"] for check in report["checks"].values())
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report
