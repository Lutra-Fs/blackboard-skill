from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
SCRIPT_DIR = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0,str(SCRIPT_DIR))
from blackboard.archive_io import ArchiveIntegrityError
from blackboard.archive_repository import ArchiveState, MetadataWriter
from blackboard import archive_repository

SCRIPTS = SCRIPT_DIR
from blackboard.course_lock import CourseLock
from site_fixture import EXAMPLE_SITE
from blackboard.sync_models import Page


class CourseLockTests(unittest.TestCase):
    def test_second_process_fails_fast_and_lock_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with CourseLock(root, operation="primary", course_id="_1_1", run_id="run-primary"):
                code = """
import sys
from blackboard.course_lock import CourseLock, LockBusyError
try:
    with CourseLock(sys.argv[1], operation='contender', timeout=0):
        raise SystemExit(0)
except LockBusyError as exc:
    assert exc.owner.get('operation') == 'primary'
    raise SystemExit(75)
"""
                env = dict(os.environ)
                env["PYTHONPATH"] = str(SCRIPTS)
                result = subprocess.run(
                    [sys.executable, "-c", code, str(root)],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 75, result.stderr)

            with CourseLock(root, operation="after-release", timeout=0):
                self.assertTrue((root / "_blackboard/.sync/writer.lock").exists())

    def test_exception_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with CourseLock(root, operation="failing"):
                    raise RuntimeError("boom")
            with CourseLock(root, operation="recovery", timeout=0):
                pass

    def test_stale_owner_is_diagnostic_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            owner = root / "_blackboard/.sync/owner.json"
            owner.parent.mkdir(parents=True)
            owner.write_text('{"operation":"stale","pid":999999}', encoding="utf-8")
            with CourseLock(root, operation="fresh", timeout=0):
                data = json.loads(owner.read_text(encoding="utf-8"))
                self.assertEqual(data["operation"], "fresh")

    def test_symlink_lock_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(temp_dir)
            sync_dir = root / "_blackboard/.sync"
            sync_dir.mkdir(parents=True)
            (sync_dir / "writer.lock").symlink_to(Path(outside_dir) / "external.lock")
            with self.assertRaises(ArchiveIntegrityError):
                CourseLock(root, operation="unsafe").acquire()

    def test_different_courses_can_lock_independently(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with CourseLock(first, operation="first"), CourseLock(second, operation="second"):
                self.assertTrue(Path(first, "_blackboard/.sync/owner.json").is_file())
                self.assertTrue(Path(second, "_blackboard/.sync/owner.json").is_file())


class MetadataGenerationTests(unittest.TestCase):
    @staticmethod
    def page(content_id: str, title: str = "Week 1") -> Page:
        return Page(
            content_id=content_id,
            title=title,
            parent_title=None,
            parent_id=None,
            depth=0,
            breadcrumbs=[title],
            url=f"https://learn.example.edu/ultra/courses/_1_1/outline/{content_id}",
            body_text=f"Body for {title}",
        )

    def test_tree_points_to_complete_immutable_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = ArchiveState(root)
            writer = MetadataWriter(state, "_1_1", run_id="run-one", site_config=EXAMPLE_SITE)
            result = writer.write_page_artifacts(
                [self.page("node-one")],
                {"status": "complete", "safe_to_replace": True},
            )

            self.assertEqual(result, (1, 0, True))
            tree = json.loads((root / "_blackboard/course-tree.json").read_text(encoding="utf-8"))
            self.assertEqual(tree["run_id"], "run-one")
            page_dir = root / tree["nodes"][0]["page_dir"]
            self.assertEqual(page_dir, root / "_blackboard/generations/run-one/pages/node-one")
            self.assertTrue((page_dir / "page.md").is_file())
            self.assertTrue((page_dir / "links.json").is_file())
            self.assertTrue((page_dir / "source.json").is_file())
            self.assertFalse((root / "_blackboard/generations/.run-one.staging").exists())

    def test_description_and_due_date_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = ArchiveState(root)
            page = self.page("assessment", "Lesson Plan")
            page = Page(**{
                **page.__dict__,
                "body_text": "",
                "description_text": "Write a lesson plan.",
                "due_at": "2026-09-11T13:59:00.000Z",
            })
            writer = MetadataWriter(state, "_1_1", run_id="run-description", site_config=EXAMPLE_SITE)
            writer.write_page_artifacts([page], {"status": "complete", "safe_to_replace": True})

            loaded = state.load_published_pages()[0]
            self.assertEqual(loaded.description_text, "Write a lesson plan.")
            self.assertEqual(loaded.due_at, "2026-09-11T13:59:00.000Z")
            markdown = (root / "_blackboard/generations/run-description/pages/assessment/page.md").read_text(encoding="utf-8")
            self.assertIn("## Description", markdown)
            self.assertIn("## Blackboard Metadata", markdown)
            self.assertIn("Due (Australia/Sydney): `2026-09-11 23:59 AEST`", markdown)
            self.assertIn("Due (UTC source): `2026-09-11T13:59:00.000Z`", markdown)

    def test_deferred_generation_keeps_old_tree_until_explicit_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blackboard = root / "_blackboard"
            blackboard.mkdir()
            old_tree = {"schema_version": 2, "course_id": "_1_1", "run_id": "old-run", "nodes": []}
            tree_path = blackboard / "course-tree.json"
            tree_path.write_text(json.dumps(old_tree), encoding="utf-8")
            state = ArchiveState(root)
            writer = MetadataWriter(state, "_1_1", run_id="new-run", site_config=EXAMPLE_SITE)

            writer.write_page_artifacts(
                [self.page("new-node")],
                {"status": "complete", "safe_to_replace": True},
                defer_publish=True,
            )

            self.assertEqual(json.loads(tree_path.read_text(encoding="utf-8")), old_tree)
            prepared = state.load_generation_tree("new-run", expected_run_id="new-run")
            self.assertEqual(prepared["nodes"][0]["id"], "new-node")
            writer.publish_generation("new-run")
            self.assertEqual(json.loads(tree_path.read_text(encoding="utf-8"))["run_id"], "new-run")

    def test_generation_tree_cannot_mix_page_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = ArchiveState(root)
            writer = MetadataWriter(state, "_1_1", run_id="new-run", site_config=EXAMPLE_SITE)
            writer.write_page_artifacts(
                [self.page("new-node")],
                {"status": "complete", "safe_to_replace": True},
                defer_publish=True,
            )
            tree = state.load_generation_tree("new-run", expected_run_id="new-run")
            tree["nodes"][0]["page_dir"] = "_blackboard/generations/other-run/pages/new-node"

            with self.assertRaisesRegex(ArchiveIntegrityError, "mixes metadata generations"):
                state.load_tree_pages(tree, expected_run_id="new-run")

    def test_tree_publish_failure_preserves_previous_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blackboard = root / "_blackboard"
            blackboard.mkdir()
            old_tree = {"schema_version": 2, "course_id": "_1_1", "run_id": "old-run", "nodes": []}
            tree_path = blackboard / "course-tree.json"
            tree_path.write_text(json.dumps(old_tree), encoding="utf-8")
            state = ArchiveState(root)
            writer = MetadataWriter(state, "_1_1", run_id="run-two", site_config=EXAMPLE_SITE)
            real_write = archive_repository.atomic_write_json

            def fail_tree(path: Path, data: object) -> None:
                if Path(path).resolve() == tree_path.resolve():
                    raise OSError("injected tree publish failure")
                real_write(path, data)

            with mock.patch.object(archive_repository, "atomic_write_json", side_effect=fail_tree):
                with self.assertRaisesRegex(OSError, "injected tree publish failure"):
                    writer.write_page_artifacts(
                        [self.page("node-two")],
                        {"status": "complete", "safe_to_replace": True},
                    )

            self.assertEqual(json.loads(tree_path.read_text(encoding="utf-8")), old_tree)
            self.assertTrue((root / "_blackboard/generations/run-two/pages/node-two/source.json").is_file())
