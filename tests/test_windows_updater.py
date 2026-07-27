import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omar_ai_core.updater import ReleaseInfo, UpdateError
from omar_ai_core.windows_updater import (
    WINDOWS_MANIFEST_ASSET,
    WINDOWS_PACKAGE_ASSET,
    WindowsUpdateManager,
    read_windows_version,
)
from tools.build_windows_version_info import build_version_info


class _Response:
    def __init__(self, url, payload=None, content=b""):
        self.url = url
        self._payload = payload
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1024 * 1024):
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset : offset + chunk_size]


class _Session:
    def __init__(self, responses):
        self.responses = responses

    def get(self, url, **_kwargs):
        return self.responses[url]


class WindowsUpdaterTests(unittest.TestCase):
    def test_windows_version_is_independent_from_pi_version(self):
        with tempfile.TemporaryDirectory() as folder:
            version_file = Path(folder) / "WINDOWS_VERSION"
            version_file.write_text("0.1.0\n", encoding="utf-8")
            self.assertEqual(read_windows_version(version_file), "0.1.0")
        self.assertIn("ProductVersion', '0.1.0.0", build_version_info("0.1.0"))

    def test_check_ignores_pi_releases_and_selects_highest_windows_version(self):
        api = "https://api.github.com/repos/example/Jarvis/releases?per_page=50"
        manifest_url = (
            "https://github.com/example/Jarvis/releases/download/"
            "windows-v0.2.0/jarvis-windows-manifest.json"
        )
        package_url = (
            "https://github.com/example/Jarvis/releases/download/"
            "windows-v0.2.0/Jarvis-Setup.exe"
        )
        releases = [
            {"tag_name": "v9.0.0", "draft": False, "prerelease": False, "assets": []},
            {
                "tag_name": "windows-v0.1.1",
                "draft": False,
                "prerelease": False,
                "assets": [],
            },
            {
                "tag_name": "windows-v0.2.0",
                "draft": False,
                "prerelease": False,
                "body": "Nueva versión",
                "assets": [
                    {
                        "name": WINDOWS_MANIFEST_ASSET,
                        "browser_download_url": manifest_url,
                    },
                    {
                        "name": WINDOWS_PACKAGE_ASSET,
                        "browser_download_url": package_url,
                    },
                ],
            },
        ]
        manifest = {
            "schema_version": 1,
            "version": "0.2.0",
            "platform": "windows-x86_64",
            "package_asset": WINDOWS_PACKAGE_ASSET,
            "sha256": "a" * 64,
            "size": 1234,
            "notes": "Nueva versión",
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            version_file = root / "WINDOWS_VERSION"
            version_file.write_text("0.1.0\n", encoding="utf-8")
            session = _Session(
                {
                    api: _Response(api, releases),
                    manifest_url: _Response(manifest_url, manifest),
                }
            )
            result = WindowsUpdateManager(
                root,
                repository="example/Jarvis",
                session=session,
                version_file=version_file,
            ).check_for_updates()
        self.assertTrue(result.available)
        self.assertEqual(result.current_version, "0.1.0")
        self.assertEqual(result.release.version, "0.2.0")
        self.assertEqual(result.release.package_url, package_url)

    def test_install_verifies_installer_and_records_state_before_launch(self):
        content = b"MZ" + (b"verified-installer" * 128)
        digest = hashlib.sha256(content).hexdigest()
        package_url = "https://github.com/example/Jarvis/Jarvis-Setup.exe"
        release = ReleaseInfo(
            version="0.1.1",
            tag="windows-v0.1.1",
            package_url=package_url,
            package_asset=WINDOWS_PACKAGE_ASSET,
            sha256=digest,
            size=len(content),
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            version_file = root / "WINDOWS_VERSION"
            version_file.write_text("0.1.0\n", encoding="utf-8")
            session = _Session(
                {package_url: _Response(package_url, content=content)}
            )
            manager = WindowsUpdateManager(
                root,
                repository="example/Jarvis",
                session=session,
                version_file=version_file,
            )
            with patch.object(manager, "_launch_installer") as launch:
                result = manager.install(release)
            state = json.loads(manager.state_path.read_text(encoding="utf-8"))
            installer = Path(result.backup_path)
            self.assertEqual(installer.read_bytes(), content)
            self.assertEqual(state["status"], "installer_started")
            self.assertEqual(state["target_version"], "0.1.1")
            launch.assert_called_once_with(installer)

    def test_install_rejects_a_bad_hash_and_never_launches(self):
        content = b"not-the-declared-installer"
        package_url = "https://github.com/example/Jarvis/Jarvis-Setup.exe"
        release = ReleaseInfo(
            version="0.1.1",
            tag="windows-v0.1.1",
            package_url=package_url,
            package_asset=WINDOWS_PACKAGE_ASSET,
            sha256="0" * 64,
            size=len(content),
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            version_file = root / "WINDOWS_VERSION"
            version_file.write_text("0.1.0\n", encoding="utf-8")
            manager = WindowsUpdateManager(
                root,
                repository="example/Jarvis",
                session=_Session(
                    {package_url: _Response(package_url, content=content)}
                ),
                version_file=version_file,
            )
            with patch.object(manager, "_launch_installer") as launch:
                with self.assertRaises(UpdateError):
                    manager.install(release)
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
