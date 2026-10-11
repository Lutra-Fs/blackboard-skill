"""Parse Blackboard BBML content bodies returned by the REST contents API.

The REST ``GET /learn/api/public/v1/courses/{courseId}/contents`` response puts
Ultra document bodies in a ``body`` field as BBML (Blackboard Markup Language).
In practice BBML is HTML with a few Blackboard-specific attributes; the
attachment download URLs live in ordinary ``<a href=".../bbcswebdav/...">``
anchors. This module converts a BBML body string into the ``(body_text,
attachments)`` pair that :class:`sync_models.Page` consumes.

The parser is standard-library only (see SKILL.md: the runtime is stdlib-only)
and mirrors the ``_EmbeddedHtmlDownloadParser`` pattern in ``content_rules.py``.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urlparse

from .content_rules import normalize_block, safe_filename
from .sync_models import Attachment
from .site_config import SiteConfig
from .url_policy import is_blackboard_url, is_trusted_attachment_source_url


# Anchors whose href signals a downloadable attachment rather than an internal
# navigation link. ``bbcswebdav`` is the canonical Blackboard file-store path;
# the ``xid`` query parameter marks a signed attachment reference.
def _looks_like_attachment_href(href: str, *, site_config: SiteConfig) -> bool:
    if not href:
        return False
    parsed = urlparse(href)
    path = parsed.path.lower()
    query = (parsed.query or "").lower()
    if "/bbcswebdav/" in path:
        return True
    if "xid=" in query:
        return True
    if site_config.is_trusted_host(parsed.hostname):
        return path.endswith((".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip", ".ris"))
    return False


class _BbmlBodyParser(HTMLParser):
    """Collect attachment anchors and visible text from a BBML body."""

    def __init__(self, *, site_config: SiteConfig) -> None:
        super().__init__()
        self.site = site_config
        self.attachments: list[Attachment] = []
        self._text_parts: list[str] = []
        # Track the anchor currently being traversed so its visible text can be
        # used as the attachment label.
        self._anchor_depth = 0
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower == "a":
            values = {key.lower(): value or "" for key, value in attrs}
            href = values.get("href", "").strip()
            if href:
                self._anchor_depth = 1
                self._anchor_href = href
                self._anchor_text = []
        elif self._anchor_depth:
            self._anchor_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if not self._anchor_depth:
            return
        if tag.lower() == "a" and self._anchor_depth == 1:
            self._finish_anchor()
        else:
            self._anchor_depth = max(0, self._anchor_depth - 1)

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if self._anchor_depth:
            self._anchor_text.append(data)
        self._text_parts.append(data)

    def _finish_anchor(self) -> None:
        href = self._anchor_href
        label = " ".join(part.strip() for part in self._anchor_text if part.strip())
        self._anchor_depth = 0
        self._anchor_href = None
        self._anchor_text = []
        if href and _looks_like_attachment_href(href, site_config=self.site):
            filename = _filename_from_href_or_label(href, label)
            self.attachments.append(
                Attachment(
                    text=label or filename,
                    href=href,
                    mime_type=None,
                    source_kind="rest-bbml",
                    original_filename=filename,
                )
            )

    @property
    def body_text(self) -> str:
        return normalize_block(" ".join(self._text_parts))


def _filename_from_href_or_label(href: str, label: str) -> str:
    """Derive a deterministic filename for a BBML attachment anchor."""
    path_name = ""
    parsed = urlparse(href)
    if parsed.path:
        path_name = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
    candidate = path_name or label
    return safe_filename(candidate, href)


def parse_body(body: str | None, *, site_config: SiteConfig) -> tuple[str, list[Attachment]]:
    """Convert a BBML ``body`` string into ``(body_text, attachments)``.

    Returns ``("", [])`` for empty/None bodies. Attachment anchors are kept in
    document order and deduplicated by href. The body text preserves visible
    content with whitespace normalized via :func:`content_rules.normalize_block`.
    """
    if not body:
        return "", []
    parser = _BbmlBodyParser(site_config=site_config)
    try:
        parser.feed(body)
        parser.close()
    except Exception:
        # A malformed BBML fragment must not abort the crawl; fall back to the
        # text gathered so far.
        pass
    # Dedup attachments by href while preserving order; BBML sometimes repeats
    # the same signed href in a preview control and a download link.
    seen: set[str] = set()
    unique: list[Attachment] = []
    for attachment in parser.attachments:
        if attachment.href in seen:
            continue
        seen.add(attachment.href)
        unique.append(attachment)
    return parser.body_text, unique


def classify_content_handler(handler_id: str | None) -> str:
    """Map a Blackboard ``contentHandler.id`` to a skill ``node_type`` hint.

    Used by the REST crawler so content nodes carry a consistent node_type
    vocabulary across all handler types.
    """
    handler = (handler_id or "").lower()
    if not handler:
        return "page"
    if handler == "resource/x-bb-lesson":
        return "module"
    if handler == "resource/x-bb-document":
        return "document"
    if handler == "resource/x-bb-file":
        return "document"
    if handler == "resource/x-bb-folder":
        return "module"
    if handler.startswith("resource/x-bb-blti"):
        return "lti_tool"
    if handler == "resource/x-bb-externallink":
        return "external"
    if handler == "resource/x-bb-assignment":
        return "assessment"
    if handler == "resource/x-bb-toollink":
        return "lti_tool"
    if handler == "resource/x-bb-discussionboard" or handler == "resource/x-bb-forum":
        return "discussion_forum"
    return "page"


def external_links_from_body(body: str | None, *, site_config: SiteConfig) -> list[dict[str, Any]]:
    """Extract off-platform links from a BBML body for ``Page.external_links``.

    Only non-Blackboard, non-attachment HTTPS links are returned.
    """
    if not body:
        return []
    links: list[dict[str, Any]] = []
    seen: set[str] = set()

    class _LinkCollector(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag.lower() != "a":
                return
            values = {key.lower(): value or "" for key, value in attrs}
            href = values.get("href", "").strip()
            if not href or href in seen:
                return
            # Skip Blackboard-internal and attachment links.
            if is_blackboard_url(href, site_config=site_config):
                return
            if is_trusted_attachment_source_url(href, site_config=site_config):
                return
            if not href.startswith(("http://", "https://")):
                return
            seen.add(href)
            label = values.get("aria-label", "") or ""
            links.append({"text": label, "href": href})

    collector = _LinkCollector()
    try:
        collector.feed(body)
        collector.close()
    except Exception:
        pass
    return links
