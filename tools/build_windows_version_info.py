from __future__ import annotations

import argparse
import re
from pathlib import Path


VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def build_version_info(version: str) -> str:
    match = VERSION_PATTERN.fullmatch(version.strip())
    if not match:
        raise ValueError(f"Versión de Windows no válida: {version!r}")
    major, minor, patch = (int(part) for part in match.groups())
    dotted = f"{major}.{minor}.{patch}.0"
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, 0),
    prodvers=({major}, {minor}, {patch}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'Personal AI Lab'),
          StringStruct('FileDescription', 'Jarvis Windows'),
          StringStruct('FileVersion', '{dotted}'),
          StringStruct('InternalName', 'Jarvis'),
          StringStruct('LegalCopyright', 'Personal AI Lab'),
          StringStruct('OriginalFilename', 'Jarvis.exe'),
          StringStruct('ProductName', 'Jarvis Windows'),
          StringStruct('ProductVersion', '{dotted}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version-file", default="WINDOWS_VERSION")
    parser.add_argument("--output", default="build/windows-version-info.txt")
    args = parser.parse_args()

    version = Path(args.version_file).read_text(encoding="utf-8").strip()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_version_info(version), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
