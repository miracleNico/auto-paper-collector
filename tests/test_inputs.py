from __future__ import annotations

import unittest

from paper_endnote.inputs import normalize_doi, parse_input, title_similarity


class InputTests(unittest.TestCase):
    def test_normalize_doi(self) -> None:
        self.assertEqual(normalize_doi("https://doi.org/10.1000/ABC.123."), "10.1000/abc.123")

    def test_parse_lines_and_deduplicate(self) -> None:
        items = parse_input("10.1000/ABC\nhttps://doi.org/10.1000/abc\nA useful paper")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["doi"], "10.1000/abc")
        self.assertEqual(items[1]["title"], "A useful paper")

    def test_parse_csv(self) -> None:
        items = parse_input("doi,title,year,author\n10.1000/test,Example,2024,Doe")
        self.assertEqual(items[0]["year"], 2024)
        self.assertEqual(items[0]["author"], "Doe")

    def test_title_similarity(self) -> None:
        self.assertGreater(title_similarity("Attention Is All You Need", "Attention is all you need"), 0.99)


if __name__ == "__main__":
    unittest.main()
