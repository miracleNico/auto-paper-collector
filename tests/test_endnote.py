from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paper_endnote.endnote import (
    EndNoteExportRecord,
    build_endnote_export_package,
    match_records,
    parse_endnote_xml,
    resolve_export_attachment,
    resolve_internal_attachment,
    write_enw,
)


SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<xml><records><record><rec-number>42</rec-number><contributors><authors>
<author>Doe, Jane</author></authors></contributors><titles><title>Example Paper</title></titles>
<dates><year>2024</year></dates><electronic-resource-num>10.1000/EXAMPLE</electronic-resource-num>
<urls><pdf-urls><url>internal-pdf://example.pdf</url></pdf-urls></urls></record></records></xml>
"""

MINIMAL_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


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

    def test_zotero_to_endnote_export_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            source.write_bytes(MINIMAL_PDF)
            library = root / "DTN.enl"
            library.write_bytes(b"")
            records = [
                EndNoteExportRecord(
                    rec_number=1,
                    metadata={
                        "title": "Example Paper",
                        "doi": "10.1000/example",
                        "year": 2024,
                        "authors": ["Doe, Jane"],
                        "journal": "A Journal",
                    },
                    source_pdf=source,
                    folder_id="ITEM1234",
                    filename="10.1000_example.pdf",
                    zotero_key="ITEM1234",
                )
            ]
            destination = root / "endnote-export"
            manifest = build_endnote_export_package(
                records, destination, collection_name="DTN", library=library
            )
            self.assertEqual(manifest["pdf_count"], 1)
            self.assertTrue(Path(manifest["xml"]).is_file())
            self.assertTrue(Path(manifest["zip"]).is_file())
            parsed = parse_endnote_xml(destination / "records.xml")
            self.assertEqual(parsed[0].doi, "10.1000/example")
            copied = resolve_export_attachment(destination, parsed[0].attachments[0])
            self.assertIsNotNone(copied)
            self.assertTrue(copied.is_file())
            internal = parse_endnote_xml(destination / "records-internal.xml")
            self.assertTrue(internal[0].attachments[0].startswith("internal-pdf://ITEM1234/"))
            library_pdf = library.with_suffix(".Data") / "PDF" / "ITEM1234" / "10.1000_example.pdf"
            self.assertTrue(library_pdf.is_file())
            ris = (destination / "records.ris").read_text(encoding="utf-8-sig")
            self.assertIn("DO  - 10.1000/example", ris)
            self.assertIn("L1  - ", ris)


if __name__ == "__main__":
    unittest.main()
