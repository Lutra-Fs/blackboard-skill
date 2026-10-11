from __future__ import annotations

from typing import Any

from .archive_io import ArchiveIntegrityError


def attachment_urls(links: dict[str, Any]) -> list[str]:
    raw = links.get("attachments")
    if not isinstance(raw, list):
        raise ArchiveIntegrityError("links.json attachments must be a JSON array")
    return [
        str(item.get("href"))
        for item in raw
        if isinstance(item, dict) and item.get("href")
    ]


def verify_node_links(node: dict[str, Any], links: dict[str, Any]) -> list[str]:
    """Verify one published tree node against its page links artifact."""
    node_id = str(node.get("id") or "unknown")
    actual_urls = attachment_urls(links)
    expected_count = int(node.get("attachment_count") or 0)
    if len(actual_urls) != expected_count:
        raise ArchiveIntegrityError(
            f"attachment count mismatch for {node_id}: tree={expected_count}, links={len(actual_urls)}"
        )

    recorded_urls = node.get("attachment_urls")
    if recorded_urls is not None:
        if not isinstance(recorded_urls, list) or any(not isinstance(value, str) for value in recorded_urls):
            raise ArchiveIntegrityError(f"attachment_urls is invalid for {node_id}")
        if set(recorded_urls) != set(actual_urls):
            raise ArchiveIntegrityError(f"attachment URL identity mismatch for {node_id}")
    return actual_urls
