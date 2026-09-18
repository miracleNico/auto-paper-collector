from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path


START_SCRIPT = Path(__file__).parents[1] / "start.cmd"


class LauncherScriptTests(unittest.TestCase):
    def test_launcher_encoding_suits_cmd(self) -> None:
        data = START_SCRIPT.read_bytes()
        # cmd.exe cannot parse a UTF-8 BOM and misreads batch files with bare LF endings.
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(data.replace(b"\r\n", b"").count(b"\n"), 0)
        # cmd.exe reads the batch part in the OEM code page, so keep it ASCII.
        batch_part, separator, _ = data.partition(b"#>")
        self.assertEqual(separator, b"#>")
        self.assertTrue(batch_part.isascii())

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell only exists on Windows")
    def test_powershell_part_parses_in_windows_powershell(self) -> None:
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("powershell.exe is unavailable")
        command = (
            "$errors = $null; $tokens = $null; "
            f"$text = [IO.File]::ReadAllText('{START_SCRIPT}', [Text.Encoding]::UTF8); "
            "[void][System.Management.Automation.Language.Parser]::ParseInput("
            "$text, [ref]$tokens, [ref]$errors); "
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

    @unittest.skipUnless(os.name == "nt", "cmd.exe only exists on Windows")
    def test_cmd_runs_powershell_part_and_forwards_arguments(self) -> None:
        # An out-of-range -ProxyPort fails parameter validation before any setup runs,
        # proving cmd.exe started Windows PowerShell and passed the arguments through.
        # Drop an inherited policy override so the machine's default policy applies.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() != "PSEXECUTIONPOLICYPREFERENCE"
        }
        result = subprocess.run(
            ["cmd.exe", "/c", str(START_SCRIPT), "-ProxyPort", "70000"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=environment,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"ProxyPort", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
