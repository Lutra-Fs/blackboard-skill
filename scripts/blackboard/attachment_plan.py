from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Callable, Iterable


@dataclass
class AttachmentPlan:
    attachment: Any
    destination: Path


def destination_priority(path: Path) -> int:
    text = str(path).casefold()
    if "assessment" in text or text.startswith(("20_assessments/", "50_assessments/")):
        return 30
    if re.search(r"(?:^|/)\d+_week_\d+", text):
        return 20
    if text.startswith("00_admin/"):
        return 0
    return 10


def plan_unique_attachments(
    pages: Iterable[Any],
    bucket_for_page: Callable[[Any], str],
) -> list[AttachmentPlan]:
    """Choose one deterministic, hierarchy-preferred destination per source URL."""
    plans: dict[str, AttachmentPlan] = {}
    order: list[str] = []
    for page in pages:
        bucket = bucket_for_page(page)
        for attachment in page.attachments:
            href = str(attachment.href or "")
            if not href:
                continue
            # LTI launch and external-link handlers are not downloadable files.
            # The crawler surfaces them as attachments so the URL is retained on
            # the page, but routing them through the attachment downloader only
            # produces ``attachment-host-not-allowed`` failures (their hosts are
            # not trusted Blackboard sources). Books & Tools LTI placements
            # remain discoverable via ``external_links`` downstream.
            if attachment.source_kind in {"rest-lti-launch", "rest-external-link"}:
                continue
            destination = page.local_path_for_bucket(bucket, attachment)
            current = plans.get(href)
            if current is None:
                plans[href] = AttachmentPlan(attachment, destination)
                order.append(href)
                continue
            if destination_priority(destination) > destination_priority(current.destination):
                current.attachment = attachment
                current.destination = destination
    return [plans[href] for href in order]
