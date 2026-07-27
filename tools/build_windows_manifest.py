from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installer", default="dist-installer/Jarvis-Setup.exe")
    parser.add_argument("--version-file", default="WINDOWS_VERSION")
    parser.add_argument("--notes-file", default="CHANGELOG_WINDOWS.md")
    parser.add_argument(
        "--output", default="dist-installer/jarvis-windows-manifest.json"
    )
    args = parser.parse_args()

    installer = Path(args.installer)
    version = Path(args.version_file).read_text(encoding="utf-8").strip()
    notes_path = Path(args.notes_file)
    notes = notes_path.read_text(encoding="utf-8").strip() if notes_path.is_file() else ""
    manifest = {
        "schema_version": 1,
        "version": version,
        "platform": "windows-x86_64",
        "package_asset": installer.name,
        "sha256": sha256(installer),
        "size": installer.stat().st_size,
        "notes": notes[:4000],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
