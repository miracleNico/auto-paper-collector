from __future__ import annotations

import unittest

from paper_endnote.browser import BrowserSession


class BrowserSessionTests(unittest.TestCase):
    def test_extracts_embedded_pdf_from_wrapper(self) -> None:
        markup = '<html><embed src="/ielx8/1/2/article.pdf?token=ok"></html>'
        self.assertEqual(
            BrowserSession._embedded_candidates(markup, "https://publisher.example/stamp.jsp"),
            ["https://publisher.example/ielx8/1/2/article.pdf?token=ok"],
        )

    def test_ignores_javascript_and_supplement_has_lower_rank(self) -> None:
        main = {"text": "Download PDF", "href": "https://example.test/main.pdf"}
        supplement = {"text": "Supplement PDF", "href": "https://example.test/supp.pdf"}
        self.assertGreater(
            BrowserSession._rank_candidate(main), BrowserSession._rank_candidate(supplement)
        )

    def test_builds_worldcat_resolver_url_from_mcgill_doi_entry(self) -> None:
        resolver = BrowserSession._worldcat_resolver_url(
            "https://proxy.library.mcgill.ca/login?url=https://doi.org/10.1016/j.adhoc.2023.103307"
        )
        self.assertEqual(
            resolver,
            "https://mcgill.on.worldcat.org/atoztitles/link?"
            "url_ver=Z39.88-2004&rft_id=info%3Adoi%2F10.1016%2Fj.adhoc.2023.103307",
        )

    def test_skips_worldcat_resolver_for_non_doi_targets(self) -> None:
        self.assertIsNone(
            BrowserSession._worldcat_resolver_url(
                "https://proxy.library.mcgill.ca/login?url=https://publisher.example/article/1"
            )
        )


if __name__ == "__main__":
    unittest.main()
