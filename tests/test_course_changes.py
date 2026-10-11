from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from blackboard import course_service as svc
from blackboard.archive_io import ArchiveIntegrityError
from blackboard.cli import main, build_parser
from blackboard.cli_requests import FetchRequest, UsageError
from blackboard.content_rules import sha256_file
from blackboard.course_changes import apply_plan, change_diff, ChangeStale
from blackboard.resource_store import Store, asset_id
from site_fixture import EXAMPLE_SITE
from blackboard.sync_models import Page, Attachment

URL = EXAMPLE_SITE.origin + '/bbcswebdav/xid-123_1'
PDF = b'%PDF-1.4\nexample\n%%EOF\n'


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.page = Page('_1_1', 'Week 1', None, None, 0, ['Week 1'],
                         EXAMPLE_SITE.origin + '/ultra/courses/_1_1/outline', 'Body',
                         [Attachment('reading.pdf', URL)], [], [], [])
        self.capture()
        self.key = asset_id('attachments', URL)

    def capture(self, page=None, errors=None):
        return svc.capture(self.root, '_1_1', None,
                           pages=[page or self.page], source_status={'rest': {'errors': errors or []}},
                           site_config=EXAMPLE_SITE)

    def plan(self, decision=None, files=None):
        result = {'version': 1, 'base_snapshot': svc.status(self.root)['snapshot']}
        if decision:
            result['resources'] = [{'asset_id': self.key, 'decision': decision, 'reason': 'Reviewed source evidence'}]
        if files:
            result['files'] = files
        return result

    def register(self, path='old.pdf', body=PDF, url=URL):
        with Store(self.root, create=True) as db:
            key = db.asset('attachments', url)
            attempt = db.start(key, svc.status(self.root)['snapshot'])
            staged = self.root / ('stage-' + path)
            staged.write_bytes(body)
            db.prepare(attempt, [(staged, Path(path))])
            db.publish(attempt)
        return {'path': path, 'sha256': sha256_file(self.root / path)}

    def dispositions(self, action='remove_old', url=URL):
        old = self.register()
        new = self.register('new.pdf', PDF + b'new', url)
        return [{'old': old, 'new': new, 'action': action, 'reason': 'New file includes all old material'}]

    def state_bytes(self):
        return (self.root / '_blackboard/state.json').read_bytes()

    def invoke(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(argv + ['--root', str(self.root)])
        return code, json.loads(out.getvalue())

    def test_removed_commands_and_strict_flag_are_invalid(self):
        for command in ('doctor', 'diff', 'accept', 'ignore', 'unignore', 'defer', 'undefer'):
            with self.subTest(command=command), self.assertRaises(UsageError):
                build_parser().parse_args([command])
        with self.assertRaises(UsageError):
            build_parser().parse_args(['validate', '--root', str(self.root), '--strict'])

    def test_change_cli_leaf_help_and_json_command(self):
        code, payload = self.invoke(['change', 'diff'])
        self.assertEqual(code, 0)
        self.assertEqual(payload['command'], 'change diff')
        self.assertIn('base_snapshot', payload['data'])
        for action in ('diff', 'apply'):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exc:
                build_parser().parse_args(['change', action, '--help'])
            self.assertEqual(exc.exception.code, 0)

    def test_unavailable_is_offline_success_with_an_explicit_gap(self):
        with Store(self.root, create=True) as db:
            db.asset('attachments', URL)
            db.finish(db.start(self.key, 'snapshot'), 'failed', {'error': 'HTTP 404'})
        apply_plan(self.root, self.plan('unavailable'))
        with patch.object(svc, 'Session', side_effect=AssertionError('must stay offline')):
            code, payload = self.invoke(['fetch', '--asset', self.key, '--refresh'])
        self.assertEqual(code, 0)
        self.assertEqual(payload['errors'], [])
        data = payload['data']
        self.assertFalse(data['coverage']['complete'])
        self.assertEqual(data['gaps'][0]['decision'], 'unavailable')
        self.assertEqual(data['gaps'][0]['last_error']['message'], 'HTTP 404')
        self.assertEqual(data['gaps'][0]['last_error']['error_code'], 'download.failed')
        self.assertEqual(data['gaps'][0]['source_pages'][0]['title'], 'Week 1')
        self.assertFalse(svc.validate(self.root)['ok'])

    def test_exclude_is_not_a_gap_and_fetch_restores_default(self):
        apply_plan(self.root, self.plan('exclude'))
        data = svc.status(self.root)
        self.assertEqual(data['gaps'], [])
        self.assertEqual(data['excluded'][0]['asset_id'], self.key)
        self.assertTrue(svc.validate(self.root)['ok'])
        apply_plan(self.root, self.plan('fetch'))
        self.assertEqual(svc.status(self.root)['gaps'][0]['decision'], 'fetch')
        self.assertFalse(svc.validate(self.root)['ok'])

    def test_decisions_preserve_files_and_acquisition_facts(self):
        self.register()
        before = json.loads(self.state_bytes())['resources'][self.key]
        for decision in ('exclude', 'unavailable', 'fetch'):
            apply_plan(self.root, self.plan(decision))
            row = json.loads(self.state_bytes())['resources'][self.key]
            self.assertEqual(row['files'], before['files'])
            self.assertEqual(row['success'], before['success'])
        self.assertTrue((self.root / 'old.pdf').exists())

    def test_fetch_decision_retries_only_on_a_later_fetch(self):
        apply_plan(self.root, self.plan('unavailable'))
        with patch.object(svc, 'Session', side_effect=AssertionError('apply is offline')):
            apply_plan(self.root, self.plan('fetch'))
        def download(url, dest, **kw):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(PDF)
            return SimpleNamespace(content_disposition=None, effective_url=url)
        fetcher = SimpleNamespace(download=MagicMock(side_effect=download))
        result = svc.fetch(self.root, FetchRequest(kinds=('attachments',)),
                           SimpleNamespace(transport=lambda: (None, fetcher)))
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(fetcher.download.call_count, 1)

    def test_new_failure_is_partial_and_requires_decision(self):
        def fail(*args, **kwargs):
            raise OSError('network offline')
        result = svc.fetch(self.root, FetchRequest(kinds=('attachments',)),
                           SimpleNamespace(transport=lambda: (None, SimpleNamespace(download=fail))))
        self.assertEqual(result['outcome'], 'partial')
        self.assertEqual(result['gaps'][0]['decision'], 'fetch')

    def test_plan_dry_run_and_invalid_batch_write_nothing(self):
        plan = self.plan('exclude')
        before = self.state_bytes()
        apply_plan(self.root, plan, dry_run=True)
        self.assertEqual(self.state_bytes(), before)
        for bad in ('decision', 'asset_id', 'reason'):
            broken = json.loads(json.dumps(plan))
            broken['resources'].append({**plan['resources'][0], bad: 'invalid' if bad != 'reason' else ''})
            with self.subTest(field=bad), self.assertRaises(UsageError):
                apply_plan(self.root, broken)
            self.assertEqual(self.state_bytes(), before)

    def test_stale_snapshot_is_actionable(self):
        plan = self.plan('exclude')
        self.capture(replace(self.page, body_text='Teacher revision'))
        path = self.root / 'plan.json'
        path.write_text(json.dumps(plan))
        code, payload = self.invoke(['change', 'apply', str(path)])
        self.assertEqual(code, 3)
        self.assertEqual(payload['errors'][0]['code'], 'blackboard.change_stale')
        self.assertIn('change diff', payload['errors'][0]['solution'])

    def test_pending_acquisition_blocks_plan_before_decisions(self):
        with Store(self.root, create=True) as db:
            db.asset('attachments', URL)
            db.start(self.key, 'snapshot')
        before = self.state_bytes()
        with self.assertRaises(ArchiveIntegrityError):
            apply_plan(self.root, self.plan('exclude'))
        self.assertEqual(self.state_bytes(), before)

    def test_remove_old_updates_inventory_and_keeps_fetch_reusable(self):
        entries = self.dispositions()
        apply_plan(self.root, self.plan(files=entries))
        self.assertFalse((self.root / 'old.pdf').exists())
        self.assertTrue((self.root / 'new.pdf').exists())
        with Store(self.root) as db:
            self.assertEqual(db.reusable(self.key)['files'], ['new.pdf'])
            self.assertEqual([f['path'] for f in db.files()], ['new.pdf'])
        self.assertTrue(svc.validate(self.root)['ok'])

    def test_keep_both_and_remove_dry_run_preserve_bytes(self):
        entries = self.dispositions('keep_both')
        apply_plan(self.root, self.plan(files=entries))
        before = self.state_bytes()
        entries[0]['action'] = 'remove_old'
        apply_plan(self.root, self.plan(files=entries), dry_run=True)
        self.assertEqual(self.state_bytes(), before)
        self.assertTrue((self.root / 'old.pdf').exists())

    def test_edited_file_or_changed_replacement_rejects_whole_plan(self):
        entries = self.dispositions()
        for path in ('old.pdf', 'new.pdf'):
            original = (self.root / path).read_bytes()
            (self.root / path).write_bytes(original + b'annotation')
            before = self.state_bytes()
            with self.subTest(path=path), self.assertRaises(ChangeStale):
                apply_plan(self.root, self.plan('exclude', files=entries))
            self.assertEqual(self.state_bytes(), before)
            (self.root / path).write_bytes(original)

    def test_path_escape_and_conflicting_file_changes_are_rejected(self):
        entries = self.dispositions()
        bad = json.loads(json.dumps(entries))
        bad[0]['old']['path'] = '../old.pdf'
        with self.assertRaises(UsageError):
            apply_plan(self.root, self.plan(files=bad))
        with self.assertRaises(UsageError):
            apply_plan(self.root, self.plan(files=entries + entries))
        inverse = {'old': entries[0]['new'], 'new': entries[0]['old'], 'action': 'remove_old', 'reason': 'Invalid cycle'}
        with self.assertRaises(UsageError):
            apply_plan(self.root, self.plan(files=entries + [inverse]))
        self.assertTrue((self.root / 'old.pdf').exists())

    def test_cross_resource_replacement_requires_scope_decision(self):
        entries = self.dispositions(url=URL + '-replacement')
        with self.assertRaises(UsageError):
            apply_plan(self.root, self.plan(files=entries))
        apply_plan(self.root, self.plan('exclude', files=entries))
        self.assertTrue(svc.validate(self.root)['ok'])

    def test_unlink_failure_reports_pending_and_fetch_recovers(self):
        entries = self.dispositions()
        original_unlink = Path.unlink
        def fail_old(path, *args, **kwargs):
            if path == (self.root / 'old.pdf').resolve():
                raise OSError('permission denied')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_old), self.assertRaises(OSError) as exc:
            apply_plan(self.root, self.plan(files=entries))
        self.assertEqual(len(exc.exception.data['pending']), 1)
        with Store(self.root) as db:
            self.assertEqual(len(db.cleanup_pending()), 1)
        apply_plan_error = self.plan('fetch')
        with self.assertRaises(ArchiveIntegrityError):
            apply_plan(self.root, apply_plan_error)
        # An excluded-only fetch must still settle pending file cleanup.
        with Store(self.root, create=True) as db:
            db.set_decision(self.key, 'exclude', 'Review complete', 'snapshot', kind='attachments', url=URL)
            db.save()
        svc.fetch(self.root, FetchRequest(assets=(self.key,)), None)
        self.assertFalse((self.root / 'old.pdf').exists())
        self.assertTrue(svc.validate(self.root)['ok'])

    def test_crash_after_unlink_before_state_save_is_recoverable(self):
        entries = self.dispositions()
        original_save = Store.save
        def fail_after_unlink(db):
            if not (self.root / 'old.pdf').exists():
                raise OSError('state write interrupted')
            original_save(db)
        with patch.object(Store, 'save', fail_after_unlink), self.assertRaises(OSError):
            apply_plan(self.root, self.plan(files=entries))
        with patch.object(svc, 'Session', side_effect=AssertionError('Cleanup is offline')):
            result = apply_plan(self.root, self.plan(files=entries))
        self.assertEqual(result['completed'], entries)
        self.assertTrue(svc.validate(self.root)['ok'])

    def test_partial_cleanup_reports_completed_and_pending_separately(self):
        entries = self.dispositions()
        entries.append({'old': self.register('second.pdf', PDF + b'old second'),
                        'new': self.register('second-new.pdf', PDF + b'new second'),
                        'action': 'remove_old', 'reason': 'Second replacement reviewed'})
        original_unlink = Path.unlink
        def fail_second(path, *args, **kwargs):
            if path == (self.root / 'second.pdf').resolve():
                raise OSError('second deletion failed')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_second), self.assertRaises(OSError) as exc:
            apply_plan(self.root, self.plan(files=entries))
        self.assertEqual(exc.exception.data['completed'], [entries[0]])
        self.assertEqual(exc.exception.data['pending'], [entries[1]])
        with patch.object(svc, 'Session', side_effect=AssertionError('Cleanup is offline')):
            result = apply_plan(self.root, self.plan(files=entries))
        self.assertEqual(result['completed'], entries)
        self.assertTrue(svc.validate(self.root)['ok'])

    def test_change_apply_retry_settles_cleanup_without_acquiring_other_gaps(self):
        entries = self.dispositions()
        plan = self.plan(files=entries)
        original_unlink = Path.unlink
        def fail_old(path, *args, **kwargs):
            if path == (self.root / 'old.pdf').resolve():
                raise OSError('permission denied')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_old), self.assertRaises(OSError) as exc:
            apply_plan(self.root, plan)
        self.assertIn('change apply', exc.exception.solution)
        self.assertNotIn('run fetch', exc.exception.solution)
        self.capture(replace(self.page, attachments=self.page.attachments + [Attachment('other.pdf', URL + '-other')]))
        plan['base_snapshot'] = svc.status(self.root)['snapshot']
        with patch.object(svc, 'Session', side_effect=AssertionError('Cleanup is offline')):
            result = apply_plan(self.root, plan)
        self.assertEqual(result['completed'], entries)
        self.assertFalse((self.root / 'old.pdf').exists())
        self.assertEqual(Store(self.root).cleanup_pending(), [])
        self.assertTrue(svc.status(self.root)['gaps'])

    def test_cleanup_retry_still_checks_reviewed_replacement(self):
        entries = self.dispositions()
        plan = self.plan(files=entries)
        with Store(self.root, create=True) as db:
            db.begin_cleanup(entries)
        (self.root / 'new.pdf').write_bytes(PDF + b'changed replacement')
        before = self.state_bytes()
        with self.assertRaises(ChangeStale):
            apply_plan(self.root, plan)
        self.assertEqual(self.state_bytes(), before)
        self.assertTrue((self.root / 'old.pdf').exists())

    def test_pending_cleanup_dry_run_is_blocked_and_does_not_recover(self):
        entries = self.dispositions()
        with Store(self.root, create=True) as db:
            db.begin_cleanup(entries)
        before = self.state_bytes()
        result = svc.fetch(self.root, FetchRequest(dry_run=True, kinds=('attachments',)), None)
        self.assertEqual(result['outcome'], 'blocked')
        self.assertEqual(self.state_bytes(), before)
        self.assertTrue((self.root / 'old.pdf').exists())

    def test_diff_unchanged_capture_and_malformed_plan(self):
        self.capture()
        diff = change_diff(self.root)
        self.assertEqual(diff['pages']['changed_nodes'], [])
        self.assertEqual(diff['pages']['added_nodes'], [])
        self.assertEqual(diff['pages']['removed'], [])
        self.assertEqual(svc.status(self.root)['crawl']['diff']['changed_nodes'], [])
        path = self.root / 'bad-plan.json'
        path.write_text('[]')
        code, payload = self.invoke(['change', 'apply', str(path)])
        self.assertEqual(code, 2)
        self.assertEqual(payload['errors'][0]['code'], 'invalid-input')

    def test_diff_reports_body_changes_local_edits_and_real_files(self):
        before = svc.status(self.root)['snapshot']
        self.register()
        (self.root / 'old.pdf').write_bytes(PDF + b'local note')
        self.capture(replace(self.page, body_text='Updated body'))
        diff = change_diff(self.root)
        self.assertEqual(diff['before_snapshot'], before)
        self.assertEqual(diff['pages']['changed_nodes'][0]['id'], '_1_1')
        self.assertTrue(diff['files'][0]['locally_modified'])
        self.assertTrue((self.root / diff['source_pages'][0]['path']).exists())

    def test_complete_withdrawal_preserves_registered_file(self):
        self.register()
        self.capture(replace(self.page, attachments=[]))
        self.assertTrue((self.root / 'old.pdf').exists())
        self.assertTrue(change_diff(self.root)['pages']['removed'])

    def test_legacy_ignore_and_defer_migrate_without_a_timer(self):
        self.register()
        state = json.loads(self.state_bytes())
        row = state['resources'][self.key]
        row['ignore'] = {'reason': 'Outside scope', 'snapshot': 'old', 'ignored_at': 'old'}
        row['defer'] = {'reason': 'Former temporary pause', 'until': '2099-01-01', 'source_fingerprint': 'obsolete'}
        path = self.root / '_blackboard/state.json'
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        with Store(self.root) as db:
            self.assertEqual(db.decision_info(self.key)['decision'], 'exclude')
        self.assertEqual(path.read_bytes(), before)
        apply_plan(self.root, self.plan('fetch'))
        row = json.loads(self.state_bytes())['resources'][self.key]
        self.assertNotIn('ignore', row)
        self.assertNotIn('defer', row)
        self.assertEqual(row['previous_retry_note'], 'Former temporary pause')

    def test_legacy_defer_alone_is_fetch_not_unavailable(self):
        self.register()
        state = json.loads(self.state_bytes())
        state['resources'][self.key]['defer'] = {'reason': 'Earlier failure'}
        (self.root / '_blackboard/state.json').write_text(json.dumps(state))
        with Store(self.root) as db:
            self.assertIsNone(db.decision_info(self.key))

    def test_cli_plan_preview_has_no_state_or_file_changes(self):
        path = self.root / 'plan.json'
        path.write_text(json.dumps(self.plan('exclude')))
        before = self.state_bytes()
        code, payload = self.invoke(['change', 'apply', str(path), '--dry-run'])
        self.assertEqual(code, 0)
        self.assertEqual(payload['command'], 'change apply')
        self.assertTrue(payload['data']['dry_run'])
        self.assertEqual(self.state_bytes(), before)


if __name__ == '__main__':
    unittest.main()
