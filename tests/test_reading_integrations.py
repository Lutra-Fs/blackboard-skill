from __future__ import annotations
import sys
import tempfile
import unittest
from pathlib import Path
SCRIPT_DIR = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0,str(SCRIPT_DIR))
from blackboard.sync_models import Page
from blackboard.crawl_policy import evaluate_crawl_completeness
from site_fixture import EXAMPLE_SITE
from blackboard.archive_repository import ArchiveState, MetadataWriter
from blackboard import reading_lists
from blackboard import panopto_transcripts

COURSE_ID = '_9007_1'
BASE = f'https://learn.example.edu/ultra/courses/{COURSE_ID}'


class BooksAndToolsStageTests(unittest.TestCase):
    def test_ris_validation_requires_balanced_complete_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            valid = Path(temp_dir) / "valid.ris"
            valid.write_text("TY  - BOOK\nTI  - One\nER  -\nTY  - JOUR\nTI  - Two\nER  -\n", encoding="utf-8")
            invalid = Path(temp_dir) / "invalid.ris"
            invalid.write_text("TY  - BOOK\nTI  - Truncated\n", encoding="utf-8")
            self.assertEqual(reading_lists.ris_entry_count(valid), 2)
            self.assertEqual(reading_lists.ris_entry_count(invalid), 0)

    def test_archived_panopto_lti_launch_is_preferred(self) -> None:
        page = Page('ultra-lti-_1173_1', 'Panopto', None, None, 0, ['Panopto'], '', '',
                    external_links=[{
                        "text": "Panopto",
                        "href": "https://learn.example.edu/webapps/blackboard/execute/blti/launchPlacement?blti_placement_id=_1173_1",
                        "kind": "lti_launch",
                        "handle": "panopto",
                        "placement_id": "_1173_1",
                    }])
        tools = panopto_transcripts.archived_panopto_tools([page], site_config=EXAMPLE_SITE)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["source"], "ultra-books-and-tools-archive")

    def test_prepared_tree_tools_are_visible_before_course_tree_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            leganto_url = (
                "https://learn.example.edu/webapps/blackboard/execute/blti/"
                "launchPlacement?blti_placement_id=_977_1"
            )
            panopto_url = (
                "https://learn.example.edu/webapps/blackboard/execute/blti/"
                "launchPlacement?blti_placement_id=_1173_1"
            )
            page = Page('tools', 'Books & Tools', None, None, 0, ['Books & Tools'], '', '',
                        external_links=[
                            {"text": "Reading List", "href": leganto_url,
                             "kind": "lti_launch", "handle": "leganto_reading_list"},
                            {"text": "Panopto", "href": panopto_url,
                             "kind": "lti_launch", "handle": "panopto"},
                        ])
            state = ArchiveState(root)
            writer = MetadataWriter(state, COURSE_ID, site_config=EXAMPLE_SITE)
            writer.write_page_artifacts([page], {'safe_to_replace': True}, defer_publish=True)
            prepared_tree = writer.last_tree_payload
            pages = state.load_tree_pages(prepared_tree)

            reading_tools = reading_lists.iter_archived_reading_list_tools(
                root, tree=prepared_tree, pages=pages, site_config=EXAMPLE_SITE,
            )
            panopto_tools = panopto_transcripts.archived_panopto_tools(
                pages,
                site_config=EXAMPLE_SITE,
            )

            self.assertFalse((root / "_blackboard/course-tree.json").exists())
            self.assertEqual([tool.launch_url for tool in reading_tools], [leganto_url])
            self.assertEqual([tool["href"] for tool in panopto_tools], [panopto_url])

    def test_empty_books_and_tools_route_is_not_reported_complete(self) -> None:
        page = Page(
            content_id="ultra-books-and-tools",
            title="Books & Tools",
            parent_title=None,
            parent_id=None,
            depth=0,
            breadcrumbs=["Books & Tools"],
            url=f"{BASE}/outline/booksAndTools",
            body_text="",
        )
        source_status = {
            "rest": {
                "attempted": True,
                "pages": 1,
                "api_calls": 1,
                "errors": [],
            },
        }
        result = evaluate_crawl_completeness([page], source_status, None)
        # A route-shell-only crawl (no substantive pages) must block publication.
        self.assertEqual(result["status"], "blocked")
        self.assertIn("route-shell-only", result["reasons"])
