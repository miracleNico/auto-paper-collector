from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paper_endnote.user_config import (
    ConfigError,
    dump_acquisition_config,
    extract_doi_from_ezproxy_url,
    institution_openurl,
    institution_proxy_url,
    load_acquisition_config,
    load_preset,
    normalize_sources,
)


class UserConfigTests(unittest.TestCase):
    def test_mcgill_preset_urls(self) -> None:
        profile = load_preset("mcgill")
        self.assertEqual(
            institution_proxy_url(profile, "https://doi.org/10.1016/j.adhoc.2023.103307"),
            "https://proxy.library.mcgill.ca/login?url=https://doi.org/10.1016/j.adhoc.2023.103307",
        )
        self.assertEqual(
            institution_openurl(profile, "10.1016/j.adhoc.2023.103307"),
            "https://mcgill.on.worldcat.org/atoztitles/link?"
            "url_ver=Z39.88-2004&rft_id=info%3Adoi%2F10.1016%2Fj.adhoc.2023.103307",
        )

    def test_extracts_doi_from_ezproxy_entry(self) -> None:
        doi = extract_doi_from_ezproxy_url(
            "https://proxy.library.mcgill.ca/login?url=https://doi.org/10.1016/j.adhoc.2023.103307"
        )
        self.assertEqual(doi, "10.1016/j.adhoc.2023.103307")
        self.assertIsNone(
            extract_doi_from_ezproxy_url(
                "https://proxy.library.mcgill.ca/login?url=https://publisher.example/article/1"
            )
        )

    def test_rejects_scihub_source(self) -> None:
        with self.assertRaises(ConfigError):
            normalize_sources(["open_access", "scihub"])
        with self.assertRaises(ConfigError):
            normalize_sources(["sci-hub"])

    def test_roundtrip_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            original = load_acquisition_config(path, create=True)
            self.assertEqual(original.institution.id, "mcgill")
            path.write_text(dump_acquisition_config(original), encoding="utf-8")
            loaded = load_acquisition_config(path, create=False)
            self.assertEqual(loaded.sources, ("open_access", "institution"))
            self.assertEqual(loaded.institution.ezproxy_hosts[0], "proxy.library.mcgill.ca")
            self.assertTrue(loaded.ocr.enabled)
            self.assertTrue(loaded.auto_institution)
            self.assertTrue(loaded.auto_commit)
            self.assertEqual(loaded.login_wait_seconds, 600)


if __name__ == "__main__":
    unittest.main()
