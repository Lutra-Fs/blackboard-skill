#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .archive_io import (
    ArchiveIntegrityError,
    atomic_write_json,
    safe_archive_path,
    safe_artifact_name,
    utc_now_iso,
)
from .content_rules import bucket_for_hierarchy, sha256_file
from .runtime import CommandRunner
from .safe_fetch import HttpFetcher, FetchError
from .site_config import SiteConfig
from .url_policy import (
    is_blackboard_url,
    is_leganto_url,
    is_panopto_url,
    is_safe_public_https_url,
    is_trusted_attachment_source_url,
    is_video_transcript_url,
)

from .sync_models import Page

# Document extensions captured by this stage. Anything else in external_links is
# left as a plain web link (org sites, search portals) — link-only is correct.
DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".epub")

# A fixed browser User-Agent. Some CMS-driven sites (Squiz/Plone) return 406 for
# minimal default User-Agents; this never carries cookies or session state.
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# MIME types accepted as genuine documents. An HTML response means the URL hit a
# login wall, 404 page, or redirect-to-home — recorded as failed, never stored.
ACCEPTED_MIME_PREFIXES = (
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument",
    "application/epub",
)


@dataclass(frozen=True)
class ExternalDoc:
    url: str
    course_root: Path
    page_id: str | None = None
    page_title: str | None = None
    parent_title: str | None = None
    breadcrumbs: list[str] | None = None
    link_text: str | None = None
    normalized_bucket: str | None = None
    source_type: str = "page-link"


def is_external_document_url(url: str, *, site_config: SiteConfig) -> bool:
    """An HTTPS document URL on a host no other stage handles."""
    if not is_safe_public_https_url(url):
        return False
    # Exclude hosts owned by other stages / internal Blackboard.
    if (
        is_trusted_attachment_source_url(url, site_config=site_config)
        or is_blackboard_url(url, site_config=site_config)
        or is_panopto_url(url, site_config=site_config)
        or is_video_transcript_url(url)
        or is_leganto_url(url, site_config=site_config)
    ):
        return False
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in DOCUMENT_EXTENSIONS)


def filename_from_url(url: str, link_text: str | None) -> str:
    """Derive a filename from the URL path, falling back to the link text."""
    path = urlparse(url).path
    name = unquote(path.rsplit("/", 1)[-1]) if path else ""
    name = safe_artifact_name(name, fallback="")
    if name and any(name.lower().endswith(ext) for ext in DOCUMENT_EXTENSIONS):
        return name
    if link_text:
        stem = safe_artifact_name(link_text, fallback="")
        if stem:
            return f"{stem}.pdf"
    return name or "document.pdf"


def bucket_for_doc(doc: ExternalDoc) -> str:
    """Reuse the published normalized_bucket when available, else infer."""
    if doc.normalized_bucket:
        return doc.normalized_bucket
    return bucket_for_hierarchy(
        doc.page_title or "",
        doc.parent_title,
        list(doc.breadcrumbs or []),
    )


def _node_lookup(tree: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(node.get("id") or ""): node for node in tree.get("nodes", []) if isinstance(node, dict)}


def _bucket_priority(bucket: str) -> int:
    """Assessment > earliest week > admin. Lower week number = higher priority
    because a cross-referenced reading is most canonically the week it first
    appears in."""
    text = str(bucket).casefold()
    import re
    if "assessment" in text:
        return 100
    week = re.search(r"(?:^|/)(\d+)_week", text)
    if week:
        return 50 - int(week.group(1))  # week 2 (48) beats week 10 (40)
    if "admin" in text:
        return 0
    return 10


def iter_external_documents(
    root: Path,
    *,
    tree: dict[str, Any],
    pages: list[Page],
    site_config: SiteConfig,
) -> list[ExternalDoc]:
    site = site_config
    docs: dict[str, ExternalDoc] = {}
    nodes = _node_lookup(tree)
    for page in pages:
        node = nodes.get(page.content_id, {})
        for item in page.external_links + [vars(a) for a in page.attachments] + page.child_links:
            href = item.get("href") or ""
            if not is_external_document_url(href, site_config=site):
                continue
            candidate = ExternalDoc(
                href, course_root=root.resolve(), page_id=page.content_id,
                page_title=node.get("title", page.title),
                parent_title=node.get("parent_title", page.parent_title),
                breadcrumbs=node.get("breadcrumbs", page.breadcrumbs),
                normalized_bucket=node.get("normalized_bucket", page.bucket),
                link_text=item.get("text") or item.get("title"),
                source_type="archived-page-link",
            )
            existing = docs.get(href)
            # Keep the highest-priority bucket (assessment > earliest week).
            if existing is None or _bucket_priority(candidate.normalized_bucket or "") > _bucket_priority(existing.normalized_bucket or ""):
                docs[href] = candidate
    return list(docs.values())


def _mime_for(runner: CommandRunner, path: Path) -> str:
    result = runner.run(["file", "-b", "--mime-type", str(path)], check=False)
    return result.stdout.strip() or "application/octet-stream"


def _is_accepted_document_mime(mime: str) -> bool:
    return any(mime.startswith(prefix) for prefix in ACCEPTED_MIME_PREFIXES)


def download_document(
    doc: ExternalDoc,
    *,
    runner: CommandRunner,
    dry_run: bool = False,
) -> dict[str, Any]:
    bucket = bucket_for_doc(doc)
    filename = filename_from_url(doc.url, doc.link_text)
    try:
        relative_dest = Path(bucket) / filename
        absolute_dest = safe_archive_path(doc.course_root, relative_dest, label="external document destination")
    except ArchiveIntegrityError as exc:
        return {"url": doc.url, "status": "failed", "reason": f"unsafe-destination: {exc}"}
    meta_dir = doc.course_root / "_blackboard" / "external-docs"
    meta_name = safe_artifact_name(f"{filename} {sha256_text_short(doc.url)}", fallback="external-doc")
    meta_path = meta_dir / f"{meta_name}.json"
    if dry_run:
        return {
            "url": doc.url,
            "status": "planned",
            "bucket": bucket,
            "filename": filename,
            "destination": str(absolute_dest),
        }
    fetcher = HttpFetcher()
    working = absolute_dest.with_name(absolute_dest.name + ".part")
    working.unlink(missing_ok=True)
    try:
        try:
            fetcher.download(
                doc.url,
                working,
                headers={"User-Agent": BROWSER_UA, "Accept": "application/pdf,*/*"},
            )
        except FetchError as exc:
            return {"url": doc.url, "status": "failed", "reason": f"fetch-failed: {exc}"}
        if not working.exists() or working.stat().st_size <= 0:
            return {"url": doc.url, "status": "failed", "reason": "empty-response"}
        mime = _mime_for(runner, working)
        if not _is_accepted_document_mime(mime):
            working.unlink(missing_ok=True)
            return {"url": doc.url, "status": "failed", "reason": f"non-document-mime: {mime}"}
        # Dedup against an existing identical file anywhere in the course root.
        digest = sha256_file(working)
        absolute_dest.parent.mkdir(parents=True, exist_ok=True)
        if absolute_dest.exists() and sha256_file(absolute_dest) == digest:
            working.unlink(missing_ok=True)
            _write_source_meta(meta_path, doc, absolute_dest, digest, bucket, mime)
            return {"url": doc.url, "status": "skipped", "reason": "same-content-existing", "destination": str(absolute_dest)}
        working.replace(absolute_dest)
        _write_source_meta(meta_path, doc, absolute_dest, digest, bucket, mime)
        return {
            "url": doc.url,
            "status": "downloaded",
            "destination": str(absolute_dest),
            "files": [str(absolute_dest)],
            "sha256": digest,
            "mime": mime,
            "bucket": bucket,
        }
    finally:
        if working.exists() and working.name.endswith(".part"):
            working.unlink(missing_ok=True)


def sha256_text_short(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _write_source_meta(
    meta_path: Path,
    doc: ExternalDoc,
    dest: Path,
    digest: str,
    bucket: str,
    mime: str,
) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(meta_path, {
        "schema_version": 1,
        "kind": "external-document-source",
        "source_url": doc.url,
        "destination": str(dest),
        "sha256": digest,
        "mime_type": mime,
        "bucket": bucket,
        "filename": dest.name,
        "course_root": str(doc.course_root),
        "blackboard_page_id": doc.page_id,
        "blackboard_page_title": doc.page_title,
        "link_title": doc.link_text,
        "source_type": doc.source_type,
        "captured_at": utc_now_iso(),
    })
