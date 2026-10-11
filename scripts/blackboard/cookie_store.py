"""Blackboard cookie cache + RFC 6265 origin filtering over one resolved browser.

``document.cookie`` cannot see ``HttpOnly`` cookies used by Blackboard REST
and attachment sessions. :class:`CookieStore` therefore reads cookies through the
backend its :class:`browser_backend.BrowserSession` already resolved
(ego-browser, browser-use, and the kimi-webbridge daemon all expose CDP
cookie access) and has no transport logic of its
own. Navigation and cookies consequently always come from the same browser:
there is no backend parameter to forget, and a fetch failure is a
single-backend failure, never a silent switch to another browser's cookie jar.

This module's single responsibility is cookie lifecycle: cache the raw
CDP-style cookie set for ``max_age_seconds``, and filter it per request by
RFC 6265 domain/path/secure matching so a Blackboard request only carries
Blackboard cookies and a Leganto request only carries Leganto cookies. The
resulting ``Cookie:`` header authorises :class:`safe_fetch.HttpFetcher` to
call REST APIs and download attachments directly.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

from .browser_backend import BrowserSession, BrowserUnavailable


class CookieStore:
    """Cached cookie set from the session's browser backend, filtered per-request by origin.

    Cookies are cached for ``max_age_seconds`` (default 5 minutes); call
    :meth:`refresh` to force a re-fetch when the session may have expired.
    ``domains`` names the origins whose cookies this store reads (the site's
    ``cookie_origins()``); reads are scoped to the selected site and integrations.
    """

    def __init__(self, browser: BrowserSession, *, domains: tuple[str, ...]) -> None:
        self._browser = browser
        self.domains = tuple(domains)
        self._cached: list[dict[str, Any]] | None = None
        self._cache_at: float = 0.0

    def cookie_header(self, url: str = "", *, max_age_seconds: float = 300.0) -> str:
        """Return a ``name=value; name=value; ...`` string for the URL's origin.

        If ``url`` is empty, returns all cached cookies (backwards compat for
        callers that don't pass a URL). Otherwise filters by RFC 6265 domain
        matching so cross-origin cookies are not leaked.
        """
        now = time.time()
        if self._cached is None or (now - self._cache_at) > max_age_seconds:
            self._cached = self._fetch_raw()
            self._cache_at = now
        if not url:
            return self._all_cookies_header()
        return self._filtered_header(url)

    def refresh(self) -> str:
        """Force a re-fetch on the next :meth:`cookie_header` call."""
        self._cached = None
        return self.cookie_header()

    def _all_cookies_header(self) -> str:
        pairs = [f"{c['name']}={c['value']}" for c in (self._cached or []) if c.get("name") and c.get("value")]
        return "; ".join(pairs)

    def _filtered_header(self, url: str) -> str:
        parsed_url = urlparse(url)
        request_host = (parsed_url.hostname or "").lower()
        if not request_host:
            return self._all_cookies_header()
        request_path = parsed_url.path or "/"
        pairs: list[str] = []
        for cookie in self._cached or []:
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            if not name or not value:
                continue
            cookie_domain = str(cookie.get("domain") or "").lower().lstrip(".")
            if not cookie_domain:
                continue
            # RFC 6265 host-only cookies match exactly; domain cookies match
            # the domain itself or a subdomain. CDP may omit hostOnly for
            # older sessions, where a leading dot is the useful signal.
            host_only = cookie.get("hostOnly")
            if host_only is True or (host_only is None and not str(cookie.get("domain") or "").startswith(".")):
                host_match = request_host == cookie_domain
            else:
                host_match = request_host == cookie_domain or request_host.endswith("." + cookie_domain)
            cookie_path = str(cookie.get("path") or "/")
            if not cookie_path.startswith("/"):
                cookie_path = "/"
            path_match = request_path == cookie_path or (
                request_path.startswith(cookie_path.rstrip("/"))
                and (cookie_path.endswith("/") or len(request_path) > len(cookie_path) and request_path[len(cookie_path)] == "/")
            )
            secure_match = not cookie.get("secure") or parsed_url.scheme.lower() == "https"
            if host_match and path_match and secure_match:
                pairs.append(f"{name}={value}")
        return "; ".join(pairs)

    def _fetch_raw(self) -> list[dict[str, Any]]:
        """Fetch raw CDP-style cookies through the session's resolved backend.

        The backend was fixed when the owning ``BrowserSession`` was
        constructed. An empty cookie set is a valid read result; transport
        failures surface as browser dependencies, not authentication failures.
        """
        try:
            return self._browser.cookies(list(self.domains))
        except RuntimeError as exc:
            raise BrowserUnavailable(
                f"cookie fetch via {self._browser.backend} backend failed: {exc}"
            ) from exc
