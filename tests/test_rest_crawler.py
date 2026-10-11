from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, ClassVar


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from blackboard import bbml
from blackboard import rest_crawler
from blackboard.rest_crawler import RestCrawler, RestError
from blackboard.safe_fetch import FetchError
from site_fixture import EXAMPLE_SITE


# ---------------------------------------------------------------------------
# BBML parsing
# ---------------------------------------------------------------------------


class BbmlParseTests(unittest.TestCase):


    def test_classify_content_handler_mappings(self) -> None:
        cases = {
            "resource/x-bb-lesson": "module",
            "resource/x-bb-document": "document",
            "resource/x-bb-file": "document",
            "resource/x-bb-folder": "module",
            "resource/x-bb-blti-link": "lti_tool",
            "resource/x-bb-bltiplacement-Zoom": "lti_tool",
            "resource/x-bb-externallink": "external",
            "resource/x-bb-assignment": "assessment",
            "resource/x-bb-discussionboard": "discussion_forum",
            "resource/x-bb-forum": "discussion_forum",
            None: "page",
            "": "page",
            "resource/x-bb-unknown": "page",
        }
        for handler, expected in cases.items():
            with self.subTest(handler=handler):
                self.assertEqual(bbml.classify_content_handler(handler), expected)


# ---------------------------------------------------------------------------
# RestCrawler — unit tests with a fake browser
# ---------------------------------------------------------------------------


class _FakeFetcher:
    """Mock CurlFetcher that returns scripted JSON responses keyed by REST path.

    Mirrors the previous _FakeBrowser's path-keyed contract, but adapted to
    fetch_json(url) instead of eval_json(tab_id, script). The announcements,
    calendars, and books-and-tools endpoints default to empty results so tests
    focused on the content tree do not have to mock them.
    """

    _BASE = "https://learn.example.edu"
    _EMPTY_DEFAULTS: ClassVar[dict] = {
        "announcements": {"results": []},
        "calendars": {"results": []},
        "books_and_tools": {"results": []},
        "discussion_forums": {"results": []},
    }

    def __init__(self, responses: dict[str, Any], *, course_id: str = "_1_1") -> None:
        self.responses = dict(responses)
        self.responses.setdefault(
            f"/learn/api/public/v1/courses/{course_id}/announcements",
            self._EMPTY_DEFAULTS["announcements"],
        )
        self.responses.setdefault(
            f"/learn/api/public/v1/calendars/items?courseId={course_id}",
            self._EMPTY_DEFAULTS["calendars"],
        )
        self.responses.setdefault(
            f"/learn/api/public/v1/lti/placements?courseId={course_id}&type=Application",
            self._EMPTY_DEFAULTS["books_and_tools"],
        )
        self.calls: list[str] = []

    def fetch_json(self, url: str, **kwargs: Any) -> Any:
        path = url.removeprefix(self._BASE)
        self.calls.append(path)
        payload = self.responses.get(path)
        if payload is None:
            raise FetchError(f"REST {path} failed: 404", http_status=404)
        if isinstance(payload, Exception):
            # Tests inject RestError/FetchError to simulate endpoint failures.
            # _rest_get catches FetchError and re-raises as RestError, so raise
            # a FetchError wrapper here to match the real CurlFetcher contract.
            if isinstance(payload, FetchError):
                raise payload
            raise FetchError(str(payload)) from payload
        return payload


# Backwards-compat alias for any test still referencing _FakeBrowser.
_FakeBrowser = _FakeFetcher


def _make_node(
    node_id: str,
    title: str,
    *,
    has_children: bool = False,
    handler: str | None = None,
    body: str | None = None,
    parent_id: str | None = None,
) -> dict[str, Any]:
    node: dict[str, Any] = {"id": node_id, "title": title, "hasChildren": has_children}
    if handler is not None:
        node["contentHandler"] = {"id": handler}
    if body is not None:
        node["body"] = body
    if parent_id is not None:
        node["parentId"] = parent_id
    return node


def _recursive_response(course_id: str, nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a responses dict with a single recursive contents payload."""
    return {
        f"/learn/api/public/v1/courses/{course_id}/contents?recursive=true": {
            "results": nodes
        }
    }


class RestCrawlerTests(unittest.TestCase):

    def test_recursive_enumerates_full_tree(self) -> None:
        responses = _recursive_response("_9006_1", [
            _make_node("_1_1", "Module 1", has_children=True, handler="resource/x-bb-lesson"),
            _make_node("_2_1", "Standalone doc", handler="resource/x-bb-document", parent_id="_1_1",
                       body=(
                           '<p>Read the guide.</p>'
                           '<a href="https://learn.example.edu/bbcswebdav/pid-9000-dt-content-rid-8637221_1/lg.pdf">Learning Guide</a>'
                           '<a href="https://learn.example.edu/bbcswebdav/pid-9000-dt-content-rid-8637221_1/lg.pdf">preview</a>'
                           '<a href="https://learn.example.edu/bbcswebdav/pid-9000-dt-content-rid-8637221_1/lg.pdf">download</a>'
                           '<a href="https://learn.example.edu/ultra/courses/_9006_1/outline">internal</a>'
                           '<a href="https://learn.example.edu/ultra/courses/_1_1/outline">internal</a>'
                           '<a href="https://www.ted.com/talks/x">TED</a>'
                       )),
            _make_node("_3_1", "Child doc", handler="resource/x-bb-document", parent_id="_1_1", body=""),
            {
                "id": "_4_1",
                "title": "Panopto",
                "contentHandler": {
                    "id": "resource/x-bb-bltiplacement-Panopto",
                    "placementHandle": "Panopto",
                    "url": "https://example.au.panopto.com/launch",
                },
            },
            {
                "id": "_5_1",
                "title": "Dan Meyer Video",
                "contentHandler": {
                    "id": "resource/x-bb-externallink",
                    "url": "https://www.ted.com/talks/dan_meyer",
                },
            },
        ])
        browser = _FakeBrowser(responses, course_id="_9006_1")
        crawler = RestCrawler(browser, "_9006_1", site_config=EXAMPLE_SITE)
        pages = crawler.crawl()

        ids = {p.content_id for p in pages}
        self.assertIn("_1_1", ids)
        self.assertIn("_2_1", ids)
        self.assertIn("_3_1", ids)
        # Route shells are also present
        route_ids = {
            "ultra-outline", "ultra-announcements", "ultra-calendar",
            "ultra-discussions", "ultra-books-and-tools",
        }
        self.assertEqual({p.content_id for p in pages if p.node_type == "route"}, route_ids)
        for page in pages:
            if page.content_id in route_ids:
                self.assertEqual(page.depth, 0)
                self.assertEqual(page.node_type, "route")
                self.assertIn("/ultra/courses/_9006_1/", page.url)
        # The document with a body should carry its attachment
        doc = next(p for p in pages if p.content_id == "_2_1")
        self.assertEqual(len(doc.attachments), 1)
        self.assertIn("/bbcswebdav/", doc.attachments[0].href)
        self.assertEqual(doc.attachments[0].href, "https://learn.example.edu/bbcswebdav/pid-9000-dt-content-rid-8637221_1/lg.pdf")
        self.assertEqual(doc.attachments[0].filename, "lg.pdf")
        self.assertEqual(doc.attachments[0].source_kind, "rest-bbml")
        self.assertIn("Read the guide.", doc.body_text)
        self.assertEqual(doc.external_links, [{"text": "", "href": "https://www.ted.com/talks/x"}])
        self.assertEqual(doc.node_type, "document")
        for content_id in ("_1_1", "_3_1"):
            empty = next(p for p in pages if p.content_id == content_id)
            self.assertEqual(empty.body_text, "")
            self.assertEqual(empty.attachments, [])
        lti = next(p for p in pages if p.content_id == "_4_1")
        self.assertEqual(len(lti.attachments), 1)
        self.assertEqual(lti.attachments[0].href, "https://example.au.panopto.com/launch")
        self.assertEqual(lti.attachments[0].source_kind, "rest-lti-launch")
        self.assertIn("Panopto", lti.attachments[0].text)
        external = next(p for p in pages if p.content_id == "_5_1")
        self.assertEqual(len(external.attachments), 1)
        self.assertEqual(external.attachments[0].href, "https://www.ted.com/talks/dan_meyer")
        self.assertEqual(external.attachments[0].source_kind, "rest-external-link")
        # Status recorded. api_calls = 1 recursive contents + announcements + calendar + books_and_tools.
        self.assertTrue(crawler.rest_status["attempted"])
        self.assertEqual(crawler.rest_status["pages"], len(pages))
        self.assertEqual(crawler.rest_status["errors"], [])

    def test_lti_content_preserves_description_and_due_date(self) -> None:
        """Ultra LTI items often put all visible instructions in description."""
        course_id = "_1_1"
        responses = _recursive_response(course_id, [
            {
                "id": "_assessment_1",
                "title": "Lesson Plan",
                "description": "Write a lesson plan.",
                "contentHandler": {
                    "id": "resource/x-bb-blti-link",
                    "grading": {"due": "2026-09-11T13:59:00.000Z"},
                },
            }
        ])
        browser = _FakeBrowser(responses, course_id=course_id)
        crawler = RestCrawler(browser, course_id, site_config=EXAMPLE_SITE)
        pages = crawler.crawl()

        assessment = next(p for p in pages if p.content_id == "_assessment_1")
        self.assertEqual(assessment.body_text, "")
        self.assertEqual(assessment.description_text, "Write a lesson plan.")
        self.assertEqual(assessment.due_at, "2026-09-11T13:59:00.000Z")

    def test_announcements_and_calendar_fetched_as_child_pages(self) -> None:
        responses = {
            "/learn/api/public/v1/courses/_1_1/contents": {"results": []},
            "/learn/api/public/v1/courses/_1_1/announcements": {
                "results": [
                    {
                        "id": "_9009_1",
                        "title": "Welcome",
                        "created": "2026-07-14T22:21:01.433Z",
                        "body": "<p>Hi everyone, welcome to the course.</p>",
                    }
                ]
            },
            "/learn/api/public/v1/calendars/items?courseId=_1_1": {
                "results": [
                    {
                        "id": "_cal1_1",
                        "title": "Assignment 1 due",
                        "calendarType": "Assignment",
                        "start": "2026-08-15T23:59:00Z",
                        "end": "2026-08-15T23:59:00Z",
                    }
                ]
            },
        }
        browser = _FakeBrowser(responses)
        crawler = RestCrawler(browser, "_1_1", site_config=EXAMPLE_SITE)
        pages = crawler.crawl()
        ann = [p for p in pages if p.node_type == "announcement"]
        self.assertEqual(len(ann), 1)
        self.assertEqual(ann[0].content_id, "_9009_1")
        self.assertIn("Welcome", ann[0].title)
        self.assertIn("2026-07-14", ann[0].title)  # created date in title
        self.assertIn("Hi everyone", ann[0].body_text)
        self.assertEqual(ann[0].parent_id, "ultra-announcements")
        self.assertEqual(ann[0].depth, 1)
        cal = [p for p in pages if p.node_type == "calendar"]
        self.assertEqual(len(cal), 1)
        self.assertEqual(cal[0].content_id, "_cal1_1")
        self.assertIn("Assignment 1 due", cal[0].title)
        self.assertIn("Assignment", cal[0].body_text)
        self.assertEqual(cal[0].parent_id, "ultra-calendar")

    def test_discussions_discovered_from_interactive_and_messages_fetched(self) -> None:
        # Discussion forums are discovered from INTERACTIVE content children:
        # one call returns forumlink nodes with forum ID in contentDetailUrl.
        responses = {
            "/learn/api/public/v1/courses/_1_1/announcements": {"results": []},
            "/learn/api/public/v1/calendars/items?courseId=_1_1": {"results": []},
            "/learn/api/public/v1/courses/_1_1/contents?recursive=true": {"results": []},
            # INTERACTIVE children → forumlink nodes with forum IDs.
            "/learn/api/v1/courses/_1_1/contents/INTERACTIVE/children": {
                "results": [
                    {
                        "id": "_fl1_1",
                        "title": "General questions and discussion",
                        "contentHandler": {"id": "resource/x-bb-forumlink"},
                        "contentDetailUrl": "/learn/api/v1/courses/_1_1/discussionboards/_conf1_1/forums/_f1_1",
                    }
                ]
            },
            # Messages for forum _f1_1.
            "/learn/api/v1/courses/_1_1/discussionboards/default/forums/_f1_1/messages": {
                "results": [
                    {
                        "id": "_m1_1",
                        "subject": "When is Assignment 1 due?",
                        "body": {"rawText": "<p>Can someone remind me?</p>"},
                        "userId": "_9005_1",
                        "postDate": "2026-07-20T10:00:00.000Z",
                        "messageStatus": {"numberOfChildren": 1},
                    }
                ]
            },
            # Replies to _m1_1 — carries givenName/familyName.
            "/learn/api/v1/courses/_1_1/discussionboards/default/forums/_f1_1/messages/_m1_1/replies": {
                "results": [
                    {
                        "id": "_r1_1",
                        "subject": "Re: When is Assignment 1 due?",
                        "body": {"rawText": "<p>It is due in Week 5.</p>"},
                        "givenName": "Example",
                        "familyName": "Instructor",
                        "postDate": "2026-07-20T11:00:00.000Z",
                        "messageStatus": {"numberOfChildren": 0},
                    }
                ]
            },
        }
        browser = _FakeBrowser(responses)
        crawler = RestCrawler(browser, "_1_1", site_config=EXAMPLE_SITE)
        pages = crawler.crawl()

        forums = [p for p in pages if p.node_type == "discussion_forum"]
        messages = [p for p in pages if p.node_type == "discussion_message"]

        self.assertEqual(len(forums), 1)
        self.assertEqual(forums[0].content_id, "discussion-forum-_f1_1")
        self.assertEqual(forums[0].parent_id, "ultra-discussions")
        self.assertIn("General questions", forums[0].title)

        # Top-level message + one reply = 2 message pages.
        self.assertEqual(len(messages), 2)
        top_msg = [m for m in messages if m.content_id == "discussion-forum-_f1_1-_m1_1"][0]
        self.assertEqual(top_msg.parent_id, "discussion-forum-_f1_1")
        self.assertIn("Can someone remind me", top_msg.body_text)
        self.assertIn("Author: _9005_1", top_msg.body_text)
        self.assertIn("2026-07-20", top_msg.title)

        reply = [m for m in messages if m.content_id == "discussion-forum-_f1_1-_m1_1-_r1_1"][0]
        self.assertEqual(reply.parent_id, "discussion-forum-_f1_1-_m1_1")
        self.assertIn("due in Week 5", reply.body_text)
        self.assertIn("Example Instructor", reply.body_text)

    def test_no_discussion_forums_means_no_discussion_pages(self) -> None:
        # A course whose INTERACTIVE children have no forumlink nodes (or 404)
        # produces no discussion pages and no errors. This is the normal case
        # for a course without discussion boards.
        responses = {
            "/learn/api/public/v1/courses/_1_1/announcements": {"results": []},
            "/learn/api/public/v1/calendars/items?courseId=_1_1": {"results": []},
            "/learn/api/public/v1/courses/_1_1/contents?recursive=true": {
                "results": [
                    {"id": "_doc1_1", "title": "A document", "contentHandler": {"id": "resource/x-bb-document"}},
                ]
            },
            # INTERACTIVE returns empty (no forums).
            "/learn/api/v1/courses/_1_1/contents/INTERACTIVE/children": {"results": []},
        }
        browser = _FakeBrowser(responses)
        crawler = RestCrawler(browser, "_1_1", site_config=EXAMPLE_SITE)
        pages = crawler.crawl()
        discussion_pages = [p for p in pages if p.node_type and p.node_type.startswith("discussion")]
        self.assertEqual(discussion_pages, [])
        discussion_errors = [
            e for e in crawler.rest_status.get("errors", [])
            if "discussion" in str(e.get("phase", ""))
        ]
        self.assertEqual(discussion_errors, [], f"no forums should not produce errors: {discussion_errors}")

    def test_books_and_tools_via_rest(self) -> None:
        # Panopto + Reading List (both allowStudents=true, Application type).
        responses = {
            "/learn/api/public/v1/courses/_9006_1/contents": {"results": []},
            "/learn/api/public/v1/lti/placements?courseId=_9006_1&type=Application": {
                "results": [
                    {
                        "id": "_1173_1",
                        "name": "Panopto",
                        "handle": "panopto",
                        "type": "Application",
                        "availability": {"available": "Yes"},
                        "allowStudents": True,
                        "launchInNewWindow": False,
                        "launchLink": "/webapps/blackboard/execute/blti/launchPlacement?blti_placement_id=_1173_1&wrapped=true",
                    },
                    {
                        "id": "_977_1",
                        "name": "Reading List",
                        "handle": "leganto_reading_list",
                        "type": "Application",
                        "availability": {"available": "Yes"},
                        "allowStudents": True,
                        "launchInNewWindow": True,
                        "launchLink": "/webapps/blackboard/execute/blti/launchPlacement?blti_placement_id=_977_1&wrapped=true",
                    },
                    # allowStudents=false (instructor-only) tools are dropped.
                    {
                        "id": "_2_1",
                        "name": "Accessibility Report",
                        "handle": "ally-instructor-report",
                        "type": "Application",
                        "availability": {"available": "Yes"},
                        "allowStudents": False,
                        "launchLink": "/webapps/blackboard/execute/blti/launchPlacement?blti_placement_id=_2_1",
                    },
                ]
            },
        }
        browser = _FakeBrowser(responses, course_id="_9006_1")
        crawler = RestCrawler(browser, "_9006_1", site_config=EXAMPLE_SITE)
        pages = crawler.crawl()
        tools = {p.content_id: p for p in pages if p.node_type == "lti_tool"}
        self.assertEqual(set(tools), {"ultra-lti-_1173_1", "ultra-lti-_977_1"})

        # Panopto Page contract (downstream panopto_transcripts depends on this).
        panopto = tools["ultra-lti-_1173_1"]
        self.assertEqual(panopto.parent_id, "ultra-books-and-tools")
        self.assertEqual(panopto.depth, 1)
        launch = next(
            link for link in panopto.external_links if link["kind"] == "lti_launch"
        )
        self.assertEqual(launch["handle"], "panopto")
        self.assertEqual(launch["placement_id"], "_1173_1")
        self.assertIn("/webapps/blackboard/execute/blti/launchPlacement", launch["href"])
        self.assertIn("blti_placement_id=_1173_1", launch["href"])
        self.assertIn("course_id=_9006_1", launch["href"])
        self.assertIn("from_ultra=true", launch["href"])

        # Reading List Page contract (downstream reading_lists depends on this).
        leganto = tools["ultra-lti-_977_1"]
        leganto_launch = next(
            link for link in leganto.external_links if link["kind"] == "lti_launch"
        )
        self.assertEqual(leganto_launch["handle"], "leganto_reading_list")

    def test_books_and_tools_error_isolated(self) -> None:
        # Placements endpoint 500s; content tree still crawls.
        responses = _recursive_response("_1_1", [
            _make_node("_1_1", "Doc", handler="resource/x-bb-document")
        ])
        responses["/learn/api/public/v1/lti/placements?courseId=_1_1&type=Application"] = FetchError(
            "server error: HTTP 500"
        )
        browser = _FakeBrowser(responses)
        crawler = RestCrawler(browser, "_1_1", site_config=EXAMPLE_SITE)
        pages = crawler.crawl()
        self.assertIn("_1_1", {p.content_id for p in pages})
        # _fetch_collection records errors as "<phase>-page-<N>".
        phases = {e["phase"] for e in crawler.rest_status["errors"]}
        self.assertTrue(any(p.startswith("books_and_tools") for p in phases), phases)


    def test_contents_recursive_error_is_recorded_not_raised(self) -> None:
        # The recursive contents endpoint 404s; crawl still returns route shells
        # and other endpoints' pages, with the error recorded.
        responses = {}
        browser = _FakeBrowser(responses)  # no contents response -> 404
        crawler = RestCrawler(browser, "_1_1", site_config=EXAMPLE_SITE)
        pages = crawler.crawl()
        # Route shells still present.
        self.assertIn("ultra-outline", {p.content_id for p in pages})
        self.assertTrue(crawler.rest_status["errors"])
        self.assertNotIn("pagination_truncated", crawler.rest_status)
        self.assertNotIn("truncated_at_page", crawler.rest_status)


    def test_breadcrumbs_built_from_parent_chain(self) -> None:
        # Breadcrumbs are rebuilt from parentId chain, not traversal order.
        responses = _recursive_response("_1_1", [
            _make_node("_1_1", "Top", handler="resource/x-bb-lesson"),
            _make_node("_2_1", "Middle", handler="resource/x-bb-folder", parent_id="_1_1"),
            _make_node("_3_1", "Leaf", handler="resource/x-bb-document", parent_id="_2_1"),
        ])
        browser = _FakeBrowser(responses)
        crawler = RestCrawler(browser, "_1_1", site_config=EXAMPLE_SITE)
        pages = crawler.crawl()
        leaf = next(p for p in pages if p.content_id == "_3_1")
        self.assertEqual(leaf.breadcrumbs, ["Top", "Middle", "Leaf"])
        self.assertEqual(leaf.parent_title, "Middle")
        self.assertEqual(leaf.parent_id, "_2_1")

    def test_bucket_derived_from_breadcrumbs(self) -> None:
        # REST-derived pages flow through the same bucket logic as DOM pages.
        responses = _recursive_response("_1_1", [
            _make_node("_1_1", "Assessment Hub", handler="resource/x-bb-lesson"),
            _make_node("_2_1", "Assessment 1", handler="resource/x-bb-document", parent_id="_1_1"),
        ])
        browser = _FakeBrowser(responses)
        crawler = RestCrawler(browser, "_1_1", site_config=EXAMPLE_SITE)
        pages = crawler.crawl()
        assessment = next(p for p in pages if p.content_id == "_2_1")
        self.assertEqual(assessment.bucket, "50_Assessments/Assessment 1")

    def test_recursive_follows_pagination_nextPage(self) -> None:
        """The recursive contents endpoint paginates at 200 nodes; the crawler
        must follow ``paging.nextPage`` until exhausted or nodes are lost."""
        course_id = "_1_1"
        page1 = [_make_node(f"_{i}_1", f"Node{i}", handler="resource/x-bb-document") for i in range(200)]
        page2 = [_make_node(f"_{i}_1", f"Node{i}", handler="resource/x-bb-document") for i in range(200, 237)]
        responses = {
            f"/learn/api/public/v1/courses/{course_id}/contents?recursive=true": {
                "results": page1,
                "paging": {"nextPage": f"/learn/api/public/v1/courses/{course_id}/contents?recursive=true&offset=200"},
            },
            f"/learn/api/public/v1/courses/{course_id}/contents?recursive=true&offset=200": {
                "results": page2,
            },
        }
        browser = _FakeBrowser(responses, course_id=course_id)
        crawler = RestCrawler(browser, course_id, site_config=EXAMPLE_SITE)
        pages = crawler.crawl()
        content_pages = [p for p in pages if p.node_type != "route"]
        self.assertEqual(len(content_pages), 237)
        # Both pages were fetched
        contents_calls = [c for c in browser.calls if "/contents" in c and "recursive" in c]
        self.assertEqual(len(contents_calls), 2)

    def test_pagination_break_marks_truncated(self) -> None:
        """A page-2+ REST failure must mark the crawl truncated so crawl_policy
        refuses to publish the partial enumeration. The first-page nodes are
        still returned so the candidate tree can be inspected for diagnosis."""
        course_id = "_1_1"
        page1 = [_make_node(f"_{i}_1", f"Node{i}", handler="resource/x-bb-document") for i in range(200)]
        responses = {
            f"/learn/api/public/v1/courses/{course_id}/contents?recursive=true": {
                "results": page1,
                "paging": {"nextPage": f"/learn/api/public/v1/courses/{course_id}/contents?recursive=true&offset=200"},
            },
            f"/learn/api/public/v1/courses/{course_id}/contents?recursive=true&offset=200": RestError("page 2 HTTP 500"),
        }
        browser = _FakeBrowser(responses, course_id=course_id)
        crawler = RestCrawler(browser, course_id, site_config=EXAMPLE_SITE)
        pages = crawler.crawl()
        # First-page content nodes are preserved for diagnosis.
        content_pages = [p for p in pages if p.node_type != "route"]
        self.assertEqual(len(content_pages), 200)
        # Truncation is flagged for crawl_policy to act on.
        self.assertTrue(crawler.rest_status.get("pagination_truncated"))
        self.assertEqual(crawler.rest_status.get("truncated_at_page"), 2)
        # The error is also recorded for forensics.
        phases = [e.get("phase") for e in crawler.rest_status.get("errors") or []]
        self.assertIn("contents-recursive-page-2", phases)


    # ------------------------------------------------------------------
    # _fetch_collection: unified pagination + envelope validation
    # (announcements / calendar / books_and_tools / file-attachments)
    # ------------------------------------------------------------------

    def test_announcements_follow_pagination(self) -> None:
        """Announcements paginating via nextPage must yield all pages, not
        just the first. Validates _fetch_collection on a non-recursive
        collection endpoint."""
        course_id = "_1_1"
        responses = _recursive_response(course_id, [])
        ann_path = f"/learn/api/public/v1/courses/{course_id}/announcements"
        responses[ann_path] = {
            "results": [
                {"id": "_a1_1", "title": "First", "created": "2026-07-01T00:00:00Z", "body": "<p>1</p>"},
                {"id": "_a2_1", "title": "Second", "created": "2026-07-02T00:00:00Z", "body": "<p>2</p>"},
            ],
            "paging": {"nextPage": ann_path + "&offset=2"},
        }
        responses[ann_path + "&offset=2"] = {
            "results": [
                {"id": "_a3_1", "title": "Third", "created": "2026-07-03T00:00:00Z", "body": "<p>3</p>"},
            ],
        }
        browser = _FakeBrowser(responses, course_id=course_id)
        crawler = RestCrawler(browser, course_id, site_config=EXAMPLE_SITE)
        pages = crawler.crawl()
        ann_titles = {p.title.split(" (")[0] for p in pages if p.node_type == "announcement"}
        self.assertEqual(ann_titles, {"First", "Second", "Third"})
        # Both pages fetched.
        ann_calls = [c for c in browser.calls if "/announcements" in c]
        self.assertEqual(len(ann_calls), 2)

    def test_collection_malformed_envelope_recorded(self) -> None:
        """A non-list or missing 'results' field is recorded as an integration
        error, not silently treated as empty. The crawl continues."""
        course_id = "_1_1"
        responses = _recursive_response(course_id, [
            _make_node("_1_1", "Doc", handler="resource/x-bb-document")
        ])
        responses[f"/learn/api/public/v1/courses/{course_id}/announcements"] = {
            "not_results": [{"id": "_a1_1"}],
        }
        browser = _FakeBrowser(responses, course_id=course_id)
        crawler = RestCrawler(browser, course_id, site_config=EXAMPLE_SITE)
        pages = crawler.crawl()
        # Content node still present.
        self.assertIn("_1_1", {p.content_id for p in pages})
        # Malformed envelope recorded, not silent.
        phases = {e["phase"] for e in crawler.rest_status["errors"]}
        self.assertTrue(any(p.startswith("announcements") for p in phases), phases)
        # No announcement pages produced.
        self.assertEqual([p for p in pages if p.node_type == "announcement"], [])
        # Not flagged as truncated (page-1 malformed is not truncation).
        self.assertNotIn("pagination_truncated", crawler.rest_status)

    def test_file_attachments_follow_pagination(self) -> None:
        """A file node whose attachments span two pages must surface every
        attachment. Validates _fetch_collection through _fetch_file_attachments."""
        course_id = "_1_1"
        node = _make_node("_f1_1", "File node", handler="resource/x-bb-file")
        responses = _recursive_response(course_id, [node])
        att_path = f"/learn/api/public/v1/courses/{course_id}/contents/_f1_1/attachments"
        responses[att_path] = {
            "results": [{"id": "_att1_1", "fileName": "a.pdf", "mimeType": "application/pdf"}],
            "paging": {"nextPage": att_path + "&offset=1"},
        }
        responses[att_path + "&offset=1"] = {
            "results": [{"id": "_att2_1", "fileName": "b.pdf", "mimeType": "application/pdf"}],
        }
        browser = _FakeBrowser(responses, course_id=course_id)
        crawler = RestCrawler(browser, course_id, site_config=EXAMPLE_SITE)
        pages = crawler.crawl()
        file_page = next(p for p in pages if p.content_id == "_f1_1")
        hrefs = {a.href for a in file_page.attachments}
        self.assertTrue(any(h.endswith("/_att1_1/download") for h in hrefs), hrefs)
        self.assertTrue(any(h.endswith("/_att2_1/download") for h in hrefs), hrefs)
        # Both attachment pages fetched.
        att_calls = [c for c in browser.calls if "/attachments" in c]
        self.assertEqual(len(att_calls), 2)
