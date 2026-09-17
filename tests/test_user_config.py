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
            self.assertEqual(original.sources, ("open_access",))
            self.assertFalse(original.auto_institution)
            path.write_text(dump_acquisition_config(original), encoding="utf-8")
            loaded = load_acquisition_config(path, create=False)
            self.assertEqual(loaded.sources, ("open_access",))
            self.assertEqual(loaded.institution.ezproxy_hosts[0], "proxy.library.mcgill.ca")
            self.assertTrue(loaded.ocr.enabled)
            self.assertFalse(loaded.auto_institution)
            self.assertTrue(loaded.auto_commit)
            self.assertEqual(loaded.login_wait_seconds, 600)

    def test_existing_institution_choices_and_profile_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """[acquisition]
sources = ["open_access", "institution"]
auto_institution = true

[institution]
preset = "mcgill"
""",
                encoding="utf-8",
            )

            loaded = load_acquisition_config(path, create=False)

            self.assertEqual(loaded.sources, ("open_access", "institution"))
            self.assertTrue(loaded.auto_institution)
            self.assertEqual(loaded.institution.id, "mcgill")
            self.assertEqual(loaded.institution.name, "McGill University")

    def test_saved_institution_enables_institution_defaults_when_switches_are_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """[institution]
preset = "mcgill"
""",
                encoding="utf-8",
            )

            loaded = load_acquisition_config(path, create=False)

            self.assertEqual(loaded.sources, ("open_access", "institution"))
            self.assertTrue(loaded.auto_institution)
            self.assertEqual(loaded.institution.id, "mcgill")

    def test_explicit_acquisition_switches_override_saved_institution_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """[acquisition]
sources = ["open_access"]
auto_institution = false

[institution]
preset = "mcgill"
""",
                encoding="utf-8",
            )

            loaded = load_acquisition_config(path, create=False)

            self.assertEqual(loaded.sources, ("open_access",))
            self.assertFalse(loaded.auto_institution)
            self.assertEqual(loaded.institution.id, "mcgill")

    def test_saved_institution_infers_only_each_omitted_switch(self) -> None:
        cases = (
            (
                """[acquisition]
sources = ["open_access"]

[institution]
preset = "mcgill"
""",
                ("open_access",),
                True,
            ),
            (
                """[acquisition]
auto_institution = false

[institution]
preset = "mcgill"
""",
                ("open_access", "institution"),
                False,
            ),
        )
        for contents, expected_sources, expected_auto in cases:
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.toml"
                path.write_text(contents, encoding="utf-8")

                loaded = load_acquisition_config(path, create=False)

                self.assertEqual(loaded.sources, expected_sources)
                self.assertEqual(loaded.auto_institution, expected_auto)


if __name__ == "__main__":
    unittest.main()
