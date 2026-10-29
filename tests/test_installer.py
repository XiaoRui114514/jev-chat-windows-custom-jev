"""Exercise the real installer with tiny disposable binary payloads, offline.

Run with ``python -m unittest discover -s tests``. Set PWSH to select a
PowerShell executable; otherwise pwsh / Windows PowerShell is discovered.
No real Jev executable or user directories are touched.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SHELL = os.environ.get("PWSH") or shutil.which("pwsh") or shutil.which("powershell")
SCRIPT = Path(__file__).resolve().parents[1] / "patch" / "apply.ps1"


def digest(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(SHELL, "PowerShell is needed to exercise the installer")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = self.root / "patch"
        self.patch.mkdir()
        shutil.copyfile(SCRIPT, self.patch / "apply.ps1")
        self.original = b"HEADoldMIDDLEhdrDATAtoc"
        self.patched = b"HEADnew-mainMIDDLEHDRDATAappendedtoc-newctoccookie"
        payloads = {"main.bin": b"new-main", "pyz-header.bin": b"HDR",
                    "pyz-append.bin": b"appended", "pyz-toc.bin": b"toc-new",
                    "carchive-toc.bin": b"ctoc", "carchive-cookie.bin": b"cookie"}
        for name, data in payloads.items():
            (self.patch / name).write_bytes(data)
        manifest = {
            "app_version": "v0.1.11",
            "original": {"sha256": digest(self.original), "size": len(self.original)},
            "patched": {"sha256": digest(self.patched), "size": len(self.patched)},
            "archive_start": 4,
            "main_entry": {"offset": 0, "length": 3, "file": "main.bin"},
            "pyz": {"offset": 9, "header_length": 3, "toc_offset": 7,
                    "header_file": "pyz-header.bin", "append_file": "pyz-append.bin",
                    "toc_file": "pyz-toc.bin"},
            "carchive": {"toc_file": "carchive-toc.bin", "cookie_file": "carchive-cookie.bin"},
        }
        (self.patch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.exe = self.root / "jev-chat-windows.exe"
        self.exe.write_bytes(self.original)
        self.backup = self.exe.with_suffix(".exe.bak")
        self.profile = self.root / "profile"
        (self.profile / "Downloads").mkdir(parents=True)

    def test_check_never_modifies_original_patched_or_unsupported_file(self):
        for content in (self.original,self.patched,b'unknown executable'):
            self.exe.write_bytes(content)
            self.backup.write_bytes(self.original)
            result=subprocess.run([SHELL,'-NoProfile','-NonInteractive','-File',str(self.patch/'apply.ps1'),'-Check',str(self.exe)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('restorable',result.stdout)
            self.assertEqual(self.exe.read_bytes(),content)
            self.assertEqual(self.backup.read_bytes(),self.original)

    def run_installer(self, restore=False, target=True):
        args = [SHELL, "-NoProfile", "-NonInteractive", "-File", str(self.patch / "apply.ps1")]
        if restore:
            args.append("-Restore")
        if target:
            args.append(str(self.exe))
        result = subprocess.run(args, env={**os.environ, "USERPROFILE": str(self.profile)},
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        return result

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_install_reinstall_restore(self):
        self.assert_success(self.run_installer())
        self.assertEqual(self.exe.read_bytes(), self.patched)
        self.assertEqual(self.backup.read_bytes(), self.original)
        self.assert_success(self.run_installer())
        self.assertEqual(self.exe.read_bytes(), self.patched)
        self.assert_success(self.run_installer(restore=True))
        self.assertEqual(self.exe.read_bytes(), self.original)

    def test_corrupt_backup_never_overwrites_executable(self):
        self.backup.write_bytes(b"corrupt backup")
        for restore in (True, False):
            with self.subTest(restore=restore):
                self.exe.write_bytes(self.original)
                self.assertNotEqual(self.run_installer(restore=restore).returncode, 0)
                self.assertEqual(self.exe.read_bytes(), self.original)
                self.assertEqual(self.backup.read_bytes(), b"corrupt backup")

    def test_explicit_invalid_target_does_not_patch_other_installation(self):
        other = self.profile / "Downloads" / "jev-chat-windows.exe"
        other.write_bytes(self.original)
        self.exe.write_bytes(b"unsupported explicit executable")
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertEqual(other.read_bytes(), self.original)
        self.assertFalse(other.with_suffix(".exe.bak").exists())

    def test_corrupt_payload_preserves_executable_and_backup(self):
        (self.patch / "main.bin").write_bytes(b"bad payload")
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertEqual(self.exe.read_bytes(), self.original)
        self.assertFalse(self.backup.exists())

    def test_restore_discovery_skips_unpatched_executable_without_backup(self):
        # The repository-local exe is encountered before discovered downloads.
        other = self.profile / "Downloads" / "jev-chat-windows.exe"
        other.write_bytes(self.patched)
        other.with_suffix(".exe.bak").write_bytes(self.original)
        self.assert_success(self.run_installer(restore=True, target=False))
        self.assertEqual(self.exe.read_bytes(), self.original)
        self.assertEqual(other.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
