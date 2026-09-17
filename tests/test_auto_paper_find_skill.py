from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from paper_endnote.inputs import parse_input


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/auto-paper-find-skill/scripts/build_input.py"
SPEC = importlib.util.spec_from_file_location("build_paper_input", SCRIPT)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class AutoPaperFindSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.records = self.directory / "records.json"

    def write_records(self, rows):
        self.records.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    def build(self, rows, suffix=".csv"):
        self.write_records(rows)
        output = self.directory / ("papers" + suffix)
        result = builder.build(ROOT, self.records, output)
        return output, result

    def test_csv_unicode_quotes_and_optional_fields(self):
        output, result = self.build([
            {"doi": "https://doi.org/10.1000/ABC", "title": '网络, "Routing"', "year": 2024, "author": "张三; Doe, Jane"},
            {"title": "Another paper"},
        ])
        self.assertTrue(output.read_bytes().startswith(b"\xef\xbb\xbf"))
        parsed = parse_input(output.read_text(encoding="utf-8-sig"))
        self.assertEqual(result["records"], 2)
        self.assertEqual(parsed[0]["title"], '网络, "Routing"')
        self.assertEqual(parsed[0]["doi"], "10.1000/abc")
        self.assertEqual(parsed[0]["author"], "张三; Doe, Jane")
        self.assertEqual(parsed[0]["year"], 2024)
        self.assertIsNone(parsed[1]["year"])

    def test_txt_prefers_bare_doi(self):
        output, result = self.build([
            {"doi": "doi: 10.1000/ABC", "title": "Canonical title", "year": 2024},
            {"title": "另一篇论文"},
        ], ".txt")
        self.assertEqual(output.read_text(encoding="utf-8"), "10.1000/abc\n另一篇论文\n")
        self.assertEqual(result["records"], 2)

    def test_rejects_silent_deduplication(self):
        with self.assertRaisesRegex(ValueError, "round-trip"):
            self.build([{"doi": "10.1000/ABC"}, {"doi": "10.1000/abc"}])
        self.assertFalse((self.directory / "papers.csv").exists())

    def test_txt_header_collision_is_caught_but_csv_works(self):
        rows = [{"title": "Title prediction, a survey"}]
        with self.assertRaisesRegex(ValueError, "round-trip"):
            self.build(rows, ".txt")
        output, _ = self.build(rows)
        self.assertEqual(parse_input(output.read_text(encoding="utf-8-sig"))[0]["title"], rows[0]["title"])

    def test_rejects_invalid_records_without_writing(self):
        invalid = [
            [], [{}], [{"doi": "not-a-doi"}],
            [{"doi": "10.1000/abc."}], [{"doi": "10.1000/(abc)"}],
            [{"title": "Paper", "year": "2020-2024"}],
            [{"title": "Paper", "year": True}],
            [{"title": "Paper\nComment"}],
            [{"title": "Paper", "source": "https://example.org"}],
        ]
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.build(rows)
        self.assertFalse((self.directory / "papers.csv").exists())

    def test_does_not_overwrite_existing_file(self):
        output, _ = self.build([{"title": "Original paper"}])
        original = output.read_bytes()
        with self.assertRaises(FileExistsError):
            self.build([{"title": "Replacement paper"}])
        self.assertEqual(output.read_bytes(), original)

    def test_requires_actual_project_parser(self):
        self.write_records([{"title": "Paper"}])
        with self.assertRaisesRegex(ValueError, "parser not found"):
            builder.build(self.directory, self.records, self.directory / "papers.csv")

    def test_cli_generates_valid_file(self):
        self.write_records([{"title": "Example paper", "year": 2024}])
        output = self.directory / "cli.csv"
        result = subprocess.run([
            sys.executable, str(SCRIPT), "--project-root", str(ROOT),
            "--records", str(self.records), "--output", str(output),
        ], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["parser_round_trip"], "passed")
        self.assertEqual(len(parse_input(output.read_text(encoding="utf-8-sig"))), 1)


if __name__ == "__main__":
    unittest.main()
