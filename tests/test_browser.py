from __future__ import annotations

import unittest

from paper_endnote.browser import BrowserSession
from paper_endnote.publishers import is_publisher_block, publisher_pdf_candidates
from paper_endnote.user_config import extract_doi_from_ezproxy_url, institution_openurl, load_preset


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
        entry = "https://proxy.library.mcgill.ca/login?url=https://doi.org/10.1016/j.adhoc.2023.103307"
        doi = extract_doi_from_ezproxy_url(entry)
        resolver = institution_openurl(load_preset("mcgill"), doi or "")
        self.assertEqual(
            resolver,
            "https://mcgill.on.worldcat.org/atoztitles/link?"
            "url_ver=Z39.88-2004&rft_id=info%3Adoi%2F10.1016%2Fj.adhoc.2023.103307",
        )

    def test_skips_worldcat_resolver_for_non_doi_targets(self) -> None:
        self.assertIsNone(
            extract_doi_from_ezproxy_url(
                "https://proxy.library.mcgill.ca/login?url=https://publisher.example/article/1"
            )
        )

    def test_ieee_stamp_pdf_from_arnumber(self) -> None:
        candidates = publisher_pdf_candidates(
            "https://ieeexplore-ieee-org.proxy3.library.mcgill.ca/document/6196145"
        )
        hrefs = [item["href"] for item in candidates]
        self.assertEqual(
            hrefs[0],
            "https://ieeexplore-ieee-org.proxy3.library.mcgill.ca/stampPDF/getPDF.jsp?tp=&arnumber=6196145&ref=",
        )
        self.assertIn(
            "https://ieeexplore-ieee-org.proxy3.library.mcgill.ca/stamp/stamp.jsp?tp=&arnumber=6196145",
            hrefs,
        )

    def test_ieee_ielx_from_article_url(self) -> None:
        candidates = publisher_pdf_candidates(
            "https://ieeexplore.ieee.org/ielx7/6287639/9312710/10766816.pdf?tp=&arnumber=10766816"
        )
        hrefs = [item["href"] for item in candidates]
        self.assertTrue(any("stampPDF/getPDF.jsp" in href for href in hrefs))
        self.assertIn(
            "https://ieeexplore.ieee.org/ielx7/6287639/9312710/10766816.pdf",
            hrefs,
        )

    def test_publisher_block_detects_403_and_captcha(self) -> None:
        self.assertTrue(is_publisher_block(403, b"<html>no</html>"))
        self.assertTrue(is_publisher_block(200, b"<html>ip blocked</html>", "ip blocked"))
        self.assertTrue(is_publisher_block(200, b"<html>Please complete captcha</html>", "Please complete captcha"))
        self.assertFalse(is_publisher_block(403, b"%PDF-1.4\n"))
        self.assertFalse(is_publisher_block(200, b"<html>article</html>", "article"))

    def test_detects_ezproxy_login_and_idp_urls(self) -> None:
        session = BrowserSession.__new__(BrowserSession)
        session.settings = type("Settings", (), {"institution": load_preset("mcgill")})()
        self.assertTrue(
            session._is_login_url(
                "https://proxy.library.mcgill.ca/login?url=https://doi.org/10.1109/example"
            )
        )
        self.assertTrue(session._is_login_url("https://login.microsoftonline.com/common/oauth2"))
        self.assertFalse(
            session._is_login_url(
                "https://ieeexplore-ieee-org.proxy3.library.mcgill.ca/document/6196145"
            )
        )

    def test_elsevier_pdfft_from_pii(self) -> None:
        candidates = publisher_pdf_candidates(
            "https://www-sciencedirect-com.proxy3.library.mcgill.ca/science/article/pii/S1570870523001234"
        )
        self.assertIn("/pdfft?isDTMRedir=true&download=true", candidates[0]["href"])
        self.assertIn("S1570870523001234", candidates[0]["href"])


if __name__ == "__main__":
    unittest.main()
