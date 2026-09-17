from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit


REDACTED = "[REDACTED]"

# These values are generated for a single authentication transaction and must
# not become part of saved settings, task errors, or event history.  Stable
# discovery parameters such as ``entityID`` deliberately are not included.
TRANSIENT_AUTH_QUERY_KEYS = frozenset(
    {
        "access_token",
        "assertion",
        "awsaccesskeyid",
        "client_secret",
        "code",
        "download_token",
        "id_token",
        "jwt",
        "key_pair_id",
        "nonce",
        "oauth_token",
        "oauth_verifier",
        "policy",
        "refresh_token",
        "relaystate",
        "samlrequest",
        "samlresponse",
        "session_state",
        "sigalg",
        "signature",
        "state",
        "ticket",
        "token",
        "x_amz_credential",
        "x_amz_security_token",
        "x_amz_signature",
    }
)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_QUERY_PART_RE = re.compile(r"(^|[&;])([^&;=]*)(=)([^&;]*)")
_BARE_SECRET_RE = re.compile(
    r"(?i)(\b(?:"
    + "|".join(re.escape(key) for key in sorted(TRANSIENT_AUTH_QUERY_KEYS, key=len, reverse=True))
    + r")=)([^&#\s<>\"']+)"
)
_TRAILING_URL_PUNCTUATION = ").,;:!?]}，。；：！？】》"


def _normalise_query_key(value: str) -> str:
    return unquote_plus(value).strip().casefold().replace("-", "_")


def transient_auth_query_keys(url: str) -> frozenset[str]:
    """Return one-use authentication parameter names present in a URL."""

    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return frozenset()
    found: set[str] = set()
    for component in (parsed.query, parsed.fragment if "=" in parsed.fragment else ""):
        for match in _QUERY_PART_RE.finditer(component):
            key = _normalise_query_key(match.group(2))
            if key in TRANSIENT_AUTH_QUERY_KEYS:
                found.add(key)
    return frozenset(found)


def has_transient_auth_query(url: str) -> bool:
    return bool(transient_auth_query_keys(url))


def _redact_query_component(component: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if _normalise_query_key(match.group(2)) not in TRANSIENT_AUTH_QUERY_KEYS:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}{match.group(3)}{REDACTED}"

    return _QUERY_PART_RE.sub(replace, component)


def redact_url(url: str | None) -> str | None:
    """Redact one-use SAML/OAuth values while preserving a useful URL."""

    if url is None:
        return None
    value = str(url)
    try:
        parsed = urlsplit(value)
    except ValueError:
        return _BARE_SECRET_RE.sub(
            lambda match: f"{match.group(1)}{REDACTED}", value
        )

    netloc = parsed.netloc
    if "@" in netloc:
        _userinfo, host = netloc.rsplit("@", 1)
        netloc = f"{REDACTED}@{host}"
    query = _redact_query_component(parsed.query)
    fragment = (
        _redact_query_component(parsed.fragment)
        if "=" in parsed.fragment
        else parsed.fragment
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment))


def redact_diagnostic_text(value: object) -> str:
    """Redact sensitive URLs and bare auth parameters in diagnostic text."""

    text = str(value)

    def replace_url(match: re.Match[str]) -> str:
        candidate = match.group(0)
        suffix = ""
        while candidate and candidate[-1] in _TRAILING_URL_PUNCTUATION:
            suffix = candidate[-1] + suffix
            candidate = candidate[:-1]
        return str(redact_url(candidate)) + suffix

    text = _URL_RE.sub(replace_url, text)
    return _BARE_SECRET_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", text)


def redact_diagnostic(value: Any) -> Any:
    """Recursively redact strings before diagnostic data is persisted."""

    if isinstance(value, str):
        return redact_diagnostic_text(value)
    if isinstance(value, Mapping):
        return {key: redact_diagnostic(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_diagnostic(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_diagnostic(item) for item in value)
    return value
