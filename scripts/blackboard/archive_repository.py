from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .archive_io import (
    ArchiveIntegrityError,
    atomic_write_json,
    atomic_write_text,
    load_course_tree,
    load_json_object,
    safe_archive_path,
    safe_path_component,
    utc_now_iso,
)
from .content_rules import (
    assessment_number_for_title,
    normalize_block,
    sha256_text,
)
from .sync_models import Attachment, Page
from .site_config import SiteConfig


class ArchiveState:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.blackboard_root = safe_archive_path(self.root, "_blackboard")
        self.course_tree_path = self.blackboard_root / "course-tree.json"
        self.existing_tree = load_course_tree(self.course_tree_path) or {}
        self.old_hashes = {n["id"]: n.get("page_text_sha256") for n in self.existing_tree.get("nodes", [])}

    def bucket_for_page(self, page: Page) -> str:
        hierarchy_text = " > ".join([*page.breadcrumbs, page.parent_title or "", page.title])
        assessment_number = assessment_number_for_title(hierarchy_text)
        if assessment_number is None:
            return page.bucket

        base = self.root / "20_Assessments"
        if base.exists():
            candidates = sorted(
                path for path in base.iterdir()
                if path.is_dir() and re.search(rf"\bAssessment\s*{assessment_number}(?:\b|[A-Za-z])", path.name, re.IGNORECASE)
            )
            if candidates:
                return str(candidates[0].relative_to(self.root))

        return page.bucket

    def load_published_pages(self, *, expected_run_id: str | None = None) -> list[Page]:
        """Rehydrate pages from the immutable generation selected by the tree."""
        tree = load_course_tree(self.course_tree_path, required=True)
        assert tree is not None
        return self.load_tree_pages(tree, expected_run_id=expected_run_id)

    def load_generation_tree(self, generation_id: str, *, expected_run_id: str | None = None) -> dict[str, Any]:
        safe_generation = safe_path_component(generation_id, label="metadata generation id")
        tree_path = safe_archive_path(
            self.root,
            Path("_blackboard/generations") / safe_generation / "course-tree.json",
            label="prepared course tree",
        )
        tree = load_course_tree(tree_path, required=True)
        assert tree is not None
        if tree.get("generation_id") != safe_generation:
            raise ArchiveIntegrityError(f"Prepared course tree generation mismatch: {tree_path}")
        if expected_run_id is not None and tree.get("run_id") != expected_run_id:
            raise ArchiveIntegrityError(f"Prepared course tree does not belong to run {expected_run_id}")
        return tree

    def load_tree_pages(
        self,
        tree: dict[str, Any],
        *,
        expected_run_id: str | None = None,
    ) -> list[Page]:
        """Rehydrate and verify every page selected by one tree payload."""
        if expected_run_id is not None and tree.get("run_id") != expected_run_id:
            raise ArchiveIntegrityError(
                f"Published course tree does not belong to resumable run {expected_run_id}"
            )
        generation_id = str(tree.get("generation_id") or "")
        if generation_id:
            generation_id = safe_path_component(generation_id, label="metadata generation id")
        tree_course_id = str(tree.get("course_id") or "")
        tree_site_origin = str(tree.get("site_origin") or "").strip()
        pages: list[Page] = []
        for node in tree["nodes"]:
            content_id = safe_path_component(str(node.get("id") or ""), label="published content id")
            page_dir = safe_archive_path(
                self.root,
                str(node.get("page_dir") or ""),
                label=f"published page directory for {content_id}",
            )
            if generation_id:
                expected_page_dir = safe_archive_path(
                    self.root,
                    Path("_blackboard/generations") / generation_id / "pages" / content_id,
                    label=f"generation page directory for {content_id}",
                )
                if page_dir != expected_page_dir:
                    raise ArchiveIntegrityError(
                        f"Published tree mixes metadata generations for {content_id}: {page_dir}"
                    )
            source = load_json_object(page_dir / "source.json", required=True)
            links = load_json_object(page_dir / "links.json", required=True)
            assert source is not None and links is not None
            if source.get("content_id") != content_id:
                raise ArchiveIntegrityError(f"Published page source identity mismatch: {page_dir}")
            if generation_id and source.get("course_id") != tree_course_id:
                raise ArchiveIntegrityError(f"Published page course identity mismatch: {page_dir}")
            if tree_site_origin and source.get("site_origin") != tree_site_origin:
                raise ArchiveIntegrityError(f"Published page site identity mismatch: {page_dir}")
            raw_attachments = links.get("attachments")
            if not isinstance(raw_attachments, list) or any(not isinstance(item, dict) for item in raw_attachments):
                raise ArchiveIntegrityError(f"Published page has invalid attachments: {page_dir}")
            attachments = [
                Attachment(
                    text=str(item.get("text") or ""),
                    href=str(item.get("href") or ""),
                    mime_type=str(item.get("mime_type")) if item.get("mime_type") else None,
                    source_kind=str(item.get("source_kind")) if item.get("source_kind") else None,
                    original_filename=str(item.get("original_filename")) if item.get("original_filename") else None,
                )
                for item in raw_attachments
            ]
            markdown_path = page_dir / "page.md"
            if not markdown_path.is_file():
                raise ArchiveIntegrityError(f"Published page markdown is missing: {markdown_path}")
            markdown = markdown_path.read_text(encoding="utf-8")
            body = _markdown_section(markdown, "Body")
            description = _markdown_section(markdown, "Description")
            expected_hash = str(source.get("page_text_sha256") or "")
            if body == "No visible body text extracted." and expected_hash == sha256_text(""):
                body = ""
            if not expected_hash or sha256_text(normalize_block(body)) != expected_hash:
                raise ArchiveIntegrityError(f"Published page text hash mismatch: {page_dir}")
            expected_description_hash = str(source.get("description_text_sha256") or "")
            if expected_description_hash and sha256_text(normalize_block(description)) != expected_description_hash:
                raise ArchiveIntegrityError(f"Published page description hash mismatch: {page_dir}")
            breadcrumbs = [str(value) for value in (source.get("breadcrumbs") or node.get("breadcrumbs") or [])]
            pages.append(Page(
                content_id=content_id,
                title=str(source.get("title") or node.get("title") or content_id),
                parent_title=str(source.get("parent_title")) if source.get("parent_title") else None,
                parent_id=str(source.get("parent_id")) if source.get("parent_id") else None,
                depth=max(0, len(breadcrumbs) - 1),
                breadcrumbs=breadcrumbs,
                url=str(source.get("fetched_url") or node.get("fetched_url") or ""),
                body_text=body,
                attachments=attachments,
                child_links=list(links.get("child_pages") or []),
                external_links=list(links.get("external_links") or []),
                unresolved_assets=list(links.get("unresolved_embedded_assets") or []),
                node_type_hint=str(source.get("node_type") or node.get("node_type") or "") or None,
                description_text=description,
                due_at=str(source.get("due_at")) if source.get("due_at") else None,
            ))
        return pages


class MetadataWriter:
    def __init__(
        self,
        state: ArchiveState,
        course_id: str,
        *,
        run_id: str | None = None,
        generation_id: str | None = None,
        site_config: SiteConfig,
    ) -> None:
        self.state = state
        self.course_id = course_id
        self.site = site_config
        self.run_id = safe_path_component(run_id or uuid.uuid4().hex, label="metadata generation id")
        self.generation_id = safe_path_component(
            generation_id or self.run_id,
            label="metadata generation id",
        )
        self.last_generation_id: str | None = None
        self.last_tree_payload: dict[str, Any] | None = None

    def write_page_artifacts(
        self,
        pages: list[Page],
        crawl_status: dict[str, Any],
        *,
        defer_publish: bool = False,
    ) -> tuple[int, int, bool]:
        new_pages = 0
        changed_pages = 0
        nodes: list[dict[str, Any]] = []
        safe_to_replace = bool(crawl_status.get("safe_to_replace"))

        published_root = safe_archive_path(self.state.root, Path("_blackboard/generations") / self.generation_id)
        staging_root = safe_archive_path(self.state.root, Path("_blackboard/generations") / f".{self.generation_id}.staging")
        if staging_root.exists() or published_root.exists():
            raise ArchiveIntegrityError(
                f"Metadata generation already exists and will not be overwritten: {self.generation_id}"
            )
        staging_root.mkdir(parents=True, exist_ok=False)

        for page in pages:
            text = normalize_block(page.body_text)
            page_hash = sha256_text(text)
            if page.content_id not in self.state.old_hashes:
                new_pages += 1
            elif self.state.old_hashes.get(page.content_id) != page_hash:
                changed_pages += 1

            content_id = safe_path_component(page.content_id, label="Blackboard content id")
            page_dir = published_root / "pages" / content_id
            staged_page_dir = staging_root / "pages" / content_id
            source = self._source_for(page, page_hash)
            links = {
                "attachments": [attachment.__dict__ for attachment in page.attachments],
                "child_pages": page.child_links,
                "external_links": page.external_links,
                "unresolved_embedded_assets": page.unresolved_assets,
            }
            staged_page_dir.mkdir(parents=True, exist_ok=False)
            atomic_write_text(staged_page_dir / "page.md", self._page_markdown(page, text))
            atomic_write_json(staged_page_dir / "links.json", links)
            atomic_write_json(staged_page_dir / "source.json", source)
            self._validate_staged_page(page, page_hash, staged_page_dir)
            nodes.append(self._node_for(page, page_hash, page_dir))

        nodes.sort(key=lambda node: (node.get("breadcrumbs") or [], node.get("title") or ""))
        tree_payload = {
            "schema_version": 2,
            "course_id": self.course_id,
            "site_origin": self.site.origin,
            "site_config": self.site.to_dict(),
            "run_id": self.run_id,
            "generation_id": self.generation_id,
            "generated_at": utc_now_iso(),
            "previous_snapshot": self.state.existing_tree.get("generation_id"),
            "generator": "scripts/blackboard_cli.py",
            "crawl": crawl_status,
            "nodes": nodes,
        }
        if not safe_to_replace:
            tree_payload["rejected_as_shallow"] = True
            tree_payload["preserved_course_tree"] = str(self.state.course_tree_path)
        atomic_write_json(staging_root / "course-tree.json", tree_payload)
        self.state.blackboard_root.mkdir(parents=True, exist_ok=True)
        os.replace(staging_root, published_root)
        self._fsync_directory(published_root.parent)
        self.last_generation_id = self.generation_id
        self.last_tree_payload = tree_payload
        if safe_to_replace:
            if not defer_publish:
                self.publish_generation(self.generation_id)
        return new_pages, changed_pages, safe_to_replace

    def publish_generation(self, generation_id: str) -> dict[str, Any]:
        """Atomically select a complete immutable generation as the course tree."""
        tree_payload = self.state.load_generation_tree(
            generation_id,
            expected_run_id=self.run_id,
        )
        # This replacement is the logical course commit marker. Downloads and
        # the attachment index are additive, so readers of the previous tree
        # remain valid until this pointer changes.
        atomic_write_json(self.state.course_tree_path, tree_payload)
        return tree_payload

    def _validate_staged_page(self, page: Page, page_hash: str, page_dir: Path) -> None:
        markdown = page_dir / "page.md"
        if not markdown.is_file() or not markdown.read_text(encoding="utf-8").startswith(f"# {page.title}\n"):
            raise ArchiveIntegrityError(f"Staged page markdown failed validation: {markdown}")
        source = load_json_object(page_dir / "source.json", required=True)
        links = load_json_object(page_dir / "links.json", required=True)
        assert source is not None and links is not None
        if source.get("content_id") != page.content_id or source.get("page_text_sha256") != page_hash:
            raise ArchiveIntegrityError(f"Staged page source identity failed validation: {page_dir}")
        expected_description_hash = str(source.get("description_text_sha256") or "")
        if expected_description_hash != sha256_text(normalize_block(page.description_text)):
            raise ArchiveIntegrityError(f"Staged page description identity failed validation: {page_dir}")
        attachments = links.get("attachments")
        if not isinstance(attachments, list) or len(attachments) != len(page.attachments):
            raise ArchiveIntegrityError(f"Staged page attachment inventory failed validation: {page_dir}")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _source_for(self, page: Page, page_hash: str) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "site_origin": self.site.origin,
            "content_id": page.content_id,
            "title": page.title,
            "node_type": page.node_type,
            "parent_id": page.parent_id,
            "parent_title": page.parent_title,
            "breadcrumbs": page.breadcrumbs,
            "normalized_bucket": page.bucket,
            "fetched_url": page.url,
            "captured_at": utc_now_iso(),
            "page_text_sha256": page_hash,
            "description_text_sha256": sha256_text(normalize_block(page.description_text)),
            "due_at": page.due_at,
            "attachment_count": len(page.attachments),
            "attachment_urls": [attachment.href for attachment in page.attachments if attachment.href],
            "child_page_count": len(page.child_links),
            "external_link_count": len(page.external_links),
            "unresolved_embedded_asset_count": len(page.unresolved_assets),
        }

    def _node_for(self, page: Page, page_hash: str, page_dir: Path) -> dict[str, Any]:
        source = self._source_for(page, page_hash)
        return {
            "id": page.content_id,
            "title": page.title,
            "node_type": page.node_type,
            "parent_id": page.parent_id,
            "parent_title": page.parent_title,
            "breadcrumbs": page.breadcrumbs,
            "normalized_bucket": page.bucket,
            "page_dir": str(page_dir.relative_to(self.state.root)),
            "fetched_url": page.url,
            "page_text_sha256": source["page_text_sha256"],
            "description_text_sha256": source["description_text_sha256"],
            "due_at": source["due_at"],
            "attachment_count": source["attachment_count"],
            "attachment_urls": source["attachment_urls"],
            "child_page_count": source["child_page_count"],
            "external_link_count": source["external_link_count"],
            "unresolved_embedded_asset_count": source["unresolved_embedded_asset_count"],
            "captured_at": source["captured_at"],
        }

    def _page_markdown(self, page: Page, text: str) -> str:
        lines = [
            f"# {page.title}",
            "",
            f"- Content ID: `{page.content_id}`",
            f"- Node Type: `{page.node_type}`",
            f"- Parent ID: `{page.parent_id or ''}`",
            f"- Parent Title: {page.parent_title or ''}",
            f"- Normalized Bucket: `{page.bucket}`",
            f"- Source URL: {page.url}",
            f"- Breadcrumbs: {' > '.join(page.breadcrumbs)}",
            f"- Unresolved Embedded Assets: `{len(page.unresolved_assets)}`",
        ]
        if page.description_text:
            lines.extend(["", "## Description", "", normalize_block(page.description_text)])
        lines.extend(["", "## Body", "", text or "No visible body text extracted."])
        metadata: list[str] = []
        if page.due_at:
            metadata.append(f"- Due ({self.site.display_timezone}): `{_local_due_display(page.due_at, self.site.display_timezone)}`")
            metadata.append(f"- Due (UTC source): `{page.due_at}`")
        if metadata:
            lines.extend(["", "## Blackboard Metadata", "", *metadata])
        lines.append("")
        return "\n".join(lines)


def _markdown_section(markdown: str, heading: str) -> str:
    """Read one generated ``##`` section without consuming later sections."""
    marker = f"\n## {heading}\n\n"
    if marker not in markdown:
        return ""
    section = markdown.split(marker, 1)[1]
    next_heading = section.find("\n## ")
    if next_heading >= 0:
        section = section[:next_heading]
    return section.rstrip()


def _local_due_display(value: str, timezone_name: str = "Australia/Sydney") -> str:
    """Render an API timestamp in the configured display timezone."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local = parsed.astimezone(ZoneInfo(timezone_name))
        return local.strftime("%Y-%m-%d %H:%M %Z")
    except (TypeError, ValueError):
        # Keep malformed upstream data visible rather than hiding it during
        # rendering; the raw value remains the source of truth in source.json.
        return value
