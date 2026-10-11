from __future__ import annotations

import contextlib
import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from blackboard import cli
from blackboard import course_service as service
from blackboard import site_config
from blackboard.safe_fetch import HttpFetcher, FetchError, _LimitedRedirectHandler
from blackboard.sync_models import Page
from blackboard.url_policy import is_blackboard_url


class SiteEntryTests(unittest.TestCase):
    def test_public_launcher_and_recovery_hint_work_outside_skill_directory(self):
        launcher = Path(__file__).resolve().parents[1] / 'scripts/blackboard_cli.py'
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(launcher), 'status', '--root', directory],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            hint = shlex.split(json.loads(result.stdout)['data']['next_steps'][0])
            self.assertEqual(Path(hint[1]), launcher)
            help_result = subprocess.run(hint + ['--help'], cwd=directory, capture_output=True, text=True)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn('capture', help_result.stdout)

    def preview(self, course, *flags):
        with tempfile.TemporaryDirectory() as directory:
            crawler = MagicMock(rest_status={'errors': []})
            crawler.crawl.return_value = [Page('_1_1', 'Week', None, None, 0, ['Week'], '', 'Body')]
            session = MagicMock()
            session.transport.return_value = (None, None)
            output = io.StringIO()
            with patch.object(service, 'Session', return_value=session) as factory, \
                    patch.object(service, 'RestCrawler', return_value=crawler), \
                    contextlib.redirect_stdout(output):
                code = cli.main(['capture', course, '--root', directory, '--dry-run', *flags])
            return code, json.loads(output.getvalue()), factory

    def test_course_url_supplies_site_without_config(self):
        code, payload, factory = self.preview('https://learn.example.edu/ultra/courses/_1_1/outline')
        self.assertEqual(code, 0, payload)
        self.assertEqual(factory.call_args.kwargs['site_config'].origin, 'https://learn.example.edu')

    def test_site_and_id_need_no_config_file(self):
        code, payload, factory = self.preview('_1_1', '--site', 'https://learn.example.edu')
        self.assertEqual(code, 0, payload)
        self.assertEqual(factory.call_args.kwargs['site_config'].origin, 'https://learn.example.edu')

    def test_missing_site_for_new_id_is_usage_error(self):
        code, payload, factory = self.preview('_1_1')
        self.assertEqual(code, 2, payload)
        self.assertIn('--site', payload['errors'][0]['message'])
        factory.assert_not_called()

    def test_explicit_site_does_not_silently_accept_another_course_host(self):
        code, _, factory = self.preview('https://other.example.edu/ultra/courses/_1_1/outline',
                                        '--site', 'https://learn.example.edu')
        self.assertNotEqual(code, 0)
        factory.assert_not_called()

    def test_no_public_school_preset(self):
        self.assertFalse(hasattr(site_config, 'EXAMPLE_SITE'))
        with self.assertRaises(ValueError):
            site_config.load_site_config()

    def test_internal_site_reaches_transport_with_normal_policy(self):
        for origin in ('https://learn.internal', 'https://10.0.0.10:8443',
                       'https://[fd00::1]:8443', 'http://learn.local:8080'):
            with self.subTest(origin=origin):
                code, payload, _ = self.preview('_1_1', '--site', origin)
                self.assertEqual(code, 0, payload)
                site = site_config.from_origin(origin)
                url = origin + '/learn/api/public/v1/courses/_1_1'
                self.assertTrue(is_blackboard_url(url, site_config=site))
                fetcher = HttpFetcher(auth_origin=site.origin)
                response = MagicMock(status=200)
                response.geturl.return_value = url
                response.read.return_value = b'{}'
                fetcher._opener = MagicMock()
                fetcher._opener.open.return_value = response
                self.assertEqual(fetcher.fetch_json(url), {})
                fetcher._opener.open.assert_called_once()

    def test_internal_attachments_publish_and_fetch_without_extra_setup(self):
        from blackboard.sync_models import Attachment
        from blackboard.cli_requests import FetchRequest
        site = site_config.from_origin('https://learn.internal')
        url = site.url('/bbcswebdav/xid-123_1')
        page = Page('_1_1', 'Week', None, None, 0, ['Week'], site.url('/ultra/courses/_1_1/outline'),
                    'Body', [Attachment('reading.pdf', url, original_filename='reading.pdf')])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service.capture(root, '_1_1', None, site_config=site,
                            pages=[page], source_status={'rest': {}})
            fetcher = HttpFetcher(auth_origin=site.origin)
            response = MagicMock(status=200, headers={})
            response.geturl.return_value = url
            response.read.side_effect = [b'%PDF-1.4\nfixture\n%%EOF\n', b'']
            fetcher._opener = MagicMock()
            fetcher._opener.open.return_value = response
            session = SimpleNamespace(transport=lambda: (None, fetcher))
            result = service.fetch(root, FetchRequest(kinds=('attachments',)), session)
            self.assertEqual(result['outcome'], 'complete', result)
            self.assertEqual(fetcher._opener.open.call_count, 1)

    def test_internal_redirect_to_unselected_host_stays_rejected(self):
        handler = _LimitedRedirectHandler(auth_origin='https://learn.internal')
        request = Request('https://learn.internal/bbcswebdav/xid-123_1')
        allowed = handler.redirect_request(request, None, 302, '', {}, 'https://learn.internal/files/reading.pdf')
        self.assertEqual(allowed.full_url, 'https://learn.internal/files/reading.pdf')
        with self.assertRaises(FetchError):
            handler.redirect_request(request, None, 302, '', {}, 'https://unselected.internal/private')

    def test_site_is_preserved_in_auth_repair_command(self):
        args = cli.build_parser().parse_args(['sync', '_1_1', '--site', 'https://learn.internal', '--root', '/tmp/course'])
        self.assertIn('--site https://learn.internal', cli.auth_solution(args))

    def test_advanced_config_repair_does_not_add_mutually_exclusive_site(self):
        args = cli.build_parser().parse_args(['sync', '_1_1', '--site-config', '/tmp/site.json', '--root', '/tmp/course'])
        args.site = site_config.from_origin('https://learn.internal')
        solution = cli.auth_solution(args)
        self.assertIn('--site-config ' + str(Path('/tmp/site.json').resolve()), solution)
        self.assertNotIn('--site ', solution)

    def test_explicit_internal_attachment_host_is_usable_but_not_arbitrary_redirects(self):
        site = site_config.SiteConfig(origin='https://learn.internal', trusted_hosts=('files.internal',))
        fetcher = HttpFetcher(auth_origin=site.origin, site_config=site)
        url = 'https://files.internal/reading.json'
        response = MagicMock(status=200)
        response.geturl.return_value = url
        response.read.return_value = b'{}'
        fetcher._opener = MagicMock()
        fetcher._opener.open.return_value = response
        self.assertEqual(fetcher.fetch_json(url), {})
        handler = _LimitedRedirectHandler(auth_origin=site.origin, site_config=site)
        request = Request(site.url('/bbcswebdav/xid-123_1'))
        self.assertEqual(handler.redirect_request(request, None, 302, '', {}, url).full_url, url)
        with self.assertRaises(FetchError):
            handler.redirect_request(request, None, 302, '', {}, 'https://other.internal/private')
        with self.assertRaises(FetchError):
            fetcher.fetch_json(url, url_policy=lambda _: False)
        with self.assertRaises(FetchError):
            fetcher.fetch_json('http://learn.internal/reading.json')

    def test_empty_cookie_jar_reports_authentication_action_not_browser_failure(self):
        from blackboard.browser_backend import SessionRequired
        from blackboard.cookie_store import CookieStore
        store = CookieStore(SimpleNamespace(cookies=lambda _: []), domains=('https://learn.example.edu',))
        fetcher = HttpFetcher(cookie_store=store, auth_origin='https://learn.example.edu')
        with self.assertRaises(SessionRequired) as error:
            fetcher._build_request('https://learn.example.edu/learn/api/public/v1/courses/_1_1')
        self.assertEqual(error.exception.code, 'blackboard.browser_session_required')
        self.assertTrue(error.exception.retryable)
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch.object(service, 'request_get', side_effect=error.exception), contextlib.redirect_stdout(output):
                code = cli.main(['request', 'get', '/learn/api/public/v1/courses/_1_1', '--root', directory])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 4)
            self.assertEqual(payload['errors'][0]['code'], 'blackboard.browser_session_required')

    def test_legacy_auth_label_is_read_without_school_specific_routing(self):
        site = site_config.site_config_from_dict({'origin': 'https://learn.internal', 'auth_mode': 'wsu'})
        self.assertEqual(site.auth_mode, 'browser-session')

    def test_explicit_internal_integrations_preserve_scheme_and_port(self):
        from blackboard.reading_lists import leganto_ris_export_target, leganto_excel_export_url
        from blackboard.panopto_transcripts import canonical_viewer_url
        site = site_config.SiteConfig(origin='https://learn.internal',
                                      leganto_origin='http://library.internal:8080',
                                      panopto_origin='http://video.internal:8081')
        list_url = site.leganto_origin + '/leganto/nui/lists/123'
        self.assertTrue(leganto_ris_export_target(list_url, site_config=site)[1].startswith(site.leganto_origin + '/'))
        self.assertTrue(leganto_excel_export_url(list_url, site_config=site).startswith(site.leganto_origin + '/'))
        viewer = site.panopto_origin + '/Panopto/Pages/Viewer.aspx?id=123&unused=1'
        self.assertEqual(canonical_viewer_url(viewer, site_config=site),
                         site.panopto_origin + '/Panopto/Pages/Viewer.aspx?id=123')


if __name__ == '__main__':
    unittest.main()
