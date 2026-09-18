from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path


START_SCRIPT = Path(__file__).parents[1] / "start.ps1"


class LauncherScriptTests(unittest.TestCase):
    def test_start_script_has_utf8_bom(self) -> None:
        # Windows PowerShell 5.1 decodes BOM-less scripts with the ANSI code
        # page, which corrupts the Chinese messages and breaks parsing.
        self.assertTrue(START_SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf"))

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell only exists on Windows")
    def test_start_script_parses_in_windows_powershell(self) -> None:
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("powershell.exe is unavailable")
        command = (
            "$errors = $null; $tokens = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            f"'{START_SCRIPT}', [ref]$tokens, [ref]$errors); "
            "$errors | ForEach-Object { $_.Message }"
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
