"""Capture/publish and materialize are separate, idempotent operations."""
from __future__ import annotations
import shlex
import sys
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from .archive_io import ArchiveIntegrityError, safe_archive_path
from .archive_repository import ArchiveState, MetadataWriter
from .browser_backend import BrowserSession
from .crawl_policy import evaluate_crawl_completeness
from .cookie_store import CookieStore
from . import providers
from .resource_store import Store, asset_id, clean, file_evidence, stable_url, _acquisition_errors, _needs_action
from .rest_crawler import RestCrawler
from .safe_fetch import HttpFetcher, cdn_path_key
from .sync_models import Attachment
from .ultra_content import parse_course_id
from .site_config import SiteConfig, from_origin, site_config_from_dict
from .cli_requests import CLI_PATH, FetchRequest, BrowserOptions, UsageError

def resolve_site(explicit: SiteConfig | None, tree: dict | None, *, course=None) -> SiteConfig:
    """Use the explicit site, published archive, or caller's course URL."""
    if explicit is not None:
        return explicit
    if tree:
        stored = tree.get('site_config')
        if isinstance(stored, dict):
            try:
                return site_config_from_dict(stored)
            except ValueError as exc:
                raise ArchiveIntegrityError('Published site configuration is invalid') from exc
        origin = str(tree.get('site_origin') or '').strip()
        if origin:
            return from_origin(origin)
    if course:
        from urllib.parse import urlsplit
        try:
            url = urlsplit(str(course))
            if url.scheme and url.netloc:
                return from_origin(f'{url.scheme}://{url.netloc}')
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
    raise UsageError('Specify --site URL or a full course URL for a new archive')


def resolve_course(value, site_config: SiteConfig):
    from .url_policy import require_blackboard_id
    try:
        if str(value).startswith('_'):
            return require_blackboard_id(value)
        return parse_course_id(value, None, site_config=site_config)
    except (RuntimeError, ValueError) as exc:
        raise UsageError(str(exc)) from exc


KINDS = ('attachments', 'reading-list', 'panopto', 'youtube', 'external-docs')


def _site_origin_mismatch(tree, site) -> bool:
    """True when a published tree's origin disagrees with the effective site."""
    origin = str(tree.get('site_origin') or '')
    return bool(origin) and origin != site.origin


def _verify_state_identity(db: Store, expected: dict[str, str]) -> None:
    """Fail closed unless the state file is bound to exactly this site/course."""
    if db.identity() != expected:
        raise ArchiveIntegrityError('Archive identity is missing or does not match the requested site/course; restore a complete archive or capture into a new directory')


class Session:
    def __init__(self, course_id, options: BrowserOptions, *, site_config: SiteConfig):
        self.site = site_config
        origin_token = self.site.origin.split('://', 1)[1].replace('.', '_').replace(':', '_')
        default_session = f"blackboard-{origin_token}-{course_id.strip('_')}"
        self.browser = BrowserSession(session=options.browser_session or default_session,
                                      backend=options.browser_backend)
        # Same-browser invariant by construction: the store reads cookies
        # through the very session object that navigates, so there is no
        # backend parameter to keep in sync.
        self.cookies = CookieStore(self.browser, domains=self.site.cookie_origins())
        self.fetcher = HttpFetcher(cookie_store=self.cookies, auth_origin=self.site.origin,
                                   site_config=self.site)

    def transport(self):
        """Return business transports without navigating or checking login."""
        return self.browser, self.fetcher

    def refresh_credentials(self):
        """Re-read browser cookies after an in-browser hop minted new ones."""
        self.cookies.refresh()

    def close(self):
        self.browser.close()


def ensure_writable(root):
    if not root.is_dir():
        raise ValueError('Course root must already exist')
    if (root / '_blackboard/course-tree.json').exists() and not (root / '_blackboard/state.json').exists():
        raise ArchiveIntegrityError(
            'Archive state is missing; restore a complete initialized archive or capture into a new course directory'
        )


def capture(root, course, session, *, dry_run=False, site_config: SiteConfig | None = None,
            pages=None, source_status=None) -> dict[str, Any]:
    state = ArchiveState(root)
    site = resolve_site(site_config, state.existing_tree, course=course)
    course_id = resolve_course(course, site)
    if state.existing_tree and state.existing_tree.get('course_id') != course_id:
        raise ArchiveIntegrityError('Course ID does not match existing archive')
    if state.existing_tree and _site_origin_mismatch(state.existing_tree, site):
        raise ArchiveIntegrityError('Site origin does not match existing archive')
    if state.existing_tree:
        verify_snapshot(state, state.existing_tree)
    if pages is None:
        _, fetcher = session.transport()
        crawler = RestCrawler(fetcher, course_id, site_config=site)
        pages = crawler.crawl()
        source_status = {'rest': crawler.rest_status}
    # Strip transient credentials from both structured metadata and body URLs.
    pages = [replace(p, url=stable_url(p.url), body_text=clean(p.body_text),
                     description_text=clean(p.description_text),
                     attachments=[replace(a, href=stable_url(a.href)) for a in p.attachments],
                     child_links=clean(p.child_links), external_links=clean(p.external_links),
                     unresolved_assets=clean(p.unresolved_assets)) for p in pages]
    gate = evaluate_crawl_completeness(pages, clean(source_status or {}), state.existing_tree)
    if dry_run:
        return {'published': False, 'dry_run': True, 'crawl': gate,
                'outcome': 'complete' if gate['safe_to_replace'] else 'blocked'}
    with Store(root, create=True) as db:
        generation = uuid.uuid4().hex
        db.ensure_identity(site.origin, course_id)
        writer = MetadataWriter(state, course_id, run_id=generation, site_config=site)
        writer.write_page_artifacts(pages, gate, defer_publish=True)
        # Verify a generation before selecting it, regardless of optional integrations.
        verify_snapshot(state, writer.last_tree_payload)
        if gate['safe_to_replace']:
            writer.publish_generation(generation)
        result = {'snapshot': generation, 'published': gate['safe_to_replace'], 'crawl': gate,
                  'outcome': 'complete' if gate['safe_to_replace'] else 'blocked',
                  'next': 'fetch' if gate['safe_to_replace'] else 'capture again after resolving crawl errors'}
        db.run('capture', result, run_id=generation, status='complete' if gate['safe_to_replace'] else 'blocked')
        return result


def verify_snapshot(state, tree):
    if not tree or not isinstance(tree.get('nodes'), list):
        raise ArchiveIntegrityError('Published course tree is missing or invalid')
    from .archive_checks import verify_node_links
    pages = state.load_tree_pages(tree)
    for node,page in zip(tree['nodes'],pages):
        verify_node_links(node, {'attachments':[a.__dict__ for a in page.attachments]})
        if node.get('page_text_sha256') and node['page_text_sha256'] != page.text_hash:
            raise ArchiveIntegrityError('Tree/source text hash disagreement')
        if node.get('description_text_sha256'):
            from .content_rules import normalize_block, sha256_text
            if node['description_text_sha256'] != sha256_text(normalize_block(page.description_text)):
                raise ArchiveIntegrityError('Tree/source description hash disagreement')
        if 'due_at' in node and node.get('due_at') != page.due_at:
            raise ArchiveIntegrityError('Tree/source due-date disagreement')
        if node.get('parent_id') != page.parent_id:
            raise ArchiveIntegrityError('Tree/source parent disagreement')
    return pages


def read_files(root, key=None):
    with Store(root) as db:
        return db.files(key=key)


def _resource_catalog(db, planned):
    """Return known planned and registered resource identities, read-only."""
    catalog = {item['asset_id']: dict(item) for item in planned}
    for key, row in db.registered_resources().items():
        catalog.setdefault(key, row)
    return catalog


def _coverage(root, tree, pages, planned):
    with Store(root) as db:
        catalog = _resource_catalog(db, planned)
        # Dynamic recordings/discovery are not enumerated by the static tree.
        expected = list(dict.fromkeys([item['asset_id'] for item in planned] +
                    [key for key, row in catalog.items() if row['kind'] in ('panopto', 'panopto-discovery')]))
        available = {key for key in catalog if db.reusable(key)}
        gaps, excluded = [], []
        for key in expected:
            row = catalog[key]
            info = db.decision_info(key) or {'decision': 'fetch', 'reason': ''}
            sources = [{'id': page.content_id, 'title': page.title,
                        'path': str(Path(node['page_dir']) / 'page.md')}
                       for page, node in zip(pages, tree['nodes'])
                       if page.content_id in row.get('metadata', {}).get('source_ids', []) or any(stable_url(u) == row['url'] for u in
                              [a.href for a in page.attachments] +
                              [link.get('href', '') for link in page.external_links])]
            detail = {'asset_id': key, 'kind': row['kind'], 'url': row['url'],
                      'decision': info['decision'], 'reason': info['reason'],
                      'source_pages': sources, 'present': key in available}
            if info['decision'] == 'exclude':
                excluded.append(detail)
            elif key not in available:
                failure = db.failure(key)
                detail['last_error'] = ({'error_code': failure.get('error_code', 'download.failed'),
                                         'message': failure['error']} if failure else None)
                gaps.append(detail)
        # Retained historical exclusions remain visible even after source withdrawal.
        for key in catalog.keys() - set(expected):
            info = db.decision_info(key)
            if info and info['decision'] == 'exclude':
                excluded.append({'asset_id': key, **info, 'present': key in available,
                                 'url': catalog[key]['url'], 'source_pages': []})
        pending = db.pending_ids()
        cleanup = db.cleanup_pending()
    return {'complete': not gaps and not pending and not cleanup,
            'expected_resources': len(expected),
            'locally_available': sum(key in available for key in expected),
            'gaps': gaps, 'excluded': excluded, 'pending_resource_ids': pending,
            'pending_file_cleanup': len(cleanup)}


def _identity_state_issue(existing_tree, identity, site_config):
    """Classify archive-identity readiness shared by status and validate.

    Returns 'identity-mismatch', 'identity-missing', 'site-config-required',
    or None (healthy). status maps the verdict to readiness ladders or a hard
    ArchiveIntegrityError; validate records it as an issue string.
    """
    if not identity:
        return 'identity-missing'
    expected_course_id = str(existing_tree.get('course_id') or '')
    tree_origin = str(existing_tree.get('site_origin') or '')
    if identity.get('course_id') != expected_course_id or (
        tree_origin and identity.get('origin') != tree_origin
    ):
        return 'identity-mismatch'
    if not site_config and not existing_tree.get('site_config') and not tree_origin:
        return 'site-config-required'
    return None


def status(root, *, site_config: SiteConfig | None = None):
    state = ArchiveState(root)
    if not state.existing_tree:
        state_path = safe_archive_path(root, '_blackboard/state.json')
        if state_path.exists():
            raise ArchiveIntegrityError('Course state exists without a published course tree')
        cli = CLI_PATH
        return {
            'course_id': None,
            'snapshot': None,
            'readiness': 'no-snapshot',
            'ready': False,
            'next_steps': [shlex.join([sys.executable, str(cli), 'capture', '<COURSE_URL>', '--root', str(root)])],
        }
    pages = verify_snapshot(state, state.existing_tree)
    if site_config and _site_origin_mismatch(state.existing_tree, site_config):
        raise ArchiveIntegrityError('Site config does not match the published course tree')
    if not safe_archive_path(root, '_blackboard/state.json').exists():
        return {'course_id': state.existing_tree.get('course_id'),
                'snapshot': state.existing_tree.get('generation_id'),
                'readiness': 'state-missing', 'ready': False, 'state_missing': True,
                'next_steps': [
                    'Restore a complete initialized archive',
                    'Or capture this course into a new course directory',
                ]}
    with Store(root) as db:
        identity = db.identity()
        verdict = _identity_state_issue(state.existing_tree, identity, site_config)
        if verdict == 'identity-mismatch':
            raise ArchiveIntegrityError('Archive identity does not match the published course tree')
        if verdict == 'identity-missing':
            return {'course_id': state.existing_tree.get('course_id'),
                    'site_origin': state.existing_tree.get('site_origin'),
                    'snapshot': state.existing_tree.get('generation_id'),
                    'readiness': 'identity-missing', 'ready': False,
                    'next_steps': [
                        'Restore a complete initialized archive or capture into a new course directory',
                    ]}
        if verdict == 'site-config-required':
            # This verdict is only reachable with a present identity.
            assert identity is not None
            return {'course_id': state.existing_tree.get('course_id'),
                    'site_origin': identity.get('origin'),
                    'archive_identity': identity,
                    'snapshot': state.existing_tree.get('generation_id'),
                    'readiness': 'site-config-required', 'ready': False,
                    'next_steps': ['Pass --site URL, or --site-config PATH when integration settings are needed']}
        files = db.files()
        attempts = db.attempts()
        runs = list(reversed(db.runs()))
    generations_dir = safe_archive_path(root, '_blackboard/generations')
    published = state.existing_tree.get('generation_id')
    # A healthy verdict requires a present archive identity.
    assert identity is not None
    generations = sorted(
        p.name for p in generations_dir.iterdir()
        if p.is_dir() and not p.name.startswith('.') and p.name != published
    ) if generations_dir.exists() else []
    site = resolve_site(site_config, state.existing_tree)
    planned = providers.plan_catalog(root, state, state.existing_tree, pages, site)
    inventory = _coverage(root, state.existing_tree, pages, planned)
    return {'course_id': state.existing_tree.get('course_id'), 'site_origin': state.existing_tree.get('site_origin') or identity.get('origin'),
            'archive_identity': identity, 'snapshot': state.existing_tree.get('generation_id'),
            'readiness': 'ready' if inventory['complete'] else 'partial', 'ready': inventory['complete'],
            'crawl': state.existing_tree.get('crawl'), 'files': {'registered': len(files), 'present': sum(f['present'] for f in files)},
            'runs': runs, 'attempts': attempts[:20], 'coverage': inventory, 'gaps': inventory['gaps'], 'excluded': inventory['excluded'],
            'other_generations': generations}


def request_get(root, path: str, *, site_config: SiteConfig | None = None):
    """Raw read-only GET of a site-relative JSON path through the cookie chain."""
    state = ArchiveState(root)
    if not state.existing_tree:
        raise ArchiveIntegrityError('No published course tree; capture first')
    site = resolve_site(site_config, state.existing_tree)
    if not str(path).startswith('/'):
        path = '/' + str(path)
    url = site.origin.rstrip('/') + path
    origin = site.origin.rstrip('/')
    # One short-lived resolved session per call: cookies come from the same
    # chain BrowserSession resolves (pinned/env-aware, never re-implemented
    # here). This command cleans up its own task group afterward; cookies
    # remain in the browser profile.
    browser = BrowserSession(session='blackboard-request')
    try:
        cookies = CookieStore(browser, domains=site.cookie_origins())
        fetcher = HttpFetcher(cookie_store=cookies, auth_origin=site.origin,
                              site_config=site)
        result = fetcher.fetch_json(url, url_policy=lambda u: u.startswith(origin))
    finally:
        browser.close()
    return {'path': path, 'data': result}


def materialize(db, kind, url, metadata, snapshot, root, callback, *, refresh=False, probe=None):
    key = asset_id(kind, url)
    info = db.decision_info(key)
    if info and info['decision'] != 'fetch':
        return {'asset_id': key, 'kind': kind, 'url': stable_url(url),
                'status': info['decision'], 'reason': info['reason']}
    key = db.asset(kind, url, metadata)
    reusable = None if refresh else db.reusable(key, None if kind == 'attachments' else snapshot)
    if reusable and kind == 'attachments' and probe is not None:
        prior = db.revision(key)
        if prior.get('checked_snapshot') != snapshot:
            try:
                head = probe()
            except _acquisition_errors() as exc:
                db.finish(db.start(key, snapshot), 'failed', {'error': str(exc)})
                return {**reusable, 'status':'failed', 'error':clean(str(exc)), 'needs_action': _needs_action(exc)}
            if head is None:
                db.finish(db.start(key, snapshot), 'failed', {'error': 'Remote revision check failed'})
                return {**reusable, 'status':'failed', 'error':'Remote revision check failed; existing local files preserved'}
            remote = cdn_path_key(head.get('effective_url') or '')
            if prior.get('cdn_path') != remote:
                reusable = None
            else:
                db.asset(kind,url,{'checked_snapshot':snapshot})
    if reusable:
        return reusable
    with db.acquisition(key, snapshot) as acquisition:
        acquisition.result = callback(acquisition.stage)
    return acquisition.outcome


def _decided_row(catalog, decided, key):
    row = catalog[key]
    return {'asset_id': key, 'kind': row['kind'], 'url': row['url'],
            'status': decided[key]['decision'], 'reason': decided[key]['reason']}


def _decided_only_result(snapshot, request, results, *, recovered=None, pending_ids=None):
    """Fetch payload for an answered-entirely-by-resource-decisions request.

    ``pending_ids is not None`` marks the settled variant returned after
    recovery; the offline fast path (before recovery) omits the field.
    """
    payload = {'snapshot': snapshot, 'dry_run': bool(request.dry_run), 'results': results,
               'recovered': recovered if recovered is not None else [],
               'complete': not pending_ids,
               'outcome': 'blocked' if pending_ids else 'complete',
               'dynamic_discovery_pending': False}
    if pending_ids is not None:
        payload['pending_resource_ids'] = pending_ids
    return payload


def _offline_fetch_plan(root, state, tree, site, expected_identity):
    """Verified pages plus the pre-recovery offline inventory.

    Read before any recovery, discovery or session activity: this is what
    makes a decided-only request a genuinely offline skip.
    """
    pages = verify_snapshot(state, tree)
    snapshot = tree.get('generation_id') or 'legacy'
    with Store(root) as planning_db:
        _verify_state_identity(planning_db, expected_identity)
        resources = providers.plan_resources(root, state, tree, pages, site)
        planned = providers.plan_catalog(root, state, tree, pages, site, resources=resources)
        catalog = _resource_catalog(planning_db, planned)
        decided = {key: info for key in catalog
                   if (info := planning_db.decision_info(key)) and info['decision'] != 'fetch'}
        pending_ids = planning_db.pending_ids()
    return pages, snapshot, resources, planned, catalog, decided, pending_ids


def _explicit_attachment_work(request, session_factory, site):
    """The one-off ``--url``/``--out`` override; deliberately not a provider."""
    assert request.out is not None  # enforced by the CLI boundary
    destination = request.out
    attachment = Attachment(destination.name, request.url, original_filename=destination.name)
    return [('attachments', request.url, {'destination': str(destination)},
             lambda stage: providers.attachment_download(session_factory(), attachment,
                                                         destination, stage, site_config=site))]


def _partition_selected(work, selected_ids, decided, decided_row, answered_ids=()):
    """Restrict work to an explicit selection and split off decided rows."""
    if selected_ids:
        selected = set(selected_ids)
        found = {asset_id(kind, url) for kind, url, _, _ in work}
        # Registered decided resources remain addressable even when a later
        # snapshot no longer lists them.  They are reported as decided below.
        missing_selected = selected - found - set(decided) - set(answered_ids)
        if missing_selected:
            raise UsageError('Unknown or out-of-scope asset IDs: ' + ', '.join(sorted(missing_selected)))
        work = [w for w in work if asset_id(w[0], w[1]) in selected]
    decided_work = []
    retained = []
    for item in work:
        key = asset_id(item[0], item[1])
        if key in decided:
            decided_work.append(decided_row(key))
        else:
            retained.append(item)
    represented_decided = {item['asset_id'] for item in decided_work}
    if selected_ids:
        decided_work.extend(decided_row(key) for key in selected_ids
                            if key in decided and key not in represented_decided)
    return retained, decided_work


def _materialize_work(db, ctx, work, prefix):
    """Materialize until authentication fails; keep earlier resource outcomes."""
    registry = providers.REGISTRY
    results = list(prefix)
    for k, u, m, callback in work:
        if any(row.get('needs_action') for row in results):
            break
        results.append(materialize(db, k, u, m, ctx.snapshot, ctx.root, callback,
                                   refresh=ctx.request.refresh,
                                   probe=registry[k].probe(ctx, u)))
    failed = any(r['status'] == 'failed' for r in results)
    outcome = ('needs-action' if any(r.get('needs_action') for r in results if r['status'] == 'failed')
               else 'partial' if failed else 'complete')
    return results, failed, outcome


def _dry_run_result(snapshot, pending_ids, kinds, discovery_skips, decided_work, work):
    results = discovery_skips + decided_work + [
        {'asset_id': asset_id(k, u), 'kind': k, 'url': stable_url(u)}
        for k, u, _, _ in work
    ]
    return {'snapshot': snapshot, 'dry_run': True, 'results': results,
            'pending_resource_ids': pending_ids, 'complete': not pending_ids,
            'outcome': 'blocked' if pending_ids else 'complete',
            'dynamic_discovery_pending': providers.PANOPTO.kind in kinds and not discovery_skips}


def fetch(root, request: FetchRequest, session, *, site_config: SiteConfig | None = None):
    root = root.resolve()
    state = ArchiveState(root)
    tree = state.existing_tree
    site = resolve_site(site_config, tree)
    if _site_origin_mismatch(tree, site):
        raise ArchiveIntegrityError('Site origin does not match existing archive')
    expected_identity = {'origin': site.origin, 'course_id': str(tree.get('course_id') or '')}
    # Check the mutable state identity before recovery or any provider/session
    # work. A mismatched archive must never be allowed to settle pending files.
    pages, snapshot, resources, planned, catalog, decided, pending_ids = _offline_fetch_plan(root, state, tree, site, expected_identity)
    def finish(result):
        inventory = _coverage(root, tree, pages, planned)
        if request.dry_run and inventory['pending_file_cleanup']:
            result.update(complete=False, outcome='blocked')
        return {**result, 'coverage': inventory, 'gaps': inventory['gaps'], 'excluded': inventory['excluded']}
    selected_ids = list(dict.fromkeys(request.assets or []))
    unknown_selected = [key for key in selected_ids if key not in catalog]
    if unknown_selected:
        raise UsageError('Unknown or out-of-scope asset IDs: ' + ', '.join(unknown_selected))
    def decided_row(key):
        return _decided_row(catalog, decided, key)
    # A decided-only request is answered before recovery or any session work;
    # with unrelated pending attempts it is deferred until after recovery.
    explicit_key = asset_id('attachments', request.url) if request.url else None
    decided_only = ([explicit_key] if explicit_key in decided else
                    selected_ids if selected_ids and all(key in decided for key in selected_ids) else [])
    if decided_only and not pending_ids and not Store(root).cleanup_pending():
        return finish(_decided_only_result(snapshot, request, [decided_row(key) for key in decided_only]))
    owns_session = False

    def session_for():
        """Construct browser/cookie transports only when a provider needs them."""
        nonlocal session, owns_session
        if session is None:
            session = Session(resolve_course(tree['course_id'], site),
                              BrowserOptions(browser_session=request.browser_session,
                                             browser_backend=request.browser_backend), site_config=site)
            owns_session = True
        return session

    ctx = providers.FetchContext(root=root, tree=tree, site=site, request=request,
                                 snapshot=snapshot, pages=pages, session_factory=session_for)
    registry = providers.REGISTRY
    recovered = []
    if not request.dry_run:
        with Store(root, create=True) as recovery_db:
            recovered = recovery_db.recover()
        # ``recover`` settles every pending resource before any provider work.
        pending_ids = []
    if decided_only:
        return finish(_decided_only_result(snapshot, request, [decided_row(key) for key in decided_only],
                                          recovered=recovered, pending_ids=pending_ids))
    try:
        kinds = providers.requested_kinds(request)
        planned_resources = [item for item in resources if item['kind'] in kinds]
        static_resources = [item for item in planned_resources if registry[item['kind']].configured(ctx)]
        work: list[tuple[str, str, dict[str, Any], Callable[[Path], dict[str, Any]]]] = []
        evidenced_kinds = {providers.resource_kind(item['asset_id']) for item in planned}
        discovery_skips = [provider.unconfigured_notice(ctx) for kind, provider in registry.items()
                           if kind in kinds and not provider.configured(ctx)
                           and (kind in (request.kinds or ()) or kind in evidenced_kinds)]
        for row in discovery_skips:
            if row['asset_id'] in decided:
                row.update(status=decided[row['asset_id']]['decision'], reason=decided[row['asset_id']]['reason'])
        discovery_failures = []
        if request.url:
            work.extend(_explicit_attachment_work(request, session_for, site))
        else:
            if providers.PANOPTO.kind in kinds and providers.PANOPTO.configured(ctx):
                recordings, discovery = providers.PANOPTO.expand(
                    ctx, [item for item in static_resources if item['kind'] == providers.PANOPTO.kind],
                    catalog=catalog, decisions=decided)
                static_resources = [item for item in static_resources if item['kind'] != providers.PANOPTO.kind] + recordings
                discovery_failures = [row for row in discovery if row['status'] == 'failed']
                discovery_skips.extend(row for row in discovery if row['status'] != 'failed')
        if any(row.get('needs_action') for row in discovery_failures):
            # Discovery may not have enumerated selected dynamic assets yet.
            work, decided_work = [], []
        else:
            if not request.url:
                work.extend((item['kind'], item['url'], item['metadata'], registry[item['kind']].build(ctx, item))
                            for item in static_resources)
            answered_ids = [row['asset_id'] for row in discovery_skips + discovery_failures]
            work, decided_work = _partition_selected(work, selected_ids, decided, decided_row, answered_ids)
        if request.dry_run:
            result = _dry_run_result(snapshot, pending_ids, kinds, discovery_skips, decided_work, work)
            return finish(result)
        with Store(root, create=True) as db:
            run = db.run('fetch', {'snapshot': snapshot}, status='running')
            results, failed, outcome = _materialize_work(db, ctx, work,
                                                         discovery_skips + decided_work + discovery_failures)
            result = {'snapshot': snapshot, 'results': results, 'recovered': recovered,
                      'complete': not failed, 'outcome': outcome}
            db.run('fetch', result, run_id=run, status='failed' if failed else 'complete')
            return finish(result)
    finally:
        if owns_session and session is not None:
            session.close()


def validate(root, *, site_config: SiteConfig | None = None):
    state = ArchiveState(root)
    issues = []
    # verify_snapshot already verifies node links and tree/source agreement.
    pages = verify_snapshot(state, state.existing_tree)
    with Store(root) as db:
        identity = db.identity()
        verdict = _identity_state_issue(state.existing_tree, identity, site_config)
        if verdict == 'identity-mismatch':
            issues.append('Archive identity does not match the published course tree')
        elif verdict == 'identity-missing':
            issues.append('Archive identity is missing; restore a complete archive or capture into a new directory')
        elif verdict == 'site-config-required':
            issues.append('Archive site is missing; pass --site URL or --site-config PATH')
        files = db.files()
        for f in files:
            if not f['present']:
                issues.append('Missing registered file: ' + f['path'])
                continue
            try:
                file_evidence(Path(f['absolute_path']))
            except (RuntimeError, ValueError, OSError) as exc:
                issues.append(f['path'] + ': ' + str(exc))
        pending_ids = db.pending_ids()
        prepared_attempts = db.prepared_attempt_count()
    if site_config and _site_origin_mismatch(state.existing_tree, site_config):
        issues.append('Site config does not match the published course tree')
    site = resolve_site(site_config, state.existing_tree)
    planned = providers.plan_catalog(root, state, state.existing_tree, pages, site)
    inventory = _coverage(root, state.existing_tree, pages, planned)
    pending = len(pending_ids) + inventory['pending_file_cleanup']
    coverage_ok = inventory['complete']
    return {'snapshot': state.existing_tree.get('generation_id'), 'site_origin': state.existing_tree.get('site_origin') or (identity or {}).get('origin'),
            'archive_identity': identity, 'nodes': len(pages), 'files': len(files),
            'metadata_ok':True, 'coverage':inventory, 'gaps': inventory['gaps'], 'excluded': inventory['excluded'],
            'pending_attempts': pending, 'prepared_attempts': prepared_attempts,
            'issues': issues, 'ok': not issues and not pending and coverage_ok}
