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
from blackboard import course_service as service
from blackboard import providers
from blackboard.cli import main
from blackboard.resource_store import Store
from blackboard.site_config import SiteConfig
from blackboard.sync_models import Page, Attachment
from blackboard.ultra_content import canonical_ultra_course_url


class AgentRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.site = SiteConfig(origin='https://learn.example.edu', panopto_origin='https://video.example.edu')
        self.page = Page('_10_1', 'Week 1', None, None, 0, ['Week 1'],
                         self.site.origin + '/ultra/courses/_10_1/outline', 'Teacher evidence',
                         [Attachment('reading.pdf', self.site.origin + '/bbcswebdav/xid-1_1')])
        self.publish()

    def publish(self):
        self.snapshot = service.capture(self.root, '_10_1', None,
                                        pages=[self.page], source_status={'rest': {}}, site_config=self.site)['snapshot']

    def failed_resource(self, kind, url):
        with Store(self.root, create=True) as store:
            key = store.asset(kind, url)
            store.finish(store.start(key, self.snapshot), 'failed', {'error': 'Prior failure'})
        return key

    def invoke(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([*argv, '--root', str(self.root)])
        return code, json.loads(out.getvalue())

    def test_metadata_only_capture_completes_without_file_coverage(self):
        session = SimpleNamespace(transport=lambda: (MagicMock(), MagicMock()), close=MagicMock())
        crawler = SimpleNamespace(crawl=lambda: [self.page], rest_status={})
        with patch.object(service, 'Session', return_value=session), \
             patch.object(service, 'RestCrawler', return_value=crawler), \
             patch.object(service, 'fetch', side_effect=AssertionError('Metadata-only request')):
            code, captured = self.invoke('capture', '_10_1')
        self.assertEqual(code, 0, captured)
        self.assertTrue(captured['data']['published'])
        self.assertTrue(captured['data']['crawl']['complete'])
        self.assertNotIn('coverage', captured['data'])
        session.close.assert_called_once()

        code, checked = self.invoke('validate')
        self.assertEqual(code, 1, checked)
        self.assertTrue(checked['data']['metadata_ok'])
        self.assertEqual(checked['data']['issues'], [])
        self.assertFalse(checked['data']['coverage']['complete'])

    def test_archive_inspection_needs_no_browser_and_preserves_state(self):
        paths = [self.root / '_blackboard' / name for name in ('course-tree.json', 'state.json')]
        before = [path.read_bytes() for path in paths]
        with patch.object(service, 'Session', side_effect=AssertionError('Offline inspection')):
            for command, expected in [(('status',), 0), (('files',), 0),
                                      (('validate',), 1), (('change', 'diff'), 0)]:
                with self.subTest(command=command):
                    code, payload = self.invoke(*command)
                    self.assertEqual(code, expected, payload)
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_reported_discovery_gap_is_a_targeted_retry_not_an_input_error(self):
        url = canonical_ultra_course_url('_10_1', site_config=self.site)
        key = self.failed_resource('panopto-discovery', url)
        self.assertIn(key, {g['asset_id'] for g in service.status(self.root)['gaps']})
        recording_url = self.site.panopto_origin + '/Panopto/Pages/Viewer.aspx?id=00000000-0000-0000-0000-000000000002'
        recording = providers.pan.PanoptoLink(recording_url, course_root=self.root)
        session = SimpleNamespace(transport=lambda: (MagicMock(), MagicMock()), close=MagicMock())
        with patch.object(service, 'Session', return_value=session), \
             patch.object(providers.pan, 'discover_course_menu_panopto_links', return_value=[recording]) as discover:
            code, payload = self.invoke('fetch', '--asset', key)
        self.assertEqual(code, 0, payload)
        discover.assert_called_once()
        session.close.assert_called_once()
        self.assertEqual([r['asset_id'] for r in payload['data']['results']], [key])
        self.assertNotIn(key, {g['asset_id'] for g in payload['data']['gaps']})
        self.assertTrue(any(g['kind'] == 'attachments' for g in payload['data']['gaps']))
        recording_key = providers.asset_id('panopto', recording_url)
        self.assertIn(recording_key, {g['asset_id'] for g in payload['data']['gaps']})
        with patch.object(service, 'Session', side_effect=AssertionError('Preview is offline')):
            code, preview = self.invoke('fetch', '--asset', recording_key, '--dry-run')
        self.assertEqual(code, 0, preview)

    def test_discovery_retry_failure_remains_partial_and_closes_session(self):
        from blackboard.safe_fetch import FetchError
        key = self.failed_resource('panopto-discovery', canonical_ultra_course_url('_10_1', site_config=self.site))
        session = SimpleNamespace(transport=lambda: (MagicMock(), MagicMock()), close=MagicMock())
        with patch.object(service, 'Session', return_value=session), \
             patch.object(providers.pan, 'discover_course_menu_panopto_links', side_effect=FetchError('Menu unavailable')):
            code, payload = self.invoke('fetch', '--asset', key)
        self.assertEqual(code, 1, payload)
        self.assertEqual(payload['data']['results'][0]['asset_id'], key)
        self.assertEqual(payload['data']['results'][0]['status'], 'failed')
        session.close.assert_called_once()

    def test_selected_registered_recording_can_retry_after_source_withdrawal(self):
        self.page = replace(self.page, attachments=[])
        self.publish()
        key = self.failed_resource('panopto', self.site.panopto_origin +
                                   '/Panopto/Pages/Viewer.aspx?id=00000000-0000-0000-0000-000000000001')
        browser = MagicMock()
        session = SimpleNamespace(transport=lambda: (browser, MagicMock()), close=MagicMock())
        with patch.object(service, 'Session', return_value=session), \
             patch.object(providers.pan, 'discover_course_menu_panopto_links', return_value=[]), \
             patch.object(providers.pan, 'save_transcripts', return_value={'status': 'failed', 'error': 'Recording unavailable'}), \
             patch.object(providers.time, 'sleep'):
            code, payload = self.invoke('fetch', '--asset', key)
        self.assertEqual(code, 1, payload)
        self.assertEqual(payload['data']['results'][0]['asset_id'], key)
        browser.open.assert_called_once()
        session.close.assert_called_once()

    def test_post_discovery_failure_always_closes_owned_session(self):
        key = self.failed_resource('panopto', self.site.panopto_origin +
                                   '/Panopto/Pages/Viewer.aspx?id=00000000-0000-0000-0000-000000000001')
        session = SimpleNamespace(transport=lambda: (MagicMock(), MagicMock()), close=MagicMock())
        with patch.object(service, 'Session', return_value=session), \
             patch.object(providers.pan, 'discover_course_menu_panopto_links', return_value=[]), \
             patch.object(providers.REGISTRY['panopto'], 'build', side_effect=OSError('Cannot prepare resource')):
            code, payload = self.invoke('fetch', '--asset', key)
        self.assertNotEqual(code, 0, payload)
        session.close.assert_called_once()

    def test_discovery_auth_failure_stops_before_preparing_recordings(self):
        from blackboard.browser_backend import SessionRequired
        key = self.failed_resource('panopto', self.site.panopto_origin +
                                   '/Panopto/Pages/Viewer.aspx?id=00000000-0000-0000-0000-000000000001')
        session = SimpleNamespace(transport=lambda: (MagicMock(), MagicMock()), close=MagicMock())
        with patch.object(service, 'Session', return_value=session), \
             patch.object(providers.pan, 'discover_course_menu_panopto_links', side_effect=SessionRequired('Sign in')), \
             patch.object(providers.REGISTRY['panopto'], 'build') as build:
            code, payload = self.invoke('fetch', '--asset', key)
        self.assertEqual(code, 4, payload)
        self.assertEqual(payload['data']['outcome'], 'needs-action')
        self.assertTrue(payload['data']['results'][0]['needs_action'])
        build.assert_not_called()
        session.close.assert_called_once()

    def test_ordinary_course_has_no_phantom_optional_integrations(self):
        self.site = SiteConfig(origin='http://192.168.1.10:8080')
        # A fresh archive is needed because origin identity is immutable.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.page = replace(self.page, attachments=[], url=self.site.origin + '/ultra/courses/_10_1/outline')
        self.publish()
        with patch.object(service, 'Session', side_effect=AssertionError('No browser needed')):
            code, payload = self.invoke('fetch')
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload['data']['coverage']['complete'])
        self.assertEqual(payload['data']['results'], [])
        code, payload = self.invoke('fetch', '--kind', 'panopto')
        self.assertEqual(code, 0)
        self.assertEqual(payload['data']['results'][0]['reason'], 'site-config-missing-panopto-origin')

    def test_evidenced_unconfigured_reading_list_remains_visible(self):
        self.site = SiteConfig(origin=self.site.origin)
        self.page = replace(self.page, external_links=[{'kind': 'lti_launch', 'handle': 'leganto_reading_list',
                                     'text': 'Reading List', 'href': self.site.origin +
                                     '/webapps/blackboard/execute/blti/launchPlacement?course_id=_10_1'}], attachments=[])
        self.publish()
        with patch.object(service, 'Session', side_effect=AssertionError('No browser needed')):
            code, payload = self.invoke('fetch')
        self.assertEqual(code, 0, payload)
        self.assertEqual([r['asset_id'] for r in payload['data']['results']], ['reading-list:unconfigured'])
        self.assertTrue(any(g['kind'] == 'reading-list' for g in payload['data']['gaps']))

    def test_unconfigured_panopto_reports_addressable_discovery_gap(self):
        self.site = SiteConfig(origin=self.site.origin)
        self.page = replace(self.page, attachments=[], external_links=[{
            'kind': 'lti_launch', 'handle': 'panopto', 'text': 'Recordings',
            'href': self.site.origin + '/webapps/blackboard/execute/blti/launchPlacement?course_id=_10_1'}])
        self.publish()
        with patch.object(service, 'Session', side_effect=AssertionError('No browser needed')):
            code, payload = self.invoke('fetch')
            gap = payload['data']['gaps'][0]
            self.assertEqual(gap['kind'], 'panopto-discovery')
            self.assertEqual(gap['source_pages'][0]['id'], self.page.content_id)
            self.assertFalse(payload['data']['coverage']['complete'])
            self.assertEqual(payload['data']['coverage']['expected_resources'], 1)
            self.assertEqual(payload['data']['results'][0]['asset_id'], gap['asset_id'])
            code, selected = self.invoke('fetch', '--asset', gap['asset_id'])
            self.assertEqual(code, 0, selected)
            plan = {'version': 1, 'base_snapshot': self.snapshot, 'resources': [{
                'asset_id': gap['asset_id'], 'decision': 'exclude', 'reason': 'Outside requested scope'}]}
            path = self.root / 'plan.json'
            path.write_text(json.dumps(plan))
            code, applied = self.invoke('change', 'apply', str(path))
            self.assertEqual(code, 0, applied)
            code, selected = self.invoke('fetch', '--asset', gap['asset_id'])
            self.assertEqual(code, 0, selected)
            self.assertTrue(selected['data']['coverage']['complete'])

    def test_public_documents_and_captions_need_no_browser_backend(self):
        self.page = replace(self.page, attachments=[], external_links=[
            {'href': 'https://public.example.edu/reading.pdf', 'text': 'Reading'},
            {'href': 'https://www.youtube.com/watch?v=abcdefghijk', 'text': 'Lecture'}])
        self.publish()
        def document(doc, **kwargs):
            (doc.course_root / 'reading.pdf').write_bytes(b'%PDF-1.4\nReading\n%%EOF\n')
            return {'status': 'downloaded'}
        def transcript(link, meta, **kwargs):
            (link.course_root / 'transcript.vtt').write_text('WEBVTT\n')
            return {'status': 'downloaded'}
        with patch.object(service, 'BrowserSession', side_effect=AssertionError('Public acquisition')), \
             patch.object(providers.ext, 'download_document', side_effect=document) as download, \
             patch.object(providers.yt, 'fetch_video_metadata', return_value={'title': 'Lecture'}), \
             patch.object(providers.yt, 'save_transcripts', side_effect=transcript) as captions:
            for kind in ('external-docs', 'youtube'):
                code, payload = self.invoke('fetch', '--kind', kind)
                self.assertEqual(code, 0, payload)
                self.assertEqual(payload['data']['results'][0]['status'], 'downloaded')
        download.assert_called_once()
        captions.assert_called_once()

    def test_interrupt_hint_replays_capture_or_resumes_published_sync(self):
        session = SimpleNamespace(transport=lambda: (MagicMock(), MagicMock()), close=MagicMock())
        with patch.object(service, 'Session', return_value=session), \
             patch.object(service, 'capture', side_effect=KeyboardInterrupt):
            code, payload = self.invoke('capture', '_10_1')
        self.assertEqual(code, 130)
        self.assertIn(' capture _10_1 ', payload['errors'][0]['solution'])
        with patch.object(service, 'Session', return_value=session), \
             patch.object(service, 'capture', return_value={'outcome': 'complete', 'published': True}), \
             patch.object(service, 'fetch', side_effect=KeyboardInterrupt):
            code, payload = self.invoke('sync', '_10_1', '--kind', 'attachments', '--refresh')
        self.assertEqual(code, 130)
        self.assertIn(' fetch ', payload['errors'][0]['solution'])
        self.assertIn('--kind attachments', payload['errors'][0]['solution'])
        self.assertNotIn('--refresh', payload['errors'][0]['solution'])

    def test_fetch_reuses_verified_snapshot_and_static_inventory(self):
        with patch.object(service, 'verify_snapshot', wraps=service.verify_snapshot) as verify, \
             patch.object(providers, 'plan_resources', wraps=providers.plan_resources) as plan:
            code, payload = self.invoke('fetch', '--kind', 'attachments', '--dry-run')
        self.assertEqual(code, 0, payload)
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(plan.call_count, 1)

    def test_inventory_reads_each_page_artifact_once(self):
        recording_url = self.site.panopto_origin + '/Panopto/Pages/Viewer.aspx?id=00000000-0000-0000-0000-000000000002'
        self.page = replace(self.page,
                            attachments=self.page.attachments + [Attachment('Recording', recording_url)],
                            external_links=[{'href': 'https://example.com/reading.pdf'},
                                            {'href': 'https://www.youtube.com/watch?v=abcdefghijk'}],
                            child_links=[{'href': recording_url}])
        self.publish()
        reads = []
        original = Path.read_text
        def record(path, *args, **kwargs):
            if path.name in ('source.json', 'links.json'):
                reads.append(path.name)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', record), \
             patch.object(service, 'Session', side_effect=AssertionError('Preview is offline')):
            code, payload = self.invoke('fetch', '--dry-run')
        self.assertEqual(code, 0, payload)
        self.assertEqual(reads.count('source.json'), 1)
        self.assertEqual(reads.count('links.json'), 1)
        results = payload['data']['results']
        self.assertEqual([row['kind'] for row in results], ['attachments', 'external-docs', 'youtube', 'panopto'])
        self.assertEqual(results[-1]['url'], recording_url)
        for gap in payload['data']['gaps']:
            self.assertEqual(gap['source_pages'][0]['id'], self.page.content_id)


if __name__ == '__main__':
    unittest.main()
