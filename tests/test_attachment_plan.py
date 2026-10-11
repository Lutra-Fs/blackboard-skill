from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from blackboard.attachment_plan import plan_unique_attachments
from blackboard.sync_models import Attachment, Page


def _page(content_id: str, attachments: list[Attachment]) -> Page:
    return Page(
        content_id=content_id,
        title="Doc",
        parent_title="Content",
        parent_id="ultra-outline",
        depth=1,
        breadcrumbs=["Content", "Doc"],
        url=f"https://learn.example.edu/ultra/courses/_1_1/outline/document/{content_id}",
        body_text="body",
        attachments=list(attachments),
    )


def _bucket(page: Page) -> str:
    return "10_Content"


class AttachmentPlanSourceKindTests(unittest.TestCase):
    """LTI launch URLs and external links are not downloadable files; they must
    not enter the download plan. Real file attachments are unaffected."""

    def test_mixed_page_only_plans_real_attachments(self) -> None:
        real = Attachment(
            text="real.pdf",
            href="https://learn.example.edu/bbcswebdav/pid-3/real.pdf",
            source_kind="rest-file-attachment",
        )
        lti = Attachment(
            text="Panopto",
            href="https://example.au.panopto.com/Panopto/LTI/LTI.aspx",
            source_kind="rest-lti-launch",
        )
        ext = Attachment(
            text="TED",
            href="https://www.ted.com/talks/x",
            source_kind="rest-external-link",
        )
        body_link = Attachment(text="body.pdf", href="https://learn.example.edu/bbcswebdav/pid-4/body.pdf")
        plans = plan_unique_attachments([_page("_1_1", [real, lti, ext, body_link])], _bucket)
        self.assertEqual([plan.attachment.href for plan in plans], [real.href, body_link.href])


class AttachmentPlanTests(unittest.TestCase):
    def test_week_destination_wins_over_admin_for_same_url(self) -> None:
        href = "https://learn.example.edu/bbcswebdav/shared/file.pdf"
        attachment = Attachment(text="file.pdf", href=href)
        admin = Page(
            content_id="admin",
            title="Subject Information",
            parent_title=None,
            parent_id=None,
            depth=0,
            breadcrumbs=["Subject Information"],
            url="https://learn.example.edu/admin",
            body_text="",
            attachments=[attachment],
        )
        week = Page(
            content_id="week",
            title="Resources",
            parent_title="Week 3",
            parent_id="module",
            depth=1,
            breadcrumbs=["Week 3", "Resources"],
            url="https://learn.example.edu/week",
            body_text="",
            attachments=[attachment],
        )
        plans = plan_unique_attachments([admin, week], lambda page: page.bucket)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].destination, Path("13_Week_03/file.pdf"))


if __name__ == "__main__":
    unittest.main()
