from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .content_rules import (
    bucket_for_hierarchy,
    node_type_for,
    normalize_block,
    safe_filename,
    sha256_text,
)


@dataclass(frozen=True)
class Attachment:
    text: str
    href: str
    mime_type: str | None = None
    source_kind: str | None = None
    original_filename: str | None = None

    @property
    def filename(self) -> str:
        return safe_filename(self.original_filename or self.text, self.href)


@dataclass(frozen=True)
class Page:
    content_id: str
    title: str
    parent_title: str | None
    parent_id: str | None
    depth: int
    breadcrumbs: list[str]
    url: str
    body_text: str
    attachments: list[Attachment] = field(default_factory=list)
    child_links: list[dict[str, Any]] = field(default_factory=list)
    external_links: list[dict[str, Any]] = field(default_factory=list)
    unresolved_assets: list[dict[str, Any]] = field(default_factory=list)
    node_type_hint: str | None = None
    # Ultra content keeps the short description separate from the BBML body.
    # Preserve it as first-class evidence instead of folding it into body_text.
    description_text: str = ""
    due_at: str | None = None

    @property
    def bucket(self) -> str:
        return bucket_for_hierarchy(self.title, self.parent_title, self.breadcrumbs)

    @property
    def node_type(self) -> str:
        return self.node_type_hint or node_type_for(self.title, self.depth)

    @property
    def text_hash(self) -> str:
        return sha256_text(normalize_block(self.body_text))

    def local_path_for_bucket(self, bucket: str, attachment: Attachment) -> Path:
        return Path(bucket) / attachment.filename
