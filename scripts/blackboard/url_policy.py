from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

from .site_config import SiteConfig


# Video transcript hosts supported by the youtube_transcripts stage (yt-dlp
# driven). TED talks use /talks/<slug> and are captured through the same
# transcript pipeline as YouTube.
VIDEO_HOSTS = frozenset({
    "www.youtube.com",
    "youtube.com",
    "youtu.be",
    "www.youtube-nocookie.com",
    "youtube-nocookie.com",
    "www.ted.com",
    "ted.com",
})


def require_blackboard_id(value: str, *, label: str = "Blackboard id") -> str:
    value = str(value or "").strip()
    if not re.fullmatch(r"_[A-Za-z0-9-]+_\d+", value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def _safe_https_host(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme.lower() != "https" or not host or parsed.username or parsed.password:
            return None
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (
            address.is_loopback
            or address.is_link_local
            or address.is_private
            or address.is_reserved
            or address.is_unspecified
        ):
            return None
        return host
    except ValueError:
        return None


def is_safe_public_https_url(url: str) -> bool:
    return _safe_https_host(url) is not None


def is_valid_http_url(url: str) -> bool:
    """Syntax check for a user-selected site, including internal addresses."""
    try:
        parsed = urlparse(url)
        return (parsed.scheme.lower() in {'http', 'https'} and bool(parsed.hostname)
                and not any(c.isspace() for c in parsed.hostname)
                and not parsed.username and not parsed.password
                and (parsed.port is None or 0 < parsed.port <= 65535))
    except ValueError:
        return False


def is_blackboard_url(
    url: str,
    *,
    prefixes: tuple[str, ...] = (),
    site_config: SiteConfig,
) -> bool:
    if not is_valid_http_url(url) or not site_config.matches_origin(url):
        return False
    if not prefixes:
        return True
    return urlparse(url).path.startswith(prefixes)


def is_leganto_url(url: str, *, site_config: SiteConfig) -> bool:
    return bool(site_config.leganto_origin and site_config.matches_origin(url, site_config.leganto_origin))


def is_panopto_url(url: str, *, site_config: SiteConfig) -> bool:
    return bool(site_config.panopto_origin and site_config.matches_origin(url, site_config.panopto_origin))


def is_video_transcript_url(url: str) -> bool:
    """YouTube and TED — the two transcript-capable video hosts."""
    return _safe_https_host(url) in VIDEO_HOSTS


def is_trusted_attachment_source_url(url: str, *, site_config: SiteConfig) -> bool:
    if not is_valid_http_url(url):
        return False
    host = urlparse(url).hostname
    if site_config.is_blackboard_host(host):
        return site_config.matches_origin(url)
    # Blackboard's own WebDAV path is always eligible. A configured trusted
    # host/suffix may also serve an attachment or a site-specific redirect.
    return is_blackboard_url(url, prefixes=("/bbcswebdav/",), site_config=site_config) or site_config.is_trusted_host(host)
