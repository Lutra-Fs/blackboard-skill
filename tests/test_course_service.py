from __future__ import annotations
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import patch, MagicMock
from urllib.request import Request
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from blackboard import course_service as svc
from site_fixture import EXAMPLE_SITE
from blackboard import providers
from blackboard.cli_requests import FetchRequest  # noqa: E402
from blackboard.archive_io import ArchiveIntegrityError
from blackboard.cli import main
from blackboard import resource_store
from blackboard.resource_store import Store, asset_id, file_evidence
from blackboard.safe_fetch import _LimitedRedirectHandler, FetchError
from blackboard.sync_models import Page, Attachment

URL = 'https://learn.example.edu/bbcswebdav/xid-123_1'
PDF = b'%PDF-1.4\nexample\n%%EOF\n'

def page(id='_1_1', attachments=True):
    return Page(id, 'Week 1', None, None, 0, ['Week 1'], 'https://learn.example.edu/ultra/courses/_1_1/outline',
                'Source evidence', [Attachment('reading.pdf', URL, original_filename='reading.pdf')] if attachments else [], [], [], [])

def args(**kw):
    fields = {'dry_run':False,'kinds':('attachments',),'assets':None,'url':None,'out':None,'refresh':False,**kw}
    if 'kind' in fields: fields['kinds'] = tuple(fields.pop('kind'))
    if 'asset' in fields:
        asset = fields.pop('asset')
        fields['assets'] = tuple(asset) if asset else None
    return FetchRequest(**fields)

class CourseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def test_materialize_outcome_requires_actual_failures_for_needs_action(self):
        for rows, expected in [([], 'complete'),
                               ([{'status': 'downloaded'}, {'status': 'skipped'}], 'complete'),
                               ([{'status': 'failed', 'needs_action': True}], 'needs-action'),
                               ([{'status': 'failed', 'needs_action': False}], 'partial')]:
            with self.subTest(expected=expected, rows=rows):
                _, failed, outcome = svc._materialize_work(None, None, [], rows)
                self.assertEqual(outcome, expected)
                self.assertEqual(failed, expected != 'complete')

    def test_external_command_failure_finishes_asset_instead_of_aborting(self):
        from blackboard.runtime import CommandExecutionError
        self.capture()
        with Store(self.root, create=True) as db:
            key = db.asset('youtube', 'https://www.youtube.com/watch?v=abc123')
            with db.acquisition(key, 'snapshot') as acq:
                raise CommandExecutionError('ERROR: upstream extractor failed')
            self.assertEqual(acq.outcome['status'], 'failed')
            self.assertIn('upstream extractor failed', acq.outcome['error'])
            self.assertEqual(db.attempts()[0]['status'], 'failed')

    def test_sync_keeps_blocked_candidate_visible_and_stops_fetch(self):
        self.capture()
        blocked = {'snapshot': 'candidate-id', 'published': False, 'outcome': 'blocked'}
        output = io.StringIO()
        with patch.object(svc, 'Session'), patch.object(svc, 'capture', return_value=blocked), \
                patch.object(svc, 'fetch', side_effect=AssertionError('blocked capture must stop fetch')) as fetch, \
                contextlib.redirect_stdout(output):
            code = main(['sync', '_1_1', '--root', str(self.root)])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output.getvalue())['data']['snapshot'], 'candidate-id')
        fetch.assert_not_called()
    def capture(self, pages=None, errors=None):
        return svc.capture(self.root, '_1_1', None, pages=pages or [page()], source_status={'rest': {'errors':errors or []}}, site_config=EXAMPLE_SITE)
    def preview(self, command, pages, rest_status):
        before = {str(p.relative_to(self.root)): p.read_bytes()
                  for p in self.root.rglob('*') if p.is_file()}
        crawler = MagicMock(rest_status=rest_status)
        crawler.crawl.return_value = pages
        session = MagicMock()
        session.transport.return_value = (None, None)
        output = io.StringIO()
        with patch.object(svc, 'Session', return_value=session), patch.object(svc, 'RestCrawler', return_value=crawler), \
                patch.object(svc, 'fetch') as fetch, contextlib.redirect_stdout(output):
            code = main([command, '_1_1', '--site', EXAMPLE_SITE.origin, '--root', str(self.root), '--dry-run'])
        fetch.assert_not_called()
        after = {str(p.relative_to(self.root)): p.read_bytes()
                 for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(after, before)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload['data']['dry_run'])
        self.assertIn('crawl', payload['data'])
        return code, payload

    def test_capture_and_sync_dry_run_complete_is_success(self):
        for command in ('capture', 'sync'):
            with self.subTest(command=command):
                code, payload = self.preview(command, [page()], {'errors': []})
                self.assertEqual(code, 0)
                self.assertTrue(payload['ok'])
                self.assertEqual(payload['data']['outcome'], 'complete')
                self.assertTrue(payload['data']['dry_run'])
                self.assertFalse(payload['data']['published'])
                self.assertFalse((self.root / '_blackboard').exists())

    def test_capture_and_sync_dry_run_incomplete_stays_blocked(self):
        cases = [([], {}, 'no-pages'),
                 ([page()], {'errors': [{'error': 'fixture failure'}]}, 'rest-errors'),
                 ([page()], {'pagination_truncated': True}, 'rest-pagination-truncated')]
        for command in ('capture', 'sync'):
            for pages, rest_status, reason in cases:
                with self.subTest(command=command, reason=reason):
                    code, payload = self.preview(command, pages, rest_status)
                    self.assertEqual(code, 3)
                    self.assertFalse(payload['ok'])
                    self.assertEqual(payload['data']['outcome'], 'blocked')
                    self.assertFalse(payload['data']['crawl']['complete'])
                    self.assertIn(reason, payload['data']['crawl']['reasons'])

    def test_capture_and_sync_dry_run_removal_is_complete(self):
        self.capture([page(), page('_2_1')])
        for command in ('capture', 'sync'):
            code, payload = self.preview(command, [page()], {'errors': []})
            self.assertEqual(code, 0)
            self.assertTrue(payload['data']['crawl']['safe_to_replace'])
            self.assertTrue(payload['data']['crawl']['diff']['removed'])

    def test_complete_removal_publishes_without_deleting_files(self):
        from blackboard.course_changes import change_diff
        original = self.capture()
        candidate = self.capture([page(attachments=False)])
        self.assertTrue(candidate['published'])
        self.assertEqual(svc.status(self.root)['snapshot'], candidate['snapshot'])
        self.assertTrue(change_diff(self.root)['pages']['removed'])
        self.assertEqual(change_diff(self.root)['before_snapshot'], original['snapshot'])
        self.assertTrue((self.root / '_blackboard/generations' / original['snapshot']).exists())
    def test_partial_never_published_even_when_larger(self):
        original = self.capture()
        candidate = self.capture([page(), page('_2_1')], [{'phase': 'page2', 'error': 'failed'}])
        self.assertFalse(candidate['published'])
        self.assertEqual(svc.status(self.root)['snapshot'], original['snapshot'])
    def test_read_missing_and_dry_run_do_not_create(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out): code=main(['files','--root',str(self.root)])
        self.assertEqual(code,3); self.assertFalse((self.root/'_blackboard').exists())
        svc.capture(self.root,'_1_1',None, dry_run=True,pages=[page()],source_status={'rest':{}}, site_config=EXAMPLE_SITE)
        self.assertFalse((self.root/'_blackboard').exists())
    def test_status_on_new_root_returns_capture_guidance(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out): code=main(['status','--root',str(self.root)])
        payload=json.loads(out.getvalue())
        self.assertEqual(code,0)
        self.assertEqual(payload['data']['readiness'],'no-snapshot')
        self.assertFalse(payload['data']['ready'])
        self.assertIn('capture', payload['data']['next_steps'][0])
    def test_file_publication_recovery_and_annotations(self):
        self.capture()
        with Store(self.root,create=True) as db:
            key=db.asset('attachments',URL)
            a=db.start(key,'snapshot'); temp=self.root/'staged.pdf';temp.write_bytes(PDF)
            prepared=db.prepare(a,[(temp,Path('Week/a.pdf'))])
            target=self.root/prepared[0]['path'];target.parent.mkdir(); target.write_bytes(PDF)
            # Crash after publishing bytes, before the transaction.
            db.recover(); self.assertTrue(db.reusable(key))
            target.write_bytes(PDF+b'local annotation')
            self.assertTrue(db.reusable(key));self.assertEqual(db.files()[0]['sha256'],prepared[0]['sha256'])
            target.unlink();self.assertIsNone(db.reusable(key))
    def test_same_name_different_bytes_no_overwrite(self):
        self.capture(); dest=self.root/'same.pdf';dest.write_bytes(PDF)
        with Store(self.root,create=True) as db:
            key=db.asset('attachments',URL);a=db.start(key,'snapshot')
            source=self.root/'new.pdf'; source.write_bytes(PDF+b'different')
            prepared=db.prepare(a,[(source,Path('same.pdf'))]);db.publish(a)
            self.assertNotEqual(prepared[0]['path'],'same.pdf');self.assertEqual(dest.read_bytes(),PDF)
    def test_wrong_document_signatures(self):
        for name in ('bad.pdf','bad.xlsx'):
            p=self.root/name;p.write_text('<html>login</html>')
            with self.assertRaises(ArchiveIntegrityError): file_evidence(p)
    def test_fetch_failure_does_not_rollback_snapshot(self):
        captured=self.capture()
        fetcher=SimpleNamespace(download=MagicMock(side_effect=FetchError('offline')))
        session=SimpleNamespace(login=lambda:None,fetcher=fetcher,runner=MagicMock(),transport=lambda:(None,fetcher))
        result=svc.fetch(self.root,args(),session)
        self.assertFalse(result['complete']);self.assertEqual(svc.status(self.root)['snapshot'],captured['snapshot'])
    def test_successful_rerun_does_not_make_attempt(self):
        self.capture()
        def download(url,dest,**kw):
            dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(PDF)
            return SimpleNamespace(content_disposition=None,effective_url=URL)
        fetcher=SimpleNamespace(download=MagicMock(side_effect=download))
        session=SimpleNamespace(login=lambda:None,fetcher=fetcher,runner=MagicMock(),transport=lambda:(None,fetcher))
        one=svc.fetch(self.root,args(),session);two=svc.fetch(self.root,args(),session)
        self.assertTrue(one['complete']); self.assertEqual(two['results'][0]['status'],'skipped')
        self.assertEqual(fetcher.download.call_count,1)
        with Store(self.root) as db:self.assertEqual(len(db.attempts()),1)
    def test_auth_interruption_then_fetch_reuses_completed_file(self):
        from blackboard.browser_backend import SessionRequired
        second_url = URL.replace('123_1', '456_1')
        second_page = replace(page('_2_1'), attachments=[Attachment('second.pdf', second_url)])
        captured = self.capture([page(), second_page])
        expired = True
        def download(url, dest, **kw):
            if expired and url == second_url:
                raise SessionRequired('session expired')
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(PDF)
            return SimpleNamespace(content_disposition=None, effective_url=url)
        fetcher = SimpleNamespace(download=MagicMock(side_effect=download))
        session = SimpleNamespace(transport=lambda: (None, fetcher))
        first = svc.fetch(self.root, args(), session)
        self.assertEqual(first['outcome'], 'needs-action')
        self.assertEqual(first['snapshot'], captured['snapshot'])
        expired = False
        resumed = svc.fetch(self.root, args(), session)
        self.assertTrue(resumed['complete'])
        self.assertEqual(resumed['results'][0]['status'], 'skipped')
        self.assertEqual(fetcher.download.call_count, 3)
        with Store(self.root) as db:
            self.assertEqual(len(db.attempts()), 3)
    def test_redirect_before_dispatch_and_cookie_origin(self):
        request=Request(URL, headers={'Cookie':'secret','Authorization':'Bearer secret'})
        handler=_LimitedRedirectHandler()
        for target in ('http://learn.example.edu/file','https://127.0.0.1/file'):
            with self.assertRaises(FetchError):handler.redirect_request(request,None,302,'',{},target)
        redirected=handler.redirect_request(request,None,302,'',{},'https://example.com/file')
        self.assertIsNone(redirected.get_header('Cookie'));self.assertIsNone(redirected.get_header('Authorization'))
    def test_implicit_course_container_is_a_boundary_not_missing_page(self):
        p=replace(page(),parent_id='_container_1',parent_title='_container_1')
        result=self.capture([p])
        self.assertTrue(result['published'])
        self.assertEqual(result['crawl']['boundary_parent_ids'],['_container_1'])
    def test_parent_cycle_blocks(self):
        p=replace(page(),parent_id='_1_1')
        result=self.capture([p])
        self.assertFalse(result['published']);self.assertIn('parent-cycle',result['crawl']['reasons'])
    def test_signed_url_not_persisted(self):
        p=replace(page(), attachments=[Attachment('a.pdf',URL+'?randomsig=private')], body_text=URL+'?randomsig=private')
        self.capture([p])
        for file in (self.root/'_blackboard/generations').rglob('*'):
            if file.is_file():self.assertNotIn('private',file.read_text())

class IntegrationOutcomeTests(unittest.TestCase):
    setUp = CourseTests.setUp
    tearDown = CourseTests.tearDown
    capture = CourseTests.capture
    def test_ris_success_excel_failure_retries_only_excel(self):
        from blackboard import reading_lists as rl

        from collections import Counter
        import zipfile
        self.capture()
        tool=rl.ReadingListTool(self.root,'_1_1','https://learn.example.edu/webapps/blackboard/execute/blti/launchPlacement?blti_placement_id=_1_1')
        calls=Counter()
        def export(browser,fetcher,tool,**kw):
            fmt=kw['formats'][0];calls[fmt]+=1
            if fmt=='xlsx' and calls[fmt]==1:return {'status':'failed','reason':'Excel unavailable'}
            out=tool.course_root/f'readings.{fmt}'
            if fmt=='ris':out.write_text('TY  - BOOK\nTI  - Native list\nER  -\n')
            else:
                with zipfile.ZipFile(out,'w') as z:
                    z.writestr('[Content_Types].xml','<Types/>');z.writestr('xl/workbook.xml','<workbook/>')
            return {'status':'downloaded'}
        session=SimpleNamespace(login=lambda:None,fetcher=None,runner=MagicMock(),
                                transport=lambda:(None,None),refresh_credentials=lambda:None)
        with patch.object(rl,'iter_archived_reading_list_tools',return_value=[tool]),patch.object(rl,'export_reading_list',side_effect=export):
            first=svc.fetch(self.root,args(kind=['reading-list']),session)
            second=svc.fetch(self.root,args(kind=['reading-list']),session)
        self.assertFalse(first['complete']);self.assertTrue(second['complete'])
        self.assertEqual(calls,{'ris':1,'xlsx':2})
    def test_new_remote_revision_preserves_previous_file(self):
        self.capture();revision=['one']
        events=[]
        def probe(*a,**kw):
            events.append('probe')
            return {'effective_url':'https://cdn.example.com/'+revision[0]}
        def download(url,dest,**kw):
            dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(PDF+revision[0].encode())
            return SimpleNamespace(content_disposition=None,effective_url='https://cdn.example.com/'+revision[0])
        fetcher=SimpleNamespace(download=download,probe=probe)
        def transport():
            events.append('transport')
            return None, fetcher
        session=SimpleNamespace(fetcher=fetcher,runner=MagicMock(),transport=transport)
        first=svc.fetch(self.root,args(),session)
        revision[0]='two';self.capture();events.clear()
        second=svc.fetch(self.root,args(),session)
        self.assertTrue(second['complete'])
        self.assertNotEqual(first['results'][0]['files'],second['results'][0]['files'])
        self.assertTrue((self.root/first['results'][0]['files'][0]).exists())
        self.assertLess(events.index('transport'),events.index('probe'))

    def test_panopto_callback_receives_stage_and_saves_within_function(self):
        from blackboard import panopto_transcripts as pan

        self.capture()
        url='https://example.au.panopto.com/Panopto/Pages/Viewer.aspx?id=recording-1'
        link=pan.PanoptoLink(url,course_root=self.root,link_title='Lecture')
        planned=[{'asset_id':svc.asset_id('panopto',pan.canonical_viewer_url(url,site_config=EXAMPLE_SITE)),
                  'kind':'panopto','url':pan.canonical_viewer_url(url,site_config=EXAMPLE_SITE),'metadata':{}}]
        static=[{'asset_id':planned[0]['asset_id'],'kind':'panopto',
                 'url':planned[0]['url'],'metadata':{},'source':link}]
        browser=SimpleNamespace(open=lambda url:'tab',eval_json=lambda script:{})
        fetcher=SimpleNamespace()
        session=SimpleNamespace(login=lambda:browser,fetcher=fetcher,runner=MagicMock(),transport=lambda:(browser,fetcher))
        def save(link_arg,data,**kwargs):
            target=link_arg.course_root/'_blackboard/panopto/Lecture recording-1/transcript.json'
            target.parent.mkdir(parents=True)
            target.write_text('[]')
            return {'status':'downloaded'}
        with patch.object(providers,'plan_resources',return_value=static), \
             patch.object(providers,'plan_catalog',return_value=planned), \
             patch.object(pan,'discover_course_menu_panopto_links',return_value=[]), \
             patch.object(pan,'delivery_script',return_value='delivery-script'), \
             patch.object(pan,'save_transcripts',side_effect=save) as save_transcripts:
            result=svc.fetch(self.root,args(kind=['panopto']),session)
        self.assertTrue(result['complete'],result)
        save_transcripts.assert_called_once()
        self.assertIn('_blackboard/staging',str(save_transcripts.call_args.args[0].course_root))

    def test_youtube_uses_public_metadata_and_explicit_cookie_fallback_config(self):
        from blackboard import youtube_transcripts as yt

        self.capture()
        url='https://www.youtube.com/watch?v=abc123'
        link=yt.YouTubeLink(url,course_root=self.root,link_title='Lecture')
        planned=[{'asset_id':svc.asset_id('youtube',url),'kind':'youtube','url':url,'metadata':{}}]
        static=[{'asset_id':planned[0]['asset_id'],'kind':'youtube','url':url,'metadata':{},'source':link}]
        fetcher=SimpleNamespace()
        session=SimpleNamespace(login=MagicMock(),fetcher=fetcher,runner=MagicMock(),transport=lambda:(None,fetcher))
        def save(link_arg,meta,**kwargs):
            target=link_arg.course_root/'_blackboard/youtube/Lecture abc123/transcript.vtt'
            target.parent.mkdir(parents=True)
            target.write_text('WEBVTT\n')
            return {'status':'downloaded'}
        with patch.object(providers,'plan_resources',return_value=static), \
             patch.object(providers,'plan_catalog',return_value=planned), \
             patch.object(yt,'fetch_video_metadata',return_value={'title':'Lecture'}) as metadata, \
             patch.object(yt,'save_transcripts',side_effect=save) as save_transcripts:
            result=svc.fetch(self.root,args(kind=['youtube'],youtube_cookies_browser='firefox'),session)
        self.assertTrue(result['complete'])
        metadata.assert_called_once()
        self.assertEqual(metadata.call_args.args, (url,))
        self.assertIsNone(metadata.call_args.kwargs['cookies_browser'])
        self.assertIsNot(metadata.call_args.kwargs['runner'], session.runner)
        self.assertEqual(save_transcripts.call_args.kwargs['cookies_browser'],'firefox')
    def test_file_pagination(self):
        self.capture()
        with Store(self.root,create=True) as db:
            for i in range(2):
                key=db.asset('attachments',URL+str(i));attempt=db.start(key,'x');src=self.root/f'stage{i}.pdf';src.write_bytes(PDF)
                db.prepare(attempt,[(src,Path(f'file{i}.pdf'))]);db.publish(attempt)
        out=io.StringIO()
        with contextlib.redirect_stdout(out):code=main(['files','--root',str(self.root),'--limit','1'])
        data=json.loads(out.getvalue())['data'];self.assertEqual(code,0);self.assertEqual(data['next_offset'],1);self.assertEqual(len(data['files']),1)
        out=io.StringIO()
        with contextlib.redirect_stdout(out): code=main(['files','--root',str(self.root),'--limit','2'])
        data=json.loads(out.getvalue())['data'];self.assertEqual(code,0);self.assertIsNone(data['next_offset']);self.assertEqual(len(data['files']),2)
    def test_files_paths_format_prints_all_pages(self):
        self.capture()
        with Store(self.root,create=True) as db:
            for i in range(3):
                key=db.asset('attachments',URL+str(i));attempt=db.start(key,'x');src=self.root/f'stage{i}.pdf';src.write_bytes(PDF)
                db.prepare(attempt,[(src,Path(f'file{i}.pdf'))]);db.publish(attempt)
        out=io.StringIO()
        with contextlib.redirect_stdout(out): code=main(['files','--root',str(self.root),'--limit','1','--format','paths'])
        lines=[l for l in out.getvalue().splitlines() if l]
        self.assertEqual(code,0);self.assertEqual(len(lines),3)

    def test_data_format_success_and_failure_streams(self):
        self.capture()
        out=io.StringIO()
        with contextlib.redirect_stdout(out): code=main(['status','--root',str(self.root),'--format','data'])
        data=json.loads(out.getvalue())
        self.assertEqual(code,0);self.assertEqual(data['readiness'],'partial');self.assertNotIn('schema_version',data)
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            code=main(['change','apply','nosuch.json','--root',str(self.root),'--format','data'])
        self.assertNotEqual(code,0);self.assertEqual(out.getvalue(),'')
        payload=json.loads(err.getvalue());self.assertEqual(payload['schema_version'],2);self.assertFalse(payload['ok'])

    def test_request_get_returns_json_data(self):
        self.capture()
        fetcher=MagicMock();fetcher.fetch_json.return_value={'courses': []}
        with patch.object(svc,'HttpFetcher',return_value=fetcher), \
             patch.object(svc,'CookieStore',return_value=MagicMock()) as store_cls, \
             patch.object(svc,'BrowserSession') as session_cls:
            out=io.StringIO()
            with contextlib.redirect_stdout(out): code=main(['request','get','/learn/api/public/v1/courses','--root',str(self.root)])
        self.assertEqual(code,0)
        data=json.loads(out.getvalue())['data']
        self.assertEqual(data['data'],{'courses': []});self.assertEqual(data['path'],'/learn/api/public/v1/courses')
        url=fetcher.fetch_json.call_args.args[0]
        self.assertTrue(url.startswith('https://learn.example.edu/'))
        self.assertTrue(url.endswith('/learn/api/public/v1/courses'))
        # The request owns its short-lived group, not the profile cookie store.
        session_cls.assert_called_once_with(session='blackboard-request')
        self.assertIs(store_cls.call_args.args[0], session_cls.return_value)
        self.assertEqual(store_cls.call_args.kwargs['domains'], EXAMPLE_SITE.cookie_origins())
        session_cls.return_value.close.assert_called_once()

    def test_status_lists_other_generations(self):
        published=self.capture()['snapshot']
        (self.root/'_blackboard/generations'/'20260101-000000-test').mkdir(parents=True)
        out=io.StringIO()
        with contextlib.redirect_stdout(out): code=main(['status','--root',str(self.root)])
        self.assertEqual(code,0)
        candidates=json.loads(out.getvalue())['data']['other_generations']
        self.assertEqual(candidates,['20260101-000000-test']);self.assertNotIn(published,candidates)


class JsonStateTests(unittest.TestCase):
    setUp = CourseTests.setUp
    tearDown = CourseTests.tearDown
    capture = CourseTests.capture

    def test_bounded_outcomes_and_no_captions_remain_reusable(self):
        self.capture()
        with Store(self.root, create=True) as store:
            key = store.asset('panopto', 'https://video.example.com/view', {'destination': '/unused', 'format': 'unused'})
            success = store.start(key, 'one')
            store.prepare(success, [], status='no-captions', result={'out_dir': str(self.root), 'huge': ['unused']*100})
            store.publish(success)
            failure = store.start(key, 'two')
            store.finish(failure, 'failed', {'error': 'provider offline'})
            self.assertEqual(store.reusable(key, 'one')['previous_status'], 'no-captions')
            self.assertIsNone(store.reusable(key, 'two'))
            latest = store.start(key, 'two')
            store.prepare(latest, [], status='no-captions'); store.publish(latest)
            self.assertEqual(store.attempts()[0]['id'], latest)
            for i in range(25):
                store.run('fetch', {'snapshot': str(i), 'results': [{'status':'skipped','unneeded':'detail'}]})
            state = json.loads((self.root/'_blackboard/state.json').read_text())
            resource = state['resources'][key]
            self.assertEqual(resource['failure']['id'], failure)
            self.assertEqual(resource['success']['id'], latest)
            self.assertEqual(len(state['runs']), 20)
            self.assertEqual(state['runs'][-1]['counts'], {'skipped':1})
        persisted = (self.root/'_blackboard/state.json').read_text()
        for unwanted in ('out_dir', 'huge', 'destination', 'unneeded', str(self.root)):
            self.assertNotIn(unwanted, persisted)
        with Store(self.root) as store:
            self.assertTrue(store.reusable(key, 'two'))

    def test_commit_failure_recovers_exact_paths_and_revision(self):
        self.capture()
        with Store(self.root, create=True) as store:
            key = store.asset('attachments', URL)
            attempt = store.start(key, 'snap')
            source = self.root/'stage.pdf'; source.write_bytes(PDF)
            store.prepare(attempt, [(source, Path('a.pdf'))], result={'cdn_path':'https://cdn.example.com/version?token=secret'})
            with patch('blackboard.resource_store.atomic_write_json', side_effect=OSError('simulated commit failure')):
                with self.assertRaises(OSError): store.publish(attempt)
            self.assertEqual(store.attempt(attempt)[1]['status'], 'prepared')
        with Store(self.root, create=True) as store:
            self.assertEqual(store.recover(), [attempt])
            self.assertEqual(store.revision(key), {'cdn_path':'https://cdn.example.com/version','checked_snapshot':'snap'})
            self.assertEqual(store.files()[0]['path'], 'a.pdf')
        settled = json.loads((self.root/'_blackboard/state.json').read_text())['resources'][key]
        self.assertNotIn('pending', settled)
        self.assertNotIn('source', settled['success'])

    def test_query_and_command_surface(self):
        self.capture()
        with Store(self.root, create=True) as db:
            self.assertEqual(db.identity(), {'origin': 'https://learn.example.edu', 'course_id': '_1_1'})
            self.assertEqual(db.pending_ids(), [])
            self.assertEqual(db.prepared_attempt_count(), 0)
            self.assertEqual([run['command'] for run in db.runs()], ['capture'])
            key = db.asset('attachments', URL, {'cdn_path': 'https://cdn.example.com/x'})
            attempt = db.start(key, 'snap')
            self.assertEqual(db.pending_ids(), [key])
            source = self.root / 'stage.pdf'; source.write_bytes(PDF)
            db.prepare(attempt, [(source, Path('a.pdf'))])
            self.assertEqual(db.prepared_attempt_count(), 1)
            db.publish(attempt)
            self.assertEqual(db.pending_ids(), [])
            self.assertEqual(db.registered_resources()[key], {
                'asset_id': key, 'kind': 'attachments', 'url': URL,
                'metadata': {'cdn_path': 'https://cdn.example.com/x'}})


    def test_panopto_discovery_retains_only_usable_links(self):
        self.capture()
        with Store(self.root, create=True) as store:
            key = store.asset('panopto-discovery', URL)
            store.finish(store.start(key, 'snap'), 'complete', {'links':[
                {'url':'https://video.example.com/view?id=123&token=secret','title':'Lecture','junk':'unused'}],
                'browser_session':'unused'})
        with Store(self.root) as store:
            self.assertTrue(store.reusable(key, 'snap'))
            self.assertEqual(store.success(key)['links'], [{'url':'https://video.example.com/view?id=123','title':'Lecture'}])

    def test_bad_state_is_preserved(self):
        self.capture()
        state = self.root / '_blackboard/state.json'
        state.write_text('{"schema_version":999}')
        with self.assertRaises(ArchiveIntegrityError): Store(self.root, create=True)
        self.assertEqual(state.read_text(), '{"schema_version":999}')
        out = io.StringIO()
        with contextlib.redirect_stdout(out): code = main(['status', '--root', str(self.root)])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out.getvalue())['data']['readiness'], 'damaged')

class AcquisitionTransactionTests(unittest.TestCase):
    setUp = CourseTests.setUp
    tearDown = CourseTests.tearDown
    capture = CourseTests.capture

    def test_commit_path_publishes_files_and_removes_staging(self):
        self.capture()
        with Store(self.root, create=True) as db:
            key = db.asset('attachments', URL)
            with db.acquisition(key, 'snap') as acq:
                nested = acq.stage / 'Week 1'
                nested.mkdir()
                (nested / 'reading.pdf').write_bytes(PDF)
                acq.result = {'status': 'downloaded'}
            self.assertEqual(acq.outcome['status'], 'downloaded')
            published = self.root / acq.outcome['files'][0]
            self.assertTrue(published.is_file())
            self.assertEqual(published.read_bytes(), PDF)
            self.assertFalse(acq.stage.parent.exists())
            self.assertTrue(db.reusable(key, 'snap'))
            self.assertEqual(db.pending_ids(), [])

    def test_failed_provider_status_finishes_failed_and_removes_stage(self):
        self.capture()
        with Store(self.root, create=True) as db:
            key = db.asset('attachments', URL)
            with db.acquisition(key, 'snap') as acq:
                (acq.stage / 'partial.pdf').write_bytes(PDF)
                acq.result = {'status': 'failed', 'reason': 'provider offline'}
            self.assertEqual(acq.outcome,
                             {'asset_id': key, 'status': 'failed',
                              'result': {'status': 'failed', 'reason': 'provider offline'}})
            self.assertFalse(acq.stage.parent.exists())
            self.assertEqual(db.attempts()[0]['status'], 'failed')
            self.assertEqual(db.files(), [])

    def test_provider_error_rolls_back_without_touching_the_course(self):
        self.capture()
        with Store(self.root, create=True) as db:
            key = db.asset('attachments', URL)
            with db.acquisition(key, 'snap') as acq:
                raise FetchError('connection reset')
            self.assertEqual(acq.outcome['asset_id'], key)
            self.assertEqual(acq.outcome['status'], 'failed')
            self.assertEqual(acq.outcome['error'], 'connection reset')
            self.assertFalse(acq.stage.parent.exists())
            self.assertEqual(db.attempts()[0]['status'], 'failed')
            self.assertEqual(db.files(), [])

    def test_prepared_attempt_survives_commit_failure_for_recover(self):
        self.capture()
        real_write = resource_store.atomic_write_json

        def fail_final_commit(path, data):
            if isinstance(data, dict) and any('success' in r and 'pending' not in r
                                              for r in data.get('resources', {}).values()):
                raise OSError('simulated commit failure')
            real_write(path, data)

        with Store(self.root, create=True) as db:
            key = db.asset('attachments', URL)
            with patch('blackboard.resource_store.atomic_write_json', side_effect=fail_final_commit):
                with db.acquisition(key, 'snap') as acq:
                    (acq.stage / 'reading.pdf').write_bytes(PDF)
                    acq.result = {'status': 'downloaded'}
            self.assertEqual(acq.outcome['status'], 'failed')
            # A prepared publication keeps its staging directory and state intent.
            self.assertTrue((self.root / '_blackboard' / 'staging').exists())
        with Store(self.root, create=True) as db:
            self.assertEqual(db.recover(), [acq.attempt])
            self.assertTrue(db.reusable(key, 'snap'))
            self.assertEqual(db.files(key=key)[0]['path'], 'reading.pdf')


class ResourceDecisionOfflineTests(unittest.TestCase):
    setUp = CourseTests.setUp
    tearDown = CourseTests.tearDown
    capture = CourseTests.capture

    def exclude(self, keys, reason):
        from blackboard.course_changes import apply_plan
        snapshot = svc.status(self.root)['snapshot']
        return apply_plan(self.root, {'version': 1, 'base_snapshot': snapshot,
                         'resources': [{'asset_id': key, 'decision': 'exclude', 'reason': reason} for key in keys]})

    def test_fetch_skips_ignored_asset_before_refresh_range_probe_or_download(self):
        self.capture()
        key = asset_id('attachments', URL)
        self.exclude( [key], 'not in placement scope')
        session = SimpleNamespace(
            login=MagicMock(side_effect=AssertionError('login must not run')),
            fetcher=SimpleNamespace(
                probe=MagicMock(side_effect=AssertionError('range probe must not run')),
                download=MagicMock(side_effect=AssertionError('download must not run')),
            ), runner=MagicMock(),
        )
        for options in (
            args(asset=[key], refresh=True),
            args(url=URL + '?token=private', out=Path('explicit.pdf'), refresh=True),
            args(refresh=True),
        ):
            result = svc.fetch(self.root, options, session)
            self.assertTrue(result['complete'])
            self.assertTrue(all(row['status'] == 'exclude' for row in result['results']))
        session.login.assert_not_called()
        session.fetcher.probe.assert_not_called()
        session.fetcher.download.assert_not_called()


    def test_fetch_rejects_mixed_asset_and_url_before_any_provider_work(self):
        # Grammar is owned by the CLI boundary; the service contract is that
        # the combination never reaches provider or session work.
        self.capture()
        key = asset_id('attachments', URL)
        self.exclude( [key], 'not in placement scope')
        with patch.object(svc, 'Session', side_effect=AssertionError('grammar rejection must precede session work')):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(['fetch', '--root', str(self.root), '--asset', key,
                             '--url', URL + '-other', '--out', 'other.pdf'])
        self.assertEqual(code, 2)


    def test_ignored_explicit_url_does_not_discover_panopto_after_recovery(self):
        self.capture()
        key = asset_id('attachments', URL)
        self.exclude( [key], 'not in placement scope')
        pending_url = URL + '-pending'
        with Store(self.root, create=True) as db:
            pending_key = db.asset('attachments', pending_url)
            db.start(pending_key, 'snapshot')
        session = SimpleNamespace(
            login=MagicMock(side_effect=AssertionError('login must not run')),
            fetcher=SimpleNamespace(
                probe=MagicMock(side_effect=AssertionError('range probe must not run')),
                download=MagicMock(side_effect=AssertionError('download must not run')),
            ), runner=MagicMock(),
        )
        with patch('blackboard.panopto_transcripts.discover_course_menu_panopto_links',
                   side_effect=AssertionError('discovery must not run')) as discover:
            result = svc.fetch(self.root, args(url=URL + '?token=private', out=Path('explicit.pdf')), session)
        self.assertTrue(result['complete'])
        self.assertEqual(result['results'][0]['status'], 'exclude')
        discover.assert_not_called()
        session.login.assert_not_called()
        session.fetcher.probe.assert_not_called()
        session.fetcher.download.assert_not_called()


    def test_ignored_panopto_discovery_stays_offline(self):
        self.capture()
        from blackboard.ultra_content import canonical_ultra_course_url
        discovery_url = canonical_ultra_course_url('_1_1', site_config=EXAMPLE_SITE)
        with Store(self.root, create=True) as db:
            discovery_key = db.asset('panopto-discovery', discovery_url)
        self.exclude( [discovery_key], 'Panopto outside this review')
        session = SimpleNamespace(login=MagicMock(side_effect=AssertionError('login must not run')),
                                  fetcher=SimpleNamespace(), runner=MagicMock())
        with patch('blackboard.panopto_transcripts.discover_course_menu_panopto_links',
                   side_effect=AssertionError('dynamic discovery must not run')) as discover:
            result = svc.fetch(self.root, args(kind=['panopto'], refresh=True), session)
        self.assertTrue(result['complete'])
        self.assertEqual(result['results'][0]['status'], 'exclude')
        discover.assert_not_called()
        session.login.assert_not_called()






if __name__=='__main__': unittest.main()
