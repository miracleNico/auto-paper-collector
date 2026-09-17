from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paper_endnote.user_config import (
    AcquisitionConfig,
    ConfigError,
    dump_acquisition_config,
    extract_doi_from_ezproxy_url,
    institution_from_payload,
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
            self.assertEqual(original.institution.id, "")
            self.assertEqual(original.institution.access_type, "ezproxy")
            self.assertEqual(original.sources, ("open_access",))
            self.assertFalse(original.auto_institution)
            path.write_text(dump_acquisition_config(original), encoding="utf-8")
            loaded = load_acquisition_config(path, create=False)
            self.assertEqual(loaded.sources, ("open_access",))
            self.assertEqual(loaded.institution.ezproxy_hosts, ())
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

    def test_legacy_ezproxy_profile_infers_access_type_and_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """[institution]
id = "legacy-school"
name = "Legacy School"
ezproxy_login = "https://proxy.example.edu/login?url={url}"
""",
                encoding="utf-8",
            )

            loaded = load_acquisition_config(path, create=False)

            self.assertEqual(loaded.institution.access_type, "ezproxy")
            self.assertEqual(loaded.institution.ezproxy_hosts, ("proxy.example.edu",))
            self.assertEqual(loaded.sources, ("open_access", "institution"))
            self.assertTrue(loaded.auto_institution)

    def test_carsi_roundtrip_does_not_require_ezproxy(self) -> None:
        profile = institution_from_payload(
            {
                "id": "example-university",
                "name": "Example University",
                "access_type": "carsi_saml",
                "login_url": "https://library.example.edu/carsi",
                "login_url_markers": ["idp.example.edu"],
                "openurl": "https://resolver.example.edu/?doi={doi}",
                "school_aliases": ["Example U", "示例大学"],
                "entity_id": "https://idp.example.edu/idp/shibboleth",
                "publisher_login_urls": {
                    "IEEE": "https://example.edu/ieee-login",
                    "sciencedirect": "https://example.edu/elsevier-login",
                },
            }
        )
        self.assertEqual(profile.ezproxy_login, "")
        self.assertEqual(profile.publisher_login_urls["ieee"], "https://example.edu/ieee-login")

        config_text = dump_acquisition_config(
            AcquisitionConfig(
                sources=("open_access", "institution"), institution=profile, auto_institution=True
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(config_text, encoding="utf-8")
            loaded = load_acquisition_config(path, create=False)

        self.assertEqual(loaded.institution.as_dict(), profile.as_dict())
        self.assertEqual(loaded.institution.school_aliases, ("Example U", "示例大学"))

    def test_explicit_empty_optional_fields_override_preset(self) -> None:
        profile = institution_from_payload(
            {
                "preset": "mcgill",
                "openurl": "",
                "login_url": "",
                "login_url_markers": [],
                "ezproxy_hosts": [],
            }
        )

        self.assertEqual(profile.openurl, "")
        self.assertEqual(profile.login_url, "")
        self.assertEqual(profile.login_url_markers, ())
        self.assertEqual(profile.ezproxy_hosts, ())

    def test_manual_browser_profile_only_requires_an_id(self) -> None:
        profile = institution_from_payload(
            {"id": "vpn-school", "name": "VPN School", "access_type": "manual_browser"}
        )
        self.assertEqual(profile.access_type, "manual_browser")
        self.assertEqual(profile.ezproxy_login, "")

    def test_rejects_incomplete_ezproxy_and_unknown_access_type(self) -> None:
        with self.assertRaisesRegex(ConfigError, "ezproxy_login"):
            institution_from_payload({"id": "broken", "access_type": "ezproxy"})
        with self.assertRaisesRegex(ConfigError, "未知机构访问方式"):
            institution_from_payload({"id": "broken", "access_type": "unknown"})

    def test_carsi_requires_complete_non_template_login_urls(self) -> None:
        with self.assertRaisesRegex(ConfigError, "完整的 http"):
            institution_from_payload(
                {
                    "id": "broken",
                    "access_type": "carsi_saml",
                    "login_url": "https://idp.example.edu/login?url={url}",
                }
            )
        with self.assertRaisesRegex(ConfigError, "publisher_login_urls.ieee"):
            institution_from_payload(
                {
                    "id": "broken",
                    "access_type": "carsi_saml",
                    "publisher_login_urls": {"ieee": "/relative-login"},
                }
            )

        with self.assertRaisesRegex(ConfigError, "login_url"):
            institution_from_payload(
                {
                    "id": "unsafe-ezproxy",
                    "access_type": "ezproxy",
                    "ezproxy_login": "https://proxy.example/login?url={url}",
                    "login_url": "https://idp.example.edu/callback?code=secret",
                }
            )

    def test_login_urls_reject_transient_auth_queries_but_allow_entity_id(self) -> None:
        sensitive_urls = (
            "https://idp.example.edu/sso?SAMLRequest=secret",
            "https://idp.example.edu/sso?RelayState=secret&Signature=sig",
            "https://idp.example.edu/callback?code=secret&id_token=jwt",
            "https://idp.example.edu/callback#access_token=token",
        )
        for url in sensitive_urls:
            with self.subTest(url=url), self.assertRaisesRegex(
                ConfigError, "一次性认证参数"
            ):
                institution_from_payload(
                    {
                        "id": "unsafe",
                        "access_type": "carsi_saml",
                        "login_url": url,
                    }
                )

        with self.assertRaisesRegex(ConfigError, "publisher_login_urls.ieee"):
            institution_from_payload(
                {
                    "id": "unsafe-publisher",
                    "access_type": "carsi_saml",
                    "publisher_login_urls": {
                        "ieee": "https://idp.example.edu/sso?SAMLResponse=secret"
                    },
                }
            )

        profile = institution_from_payload(
            {
                "id": "safe",
                "access_type": "carsi_saml",
                "login_url": (
                    "https://discovery.example.edu/wayf?"
                    "entityID=https%3A%2F%2Fsp.example%2Fshibboleth&lang=zh"
                ),
            }
        )
        self.assertIn("entityID=", profile.login_url)
        self.assertIn("lang=zh", profile.login_url)


if __name__ == "__main__":
    unittest.main()
