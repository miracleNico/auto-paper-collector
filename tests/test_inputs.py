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

    def test_quoted_title_after_sniffer_sample(self) -> None:
        import csv
        import io

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["doi", "title", "year", "author"])
        for index in range(5):
            writer.writerow([f"10.1000/{index}", f"Plain title {index}", 2020, "Doe"])
        title = 'The Market for "Lemons": Quality Uncertainty and the Market Mechanism'
        writer.writerow(["10.2307/1879431", title, 1970, "George A. Akerlof"])

        parsed = parse_input(output.getvalue())
        self.assertEqual(len(parsed), 6)
        self.assertEqual(parsed[-1]["title"], title)
        self.assertEqual(parsed[-1]["doi"], "10.2307/1879431")
        self.assertEqual(parsed[-1]["year"], 1970)
        self.assertEqual(parsed[-1]["author"], "George A. Akerlof")


if __name__ == "__main__":
    unittest.main()
