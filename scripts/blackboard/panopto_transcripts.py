#!/usr/bin/env python3
from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .archive_io import (
    atomic_write_json,
    atomic_write_text,
    safe_artifact_name,
    utc_now_iso,
)
from .browser_backend import BrowserSession
from . import captions
from .sync_models import Page
from .site_config import SiteConfig
from .url_policy import is_blackboard_url, is_panopto_url


def captions_to_srt(captions_list: list[dict[str, Any]]) -> str:
    return captions.captions_to_srt(captions_list)


def captions_to_markdown(title: str, source_url: str, language: str, captions_list: list[dict[str, Any]]) -> str:
    return captions.captions_to_markdown(title, source_url, language, captions_list)


@dataclass(frozen=True)
class PanoptoLink:
    url: str
    course_root: Path
    page_id: str | None = None
    page_title: str | None = None
    link_title: str | None = None
    source_type: str = "page-link"

    @property
    def video_id(self) -> str:
        query = parse_qs(urlparse(self.url).query)
        return (query.get("id") or query.get("tid") or [""])[0]

    @property
    def is_tid(self) -> bool:
        return bool(parse_qs(urlparse(self.url).query).get("tid"))


def is_viewer_url(url: str, *, site_config: SiteConfig) -> bool:
    parsed = urlparse(url)
    if not is_panopto_url(url, site_config=site_config):
        return False
    query = parse_qs(parsed.query)
    if not (query.get("id") or query.get("tid")):
        return False
    return parsed.path in {"/Panopto/Pages/Viewer.aspx", "/Panopto/Pages/Embed.aspx"}


def canonical_viewer_url(url: str, *, site_config: SiteConfig) -> str:
    if not is_viewer_url(url, site_config=site_config):
        raise ValueError("Unsupported Panopto viewer URL")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    key = "id" if query.get("id") else "tid"
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode({key: query[key][0]}), ""))


def add_unique_link(links: dict[str, PanoptoLink], link: PanoptoLink, *, site_config: SiteConfig) -> None:
    try:
        canonical = canonical_viewer_url(link.url, site_config=site_config)
    except ValueError:
        return
    links.setdefault(canonical, replace(link, url=canonical))


def iter_panopto_links(
    root: Path,
    *,
    pages: list[Page],
    site_config: SiteConfig,
) -> list[PanoptoLink]:
    site = site_config
    links: dict[str, PanoptoLink] = {}
    for page in pages:
        for item in page.external_links + [vars(a) for a in page.attachments] + page.child_links:
            add_unique_link(links, PanoptoLink(
                item.get("href") or "", course_root=root.resolve(),
                page_id=page.content_id, page_title=page.title,
                link_title=item.get("text") or item.get("title"),
                source_type="archived-page-link",
            ), site_config=site)
    return list(links.values())


def archived_panopto_tools(
    pages: list[Page],
    *,
    site_config: SiteConfig,
) -> list[dict[str, Any]]:
    site = site_config
    tools: dict[str, dict[str, Any]] = {}
    for page in pages:
        for item in page.external_links:
            href = str(item.get("href") or "")
            handle = str(item.get("handle") or "").lower()
            kind = str(item.get("kind") or "")
            if (
                handle != "panopto"
                or kind != "lti_launch"
                or not is_blackboard_url(href, prefixes=("/webapps/blackboard/execute/blti/launchPlacement",), site_config=site)
            ):
                continue
            tools.setdefault(href, {
                "text": item.get("text") or "Panopto",
                "href": href,
                "placementId": item.get("placement_id") or "",
                "handle": item.get("handle") or "panopto",
                "source": "ultra-books-and-tools-archive",
            })
    return list(tools.values())


def blackboard_course_menu_script(course_id: str) -> str:
    return f"""
(() => {{
  const courseId = {json.dumps(course_id)};
  let rows = [];
  try {{
    const xhr = new XMLHttpRequest();
    const url = `/webapps/blackboard/content/courseMenu.jsp?course_id=${{encodeURIComponent(courseId)}}`;
    xhr.open('GET', url, false);
    xhr.send(null);
    const doc = new DOMParser().parseFromString(xhr.responseText || '', 'text/html');
    rows = [...doc.querySelectorAll('a')].map(a => {{
      const text = (a.textContent || '').replace(/\\s+/g, ' ').trim();
      const href = a.getAttribute('href') ? new URL(a.getAttribute('href'), location.origin).href : '';
      return {{ text, href, source: 'classic-course-menu' }};
    }});
  }} catch (_) {{}}
  const classicTools = rows.filter(item => {{
    const haystack = `${{item.text}} ${{item.href}}`.toLowerCase();
    return haystack.includes('panopto') ||
      haystack.includes('tool_id=_8082_1') ||
      haystack.includes('launchplacement') && haystack.includes('panopto');
  }});
  let ultraTools = [];
  try {{
    const xhr = new XMLHttpRequest();
    xhr.open('GET', `/learn/api/public/v1/lti/placements?courseId=${{encodeURIComponent(courseId)}}&type=Application`, false);
    xhr.send(null);
    const placements = xhr.status >= 200 && xhr.status < 300
      ? (JSON.parse(xhr.responseText || '{{}}').results || []) : [];
    ultraTools = placements.filter(item => {{
      const haystack = `${{item.name || ''}} ${{item.handle || ''}} ${{item.url || ''}}`.toLowerCase();
      return item.allowStudents !== false && haystack.includes('panopto');
    }}).map(item => {{
      const launch = new URL(item.launchLink || '', location.origin);
      launch.searchParams.set('course_id', courseId);
      launch.searchParams.set('from_ultra', 'true');
      return {{
        text: item.name || 'Panopto',
        href: launch.href,
        placementId: item.id || '',
        handle: item.handle || '',
        source: 'ultra-books-and-tools'
      }};
    }});
  }} catch (_) {{}}
  const seen = new Set();
  const panoptoTools = [...classicTools, ...ultraTools].filter(item => {{
    if (!item.href || seen.has(item.href)) return false;
    seen.add(item.href);
    return true;
  }});
  return JSON.stringify({{ courseId, panoptoTools }});
}})()
"""


def panopto_page_info_script() -> str:
    return """
(() => {
  const links = [];
  const seen = new Set();
  const push = (a) => {
    if (!a?.href) return;
    if (!/\\/Panopto\\/Pages\\/(Viewer|Embed)\\.aspx/.test(a.href)) return;
    const url = new URL(a.href, location.href);
    if (!url.searchParams.get('id') && !url.searchParams.get('tid')) return;
    const href = url.href;
    if (seen.has(href)) return;
    seen.add(href);
    links.push({ text: (a.textContent || '').replace(/\\s+/g, ' ').trim(), href });
  };
  for (const selector of [
    '#listViewContainer tbody > tr a.detail-title',
    '#detailsTable tbody > tr a.detail-title',
    '#thumbnailGrid > li a.detail-title',
    'a.detail-title',
    'a[href*="/Panopto/Pages/Viewer.aspx"]',
    'a[href*="/Panopto/Pages/Embed.aspx"]'
  ]) {
    document.querySelectorAll(selector).forEach(push);
  }
  const iframes = [...document.querySelectorAll('iframe')].map(f => f.src).filter(Boolean);
  return JSON.stringify({
    url: location.href,
    title: document.title || '',
    text: (document.body?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 1200),
    iframes,
    links
  });
})()
"""


def resolve_panopto_tool_links(
    browser: BrowserSession,
    root: Path,
    tool_url: str,
    *,
    wait_seconds: float,
    retry_count: int,
    site_config: SiteConfig,
) -> list[PanoptoLink]:
    site = site_config
    if not is_blackboard_url(tool_url, prefixes=("/webapps/blackboard/",), site_config=site):
        return []
    browser.open(tool_url)
    time.sleep(wait_seconds)
    info = browser.eval_json(panopto_page_info_script())

    # Blackboard wraps the LTI launch in a same-origin iframe. Navigating that
    # iframe URL as the top page lets Panopto complete the redirect and render
    # the sessions list where recording links are visible.
    if not is_panopto_url(str(info.get("url", "")), site_config=site):
        for iframe_src in info.get("iframes") or []:
            if is_blackboard_url(iframe_src, prefixes=("/webapps/blackboard/execute/blti/",), site_config=site) or is_panopto_url(iframe_src, site_config=site):
                browser.goto(iframe_src)
                time.sleep(wait_seconds)
                break

    final_info: dict[str, Any] = {}
    for _ in range(max(1, retry_count)):
        final_info = browser.eval_json(panopto_page_info_script())
        if final_info.get("links") or is_viewer_url(str(final_info.get("url") or ""), site_config=site):
            break
        time.sleep(wait_seconds)

    links: dict[str, PanoptoLink] = {}
    current_url = str(final_info.get("url") or "")
    if is_viewer_url(current_url, site_config=site):
        add_unique_link(links, PanoptoLink(
            current_url,
            course_root=root,
            link_title=final_info.get("title"),
            source_type="course-menu-current-viewer",
        ), site_config=site)
    for item in final_info.get("links") or []:
        add_unique_link(links, PanoptoLink(
            item.get("href") or "",
            course_root=root,
            link_title=item.get("text") or None,
            page_title=final_info.get("title") or None,
            source_type="course-menu-recordings",
        ), site_config=site)
    return list(links.values())


def discover_course_menu_panopto_links(
    root: Path,
    browser: BrowserSession,
    *,
    wait_seconds: float,
    retry_count: int,
    course_id: str,
    pages: list[Page],
    site_config: SiteConfig,
) -> list[PanoptoLink]:
    site = site_config
    links: dict[str, PanoptoLink] = {}
    tools = archived_panopto_tools(pages, site_config=site)
    if not tools:
        browser.open(f"{site.origin}/ultra/courses/{course_id}/outline")
        time.sleep(wait_seconds)
        menu = browser.eval_json(blackboard_course_menu_script(course_id))
        tools = menu.get("panoptoTools") or []
    for tool in tools:
        href = tool.get("href") or ""
        if not href:
            continue
        for link in resolve_panopto_tool_links(browser, root.resolve(), href, wait_seconds=wait_seconds, retry_count=retry_count, site_config=site):
            add_unique_link(links, link, site_config=site)
    return list(links.values())


def delivery_script(video_id: str, is_tid: bool) -> str:
    return f"""
(() => {{
  const videoId = {json.dumps(video_id)};
  const isTid = {str(is_tid).lower()};
  const makeBody = language => {{
    if (isTid) {{
      return language
        ? `tid=${{encodeURIComponent(videoId)}}&getCaptions=true&language=${{encodeURIComponent(language)}}&responseType=json`
        : `&tid=${{encodeURIComponent(videoId)}}&isLiveNotes=false&refreshAuthCookie=true&isActiveBroadcast=false&isEditing=false&isKollectiveAgentInstalled=false&isEmbed=false&responseType=json`;
    }}
    return language
      ? `deliveryId=${{encodeURIComponent(videoId)}}&getCaptions=true&language=${{encodeURIComponent(language)}}&responseType=json`
      : `deliveryId=${{encodeURIComponent(videoId)}}&isEmbed=true&responseType=json`;
  }};
  const post = body => {{
    const xhr = new XMLHttpRequest();
    xhr.open('POST', location.origin + '/Panopto/Pages/Viewer/DeliveryInfo.aspx', false);
    xhr.setRequestHeader('accept', 'application/json, text/javascript, */*; q=0.01');
    xhr.setRequestHeader('content-type', 'application/x-www-form-urlencoded;charset=UTF-8');
    xhr.send(body);
    if (xhr.status < 200 || xhr.status >= 300) {{
      return {{ ErrorCode: xhr.status, ErrorMessage: xhr.responseText || xhr.statusText }};
    }}
    return JSON.parse(xhr.responseText);
  }};
  const delivery = post(makeBody(null));
  const captions = [];
  for (const item of (delivery?.Delivery?.AvailableCaptions || [])) {{
    const language = item.Language;
    const content = post(makeBody(language));
    captions.push({{ language, content }});
  }}
  return JSON.stringify({{ ok: !delivery.ErrorCode, delivery, captions }});
}})()
"""


def safe_caption_items(captions_list: Any) -> list[dict[str, Any]]:
    return captions.safe_caption_items(captions_list)


def safe_delivery_summary(delivery: dict[str, Any], caption_languages: list[str] | None = None) -> dict[str, Any]:
    detail = delivery.get("Delivery") if isinstance(delivery.get("Delivery"), dict) else {}
    recording: dict[str, Any] = {}
    for source_key, output_key in (
        ("SessionName", "title"),
        ("Title", "alternate_title"),
        ("Duration", "duration_seconds"),
        ("SessionStartTime", "started_at"),
        ("Status", "status"),
        ("IsBroadcast", "is_broadcast"),
    ):
        value = detail.get(source_key)
        if isinstance(value, (str, int, float, bool)) and value not in ("", None):
            recording[output_key] = value
    languages = caption_languages or [
        str(item.get("Language") or "").strip()
        for item in (detail.get("AvailableCaptions") or [])
        if isinstance(item, dict) and item.get("Language")
    ]
    return {
        "schema_version": 1,
        "kind": "panopto-recording-summary",
        "recording": recording,
        "caption_languages": sorted(set(language for language in languages if language)),
    }


def save_transcripts(link: PanoptoLink, data: dict[str, Any], *, site_config: SiteConfig) -> dict[str, Any]:
    canonical_url = canonical_viewer_url(link.url, site_config=site_config)
    delivery = data.get("delivery") or {}
    if data.get("ok") is False:
        return {"url": canonical_url, "status": "failed", "reason": delivery.get("ErrorMessage") or "delivery-error"}
    title = delivery.get("Delivery", {}).get("SessionName") or delivery.get("Delivery", {}).get("Title") or link.link_title or link.page_title or link.video_id
    out_dir = link.course_root / "_blackboard" / "panopto" / safe_artifact_name(
        f"{title} {link.video_id}",
        fallback="panopto",
    )
    caption_sets = data.get("captions") or []
    out_dir.mkdir(parents=True, exist_ok=True)
    caption_languages = [str(item.get("language") or "") for item in caption_sets if isinstance(item, dict)]
    atomic_write_json(out_dir / "delivery.json", safe_delivery_summary(delivery, caption_languages))
    atomic_write_json(out_dir / "source.json", {
        "source_url": canonical_url,
        "video_id": link.video_id,
        "is_tid": link.is_tid,
        "course_root": str(link.course_root),
        "blackboard_page_id": link.page_id,
        "blackboard_page_title": link.page_title,
        "link_title": link.link_title,
        "source_type": link.source_type,
        "captured_at": utc_now_iso(),
    })
    written: list[str] = []
    for item in caption_sets:
        language = safe_artifact_name(str(item.get("language") or "unknown"), fallback="unknown")
        captions = safe_caption_items(item.get("content") or [])
        if not captions:
            continue
        srt_path = out_dir / f"captions_{language}.srt"
        json_path = out_dir / f"captions_{language}.json"
        md_path = out_dir / f"transcript_{language}.md"
        atomic_write_text(srt_path, captions_to_srt(captions))
        atomic_write_json(json_path, captions)
        atomic_write_text(md_path, captions_to_markdown(str(title), canonical_url, language, captions))
        written.extend([str(srt_path), str(json_path), str(md_path)])
    return {"url": canonical_url, "status": "downloaded" if written else "no-captions", "out_dir": str(out_dir), "files": written}
