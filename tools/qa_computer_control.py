"""Capture the active Windows desktop for constrained computer-control QA."""

from __future__ import annotations

import json
from pathlib import Path

from omar_ai_core.tools.computer_control import capture_screen_jpeg


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs" / "qa"


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    frame = capture_screen_jpeg()
    (OUTPUT / "computer-control-screen.jpg").write_bytes(frame.jpeg)
    report = {
        "size": [frame.width, frame.height],
        "origin": [frame.origin_x, frame.origin_y],
        "active_window": frame.active_title,
        "active_process": frame.active_process,
        "jpeg_bytes": len(frame.jpeg),
    }
    (OUTPUT / "computer-control-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
