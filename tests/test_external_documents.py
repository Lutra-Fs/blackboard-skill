from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from blackboard.sync_models import Attachment, Page
from blackboard import external_documents as ed
from site_fixture import EXAMPLE_SITE


class UrlClassificationTests(unittest.TestCase):
    def test_is_external_document_url_classification(self) -> None:
        accept = (
            "https://www.birmingham.ac.uk/Documents/stem/getting-started.pdf",
            "https://example.com/report.docx",
            "https://example.com/slides.pptx",
            "https://example.com/data.xlsx",
            "https://example.com/book.epub",
        )
        for url in accept:
            self.assertTrue(ed.is_external_document_url(url, site_config=EXAMPLE_SITE), url)
        reject = (
            # Plain HTTP is never an external document source.
            "http://library.example.edu/x.pdf",
            # Hosts owned by other pipeline stages.
            "https://www.youtube.com/watch?v=x",
            "https://www.ted.com/talks/x",
            "https://learn.example.edu/bbcswebdav/x.pdf",
            "https://example.au.panopto.com/x.pdf",
            "https://example.leganto.exlibrisgroup.com/x.pdf",
            # Non-document extensions / no path.
            "https://example.com/page.html",
            "https://example.com/page",
            "https://example.com/",
        )
        for url in reject:
            self.assertFalse(ed.is_external_document_url(url, site_config=EXAMPLE_SITE), url)


class FilenameTests(unittest.TestCase):
    def test_filename_from_url_or_link_text(self) -> None:
        self.assertEqual(
            ed.filename_from_url("https://example.com/path/getting-started.pdf", None),
            "getting-started.pdf",
        )
        self.assertEqual(
            ed.filename_from_url("https://example.com/path/", "My Report"),
            "My Report.pdf",
        )


class LinkDiscoveryTests(unittest.TestCase):
    def _build_course(self, tmp: Path):
        course_root = tmp / "TEAC7116"
        pages = [
            Page('_9001_1', 'Resources', None, None, 0, ['Resources'], '', '',
                 external_links=[
                     {'text': '', 'href': 'https://www.birmingham.ac.uk/Documents/stem/getting-started.pdf'},
                     {'text': 'Milgram', 'href': 'https://www.youtube.com/watch?v=O1VOZhwRvWo'}]),
            Page('_9003_1', 'Resources', None, None, 0, ['Resources'], '', '',
                 attachments=[Attachment('Surrey', 'https://sru.soc.surrey.ac.uk/SRU56.pdf')],
                 external_links=[{'text': '', 'href': 'https://www.birmingham.ac.uk/Documents/stem/getting-started.pdf'}]),
            Page('_assessment_1', 'Resources', None, None, 0, ['Resources'], '', '',
                 child_links=[{'text': 'Surrey', 'href': 'https://sru.soc.surrey.ac.uk/SRU56.pdf'}]),
        ]
        tree = {'course_id': '_9008_1', 'nodes': [
            {'id': page.content_id, 'normalized_bucket': bucket}
            for page, bucket in zip(pages, ('12_Week_02', '13_Week_03', '50_Assessments/Assessment 1'))
        ]}
        return course_root, tree, pages

    def test_dedup_keeps_earliest_week_and_excludes_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            course_root, tree, pages = self._build_course(Path(tmpdir))
            docs = ed.iter_external_documents(course_root, tree=tree, pages=pages, site_config=EXAMPLE_SITE)
            urls = sorted(d.url for d in docs)
            # YouTube excluded; birmingham deduped to 1 (Week 2).
            self.assertEqual(len(docs), 2)
            birmingham = [d for d in docs if "birmingham" in d.url][0]
            self.assertEqual(birmingham.normalized_bucket, "12_Week_02")
            surrey = next(d for d in docs if "surrey" in d.url)
            self.assertEqual(surrey.normalized_bucket, "50_Assessments/Assessment 1")
            self.assertIn("https://sru.soc.surrey.ac.uk/SRU56.pdf", urls)
            self.assertNotIn("https://www.youtube.com/watch?v=O1VOZhwRvWo", urls)


class DryRunTests(unittest.TestCase):
    def test_dry_run_emits_planned_without_downloading(self) -> None:
        from blackboard import runtime

        runner = runtime.CommandRunner()
        doc = ed.ExternalDoc(
            "https://example.com/report.pdf",
            course_root=Path("/tmp/nonexistent-course"),
            normalized_bucket="12_Week_02",
            link_text="Report",
        )
        result = ed.download_document(doc, runner=runner, dry_run=True)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["bucket"], "12_Week_02")
        self.assertIn("destination", result)


if __name__ == "__main__":
    unittest.main()
