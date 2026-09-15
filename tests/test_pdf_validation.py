from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter

from paper_endnote.pdf_validation import validate_pdf


class PDFValidationTests(unittest.TestCase):
    def test_rejects_html(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not.pdf"
            path.write_text("<html>login</html>", encoding="utf-8")
            result = validate_pdf(path, expected_doi="10.1000/a", expected_title="A")
            self.assertFalse(result.valid_pdf)

    def test_blank_pdf_needs_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            with path.open("wb") as handle:
                writer.write(handle)
            result = validate_pdf(path, expected_doi=None, expected_title="A")
            self.assertTrue(result.valid_pdf)
            self.assertEqual(result.identity, "needs_review")


if __name__ == "__main__":
    unittest.main()
