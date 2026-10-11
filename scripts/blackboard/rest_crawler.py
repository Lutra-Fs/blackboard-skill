"""REST API course crawler for Blackboard Ultra.

The sole content source for the crawl stage. Enumerates the course content tree
through the Blackboard REST API (``/learn/api/public/v1/courses/{courseId}/contents``
with ``?recursive=true``) in a single call, plus announcements, calendar items,
LTI placements, and per-file attachments via their dedicated endpoints.

Authentication reuses the authenticated browser session: every REST call goes
through :class:`safe_fetch.HttpFetcher` with a :class:`cookie_store.CookieStore`
backed by that session, so the full Blackboard cookie set (including the
``HttpOnly`` ``BbRouter``) authorises each request. No Bearer token or
admin integration is required.

The crawler returns :class:`sync_models.Page` objects using Blackboard's native
``_NNNNN_1`` content ids.
"""

from __future__ import annotations

import os
import time
from typing import Any

from .bbml import (
    classify_content_handler,
    external_links_from_body,
    parse_body,
)
from .content_rules import normalize_inline
from .browser_backend import SessionRequired
from .safe_fetch import FetchError
from .sync_models import Attachment, Page
from .site_config import SiteConfig
from .ultra_content import build_lti_launch_url


# Fixed Ultra route shells. These are UI navigation entries, not content nodes,
# so the REST contents endpoint does not return them. We synthesise route Pages
# so downstream consumers find the expected top-level navigation nodes.
# The (route, path, content_id_suffix) tuples mirror the route ids the archive
# expects (ultra-outline, ultra-calendar, ...).
_ROUTE_SHELLS: tuple[tuple[str, str, str], ...] = (
    ("outline", "outline", "ultra-outline"),
    ("calendar", "calendar", "ultra-calendar"),
    ("announcements", "announcements", "ultra-announcements"),
    ("discussions", "outline/discussionBoard", "ultra-discussions"),
    ("booksAndTools", "outline/booksAndTools", "ultra-books-and-tools"),
)

# Polite delay between REST calls to respect Blackboard per-developer-group
# quotas. The crawl is session-bound and short, but bounding the rate avoids
# bursts when a course has hundreds of content nodes. Overridable via env var
# for testing (BB_REST_DELAY=0 eliminates it in unit tests).
_REST_CALL_DELAY_SECONDS = float(os.environ.get("BB_REST_DELAY", "0.1"))


class RestCrawler:
    """Enumerate a Blackboard Ultra course tree via the REST contents API.

    All REST calls go through :class:`safe_fetch.HttpFetcher` with a
    :class:`cookie_store.CookieStore`, so the full Blackboard cookie
    set (including the ``HttpOnly`` ``BbRouter``) authorises each request
    without routing through browser navigation or in-page XHR.
    """

    def __init__(self, fetcher: Any, course_id: str, *, site_config: SiteConfig) -> None:
        self.fetcher = fetcher
        self.course_id = course_id
        self.site = site_config
        self.rest_status: dict[str, Any] = {
            "attempted": False,
            "pages": 0,
            "api_calls": 0,
            "errors": [],
        }

    def crawl(self) -> list[Page]:
        """Return the full course content tree as ``Page`` objects.

        Performs a breadth-first traversal of ``contents`` → ``children``,
        synthesises route-shell pages. Authentication interruptions propagate; a per-node failure is
        recorded in ``rest_status.errors`` and the node is skipped, so one bad
        node can never abort the whole crawl.
        """
        self.rest_status["attempted"] = True
        pages: list[Page] = []
        errors: list[dict[str, Any]] = []

        if self.site.ultra_supported:
            # Route shells first, so they appear at the top of the merged tree.
            pages.extend(self._route_shell_pages())

            # Route content (announcements, calendar) via dedicated endpoints.
            # These are not content-tree nodes; each item becomes a child Page
            # under its route shell, with a clean body from the API.
            try:
                pages.extend(self._announcement_pages(errors))
            except SessionRequired:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                errors.append({"phase": "announcements", "error": str(exc)})
            try:
                pages.extend(self._calendar_pages(errors))
            except SessionRequired:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                errors.append({"phase": "calendar", "error": str(exc)})
            try:
                pages.extend(self._books_and_tools_pages(errors))
            except SessionRequired:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                errors.append({"phase": "books_and_tools", "error": str(exc)})

        # BFS the content tree.
        try:
            self._crawl_contents_recursive(pages, errors)
        except SessionRequired:
            raise
        except Exception as exc:  # pragma: no cover - defensive, see errors list
            errors.append({"phase": "bfs", "error": str(exc)})

        # Discussion boards are discovered from content-tree courselink/forumlink
        # nodes (after the BFS), not from a standalone endpoint. The forum list
        # endpoint returns 403 for students, so forum IDs must be extracted from
        # the contentDetailUrl of forumlink nodes reached via courselink targets.
        if self.site.ultra_supported:
            try:
                pages.extend(self._discussion_pages(errors))
            except SessionRequired:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                errors.append({"phase": "discussions", "error": str(exc)})

        self.rest_status["pages"] = len(pages)
        self.rest_status["api_calls"] = int(self.rest_status.get("api_calls", 0))
        self.rest_status["errors"] = errors
        return pages

    def _route_shell_pages(self) -> list[Page]:
        """Synthesise top-level route pages that mirror the DOM ``route_id`` ids."""
        if not self.site.ultra_supported:
            return []
        pages: list[Page] = []
        for route_name, path, content_id in _ROUTE_SHELLS:
            url = f"{self.site.origin}/ultra/courses/{self.course_id}/{path}"
            pages.append(
                Page(
                    content_id=content_id,
                    title=_route_title(route_name),
                    parent_title=None,
                    parent_id=None,
                    depth=0,
                    breadcrumbs=[_route_title(route_name)],
                    url=url,
                    body_text="",
                    node_type_hint="route",
                )
            )
        return pages

    def _announcement_pages(
        self, errors: list[dict[str, Any]]
    ) -> list[Page]:
        """Fetch course announcements via the announcements REST endpoint.

        Each announcement becomes a child Page under the ``ultra-announcements``
        route shell. The API returns a clean HTML body (no page-chrome noise),
        which is far better than scraping the announcements route DOM.
        """
        path = f"/learn/api/public/v1/courses/{self.course_id}/announcements"
        results = self._fetch_collection(path, phase="announcements", errors=errors)
        pages: list[Page] = []
        route_title = _route_title("announcements")
        for item in results:
            ann_id = str(item.get("id") or "").strip()
            if not ann_id:
                continue
            title = str(item.get("title") or ann_id).strip()
            body_html = item.get("body") if isinstance(item.get("body"), str) else ""
            body_text, attachments = parse_body(body_html, site_config=self.site)
            created = str(item.get("created") or "").strip()
            display_title = f"{title} ({created[:10]})" if created else title
            pages.append(
                Page(
                    content_id=ann_id,
                    title=display_title,
                    parent_title=route_title,
                    parent_id="ultra-announcements",
                    depth=1,
                    breadcrumbs=[route_title, display_title],
                    url=f"{self.site.origin}/ultra/courses/{self.course_id}/announcements",
                    body_text=body_text,
                    attachments=attachments,
                    node_type_hint="announcement",
                )
            )
        return pages

    def _calendar_pages(
        self, errors: list[dict[str, Any]]
    ) -> list[Page]:
        """Fetch course calendar items via the calendars REST endpoint.

        Each item becomes a child Page under the ``ultra-calendar`` route shell.
        """
        path = f"/learn/api/public/v1/calendars/items?courseId={self.course_id}"
        results = self._fetch_collection(path, phase="calendar", errors=errors)
        pages: list[Page] = []
        route_title = _route_title("calendar")
        for item in results:
            cal_id = str(item.get("id") or "").strip()
            if not cal_id:
                continue
            title = str(item.get("title") or cal_id).strip()
            cal_type = str(item.get("calendarType") or "").strip()
            start = str(item.get("start") or "").strip()
            end = str(item.get("end") or "").strip()
            body_text = _calendar_body_text(title, cal_type, start, end)
            display_title = f"{title} ({start[:10]})" if start else title
            pages.append(
                Page(
                    content_id=cal_id,
                    title=display_title,
                    parent_title=route_title,
                    parent_id="ultra-calendar",
                    depth=1,
                    breadcrumbs=[route_title, display_title],
                    url=f"{self.site.origin}/ultra/courses/{self.course_id}/calendar",
                    body_text=body_text,
                    node_type_hint="calendar",
                )
            )
        return pages

    def _discussion_pages(
        self, errors: list[dict[str, Any]]
    ) -> list[Page]:
        """Discover and capture discussion forums and messages.

        Ultra exposes discussion forums under the ``INTERACTIVE`` content node —
        a virtual container whose children are ``resource/x-bb-forumlink`` nodes.
        Each forumlink's ``contentDetailUrl`` contains the internal forum ID:

            /learn/api/v1/courses/{cid}/discussionboards/{confId}/forums/{forumId}

        With the forum ID, messages and replies are fetched from the internal
        ``/learn/api/v1/`` API (cookie-only, no XSRF header needed). A message
        body is ``{rawText, displayText}``; replies carry ``givenName``/``familyName``.
        """
        route_title = _route_title("discussions")
        pages: list[Page] = []

        # 1. Discover forums from INTERACTIVE children (one call, forum ID + title).
        interactive = self._fetch_collection(
            f"/learn/api/v1/courses/{self.course_id}/contents/INTERACTIVE/children",
            phase="discussion-forums", errors=errors, optional_404=True,
        )
        for node in interactive:
            detail_url = str(node.get("contentDetailUrl") or "")
            if "/forums/" not in detail_url:
                continue
            forum_id = detail_url.split("/forums/")[1].split("/")[0].split("?")[0]
            if not forum_id:
                continue
            forum_title = str(node.get("title") or forum_id).strip()
            forum_page_id = f"discussion-forum-{forum_id}"
            pages.append(
                Page(
                    content_id=forum_page_id,
                    title=forum_title,
                    parent_title=route_title,
                    parent_id="ultra-discussions",
                    depth=1,
                    breadcrumbs=[route_title, forum_title],
                    url=f"{self.site.origin}/ultra/courses/{self.course_id}/engagement/discussion",
                    body_text="",
                    node_type_hint="discussion_forum",
                )
            )
            # 2. Fetch messages for this forum.
            messages = self._fetch_collection(
                f"/learn/api/v1/courses/{self.course_id}/discussionboards/default/forums/{forum_id}/messages",
                phase=f"discussion-messages-{forum_id}", errors=errors, optional_404=True,
            )
            for message in messages:
                self._append_message_page(
                    pages, message, route_title, forum_title,
                    forum_page_id, forum_id, errors, depth=2,
                )
        return pages

    def _append_message_page(
        self,
        pages: list[Page],
        message: dict[str, Any],
        route_title: str,
        parent_title: str,
        parent_page_id: str,
        forum_id: str,
        errors: list[dict[str, Any]],
        depth: int,
        breadcrumbs_prefix: list[str] | None = None,
    ) -> None:
        """Build a Page for one discussion message and recurse into its replies."""
        msg_id = str(message.get("id") or "").strip()
        if not msg_id:
            return
        msg_subject = str(message.get("subject") or "(no subject)").strip()
        raw_body = message.get("body")
        msg_body_html = ""
        if isinstance(raw_body, dict):
            msg_body_html = str(raw_body.get("rawText") or raw_body.get("displayText") or "")
        elif isinstance(raw_body, str):
            msg_body_html = raw_body
        msg_body_text, msg_attachments = parse_body(msg_body_html, site_config=self.site)
        # Author: replies carry givenName/familyName; top-level posts carry userId.
        given = str(message.get("givenName") or "").strip()
        family = str(message.get("familyName") or "").strip()
        author = f"{given} {family}".strip() or str(message.get("userId") or "").strip()
        created = str(message.get("postDate") or message.get("createdDate") or "").strip()
        meta_parts = [
            part for part in (
                f"Author: {author}" if author else "",
                f"Posted: {created[:10]}" if created else "",
            ) if part
        ]
        full_body = ("\n".join(meta_parts) + "\n\n" + msg_body_text).strip() if meta_parts else msg_body_text
        msg_display = f"{msg_subject} ({created[:10]})" if created else msg_subject
        crumbs = (breadcrumbs_prefix or [route_title, parent_title]) + [msg_display]
        msg_page_id = f"{parent_page_id}-{msg_id}"
        pages.append(
            Page(
                content_id=msg_page_id,
                title=msg_display,
                parent_title=parent_title,
                parent_id=parent_page_id,
                depth=depth,
                breadcrumbs=crumbs,
                url=f"{self.site.origin}/ultra/courses/{self.course_id}/engagement/discussion",
                body_text=full_body,
                attachments=msg_attachments,
                node_type_hint="discussion_message",
            )
        )
        # Recurse into replies if the message has children.
        status = message.get("messageStatus")
        num_children = 0
        if isinstance(status, dict):
            try:
                num_children = int(status.get("numberOfChildren") or 0)
            except (TypeError, ValueError):
                num_children = 0
        if num_children > 0:
            replies = self._fetch_collection(
                f"/learn/api/v1/courses/{self.course_id}/discussionboards/default/forums/{forum_id}/messages/{msg_id}/replies",
                phase=f"discussion-replies-{forum_id}-{msg_id}", errors=errors, optional_404=True,
            )
            for reply in replies:
                self._append_message_page(
                    pages, reply, route_title, msg_display,
                    msg_page_id, forum_id, errors,
                    depth=depth + 1, breadcrumbs_prefix=crumbs,
                )

    def _books_and_tools_pages(
        self, errors: list[dict[str, Any]]
    ) -> list[Page]:
        """Fetch course Books & Tools via the LTI placements REST endpoint.

        Replaces the dedicated ``booksAndTools`` DOM browser session. Calls
        ``/learn/api/public/v1/lti/placements?courseId=...&type=Application``
        (the same endpoint the DOM path read via ``network_body``) and emits
        one ``lti_tool`` Page per student-visible placement. Each Page carries
        the ``external_links`` block that downstream Panopto/Reading List
        integrations select on (``handle``/``kind="lti_launch"``/launch URL).

        The DOM Books & Tools capture is fragile in practice (tool cards are
        lazy-loaded behind an LTI storage iframe and the tab can auto-redirect
        into a launched tool); this REST path is both more complete and more
        stable.
        """
        path = (
            f"/learn/api/public/v1/lti/placements"
            f"?courseId={self.course_id}&type=Application"
        )
        results = self._fetch_collection(path, phase="books_and_tools", errors=errors)
        pages: list[Page] = []
        route_title = _route_title("booksAndTools")
        for placement in results:
            # Student-visibility gate: keep only placements Blackboard would
            # surface to this user. ``allowStudents`` is the REST equivalent
            # of the UI visibility filter.
            if placement.get("allowStudents") is not True:
                continue
            availability = placement.get("availability")
            if isinstance(availability, dict) and str(availability.get("available") or "").lower() != "yes":
                continue
            placement_id = normalize_inline(str(placement.get("id") or ""))
            handle = normalize_inline(str(placement.get("handle") or ""))
            launch_link = str(placement.get("launchLink") or "")
            launch_url = build_lti_launch_url(launch_link, self.course_id, site_config=self.site)
            if not handle or launch_url is None:
                continue
            content_id = f"ultra-lti-{placement_id}" if placement_id else f"ultra-lti-{handle}"
            title = normalize_inline(str(placement.get("name") or handle)) or handle
            description = normalize_inline(str(placement.get("description") or ""))
            provider_url = str(placement.get("url") or "").strip()
            external_links: list[dict[str, Any]] = [{
                "text": title,
                "href": launch_url,
                "kind": "lti_launch",
                "handle": handle,
                "placement_id": placement_id,
                "launch_in_new_window": bool(placement.get("launchInNewWindow")),
            }]
            if provider_url:
                external_links.append({
                    "text": f"{title} provider",
                    "href": provider_url,
                    "kind": "lti_provider",
                    "handle": handle,
                    "placement_id": placement_id,
                })
            pages.append(
                Page(
                    content_id=content_id,
                    title=title,
                    parent_title=route_title,
                    parent_id="ultra-books-and-tools",
                    depth=1,
                    breadcrumbs=[route_title, title],
                    url=launch_url,
                    body_text=description or title,
                    external_links=external_links,
                    node_type_hint="lti_tool",
                )
            )
        return pages

    def _crawl_contents_recursive(
        self,
        pages: list[Page],
        errors: list[dict[str, Any]],
    ) -> None:
        """Fetch the entire content tree via paginated recursive API calls.

        The ``?recursive=true`` endpoint returns up to 200 nodes per page with a
        ``paging.nextPage`` cursor. This method follows all pages until exhausted,
        then rebuilds breadcrumbs/depth from the ``parentId`` chain.
        ``resource/x-bb-file`` nodes have an empty body; their file attachments
        are fetched in a batch pass from the ``/attachments`` endpoint so the
        pages carry downloadable URLs without needing DOM hydration.
        """
        results = self._fetch_collection(
            f"/learn/api/public/v1/courses/{self.course_id}/contents?recursive=true",
            phase="contents-recursive",
            errors=errors,
        )
        if not results:
            return

        id_to_node: dict[str, dict[str, Any]] = {
            str(node.get("id")): node
            for node in results
            if node.get("id")
        }

        # Batch-fetch file attachments for x-bb-file nodes (empty body, file
        # metadata only available via the attachments endpoint). One call per
        # file node — typically a handful, not hundreds. The public attachments
        # endpoint returns HTTP 400 for Ultra document nodes on this Blackboard
        # instance, so it is intentionally not queried for other handlers; the
        # real filename for document attachments is recovered from the CDN's
        # Content-Disposition header at download time instead.
        file_attachments: dict[str, list[Attachment]] = {}
        file_node_ids = [
            nid
            for nid, node in id_to_node.items()
            if _handler_id(node) == "resource/x-bb-file"
        ]
        for nid in file_node_ids:
            try:
                atts = self._fetch_file_attachments(nid, errors)
                if atts:
                    file_attachments[nid] = atts
            except SessionRequired:
                raise
            except Exception as exc:
                errors.append({"phase": "file-attachments", "content_id": nid, "error": str(exc)})

        # Rebuild breadcrumbs/depth from parentId chain.
        id_to_title = {
            nid: str(node.get("title") or nid).strip()
            for nid, node in id_to_node.items()
        }
        breadcrumb_cache: dict[str, list[str]] = {}

        def breadcrumbs_for(nid: str, seen: set[str] | None = None) -> tuple[list[str], int, str]:
            seen = seen or set()
            if nid in breadcrumb_cache:
                cached = breadcrumb_cache[nid]
                return cached, len(cached) - 1, ""
            if nid in seen:
                return [], 0, ""  # cycle guard
            seen.add(nid)
            node = id_to_node.get(nid)
            if not node:
                return [], 0, ""
            parent_id = node.get("parentId")
            parent_title = ""
            if parent_id and parent_id in id_to_node:
                parent_crumbs, _, pt = breadcrumbs_for(parent_id, seen)
                crumbs = [*parent_crumbs, id_to_title.get(nid, nid)]
                parent_title = id_to_title.get(parent_id, pt)
            else:
                crumbs = [id_to_title.get(nid, nid)]
                if parent_id:
                    parent_title = str(parent_id)
            breadcrumb_cache[nid] = crumbs
            return crumbs, len(crumbs) - 1, parent_title

        for node in results:
            content_id = str(node.get("id") or "")
            if not content_id:
                continue
            title = id_to_title.get(content_id, content_id)
            crumbs, depth, parent_title = breadcrumbs_for(content_id)
            parent_id = node.get("parentId") or None
            handler_id = _handler_id(node)
            page = self._page_from_content(
                node=node,
                content_id=content_id,
                title=title,
                parent_id=parent_id,
                parent_title=parent_title,
                breadcrumbs=crumbs,
                depth=depth,
                handler_id=handler_id,
                extra_attachments=file_attachments.get(content_id),
            )
            if page is not None:
                pages.append(page)

    def _fetch_file_attachments(
        self, content_id: str, errors: list[dict[str, Any]]
    ) -> list[Attachment]:
        """Fetch the ``/contents/{id}/attachments`` list for a file node.

        Returns ``Attachment`` objects with the standard Blackboard download
        URL pattern so the downstream downloader can fetch them directly
        without DOM hydration. Collection fetch (pagination + envelope
        validation) is delegated to :meth:`_fetch_collection`; per-page errors
        are recorded in ``errors`` rather than raised.
        """
        path = f"/learn/api/public/v1/courses/{self.course_id}/contents/{content_id}/attachments"
        results = self._fetch_collection(
            path, phase=f"file-attachments-{content_id}", errors=errors
        )
        attachments: list[Attachment] = []
        for item in results:
            att_id = str(item.get("id") or "")
            file_name = str(item.get("fileName") or "").strip()
            if not att_id or not file_name:
                continue
            download_url = (
                f"{self.site.origin}/learn/api/public/v1/courses/{self.course_id}"
                f"/contents/{content_id}/attachments/{att_id}/download"
            )
            attachments.append(
                Attachment(
                    text=file_name,
                    href=download_url,
                    mime_type=str(item.get("mimeType") or ""),
                    source_kind="rest-file-attachment",
                    original_filename=file_name,
                )
            )
        return attachments

    def _rest_get(self, path: str) -> Any:
        """Issue an authenticated GET to a Blackboard REST path.

        The :class:`safe_fetch.HttpFetcher` carries the full Blackboard cookie
        set (including the ``HttpOnly`` ``BbRouter`` that ``document.cookie``
        cannot see) via the session's CDP cookie store, so the request
        authorises directly — no browser tab or in-page XHR is involved.
        """
        url = f"{self.site.origin}{path}"
        time.sleep(_REST_CALL_DELAY_SECONDS)
        self.rest_status["api_calls"] = int(self.rest_status.get("api_calls", 0)) + 1
        try:
            return self.fetcher.fetch_json(url)
        except FetchError as exc:
            raise RestError(f"REST {path} failed: {exc}", http_status=exc.http_status) from exc

    def _fetch_collection(
        self,
        path: str,
        *,
        phase: str,
        errors: list[dict[str, Any]],
        optional_404: bool = False,
    ) -> list[dict[str, Any]]:
        """Follow ``paging.nextPage`` through a Blackboard REST collection.

        Used by the announcements / calendar / discussion / Books & Tools /
        file-attachments endpoints. Returns the concatenated ``results`` list
        across all pages. A non-dict payload or a missing/non-list ``results``
        field is recorded as an integration error and returns what was
        accumulated so far (possibly empty) — the crawl never aborts on a
        single malformed collection.

        When ``optional_404`` is True, a page-1 HTTP 404 is treated as an empty
        collection (the resource simply does not exist for this course) rather
        than an error. This is used by discussion boards: a course with no
        discussion forums returns 404, which is a normal outcome, not a failure.
        A page-2+ failure marks ``pagination_truncated`` on ``rest_status`` so
        :mod:`crawl_policy` refuses to publish the partial enumeration,
        mirroring the recursive contents path. A page-1 failure leaves
        ``results`` empty and is caught by the ``no-pages`` / per-endpoint
        error surfacing instead.
        """
        results: list[dict[str, Any]] = []
        next_path: str | None = path
        page = 0
        visited: set[str] = set()
        while next_path:
            if not isinstance(next_path, str) or next_path in visited:
                errors.append({"phase":phase,"error":"invalid or repeated pagination cursor"})
                self.rest_status["pagination_truncated"] = True
                break
            visited.add(next_path)
            page += 1
            try:
                payload = self._rest_get(next_path)
            except RestError as exc:
                # A 404 on an optional collection (e.g. a course with no
                # discussion forums) is an empty collection, not an error.
                if optional_404 and page == 1 and exc.http_status == 404:
                    break
                errors.append({
                    "phase": f"{phase}-page-{page}",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "http_status": exc.http_status,
                })
                if page > 1:
                    self.rest_status["pagination_truncated"] = True
                    self.rest_status["truncated_at_page"] = page
                break
            except SessionRequired:
                raise
            except Exception as exc:
                # Preserve the exception type so downstream diagnosis can
                # distinguish e.g. RestError (server/transport) from a bug.
                errors.append({
                    "phase": f"{phase}-page-{page}",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                })
                if page > 1:
                    self.rest_status["pagination_truncated"] = True
                    self.rest_status["truncated_at_page"] = page
                break
            if not isinstance(payload, dict):
                errors.append({"phase": f"{phase}-page-{page}", "error": "non-dict payload"})
                if page > 1:
                    self.rest_status["pagination_truncated"] = True
                    self.rest_status["truncated_at_page"] = page
                break
            page_results = payload.get("results")
            if not isinstance(page_results, list):
                errors.append({"phase": f"{phase}-page-{page}", "error": "missing or non-list results"})
                if page > 1:
                    self.rest_status["pagination_truncated"] = True
                    self.rest_status["truncated_at_page"] = page
                break
            if any(not isinstance(item, dict) for item in page_results):
                errors.append({"phase":f"{phase}-page-{page}","error":"non-object collection item"})
                self.rest_status["pagination_truncated"] = True
                break
            results.extend(page_results)
            paging = payload.get("paging")
            if paging is None:
                paging = {}
            if not isinstance(paging, dict):
                errors.append({"phase":f"{phase}-page-{page}","error":"invalid paging envelope"})
                self.rest_status["pagination_truncated"] = True
                break
            next_path = paging.get("nextPage")
        return results

    def _page_from_content(
        self,
        *,
        node: dict[str, Any],
        content_id: str,
        title: str,
        parent_id: str | None,
        parent_title: str,
        breadcrumbs: list[str],
        depth: int,
        handler_id: str | None,
        extra_attachments: list[Attachment] | None = None,
    ) -> Page | None:
        """Build a ``Page`` from a REST content node and preserve its evidence.

        ``extra_attachments`` carries pre-fetched file attachments (e.g. from the
        ``/contents/{id}/attachments`` endpoint for ``resource/x-bb-file`` nodes
        whose body is empty). This keeps the method pure — no network calls —
        while still surfacing file attachments the recursive contents payload
        does not include.  Ultra descriptions and current grading due dates are
        retained separately because LTI nodes often have no body.
        """
        body_raw = node.get("body")
        body_text, attachments = parse_body(body_raw if isinstance(body_raw, str) else None, site_config=self.site)
        external_links = external_links_from_body(body_raw if isinstance(body_raw, str) else None, site_config=self.site)

        # Ultra stores the text shown beneath an item title in ``description``;
        # LTI assessment nodes commonly have no ``body`` at all.  Keep this
        # separate from the BBML body so the archive preserves the API's two
        # fields without inventing a synthetic body.
        description_raw = node.get("description")
        description_text, description_attachments = parse_body(
            description_raw if isinstance(description_raw, str) else None,
            site_config=self.site,
        )
        description_links = external_links_from_body(
            description_raw if isinstance(description_raw, str) else None,
            site_config=self.site,
        )

        # LTI / external-link handlers carry their launch URL in contentHandler
        # rather than in the body; expose it as an attachment-equivalent so the
        # downstream LTI/Leganto/Panopto integrations can still discover it.
        handler_attachments = _attachments_from_handler(node, handler_id)
        seen_hrefs = {a.href for a in attachments}
        description_only_attachments = [
            a for a in description_attachments if a.href not in seen_hrefs
        ]
        seen_hrefs.update(a.href for a in description_only_attachments)
        deduped_handler = [
            a for a in handler_attachments if a.href not in seen_hrefs
        ]
        seen_hrefs.update(a.href for a in deduped_handler)
        deduped_extra = [
            a for a in (extra_attachments or []) if a.href not in seen_hrefs
        ]
        all_attachments: list[Attachment] = [
            *attachments, *description_only_attachments,
            *deduped_handler, *deduped_extra,
        ]

        seen_links = {str(link.get("href") or "") for link in external_links}
        for link in description_links:
            href = str(link.get("href") or "")
            if href and href not in seen_links:
                external_links.append(link)
                seen_links.add(href)

        node_type_hint = classify_content_handler(handler_id)
        url = _content_url(self.course_id, content_id, handler_id, site_config=self.site)

        return Page(
            content_id=content_id,
            title=title,
            parent_title=parent_title or None,
            parent_id=parent_id,
            depth=depth,
            breadcrumbs=breadcrumbs,
            url=url,
            body_text=body_text,
            attachments=all_attachments,
            external_links=external_links,
            node_type_hint=node_type_hint,
            description_text=description_text,
            due_at=_due_at_from_node(node),
        )


class RestError(RuntimeError):
    """Raised when a Blackboard REST endpoint returns a non-2xx response."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


def _handler_id(node: dict[str, Any]) -> str | None:
    handler = node.get("contentHandler")
    if isinstance(handler, dict):
        return str(handler.get("id") or "") or None
    return None


def _route_title(route_name: str) -> str:
    titles = {
        "outline": "Content",
        "calendar": "Calendar",
        "announcements": "Announcements",
        "discussions": "Discussion Board",
        "booksAndTools": "Books & Tools",
    }
    return titles.get(route_name, route_name)


def _calendar_body_text(title: str, cal_type: str, start: str, end: str) -> str:
    """Render a calendar item as a small plain-text body for ``page.md``."""
    parts = [f"Title: {title}" if title else "", f"Type: {cal_type}" if cal_type else ""]
    if start:
        parts.append(f"Start: {start}")
    if end:
        parts.append(f"End: {end}")
    return "\n".join(p for p in parts if p)


def _content_url(
    course_id: str,
    content_id: str,
    handler_id: str | None,
    *,
    site_config: SiteConfig,
) -> str:
    """Best-effort canonical URL for a content node.

    The REST API does not return a per-node URL; reconstruct the Ultra outline
    deep link so the archived ``fetched_url`` stays meaningful. LTI/external
    nodes use their launch URL (added as an attachment) rather than this value.
    """
    return f"{site_config.origin}/ultra/courses/{course_id}/outline/document/{content_id}"


def _due_at_from_node(node: dict[str, Any]) -> str | None:
    """Return a current due timestamp from a REST content representation.

    Blackboard places grading information inside ``contentHandler`` for LTI
    items and may place it at the content root for native assessments.  The
    ``customParameters.default_duedate`` value is intentionally ignored: it is
    a tool default and can be stale (or refer to a different copy of an item).
    """
    candidates: list[Any] = [node.get("grading")]
    handler = node.get("contentHandler")
    if isinstance(handler, dict):
        candidates.append(handler.get("grading"))
    for grading in candidates:
        if not isinstance(grading, dict):
            continue
        due = grading.get("due")
        if isinstance(due, str) and due.strip():
            return due.strip()
    return None


def _attachments_from_handler(
    node: dict[str, Any], handler_id: str | None
) -> list[Attachment]:
    """Extract launch/external URLs from contentHandler for LTI/external nodes."""
    handler = node.get("contentHandler")
    if not isinstance(handler, dict) or not handler_id:
        return []
    handler_lower = handler_id.lower()

    if handler_lower == "resource/x-bb-externallink":
        url = str(handler.get("url") or "").strip()
        if url:
            title = str(node.get("title") or url)
            return [
                Attachment(
                    text=title,
                    href=url,
                    mime_type="text/html",
                    source_kind="rest-external-link",
                    original_filename=title,
                )
            ]

    if handler_lower.startswith("resource/x-bb-blti"):
        url = str(handler.get("url") or "").strip()
        if url:
            title = str(node.get("title") or url)
            placement = str(handler.get("placementHandle") or "").strip()
            label = f"{title} ({placement})" if placement else title
            return [
                Attachment(
                    text=label,
                    href=url,
                    mime_type="text/html",
                    source_kind="rest-lti-launch",
                    original_filename=title,
                )
            ]

    return []
