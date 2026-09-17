from __future__ import annotations

import unittest

from paper_endnote.redaction import (
    REDACTED,
    has_transient_auth_query,
    redact_diagnostic,
    redact_diagnostic_text,
    redact_url,
    transient_auth_query_keys,
)


class RedactionTests(unittest.TestCase):
    def test_url_redacts_auth_values_but_preserves_stable_discovery_query(self) -> None:
        url = (
            "https://login.example.edu/sso?"
            "entityID=https%3A%2F%2Fsp.example%2Fshibboleth&"
            "SAMLRequest=request-secret&RelayState=relay-secret&lang=zh"
            "#access_token=fragment-secret"
        )

        redacted = redact_url(url)

        self.assertIn("entityID=https%3A%2F%2Fsp.example%2Fshibboleth", redacted)
        self.assertIn("lang=zh", redacted)
        self.assertIn(f"SAMLRequest={REDACTED}", redacted)
        self.assertIn(f"RelayState={REDACTED}", redacted)
        self.assertIn(f"access_token={REDACTED}", redacted)
        self.assertNotIn("request-secret", redacted)
        self.assertNotIn("relay-secret", redacted)
        self.assertNotIn("fragment-secret", redacted)

    def test_diagnostic_redacts_urls_bare_parameters_and_nested_values(self) -> None:
        text = (
            "failed at https://sp.example/callback?code=oauth-secret&entityID=stable; "
            "SAMLResponse=assertion-secret"
        )
        redacted = redact_diagnostic_text(text)
        nested = redact_diagnostic({"error": text, "items": [text]})

        self.assertNotIn("oauth-secret", redacted)
        self.assertNotIn("assertion-secret", redacted)
        self.assertIn("entityID=stable", redacted)
        self.assertNotIn("oauth-secret", nested["error"])
        self.assertNotIn("assertion-secret", nested["items"][0])

    def test_detects_transient_query_and_fragment_keys(self) -> None:
        url = "https://idp.example/sso?SigAlg=rsa&Signature=secret#id_token=jwt"
        self.assertTrue(has_transient_auth_query(url))
        self.assertEqual(
            transient_auth_query_keys(url),
            frozenset({"sigalg", "signature", "id_token"}),
        )
        self.assertFalse(
            has_transient_auth_query(
                "https://discovery.example/wayf?entityID=https%3A%2F%2Fsp.example"
            )
        )

    def test_url_userinfo_is_not_exposed(self) -> None:
        redacted = redact_url("https://alice:password@example.edu/login?lang=en")
        self.assertEqual(redacted, "https://[REDACTED]@example.edu/login?lang=en")

    def test_regular_ezproxy_target_parameter_is_preserved(self) -> None:
        url = (
            "https://proxy.library.example/login?"
            "url=https://doi.org/10.1000/example&lang=en"
        )
        self.assertEqual(redact_url(url), url)

    def test_signed_download_parameters_are_redacted(self) -> None:
        url = (
            "https://cdn.example/paper.pdf?download=true&"
            "X-Amz-Credential=credential-secret&X-Amz-Signature=signature-secret&"
            "token=download-secret"
        )
        redacted = redact_url(url)
        self.assertIn("download=true", redacted)
        self.assertNotIn("credential-secret", redacted)
        self.assertNotIn("signature-secret", redacted)
        self.assertNotIn("download-secret", redacted)


if __name__ == "__main__":
    unittest.main()
