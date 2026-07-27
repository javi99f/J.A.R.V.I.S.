"""Verified GitHub Release updater for the packaged Windows edition."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from .settings import BASE_DIR, get_secret
from .updater import (
    ALLOWED_DOWNLOAD_HOSTS,
    InstallResult,
    ReleaseInfo,
    REPOSITORY_PATTERN,
    UpdateCheck,
    UpdateError,
    _version_key,
    is_newer,
)


WINDOWS_MANIFEST_ASSET = "jarvis-windows-manifest.json"
WINDOWS_PACKAGE_ASSET = "Jarvis-Setup.exe"
WINDOWS_PLATFORM = "windows-x86_64"
WINDOWS_TAG_PREFIX = "windows-v"
DEFAULT_REPOSITORY = "javi99f/J.A.R.V.I.S."
MAX_INSTALLER_BYTES = 500 * 1024 * 1024


def _bundled_version_file() -> Path:
    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return bundle / "WINDOWS_VERSION"
    return Path(__file__).resolve().parent.parent / "WINDOWS_VERSION"


def read_windows_version(version_file: Path | None = None) -> str:
    path = Path(version_file) if version_file is not None else _bundled_version_file()
    try:
        value = path.read_text(encoding="utf-8").strip()
        _version_key(value)
        return value.removeprefix("v")
    except (OSError, UpdateError):
        return "0.0.0"


class WindowsUpdateManager:
    """Downloads a verified per-user installer and starts it after confirmation.

    User data lives under ``%LOCALAPPDATA%\\Jarvis`` while application files live
    under ``%LOCALAPPDATA%\\Programs\\Jarvis``. Replacing the latter therefore
    preserves the API key, memory, history, audio choices and visual settings.
    """

    def __init__(
        self,
        data_dir: Path = BASE_DIR,
        repository: str | None = None,
        allow_prerelease: bool | None = None,
        session=None,
        version_file: Path | None = None,
        executable: str | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.repository = (
            repository
            if repository is not None
            else get_secret("UPDATE_REPOSITORY", DEFAULT_REPOSITORY)
        ).strip() or DEFAULT_REPOSITORY
        configured_prerelease = get_secret("UPDATE_ALLOW_PRERELEASE", "0").lower()
        self.allow_prerelease = (
            allow_prerelease
            if allow_prerelease is not None
            else configured_prerelease in {"1", "true", "yes", "on"}
        )
        self.session = session or requests.Session()
        self.version_file = Path(version_file) if version_file is not None else None
        self.executable = executable or sys.executable
        self.update_dir = self.data_dir / ".updates" / "windows"
        self.state_path = self.update_dir / "state.json"
        self.lock_path = self.update_dir / "update.lock"

    @property
    def current_version(self) -> str:
        return read_windows_version(self.version_file)

    def _validate_repository(self) -> None:
        if not REPOSITORY_PATTERN.fullmatch(self.repository):
            raise UpdateError("UPDATE_REPOSITORY debe tener el formato usuario/repositorio.")

    @staticmethod
    def _validate_download_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_DOWNLOAD_HOSTS:
            raise UpdateError("GitHub devolvió una dirección de descarga no permitida.")

    def _get(self, url: str, *, stream: bool = False):
        self._validate_download_url(url)
        try:
            response = self.session.get(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"Jarvis-Windows-Updater/{self.current_version}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=(8, 90),
                stream=stream,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise UpdateError(f"No se pudo contactar con GitHub: {exc}") from exc
        final_url = str(getattr(response, "url", url) or url)
        self._validate_download_url(final_url)
        return response

    def _release_payload(self) -> dict | None:
        self._validate_repository()
        url = f"https://api.github.com/repos/{self.repository}/releases?per_page=50"
        payload = self._get(url).json()
        if not isinstance(payload, list):
            raise UpdateError("GitHub devolvió una lista de versiones no válida.")

        candidates: list[tuple[tuple, dict]] = []
        for release in payload:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            if release.get("prerelease") and not self.allow_prerelease:
                continue
            tag = str(release.get("tag_name") or "")
            if not tag.startswith(WINDOWS_TAG_PREFIX):
                continue
            version = tag[len(WINDOWS_TAG_PREFIX) :]
            try:
                candidates.append((_version_key(version), release))
            except UpdateError:
                continue
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def check_for_updates(self) -> UpdateCheck:
        release_payload = self._release_payload()
        current = self.current_version
        if release_payload is None:
            return UpdateCheck(current, False, None)

        assets = {
            str(item.get("name")): str(item.get("browser_download_url"))
            for item in release_payload.get("assets", [])
            if isinstance(item, dict) and item.get("name") and item.get("browser_download_url")
        }
        manifest_url = assets.get(WINDOWS_MANIFEST_ASSET)
        if not manifest_url:
            raise UpdateError(
                f"La versión {release_payload.get('tag_name', '')} no contiene "
                f"{WINDOWS_MANIFEST_ASSET}."
            )
        manifest = self._get(manifest_url).json()
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise UpdateError("El manifiesto de Windows no es compatible.")

        version = str(manifest.get("version") or "").strip().removeprefix("v")
        _version_key(version)
        tag = str(release_payload.get("tag_name") or "").strip()
        if tag != f"{WINDOWS_TAG_PREFIX}{version}":
            raise UpdateError("La versión del manifiesto no coincide con la etiqueta de Windows.")
        if manifest.get("platform") != WINDOWS_PLATFORM:
            raise UpdateError("La actualización no corresponde a Windows de 64 bits.")

        package_asset = str(manifest.get("package_asset") or WINDOWS_PACKAGE_ASSET)
        if package_asset != WINDOWS_PACKAGE_ASSET:
            raise UpdateError("El manifiesto solicita un instalador de Windows no permitido.")
        package_url = assets.get(package_asset)
        if not package_url:
            raise UpdateError(f"La versión no contiene {package_asset}.")
        sha256 = str(manifest.get("sha256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise UpdateError("El manifiesto no contiene un SHA-256 válido.")
        try:
            size = int(manifest.get("size") or 0)
        except (TypeError, ValueError) as exc:
            raise UpdateError("El tamaño del instalador no es válido.") from exc
        if size <= 0 or size > MAX_INSTALLER_BYTES:
            raise UpdateError("El tamaño del instalador está fuera del límite permitido.")

        release = ReleaseInfo(
            version=version,
            tag=tag,
            package_url=package_url,
            package_asset=package_asset,
            sha256=sha256,
            size=size,
            notes=str(manifest.get("notes") or release_payload.get("body") or "")[:4000],
            prerelease=bool(release_payload.get("prerelease")),
        )
        return UpdateCheck(current, is_newer(version, current), release)

    def _write_state(self, **values) -> None:
        self.update_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "schema_version": 1,
            "edition": "windows",
            "installed_version": self.current_version,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **values,
        }
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    def _acquire_lock(self) -> int:
        self.update_dir.mkdir(parents=True, exist_ok=True)
        try:
            return os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            try:
                stale = time.time() - self.lock_path.stat().st_mtime > 3600
            except OSError:
                stale = False
            if stale:
                self.lock_path.unlink(missing_ok=True)
                return os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            raise UpdateError("Ya hay una actualización de Windows en curso.") from exc

    def _download(self, release: ReleaseInfo, destination: Path) -> None:
        response = self._get(release.package_url, stream=True)
        total = 0
        digest = hashlib.sha256()
        with destination.open("wb") as handle:
            for block in response.iter_content(chunk_size=1024 * 1024):
                if not block:
                    continue
                total += len(block)
                if total > release.size or total > MAX_INSTALLER_BYTES:
                    raise UpdateError("La descarga supera el tamaño declarado.")
                digest.update(block)
                handle.write(block)
        if total != release.size:
            raise UpdateError(
                f"Descarga incompleta: se esperaban {release.size} bytes y llegaron {total}."
            )
        if digest.hexdigest().lower() != release.sha256:
            raise UpdateError("El SHA-256 no coincide; el instalador no se ejecutará.")

    @staticmethod
    def _installer_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _launch_installer(self, installer: Path) -> None:
        if sys.platform != "win32":
            raise UpdateError("El instalador automático solo se puede ejecutar en Windows.")
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
        try:
            subprocess.Popen(
                [
                    str(installer),
                    "/SP-",
                    "/VERYSILENT",
                    "/SUPPRESSMSGBOXES",
                    "/NORESTART",
                    "/CLOSEAPPLICATIONS",
                    "/RESTARTAPPLICATIONS",
                ],
                cwd=str(installer.parent),
                close_fds=True,
                creationflags=flags,
            )
        except OSError as exc:
            raise UpdateError(f"Windows no pudo iniciar el instalador: {exc}") from exc

    def install(self, release: ReleaseInfo | None = None) -> InstallResult:
        if release is None:
            check = self.check_for_updates()
            release = check.release if check.available else None
        if release is None:
            raise UpdateError("Jarvis Windows ya está actualizado.")

        current = self.current_version
        if not is_newer(release.version, current):
            raise UpdateError("La versión solicitada no es posterior a la instalada.")

        lock_fd = self._acquire_lock()
        try:
            version_dir = self.update_dir / release.version
            version_dir.mkdir(parents=True, exist_ok=True)
            installer = version_dir / WINDOWS_PACKAGE_ASSET
            if (
                not installer.is_file()
                or installer.stat().st_size != release.size
                or self._installer_hash(installer).lower() != release.sha256
            ):
                partial = installer.with_suffix(".exe.partial")
                partial.unlink(missing_ok=True)
                self._download(release, partial)
                os.replace(partial, installer)
            self._write_state(
                status="installer_ready",
                target_version=release.version,
                installer_path=str(installer),
                sha256=release.sha256,
            )
            self._launch_installer(installer)
            self._write_state(
                status="installer_started",
                target_version=release.version,
                installer_path=str(installer),
                sha256=release.sha256,
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            return InstallResult(current, release.version, str(installer), True)
        except Exception as exc:
            self._write_state(status="failed", error=str(exc)[:1200])
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError(f"No se pudo preparar la actualización: {exc}") from exc
        finally:
            os.close(lock_fd)
            self.lock_path.unlink(missing_ok=True)

    def status(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                state["installed_version"] = self.current_version
                return state
        except Exception:
            pass
        return {
            "schema_version": 1,
            "edition": "windows",
            "status": "idle",
            "installed_version": self.current_version,
        }


def main() -> int:
    manager = WindowsUpdateManager()
    try:
        check = manager.check_for_updates()
        print(json.dumps(asdict(check), ensure_ascii=False, indent=2))
        return 0
    except UpdateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
