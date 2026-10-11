from __future__ import annotations

import contextlib
import io
import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from blackboard import cli
from blackboard import course_service as service
from blackboard.browser_backend import BrowserUnavailable, SessionRequired
from blackboard.cli_requests import FetchRequest, BrowserOptions
from blackboard.cookie_store import CookieStore
from blackboard.rest_crawler import RestCrawler
from blackboard.safe_fetch import FetchError, HttpFetcher, _LimitedRedirectHandler
from site_fixture import EXAMPLE_SITE

ORIGIN = EXAMPLE_SITE.origin
API = ORIGIN + '/learn/api/public/v1/courses/_1_1/contents'


def test_missing_cookies_only_block_protected_blackboard_requests():
    cookies = MagicMock()
    cookies.cookie_header.return_value = ''
    fetcher = HttpFetcher(cookie_store=cookies, auth_origin=ORIGIN)
    fetcher._opener = MagicMock()
    with pytest.raises(SessionRequired):
        fetcher.fetch_json(API)
    fetcher._opener.open.assert_not_called()
    assert fetcher._build_request('https://cdn.example.com/file').get_header('Cookie') is None
    assert fetcher._build_request(ORIGIN + '/public/info').get_header('Cookie') is None


def test_cookie_read_failure_is_a_dependency_not_authentication():
    browser = MagicMock(backend='ego')
    browser.cookies.side_effect = RuntimeError('fixture transport failure')
    cookies = CookieStore(browser, domains=EXAMPLE_SITE.cookie_origins())
    fetcher = HttpFetcher(cookie_store=cookies, auth_origin=ORIGIN)
    with pytest.raises(BrowserUnavailable):
        fetcher.fetch_json(API)


def test_session_uses_cookies_without_a_login_navigation():
    browser = MagicMock()
    browser.cookies.return_value = [{
        'name': 'fixture-cookie', 'value': 'fixture',
        'domain': 'learn.example.edu', 'path': '/', 'secure': True,
    }]
    with patch.object(service, 'BrowserSession', return_value=browser):
        session = service.Session('_1_1', BrowserOptions(), site_config=EXAMPLE_SITE)
        _, fetcher = session.transport()
        fetcher._opener = MagicMock()
        fetcher._opener.open.side_effect = HTTPError(API, 401, 'denied', {}, None)
        try:
            with pytest.raises(SessionRequired):
                fetcher.fetch_json(API)
            request = fetcher._opener.open.call_args.args[0]
            assert request.get_header('Cookie') == 'fixture-cookie=fixture'
            browser.open.assert_not_called()
            browser.eval_text.assert_not_called()
        finally:
            session.close()
    browser.close.assert_called_once()


@pytest.mark.parametrize('failure', [False, True])
def test_raw_request_cleans_up_its_own_group(failure):
    browser = MagicMock()
    fetcher = MagicMock()
    if failure:
        fetcher.fetch_json.side_effect = SessionRequired('fixture auth failure')
    state = SimpleNamespace(existing_tree={'course_id': '_1_1'})
    with patch.object(service, 'ArchiveState', return_value=state), \
            patch.object(service, 'BrowserSession', return_value=browser) as factory, \
            patch.object(service, 'HttpFetcher', return_value=fetcher):
        if failure:
            with pytest.raises(SessionRequired):
                service.request_get('/tmp/course', '/learn/api/public/v1/courses', site_config=EXAMPLE_SITE)
        else:
            service.request_get('/tmp/course', '/learn/api/public/v1/courses', site_config=EXAMPLE_SITE)
    assert factory.call_args.kwargs == {'session': 'blackboard-request'}
    browser.close.assert_called_once()


@pytest.mark.parametrize('status,target,expected', [
    (401, API, SessionRequired),
    (403, API, FetchError),
    (401, 'https://cdn.example.com/file', FetchError),
])
def test_auth_http_status_is_origin_scoped(status, target, expected):
    fetcher = HttpFetcher(auth_origin=ORIGIN)
    fetcher._opener = MagicMock()
    fetcher._opener.open.side_effect = HTTPError(target, status, 'denied', {}, None)
    with pytest.raises(expected):
        fetcher.fetch_json(API)


def test_login_redirect_stops_before_dispatch_and_final_response_is_closed():
    target = ORIGIN + '/webapps/login/'
    handler = _LimitedRedirectHandler(lambda _: False, auth_origin=ORIGIN)
    with pytest.raises(SessionRequired):
        handler.redirect_request(Request(API), None, 302, '', {}, target)
    response = MagicMock()
    response.geturl.return_value = target
    fetcher = HttpFetcher(auth_origin=ORIGIN)
    fetcher._opener = MagicMock()
    fetcher._opener.open.return_value = response
    with pytest.raises(SessionRequired):
        fetcher.fetch_json(API)
    response.close.assert_called_once()


def test_html_without_login_route_is_not_an_auth_signal():
    response = MagicMock()
    response.geturl.return_value = API
    response.read.return_value = b'<html>sign in to Zoom</html>'
    response.status = 200
    fetcher = HttpFetcher(auth_origin=ORIGIN)
    fetcher._opener = MagicMock()
    fetcher._opener.open.return_value = response
    with pytest.raises(FetchError):
        fetcher.fetch_json(API)


def test_metadata_auth_interruption_propagates_instead_of_becoming_partial():
    fetcher = MagicMock()
    fetcher.fetch_json.side_effect = SessionRequired('sign in')
    with pytest.raises(SessionRequired):
        RestCrawler(fetcher, '_1_1', site_config=EXAMPLE_SITE).crawl()
    assert fetcher.fetch_json.call_count == 1


def test_mixed_failure_keeps_auth_priority_and_stops_later_resources(tmp_path):
    ctx = SimpleNamespace(snapshot='snap', root=tmp_path, request=FetchRequest(), source_fingerprints={})
    work = [('attachments', 'https://example.com/' + str(i), {}, MagicMock()) for i in range(3)]
    outcomes = [{'status': 'failed', 'needs_action': False}, {'status': 'failed', 'needs_action': True}]
    with patch.object(service, 'materialize', side_effect=outcomes) as run:
        rows, incomplete, outcome = service._materialize_work(None, ctx, work, [])
    assert run.call_count == 2
    assert rows == outcomes and incomplete and outcome == 'needs-action'


@pytest.mark.parametrize('boundary,recovery', [('capture', 'sync'), ('fetch', 'fetch')])
def test_sync_error_solution_selects_existing_command_and_preserves_scope(tmp_path, boundary, recovery):
    output = io.StringIO()
    argv = ['sync', '_1_1', '--site', EXAMPLE_SITE.origin, '--root', str(tmp_path / 'course space'), '--kind', 'attachments',
            '--asset', 'attachments:fixture', '--browser-session', 'shared session',
            '--browser-backend', 'ego', '--refresh']
    (tmp_path / 'course space').mkdir()
    with patch.object(service, 'Session'), \
            patch.object(service, 'capture', return_value={'outcome': 'complete'}), \
            patch.object(service, 'fetch', return_value={'outcome': 'complete'}), \
            patch.object(service, boundary, side_effect=SessionRequired('sign in')), \
            contextlib.redirect_stdout(output):
        assert cli.main(argv) == 4
    error = json.loads(output.getvalue())['errors'][0]
    assert error['code'] == 'blackboard.browser_session_required'
    command = shlex.split(error['solution'].splitlines()[1])
    assert command[2] == recovery
    parsed = cli.build_parser().parse_args(command[2:])
    assert parsed.browser_session == 'shared session' and parsed.browser_backend == 'ego'
    assert parsed.root == tmp_path / 'course space'
    assert parsed.asset == ['attachments:fixture'] and parsed.kind == ['attachments']
    assert not parsed.refresh


def test_auth_resource_result_emits_stable_code_and_solution():
    data = {'snapshot': 'snap', 'outcome': 'needs-action', 'results': [
        {'status': 'failed', 'needs_action': False}, {'status': 'failed', 'needs_action': True}]}
    output = io.StringIO()
    with patch.object(cli, 'execute', return_value=(data, 4)), contextlib.redirect_stdout(output):
        assert cli.main(['sync', '_1_1', '--root', '/tmp/course', '--browser-session', 'course-sync']) == 4
    payload = json.loads(output.getvalue())
    assert payload['data']['snapshot'] == 'snap'
    assert payload['errors'][0]['code'] == 'blackboard.browser_session_required'
    assert shlex.split(payload['errors'][0]['solution'].splitlines()[1])[2] == 'fetch'
