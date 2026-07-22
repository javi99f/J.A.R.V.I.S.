from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .settings import BASE_DIR


PLAN_PATH = BASE_DIR / "memory" / "active_plan.json"
MAX_PLAN_STEPS = 12
_lock = threading.RLock()

_COMPLEX_MARKERS = (
    "paso a paso",
    "varias tareas",
    "varios pasos",
    "después",
    "despues",
    "a continuación",
    "a continuacion",
    "por último",
    "primero",
    "luego",
    "investiga",
    "desarrolla",
    "configura",
    "instala",
    "step by step",
    "multiple tasks",
    "after that",
    "finally",
    "first",
    "then",
    "research",
    "develop",
    "configure",
    "install",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_text(value: object, maximum: int = 1200) -> str:
    return " ".join(str(value or "").strip().split())[:maximum]


def is_complex_request(text: str) -> bool:
    """Detect requests that benefit from an explicit, checkable plan."""
    normalized = _clean_text(text, 5000).casefold()
    if not normalized:
        return False
    marker_count = sum(marker in normalized for marker in _COMPLEX_MARKERS)
    action_count = len(
        re.findall(
            r"\b(?:abre|busca|entra|crea|edita|guarda|envía|envia|publica|"
            r"comprueba|verifica|instala|configura|analiza|investiga|open|search|"
            r"create|edit|save|send|publish|check|verify|install|configure|analy[sz]e)\b",
            normalized,
        )
    )
    ordered_items = len(re.findall(r"(?:^|\s)(?:\d+[.)]|[-*])\s+", normalized))
    connectors = len(re.findall(r"\b(?:y luego|y después|and then|after that)\b", normalized))
    return bool(
        marker_count >= 2
        or action_count >= 3
        or ordered_items >= 3
        or connectors >= 1
        or (len(normalized) >= 220 and action_count >= 2)
    )


class PlanManager:
    """Small persistent state machine for one active multi-step task."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or PLAN_PATH)

    def load(self) -> dict | None:
        with _lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return None
        if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
            return None
        return data

    def start(self, goal: str, steps: list[str]) -> dict:
        goal = _clean_text(goal)
        cleaned_steps = []
        for step in list(steps or [])[:MAX_PLAN_STEPS]:
            text = _clean_text(step, 500)
            if text and text.casefold() not in {item["text"].casefold() for item in cleaned_steps}:
                cleaned_steps.append(
                    {
                        "index": len(cleaned_steps) + 1,
                        "text": text,
                        "status": "pending",
                        "note": "",
                    }
                )
        if not goal:
            raise ValueError("The plan requires a goal.")
        if len(cleaned_steps) < 2:
            raise ValueError("A complex-task plan requires at least two steps.")
        plan = {
            "goal": goal,
            "status": "active",
            "created_at": _now(),
            "updated_at": _now(),
            "steps": cleaned_steps,
        }
        self._save(plan)
        return plan

    def update(self, step: int, status: str, note: str = "") -> dict:
        plan = self.load()
        if not plan or plan.get("status") != "active":
            raise ValueError("There is no active plan.")
        allowed = {"pending", "in_progress", "completed", "blocked"}
        status = _clean_text(status, 40).casefold()
        if status not in allowed:
            raise ValueError(f"Unsupported step status: {status}")
        try:
            item = plan["steps"][int(step) - 1]
        except (IndexError, TypeError, ValueError):
            raise ValueError("The requested plan step does not exist.") from None
        item["status"] = status
        item["note"] = _clean_text(note, 500)
        plan["updated_at"] = _now()
        self._save(plan)
        return plan

    def finish(self, status: str = "completed", note: str = "") -> dict:
        plan = self.load()
        if not plan:
            raise ValueError("There is no active plan.")
        if status not in {"completed", "cancelled", "blocked"}:
            raise ValueError("A plan can finish as completed, cancelled, or blocked.")
        if status == "completed" and any(
            step.get("status") != "completed" for step in plan["steps"]
        ):
            raise ValueError("Every step must be verified as completed before completing the plan.")
        plan["status"] = status
        plan["final_note"] = _clean_text(note, 800)
        plan["updated_at"] = _now()
        self._save(plan)
        return plan

    def clear(self) -> None:
        with _lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def _save(self, plan: dict) -> None:
        with _lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(plan, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            temporary.replace(self.path)


def format_plan_for_prompt(plan: dict | None) -> str:
    if not plan or plan.get("status") != "active":
        return ""
    lines = [
        "[ACTIVE TASK PLAN]",
        f"Goal: {plan.get('goal', '')}",
        "Continue this plan instead of starting over. Update every step with task_plan and verify it before completion.",
    ]
    for step in plan.get("steps", []):
        note = f" — {step.get('note')}" if step.get("note") else ""
        lines.append(
            f"{step.get('index')}. [{step.get('status', 'pending')}] {step.get('text', '')}{note}"
        )
    return "\n".join(lines) + "\n"
