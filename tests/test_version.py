from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from paper_endnote import __version__


ROOT = Path(__file__).parents[1]


class VersionSourceTests(unittest.TestCase):
    def test_pyproject_reads_version_from_package(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertNotIn("version", project["project"])
        self.assertIn("version", project["project"]["dynamic"])
        self.assertEqual(
            project["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "paper_endnote.__version__"},
        )

    def test_api_reports_package_version(self) -> None:
        from paper_endnote import app as app_module

        self.assertEqual(app_module.app.version, __version__)


if __name__ == "__main__":
    unittest.main()
