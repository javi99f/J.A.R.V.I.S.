"""Windows/macOS desktop entry point for the microphone-only JARVIS edition."""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


os.environ.setdefault("APP_MODE", "desktop")


def _data_dir() -> Path:
    root = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or str(Path.home())
    path = Path(root) / "Jarvis"
    path.mkdir(parents=True, exist_ok=True)
    return path


class _LogStream:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, value: str) -> int:
        if value:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(value)
        return len(value)

    def flush(self) -> None:
        return None


def main() -> None:
    log_path = _data_dir() / "jarvis.log"
    if getattr(sys, "frozen", False):
        sys.stdout = _LogStream(log_path)
        sys.stderr = _LogStream(log_path)

    def report_exception(exc_type, exc_value, exc_tb):
        with log_path.open("a", encoding="utf-8") as handle:
            traceback.print_exception(exc_type, exc_value, exc_tb, file=handle)

    sys.excepthook = report_exception
    from omar_ai_core.runtime import main as run_jarvis

    run_jarvis()


if __name__ == "__main__":
    main()

