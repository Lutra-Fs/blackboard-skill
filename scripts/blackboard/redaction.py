"""Single redaction module: one secret-key vocabulary, two scrubbing behaviors.

Two redaction behaviors share this module:

* key-name based scrubbing of values persisted to archive state or printed in
  CLI envelopes, and
* URL/query based scrubbing of error text.

Both use ONE vocabulary here, so a key considered secret in one context
(OAuth ``code`` in a redirect URL) is considered secret in the other (an
envelope field named ``code``). ``resource_store`` and ``browser_backend``
import from this module instead of keeping private copies.

Key matching normalizes like browser-backend errors always did: the key is
URL-unquoted up to 6 times, NFKC-normalized, and stripped to alphanumeric
compact form, then matched against :data:`SECRET_KEY_NAMES` (exact match),
:data:`SECRET_KEY_MARKERS` (substring match), or :data:`SECRET_KEY_PREFIXES`
(AWS/GCS signed-header prefixes).
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# Shared secret-key vocabulary
# ---------------------------------------------------------------------------

#: Exact compact-match key names that always denote a secret bearer value:
#: OAuth/SAML grant parameters and cloud-signing key ids. These apply to
#: envelope field names too (see module docs).
SECRET_KEY_NAMES = frozenset({
    "accesstoken", "apikey", "assertion", "auth", "authorization",
    "awsaccesskeyid", "clientsecret", "code", "credential",
    "googleaccessid", "idtoken", "jwt", "key", "keypairid", "nonce",
    "oauthsignature", "oauthtoken", "oauthverifier", "password", "passwd",
    "refreshtoken", "relaystate", "samlart", "samlrequest", "samlresponse",
    "secret", "session", "sessionid", "sharedaccesssignature", "sig",
    "sigalg", "signature", "state", "subscriptionkey", "ticket", "token",
})

#: Substring markers: any key whose compact form contains one of these is
#: treated as secret-bearing. Persister scrubbing contributes ``tabid`` and
#: URL-canonicalization contributes ``sign``/``auth``/``policy``/``key``
#: beyond the error-redaction markers, so both contexts stay equally strict.
SECRET_KEY_MARKERS = frozenset({
    "accesstoken", "apikey", "assertion", "auth", "authcode", "authorization",
    "clientsecret", "codeverifier", "cookie", "credential", "csrf",
    "devicecode", "idtoken", "jwt", "key", "nonce", "oauth", "password",
    "passwd", "policy", "privatekey", "refreshtoken", "relaystate", "saml",
    "secret", "sessid", "session", "sharedaccesssignature", "sign",
    "signature", "subscriptionkey", "tabid", "ticket", "token", "usercode",
    "xsrf",
})

#: Signed-request header prefixes (AWS ``X-Amz-*``, GCS ``X-Goog-*``).
SECRET_KEY_PREFIXES = ("xamz", "xgoog")


def _bounded_decode(value: str) -> str:
    decoded = str(value)
    for _ in range(6):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return unicodedata.normalize("NFKC", decoded)


def is_secret_key(value: str) -> bool:
    """True when a field/query key name marks its value as a secret.

    The key is unquoted (bounded), NFKC-normalized, and compacted to
    alphanumerics before matching, so ``Key-Pair-Id``, ``key_pair_id``, and
    ``keypairid`` all behave identically.
    """
    compact = re.sub(r"[^a-z0-9]", "", _bounded_decode(value).casefold())
    return bool(
        compact in SECRET_KEY_NAMES
        or compact.startswith(SECRET_KEY_PREFIXES)
        or any(marker in compact for marker in SECRET_KEY_MARKERS)
    )


# ---------------------------------------------------------------------------
# URL/query redaction for error text
# ---------------------------------------------------------------------------

_URL_IN_TEXT = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_NETWORK_PATH_REF_IN_TEXT = re.compile(
    r"(?<![:/\w])//[^\s<>'\"]+"
)
_RELATIVE_URL_QUERY_IN_TEXT = re.compile(
    r"(?P<path>(?<![:/\w])/(?!/)[^\s?#]*)\?[^\s#]+"
)
_QUERY_ASSIGNMENT_IN_TEXT = re.compile(
    r"(?P<key>[\w%.\-\[\]]+(?:[\s_-]+[\w%.\-\[\]]+){0,2})\s*="
    r"(?P<value>[^&\s#]+)",
    re.UNICODE,
)


def _value_contains_secret_assignment(value: str) -> bool:
    decoded = _bounded_decode(value)
    for match in _QUERY_ASSIGNMENT_IN_TEXT.finditer(decoded):
        if is_secret_key(match.group("key")):
            return True
    return False


def redact_url_for_error(url: str) -> str:
    """Keep a URL useful for diagnosis without retaining auth-bearing parts."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        return "<redacted-url>"
    if not parsed.scheme or not hostname:
        return "<redacted-url>"
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    safe_segments: list[str] = []
    for raw_segment in parsed.path.split("/"):
        segment = re.split(
            r";|%(?:25)*3b",
            raw_segment,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        decoded = _bounded_decode(segment)
        if (
            re.search(r"https?://", decoded, flags=re.IGNORECASE)
            or re.search(r"//[^/\s]*@", decoded)
            or _value_contains_secret_assignment(decoded)
        ):
            safe_segments.append("redacted")
        else:
            safe_segments.append(segment)
    safe_path = "/".join(safe_segments)
    return urlunsplit((parsed.scheme, netloc, safe_path, "", ""))


def redact_urls_for_error(text: str) -> str:
    """Scrub URLs and secret query assignments from error text.

    Absolute URLs keep scheme/host/path but drop queries and auth-bearing path
    segments; bare ``key=value`` assignments whose key is in the shared
    vocabulary are masked. Used for every backend error message.
    """
    normalized = text.replace("\\/", "/")
    redacted_urls = _URL_IN_TEXT.sub(
        lambda match: redact_url_for_error(match.group(0)),
        normalized,
    )
    redacted_urls = _NETWORK_PATH_REF_IN_TEXT.sub(
        lambda match: redact_url_for_error(
            "https:" + match.group(0)
        ).removeprefix("https:"),
        redacted_urls,
    )
    redacted_urls = _RELATIVE_URL_QUERY_IN_TEXT.sub(
        lambda match: match.group("path"),
        redacted_urls,
    )

    def redact_assignment(match: re.Match[str]) -> str:
        if (
            is_secret_key(match.group("key"))
            or _value_contains_secret_assignment(match.group("value"))
        ):
            return f"{match.group('key')}=<redacted>"
        return match.group(0)

    return _QUERY_ASSIGNMENT_IN_TEXT.sub(redact_assignment, redacted_urls)


# ---------------------------------------------------------------------------
# Key-name based scrubbing for persisted values
# ---------------------------------------------------------------------------

_BEARER_TOKEN_IN_TEXT = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_URL_IN_VALUE = re.compile(r'https?://[^\s<>"\x27)]+')


def stable_url(value: str) -> str:
    """Drop transient auth parameters while preserving existing asset IDs."""
    p = urlsplit(value)
    if "/bbcswebdav/" in p.path or "blackboardcdn" in p.netloc:
        query = ""
    else:
        query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                           if not is_secret_key(k)])
    return urlunsplit((p.scheme, p.netloc.split("@")[-1], p.path, query, ""))


def clean(value):
    """Recursively drop credential-bearing keys and scrub URLs from a value.

    Dict keys are checked against the shared secret-key vocabulary; strings
    have their URLs canonicalized through :func:`stable_url` and Bearer tokens
    masked. Used before anything is persisted to archive state or printed in
    CLI envelopes.
    """
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if not is_secret_key(k)}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, str):
        text = _URL_IN_VALUE.sub(lambda m: stable_url(m[0]), value)
        return _BEARER_TOKEN_IN_TEXT.sub("Bearer [redacted]", text)
    return value
