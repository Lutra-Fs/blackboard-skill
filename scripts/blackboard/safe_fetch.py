from __future__ import annotations

import json
import os
import re
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .url_policy import is_safe_public_https_url, is_valid_http_url, is_trusted_attachment_source_url
from .browser_backend import SessionRequired


def _at_origin(url: str, origin: str | None) -> bool:
    if not origin:
        return False
    try:
        a, b = urlsplit(url), urlsplit(origin)
        default = 443 if b.scheme == 'https' else 80
        return (a.scheme, a.hostname, a.port or default) == (b.scheme, b.hostname, b.port or default)
    except ValueError:
        return False


def _allowed_target(url, policy, auth_origin, site):
    selected = is_valid_http_url(url) and (
        _at_origin(url, auth_origin) or bool(site and (
            any(site.matches_origin(url, origin) for origin in site.cookie_origins())
            or is_trusted_attachment_source_url(url, site_config=site))))
    if not (is_safe_public_https_url(url) or selected):
        return False
    return policy(url) or (selected and policy is is_safe_public_https_url)


def _protected(url: str, origin: str | None) -> bool:
    return _at_origin(url, origin) and urlsplit(url).path.startswith(('/learn/api/', '/bbcswebdav/'))


def _login_target(url: str, origin: str | None) -> bool:
    parsed = urlsplit(url)
    return _at_origin(url, origin) and (
        parsed.path.rstrip('/') == '/webapps/login'
        or parsed.path.startswith('/webapps/login/')
        or (parsed.path.rstrip('/') == '/ultra' and 'new_loc' in parse_qs(parsed.query, keep_blank_values=True))
    )


class FetchError(RuntimeError):
    """Raised when a bounded HTTP(S) transfer cannot be validated."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


def cdn_path_key(effective_url: str) -> str:
    """Strip query parameters from a CDN URL to get a stable dedup key.

    Blackboard CDN URLs carry S3 signature/AWS params in the query string
    that change per request, but the path identifies the file stably.
    """
    return effective_url.split("?", 1)[0] if effective_url else ""


@dataclass(frozen=True)
class DownloadOutcome:
    """Result of a bounded download: the effective URL plus optional headers.

    ``content_disposition`` carries the final response's ``Content-Disposition``
    header verbatim (or ``None`` when absent). Blackboard's CDN sets
    ``inline; filename*=UTF-8''<real name>`` for every attachment, so this is
    the authoritative source of a file's real name — far more reliable than a
    BBML body anchor (which degrades to a signed ``xid-`` token).
    """

    effective_url: str
    content_disposition: str | None = None

    def __str__(self) -> str:
        return self.effective_url


# Parse a ``Content-Disposition`` header into a filename. Handles both the
# legacy ``filename="..."`` and the RFC 5987 ``filename*=UTF-8''<pct-encoded>``
# forms. Blackboard's CDN uses the RFC 5987 form with percent-encoded UTF-8.
_DISPOSITION_FILENAME = re.compile(
    r"filename\*\s*=\s*([^''\s]+)''(?P<ext>.*?)"
    r"(?:;|$)",
    re.IGNORECASE,
)
_DISPOSITION_FILENAME_QUOTED = re.compile(
    r'filename\s*=\s*"(?P<q>[^"]+)"',
    re.IGNORECASE,
)
_DISPOSITION_FILENAME_BARE = re.compile(
    r"filename\s*=\s*(?P<bare>[^;]+)",
    re.IGNORECASE,
)


def filename_from_content_disposition(disposition: str | None) -> str | None:
    """Extract a decoded filename from a ``Content-Disposition`` header value.

    Returns ``None`` when the header is absent or carries no filename. Handles
    the RFC 5987 ``filename*=UTF-8''<pct-encoded>`` form (URL-decoded) used by
    the Blackboard CDN, plus the quoted/bare ``filename="..."`` fallbacks.
    """
    if not disposition:
        return None
    text = disposition.strip()
    # RFC 5987 extended form takes precedence: filename*=charset''<pct-encoded>
    match = _DISPOSITION_FILENAME.search(text)
    if match:
        encoded = match.group("ext").strip().strip('"')
        # The value is percent-encoded UTF-8; unquote decodes %20 etc.
        decoded = unquote(encoded)
        if decoded:
            return decoded
    match = _DISPOSITION_FILENAME_QUOTED.search(text)
    if match:
        value = match.group("q").strip()
        if value:
            return value
    match = _DISPOSITION_FILENAME_BARE.search(text)
    if match:
        value = match.group("bare").strip().strip('"')
        if value:
            return value
    return None


# Cap redirect hops and strip the Cookie header when a redirect crosses to a
# different host so a Blackboard session cookie (BbRouter) is never sent to the
# CDN host.
class _LimitedRedirectHandler(HTTPRedirectHandler):
    """Follow redirects with a hop cap and cross-origin cookie isolation."""

    max_redirections = 5
    max_repeats = 4  # HTTPRedirectHandler allows 4 repeats => 5 total hops.

    def __init__(self, policy=is_safe_public_https_url, cookie_store=None, auth_origin=None, site_config=None):
        self.policy = policy
        self.cookie_store = cookie_store
        self.auth_origin = auth_origin
        self.site = site_config
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _protected(req.full_url, self.auth_origin) and _login_target(newurl, self.auth_origin):
            raise SessionRequired('Blackboard redirected a protected request to sign-in')
        if not _allowed_target(newurl, self.policy, self.auth_origin, self.site):
            raise FetchError("Redirect target rejected before dispatch")
        request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if request is not None:
            request.remove_header("Cookie")
            request.remove_header("Authorization")
            same_origin = _at_origin(newurl, req.full_url)
            if self.cookie_store is None and same_origin and req.get_header("Cookie"):
                request.add_unredirected_header("Cookie", req.get_header("Cookie"))
            if self.cookie_store is not None:
                cookie = self.cookie_store.cookie_header(newurl)
                if cookie:
                    request.add_unredirected_header("Cookie", cookie)
        return request


class HttpFetcher:
    """Bounded HTTP(S) client backed by the stdlib ``urllib``, cookie-authorised.

    When ``cookie_store`` is provided (a :class:`cookie_store.CookieStore`),
    every request carries the full Blackboard cookie set (including the
    ``HttpOnly`` ``BbRouter`` that ``document.cookie`` cannot see), so requests
    can call REST APIs and download bbcswebdav attachments directly — without
    routing through browser navigation + download events.

    The redirect chain (bbcswebdav signed URL → alt CDN → xythos CDN) is
    followed automatically; the final response's ``Content-Disposition`` header
    is the authoritative source of each attachment's real filename.
    """

    # Stream chunk size for downloads — large enough to amortise syscall overhead,
    # small enough that a size-cap violation aborts promptly.
    _CHUNK = 256 * 1024
    _USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

    def __init__(
        self,
        *,
        max_bytes: int = 1024 * 1024 * 1024,
        cookie_store: Any = None,
        auth_origin: str | None = None,
        site_config=None,
    ) -> None:
        self.max_bytes = max_bytes
        self.cookie_store = cookie_store
        self.auth_origin = auth_origin
        self.site = site_config
        self._opener = None

    def _build_request(
        self,
        url: str,
        *,
        method: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Request:
        merged: dict[str, str] = {"User-Agent": self._USER_AGENT}
        if self.cookie_store is not None:
            cookie = self.cookie_store.cookie_header(url)
            if cookie:
                merged["Cookie"] = cookie
            elif _protected(url, self.auth_origin):
                raise SessionRequired('No browser cookies are available for this protected Blackboard request')
        if headers:
            merged.update(headers)
        return Request(url, headers=merged, method=method)

    def _open(
        self,
        request: Request,
        *,
        timeout: int,
        url_policy: Callable[[str], bool],
    ) -> Any:
        """Open with urllib, validate the final URL policy, raise FetchError.

        This is the single transport boundary: every urllib exception type
        (``HTTPError``, ``URLError``, ``TimeoutError``, ``OSError``) is converted
        to :class:`FetchError` here, so no caller ever sees a raw urllib
        exception. The ``http_status`` is preserved on ``HTTPError`` so callers
        can distinguish 401 (cookie expired) from 404 (absent) from 500 (server).
        """
        try:
            if not _allowed_target(request.full_url, url_policy, self.auth_origin, self.site):
                raise FetchError("Request URL rejected before dispatch")
            opener = self._opener or build_opener(_LimitedRedirectHandler(url_policy, self.cookie_store,
                                                self.auth_origin, self.site))
            response = opener.open(request, timeout=timeout)
        except HTTPError as exc:
            # A CDN's expired signed link or a course 403 is not a lost browser session.
            if (exc.code == 401 and _protected(request.full_url, self.auth_origin)
                    and _protected(exc.geturl(), self.auth_origin)):
                raise SessionRequired('Blackboard rejected the browser session (HTTP 401)') from exc
            raise FetchError(
                exc.reason or f"HTTP {exc.code}", http_status=exc.code,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise FetchError(f"HTTP transfer failed: {exc}") from exc
        effective_url = response.geturl()
        if _protected(request.full_url, self.auth_origin) and _login_target(effective_url, self.auth_origin):
            response.close()
            raise SessionRequired('Blackboard redirected a protected request to sign-in')
        if not _allowed_target(effective_url, url_policy, self.auth_origin, self.site):
            response.close()
            raise FetchError("Redirect target rejected by site policy")
        return response

    def download(
        self,
        url: str,
        destination: Path,
        *,
        url_policy: Callable[[str], bool] = is_safe_public_https_url,
        headers: dict[str, str] | None = None,
        timeout: int = 180,
    ) -> DownloadOutcome:
        """Download once, streaming to ``destination``, returning the outcome.

        Streams the body in fixed chunks (never holding it fully in memory),
        enforces ``max_bytes``, and captures the final response's
        ``Content-Disposition`` header so callers can recover a file's real
        name from the Blackboard CDN. Writes to a ``.<name>.tmp`` sibling and
        atomically renames on success so a crash never leaves a half-file at
        the target path.
        """
        if not _allowed_target(url, url_policy, self.auth_origin, self.site):
            raise FetchError("URL rejected by HTTPS origin policy")
        if destination.is_symlink():
            raise FetchError("Download destination is a symlink")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        request = self._build_request(url, headers=headers)
        response = self._open(request, timeout=timeout, url_policy=url_policy)

        content_disposition = response.headers.get("Content-Disposition")
        content_encoding = (response.headers.get("Content-Encoding") or "identity").strip().lower()
        if content_encoding not in {"identity", "gzip"}:
            response.close()
            temporary.unlink(missing_ok=True)
            raise FetchError(f"Unsupported Content-Encoding: {content_encoding}")
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS) if content_encoding == "gzip" else None
        renamed = False
        try:
            temporary.unlink(missing_ok=True)
            written = 0
            wire_bytes = 0
            try:
                with temporary.open("wb") as handle:
                    while True:
                        chunk = response.read(self._CHUNK)
                        if not chunk:
                            break
                        wire_bytes += len(chunk)
                        if wire_bytes > self.max_bytes:
                            raise FetchError(
                                f"response exceeds max bytes ({self.max_bytes})"
                            )
                        while chunk:
                            remaining = self.max_bytes - written + 1
                            decoded = decompressor.decompress(chunk, remaining) if decompressor is not None else chunk
                            if len(decoded) > self.max_bytes - written:
                                raise FetchError(
                                    f"decoded response exceeds max bytes ({self.max_bytes})"
                                )
                            handle.write(decoded)
                            written += len(decoded)
                            if decompressor is None:
                                break
                            chunk = decompressor.unconsumed_tail
            except FetchError:
                temporary.unlink(missing_ok=True)
                raise
            except (URLError, TimeoutError, OSError) as exc:
                # A connection reset mid-download surfaces here, not from _open.
                temporary.unlink(missing_ok=True)
                raise FetchError(f"HTTP transfer failed: {exc}") from exc
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and wire_bytes != int(length):
                temporary.unlink(missing_ok=True)
                raise FetchError("Response body does not match Content-Length")
            if decompressor is not None and not decompressor.eof:
                temporary.unlink(missing_ok=True)
                raise FetchError("Incomplete gzip response")
            if written <= 0:
                temporary.unlink(missing_ok=True)
                raise FetchError("HTTPS transfer produced no file")
            os.replace(temporary, destination)
            renamed = True
            return DownloadOutcome(
                effective_url=response.geturl(),
                content_disposition=content_disposition,
            )
        finally:
            response.close()
            # Only clean up the temp file if the rename did not consume it;
            # after a successful rename the temp path no longer exists.
            if not renamed:
                temporary.unlink(missing_ok=True)

    def probe(
        self,
        url: str,
        *,
        url_policy: Callable[[str], bool] = is_safe_public_https_url,
        timeout: int = 15,
    ) -> dict[str, Any] | None:
        """Check revision with a one-byte GET instead of transferring a file.

        Some Blackboard deployments reject the HEAD probe with an unredirected
        302 while an authenticated Range GET succeeds.  For a 206 response the
        full object size comes from ``Content-Range``; its one-byte
        ``Content-Length`` is never mistaken for the object size.
        """
        if not _allowed_target(url, url_policy, self.auth_origin, self.site):
            return None
        request = self._build_request(
            url, method="GET", headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"}
        )
        try:
            response = self._open(request, timeout=timeout, url_policy=url_policy)
        except (FetchError, URLError, TimeoutError, OSError):
            return None
        try:
            content_length: int | None = None
            range_header = (response.headers.get("Content-Range") or "").strip()
            match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", range_header, re.IGNORECASE)
            if response.status == 206 and match:
                content_length = int(match.group(3))
            elif response.status != 206:
                raw = response.headers.get("Content-Length")
                try:
                    content_length = int(raw) if raw else None
                except (TypeError, ValueError):
                    content_length = None
            response.read(1)
            encoded = (response.headers.get("Content-Encoding") or "identity").strip().lower()
            return {
                "effective_url": response.geturl(),
                # Encoded lengths are not comparable with decoded local files.
                "content_length": None if encoded not in {"identity", ""} else content_length,
                "content_disposition": response.headers.get("Content-Disposition"),
            }
        except (URLError, TimeoutError, OSError):
            return None
        finally:
            response.close()

    def fetch_json(
        self,
        url: str,
        *,
        url_policy: Callable[[str], bool] = is_safe_public_https_url,
        headers: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> Any:
        """GET a JSON endpoint with cookie auth. Returns the parsed object.

        Raises :class:`FetchError` on non-2xx, policy rejection, or a non-JSON body.
        The ``http_status`` attribute on :class:`FetchError` carries the HTTP
        response code so callers can distinguish 404 (resource absent) from
        401 (cookie expired) from 500 (server).
        """
        if not _allowed_target(url, url_policy, self.auth_origin, self.site):
            raise FetchError("URL rejected by HTTPS origin policy")
        merged = {"Accept": "application/json"}
        if headers:
            merged.update(headers)
        request = self._build_request(url, headers=merged)
        response = self._open(request, timeout=timeout, url_policy=url_policy)
        http_status = response.status
        try:
            body = response.read(min(self.max_bytes, 32 * 1024 * 1024) + 1)
            if len(body) > min(self.max_bytes, 32 * 1024 * 1024):
                raise FetchError("JSON response exceeds size limit")
        except (URLError, TimeoutError, OSError) as exc:
            raise FetchError(f"HTTP transfer failed: {exc}", http_status=http_status) from exc
        finally:
            response.close()
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FetchError(f"non-JSON response: {exc}", http_status=http_status) from exc
