#!/usr/bin/env python3
from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .archive_io import (
    atomic_write_json,
    safe_artifact_name,
    utc_now_iso,
)
from .browser_backend import BrowserSession
from .safe_fetch import HttpFetcher, FetchError
from .site_config import SiteConfig
from .sync_models import Page
from .url_policy import is_blackboard_url, is_leganto_url


def leganto_list_id(url: str, *, site_config: SiteConfig) -> str | None:
    if not is_leganto_url(url, site_config=site_config):
        return None
    match = re.search(r"/leganto/nui/lists/(\d+)(?:/|$)", urlparse(url).path)
    return match.group(1) if match else None


def leganto_ris_export_target(list_url: str, *, site_config: SiteConfig) -> tuple[str, str] | None:
    list_id = leganto_list_id(list_url, site_config=site_config)
    if not list_id:
        return None
    parsed = urlparse(list_url)
    return list_id, f"{parsed.scheme}://{parsed.netloc}/leganto/rl/files/ris/list/{list_id}?"


def leganto_excel_export_url(list_url: str, *, site_config: SiteConfig) -> str | None:
    """Build Leganto's native Excel export URL for a reading list.

    The URL pattern mirrors the RIS export but lives under
    ``/rl/files/en/excel/list/{id}/true`` with query parameters controlling
    permalink/availability/notes/tags export. The HTTP fetcher downloads it
    directly with the Leganto session cookie — no DOM click needed.
    """
    list_id = leganto_list_id(list_url, site_config=site_config)
    if not list_id:
        return None
    parsed = urlparse(list_url)
    return (
        f"{parsed.scheme}://{parsed.netloc}/leganto/rl/files/en/excel/list/{list_id}/true"
        "?exportPermalinksType=pretty&exportAvailability=true"
        "&exportPublicNotes=true&exportTags=true&exportPermalinks=true&"
    )


def ris_entry_count(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return 0
    starts = len(re.findall(r"(?m)^TY  - ", text))
    ends = len(re.findall(r"(?m)^ER  -\s*$", text))
    return starts if starts > 0 and starts == ends else 0


@dataclass(frozen=True)
class ReadingListTool:
    course_root: Path
    course_id: str
    launch_url: str
    title: str = "Reading List"
    handle: str = "leganto_reading_list"


def iter_archived_reading_list_tools(
    root: Path,
    *,
    tree: dict[str, Any],
    pages: list[Page],
    site_config: SiteConfig,
) -> list[ReadingListTool]:
    site = site_config
    tools: dict[str, ReadingListTool] = {}
    course_root = root.resolve()
    course_id = str(tree.get("course_id") or "").strip()
    if not course_id:
        return []
    for page in pages:
        for item in page.external_links:
            handle = str(item.get("handle") or "")
            kind = str(item.get("kind") or "")
            href = str(item.get("href") or "")
            if (
                handle != "leganto_reading_list"
                or kind != "lti_launch"
                or not is_blackboard_url(href, prefixes=("/webapps/blackboard/execute/blti/launchPlacement",), site_config=site)
            ):
                continue
            tools.setdefault(href, ReadingListTool(
                course_root=course_root,
                course_id=course_id,
                launch_url=href,
                title=str(item.get("text") or "Reading List"),
                handle=handle,
            ))
    return list(tools.values())


def leganto_page_info_script() -> str:
    return r"""
(() => JSON.stringify({
  url: location.href,
  title: (document.querySelector('#page-title')?.textContent || document.title || '')
    .replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim(),
  sectionCount: document.querySelectorAll('lg-section h2').length,
  citationCount: document.querySelectorAll('div[id^="citation-"]').length
}))()
"""


def resolve_leganto_list(
    browser: BrowserSession,
    tool: ReadingListTool,
    *,
    wait_seconds: float,
    retry_count: int,
    site_config: SiteConfig,
) -> tuple[str, str, int] | None:
    browser.open(tool.launch_url)
    time.sleep(wait_seconds)
    for _ in range(max(1, retry_count)):
        info = browser.eval_json(leganto_page_info_script())
        current_url = str(info.get("url") or "")
        if leganto_list_id(current_url, site_config=site_config):
            return current_url, str(info.get("title") or tool.title), int(info.get("citationCount") or 0)
        time.sleep(wait_seconds)
    return None


def export_leganto_excel(
    fetcher: HttpFetcher,
    list_url: str,
    out_path: Path,
    *,
    site_config: SiteConfig,
) -> dict[str, Any]:
    site = site_config
    excel_url = leganto_excel_export_url(list_url, site_config=site)
    if not excel_url:
        return {"status": "failed", "reason": "leganto-excel-url-missing"}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_name(out_path.name + ".part")
    try:
        fetcher.download(excel_url, temporary, url_policy=lambda url: is_leganto_url(url, site_config=site))
    except FetchError as exc:
        temporary.unlink(missing_ok=True)
        return {"status": "failed", "reason": f"leganto-excel-download-failed: {exc}"}
    if temporary.stat().st_size <= 0:
        temporary.unlink(missing_ok=True)
        return {"status": "failed", "reason": "leganto-excel-empty"}
    from .resource_store import file_evidence
    try:
        file_evidence(temporary, out_path.name)
    except (RuntimeError, ValueError) as exc:
        temporary.unlink(missing_ok=True)
        return {"status": "failed", "reason": str(exc)}
    temporary.replace(out_path)
    return {"status": "downloaded", "source_path": str(out_path)}


def export_reading_list(
    browser: BrowserSession,
    fetcher: HttpFetcher,
    tool: ReadingListTool,
    *,
    wait_seconds: float,
    retry_count: int,
    formats: tuple[str, ...] = ("ris", "xlsx"),
    site_config: SiteConfig,
    refresh_credentials: Callable[[], None] | None = None,
) -> dict[str, Any]:
    site = site_config
    resolved = resolve_leganto_list(browser, tool, wait_seconds=wait_seconds, retry_count=retry_count, site_config=site)
    if not resolved:
        return {"course_id": tool.course_id, "status": "failed", "reason": "leganto-list-not-resolved"}
    list_url, list_title, visible_count = resolved
    # The LTI launch just established the Leganto session cookie. The caller
    # supplies a refresh hook so the RIS/Excel downloads carry it.
    if refresh_credentials is not None:
        refresh_credentials()
    export_target = leganto_ris_export_target(list_url, site_config=site)
    if not export_target:
        return {"course_id": tool.course_id, "status": "failed", "reason": "leganto-list-id-missing"}
    list_id, export_url = export_target

    code = tool.course_root.name.split("_", 1)[0]
    out_dir = tool.course_root / "_blackboard" / "reading-list"
    out_path = out_dir / safe_artifact_name(f"{code}-reading-list.ris", fallback="reading-list")
    manifest_path = out_dir / "manifest.json"
    base = {
        "course_id": tool.course_id,
        "title": list_title,
        "list_id": list_id,
        "list_url": list_url,
        "format": "RIS",
        "path": str(out_path),
    }

    if formats == ("xlsx",):
        excel_path = out_dir / safe_artifact_name(f"{code}-reading-list.xlsx", fallback="reading-list.xlsx")
        return export_leganto_excel(fetcher, list_url, excel_path, site_config=site)

    # The HTTP fetcher + Leganto cookie downloads RIS directly (no browser navigation).
    out_dir.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_name(out_path.name + ".part")
    temporary.unlink(missing_ok=True)
    try:
        fetcher.download(export_url, temporary, url_policy=lambda url: is_leganto_url(url, site_config=site))
    except FetchError as exc:
        temporary.unlink(missing_ok=True)
        return {**base, "status": "failed", "reason": f"leganto-ris-download-failed: {exc}"}

    entries = ris_entry_count(temporary)
    if entries <= 0:
        temporary.unlink(missing_ok=True)
        return {**base, "status": "failed", "reason": "leganto-ris-invalid"}

    if visible_count and entries < visible_count:
        candidate_path = out_path.with_name(out_path.stem + ".candidate" + out_path.suffix)
        temporary.replace(candidate_path)
        return {
            **base,
            "status": "ambiguous",
            "reason": "leganto-ris-below-visible-citation-count",
            "entry_count": entries,
            "visible_citation_count": visible_count,
            "candidate": str(candidate_path),
        }
    old_entries = ris_entry_count(out_path) if out_path.exists() else 0
    if old_entries and entries < old_entries:
        candidate_path = out_path.with_name(out_path.stem + ".candidate" + out_path.suffix)
        temporary.replace(candidate_path)
        return {
            **base,
            "status": "ambiguous",
            "reason": "leganto-ris-entry-count-regressed",
            "entry_count": entries,
            "previous_entry_count": old_entries,
            "candidate": str(candidate_path),
        }
    temporary.replace(out_path)
    excel_path = out_dir / safe_artifact_name(f"{code}-reading-list.xlsx", fallback="reading-list.xlsx")
    excel_result = export_leganto_excel(fetcher, list_url, excel_path, site_config=site) if "xlsx" in formats else {"status": "not-requested"}
    manifest = {
        **base,
        "captured_at": utc_now_iso(),
        "entry_count": entries,
        "visible_citation_count": visible_count,
        "mime_type": "application/x-research-info-systems",
        "size": out_path.stat().st_size,
        "excel_path": str(excel_path) if excel_result.get("status") == "downloaded" else None,
        "excel_status": excel_result.get("status"),
    }
    atomic_write_json(manifest_path, manifest)
    return {**manifest, "status": "downloaded" if excel_result.get("status") in {"downloaded", "not-requested"} else "partial", "excel": excel_result, "manifest": str(manifest_path)}
