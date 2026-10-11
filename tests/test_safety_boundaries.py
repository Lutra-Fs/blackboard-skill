from __future__ import annotations
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
SCRIPT_DIR = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0,str(SCRIPT_DIR))
from blackboard.archive_io import ArchiveIntegrityError, safe_archive_path
from blackboard.archive_repository import ArchiveState, MetadataWriter
from blackboard.sync_models import Page
from site_fixture import EXAMPLE_SITE
from blackboard.ultra_content import parse_course_id
from blackboard import browser_backend
from blackboard import reading_lists
from blackboard import panopto_transcripts

COURSE_ID = '_9007_1'


class ArchiveBoundaryTests(unittest.TestCase):
    def test_corrupt_authoritative_tree_fails_closed_and_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tree = root / "_blackboard/course-tree.json"
            tree.parent.mkdir()
            tree.write_text("{truncated", encoding="utf-8")

            with self.assertRaises(ArchiveIntegrityError):
                ArchiveState(root)
            self.assertEqual(tree.read_text(encoding="utf-8"), "{truncated")

    def test_archive_path_rejects_traversal_and_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(temp_dir)
            with self.assertRaises(ArchiveIntegrityError):
                safe_archive_path(root, "../outside")
            (root / "linked").symlink_to(Path(outside_dir), target_is_directory=True)
            with self.assertRaises(ArchiveIntegrityError):
                safe_archive_path(root, "linked/file.pdf")


    def test_blackboard_id_rejects_query_injection(self) -> None:
        with self.assertRaises(ValueError):
            parse_course_id(
                "https://learn.example.edu/ultra/courses/_9007_1/outline",
                "_9007_1?other=true",
                site_config=EXAMPLE_SITE,
            )


class ReadingListRegressionTests(unittest.TestCase):
    def test_ris_below_visible_citation_count_is_candidate(self) -> None:
        class Browser:
            tab_id = "tab"

        class FakeFetcher:
            def __init__(self):
                self.urls = []

            def download(self, url, destination, *, url_policy=None, headers=None):
                self.urls.append(url)
                destination.write_text("TY  - BOOK\nTI  - One\nER  -\n", encoding="utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tool = reading_lists.ReadingListTool(
                course_root=root,
                course_id=COURSE_ID,
                launch_url="https://learn.example.edu/webapps/blackboard/execute/blti/launchPlacement",
            )
            resolved = (
                "https://example.leganto.exlibrisgroup.com/leganto/nui/lists/75083168910001571",
                "Reading List",
                2,
            )
            fetcher = FakeFetcher()
            with mock.patch.object(reading_lists, "resolve_leganto_list", return_value=resolved):
                result = reading_lists.export_reading_list(
                    Browser(), fetcher, tool, wait_seconds=0.01, retry_count=1,
                    site_config=EXAMPLE_SITE,
                )
            self.assertEqual(result["status"], "ambiguous")
            self.assertEqual(result["reason"], "leganto-ris-below-visible-citation-count")
            self.assertTrue(Path(result["candidate"]).is_file())
            self.assertEqual(result["list_id"], "75083168910001571")
            self.assertEqual(result["list_url"], resolved[0])
            self.assertEqual(fetcher.urls, [
                "https://example.leganto.exlibrisgroup.com/leganto/rl/files/ris/list/75083168910001571?",
            ])


class ActiveTreeIntegrationTests(unittest.TestCase):
    def test_stale_lti_page_outside_current_tree_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stale = root / "_blackboard/pages/stale"
            stale.mkdir(parents=True)
            (stale / "links.json").write_text(json.dumps({"external_links": [
                {
                    "text": "Reading List",
                    "href": "https://learn.example.edu/webapps/blackboard/execute/blti/launchPlacement?x=1",
                    "kind": "lti_launch",
                    "handle": "leganto_reading_list",
                },
                {
                    "text": "Panopto",
                    "href": "https://learn.example.edu/webapps/blackboard/execute/blti/launchPlacement?x=2",
                    "kind": "lti_launch",
                    "handle": "panopto",
                },
            ]}), encoding="utf-8")

            page = Page('active', 'Active', None, None, 0, ['Active'], '', 'Body')
            state = ArchiveState(root)
            writer = MetadataWriter(state, COURSE_ID, site_config=EXAMPLE_SITE)
            writer.write_page_artifacts([page], {'safe_to_replace': True})
            tree = ArchiveState(root).existing_tree
            pages = state.load_tree_pages(tree)
            self.assertEqual(
                reading_lists.iter_archived_reading_list_tools(root, tree=tree, pages=pages, site_config=EXAMPLE_SITE),
                [],
            )
            self.assertEqual(panopto_transcripts.archived_panopto_tools(pages, site_config=EXAMPLE_SITE), [])


class PanoptoPrivacyTests(unittest.TestCase):
    def test_transcript_archive_uses_allowlisted_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            link = panopto_transcripts.PanoptoLink(
                "https://example.au.panopto.com/Panopto/Pages/Viewer.aspx?id=recording-1&token=secret",
                course_root=root,
            )
            data = {
                "ok": True,
                "delivery": {
                    "UserEmail": "private@example.com",
                    "DownloadUrl": "https://stream.invalid/signed?token=secret",
                    "Delivery": {
                        "SessionName": "Lecture 1",
                        "Duration": 60,
                        "Streams": [{"StreamUrl": "https://stream.invalid/private"}],
                        "AvailableCaptions": [{"Language": "en-AU"}],
                    },
                },
                "captions": [{
                    "language": "en-AU",
                    "content": [{
                        "Caption": "Welcome",
                        "Time": 0,
                        "CaptionDuration": 1.5,
                        "UserKey": "private-key",
                    }],
                }],
            }
            result = panopto_transcripts.save_transcripts(link, data, site_config=EXAMPLE_SITE)
            delivery = json.loads((Path(result["out_dir"]) / "delivery.json").read_text(encoding="utf-8"))
            source = json.loads((Path(result["out_dir"]) / "source.json").read_text(encoding="utf-8"))
            captions = json.loads((Path(result["out_dir"]) / "captions_en-AU.json").read_text(encoding="utf-8"))
            serialized = json.dumps(delivery)

            self.assertEqual(delivery["kind"], "panopto-recording-summary")
            self.assertNotIn("User", serialized)
            self.assertNotIn("Url", serialized)
            self.assertNotIn("secret", serialized)
            self.assertNotIn("token=secret", source["source_url"])
            self.assertEqual(set(captions[0]), {"Caption", "Time", "CaptionDuration"})


class BrowserLeaseTests(unittest.TestCase):
    def test_borrowed_session_is_not_closed(self) -> None:
        # A non-owning (borrowed) session must not send a close command for
        # the underlying tab when close() is called.
        with mock.patch.object(browser_backend._KimiImpl, "close", side_effect=AssertionError("borrowed tab must not be closed")):
            session = browser_backend.BrowserSession(
                session="fixture", backend="kimi", borrowed=True
            )
            session.close()  # must not raise or close the tab
