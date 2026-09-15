from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paper_endnote.endnote import match_records, parse_endnote_xml, resolve_internal_attachment, write_enw


SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<xml><records><record><rec-number>42</rec-number><contributors><authors>
<author>Doe, Jane</author></authors></contributors><titles><title>Example Paper</title></titles>
<dates><year>2024</year></dates><electronic-resource-num>10.1000/EXAMPLE</electronic-resource-num>
<urls><pdf-urls><url>internal-pdf://example.pdf</url></pdf-urls></urls></record></records></xml>
"""


class EndNoteTests(unittest.TestCase):
    def test_enw_generation_and_xml_parse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            enw = write_enw(root / "record.enw", {
                "title": "例子 Example", "doi": "10.1000/example", "year": 2024,
                "authors": ["Doe, Jane"], "journal": "A Journal",
            })
            text = enw.read_text(encoding="utf-8-sig")
            self.assertIn("%R 10.1000/example", text)
            xml = root / "export.xml"
            xml.write_text(SAMPLE_XML, encoding="utf-8")
            records = parse_endnote_xml(xml)
            self.assertEqual(records[0].record_number, "42")
            self.assertEqual(records[0].doi, "10.1000/example")
            self.assertEqual(len(match_records(records, {"doi": "10.1000/example"})), 1)

    def test_internal_attachment_decodes_spaces(self) -> None:
        library = Path(r"C:\Research\My Library.enl")
        resolved = resolve_internal_attachment(library, "internal-pdf://Doe%202024%20Paper.pdf")
        self.assertEqual(resolved, Path(r"C:\Research\My Library.Data\PDF\Doe 2024 Paper.pdf"))


if __name__ == "__main__":
    unittest.main()
