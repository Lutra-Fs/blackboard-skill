"""One acquisition provider per resource kind.

Each provider owns its kind's knowledge: how the published tree is scanned
for candidates (``plan``) and how one resource is downloaded into the per-attempt
staging directory (``build``). Panopto additionally expands its recording list.
``course_service.fetch`` orchestrates: it sequences providers, filters by
request scope and stored decisions, and runs every download through
``Store.acquisition``. The ``--url``/``--out`` explicit attachment path is a
one-off override in ``fetch`` and deliberately not a provider.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypedDict

from .archive_io import ArchiveIntegrityError, safe_archive_path
from .archive_repository import ArchiveState
from .attachment_plan import plan_unique_attachments
from .cli_requests import FetchRequest
from .content_rules import embedded_html_linked_download, is_unsafe_html_attachment_name, safe_filename
from . import external_documents as ext
from . import panopto_transcripts as pan
from . import reading_lists as rl
from .resource_store import Store, asset_id, file_evidence, stable_url, _needs_action
from .redaction import clean
from .safe_fetch import cdn_path_key, filename_from_content_disposition
from .runtime import CommandRunner
from .site_config import SiteConfig
from .sync_models import Page
from .ultra_content import canonical_ultra_course_url
from .url_policy import is_safe_public_https_url, is_trusted_attachment_source_url
from . import youtube_transcripts as yt

class StaticResource(TypedDict):
    """One offline resource description plus its archived source object.

    ``source`` is the kind-specific source object (an AttachmentPlan,
    ReadingListTool, ExternalDoc, YouTubeLink or PanoptoLink); it never
    leaves the in-process plan.
    """
    asset_id: str
    kind: str
    url: str
    metadata: dict[str, Any]
    source: Any


@dataclass
class FetchContext:
    """Per-fetch context handed to the provider hooks.

    ``session_factory`` lazily returns the (possibly caller-owned)
    ``course_service.Session``; only browser-backed downloads and live
    Panopto expansion use it. Offline planning never opens a browser.
    """
    root: Path
    tree: dict[str, Any]
    pages: list[Page]
    site: SiteConfig
    request: FetchRequest
    snapshot: str
    session_factory: Callable[[], Any]
    runner: CommandRunner = field(default_factory=CommandRunner)


def _record(kind: str, url: str, source: Any, metadata: dict[str, Any] | None = None) -> StaticResource:
    stable = stable_url(url)
    return {'asset_id': asset_id(kind, stable), 'kind': kind, 'url': stable,
            'metadata': metadata or {}, 'source': source}


class Provider:
    """Per-kind acquisition knowledge; see the module docstring."""

    kind: str

    def plan(self, root: Path, state: ArchiveState, tree: dict[str, Any],
             pages: list, site: SiteConfig) -> list[StaticResource]:
        """Offline enumeration from the published tree; no browser or network."""
        raise NotImplementedError

    def configured(self, ctx: FetchContext) -> bool:
        """Whether the site configuration enables this kind."""
        return True

    def unconfigured_notice(self, ctx: FetchContext) -> dict[str, Any]:
        """Result row explaining an unavailable kind."""
        raise NotImplementedError  # only reached when configured() is False

    def probe(self, ctx: FetchContext, url: str) -> Callable[[], dict[str, Any]] | None:
        """Remote revision probe for the refresh-reuse check, or None."""
        return None

    def build(self, ctx: FetchContext, resource: StaticResource) -> Callable[[Path], dict[str, Any]]:
        """Stage callback that downloads one planned resource."""
        raise NotImplementedError


def attachment_download(session, attachment, destination, stage, *, site_config: SiteConfig):
    if not is_trusted_attachment_source_url(attachment.href, site_config=site_config):
        raise ArchiveIntegrityError('Attachment source is not authorized')
    _, fetcher = session.transport()
    target = safe_archive_path(stage, destination)
    temp = target.with_name(target.name + '.part')
    outcome = fetcher.download(attachment.href, temp, url_policy=is_safe_public_https_url)
    if is_unsafe_html_attachment_name(attachment.original_filename or attachment.text):
        linked = embedded_html_linked_download(temp, site_config=site_config)
        if not linked:
            raise ArchiveIntegrityError('HTML wrapper has no trusted document link')
        url, name = linked
        fetcher.download(url, temp, url_policy=lambda value: is_trusted_attachment_source_url(value, site_config=site_config))
        target = target.with_name(safe_filename(name, url))
    else:
        real = filename_from_content_disposition(outcome.content_disposition)
        if real:
            target = target.with_name(safe_filename(real, attachment.href))
    file_evidence(temp, target.name)
    temp.replace(target)  # Only inside an empty per-attempt staging directory.
    return {'status': 'downloaded', 'cdn_path': cdn_path_key(outcome.effective_url)}


class AttachmentsProvider(Provider):
    kind = 'attachments'

    def plan(self, root, state, tree, pages, site):
        planned = []
        for plan in plan_unique_attachments(pages, state.bucket_for_page):
            if is_trusted_attachment_source_url(plan.attachment.href, site_config=site):
                planned.append(_record('attachments', plan.attachment.href, plan, {'destination': str(plan.destination)}))
        return planned

    def probe(self, ctx, url):
        def revision_probe():
            session = ctx.session_factory()
            _, fetcher = session.transport()
            return fetcher.probe(url, url_policy=is_safe_public_https_url)
        return revision_probe

    def build(self, ctx, resource):
        plan = resource['source']
        def download(stage):
            return attachment_download(ctx.session_factory(), plan.attachment, plan.destination,
                                       stage, site_config=ctx.site)
        return download


class ReadingListProvider(Provider):
    kind = 'reading-list'

    def plan(self, root, state, tree, pages, site):
        planned = []
        for tool in rl.iter_archived_reading_list_tools(root, tree=tree, pages=pages, site_config=site):
            for fmt in ('ris', 'xlsx'):
                sep = '&' if '?' in tool.launch_url else '?'
                planned.append(_record('reading-list', tool.launch_url + sep + 'export_format=' + fmt,
                                       tool, {'format': fmt}))
        return planned

    def configured(self, ctx):
        return bool(ctx.site.leganto_origin)

    def unconfigured_notice(self, ctx):
        return {'asset_id': 'reading-list:unconfigured', 'kind': 'reading-list',
                'url': None, 'status': 'unavailable',
                'reason': 'site-config-missing-leganto-origin'}

    def build(self, ctx, resource):
        tool = resource['source']
        fmt = resource['metadata']['format']
        def export(stage):
            session = ctx.session_factory()
            browser, fetcher = session.transport()
            return rl.export_reading_list(browser, fetcher, replace(tool, course_root=stage),
                                          wait_seconds=2, retry_count=2, formats=(fmt,),
                                          site_config=ctx.site,
                                          refresh_credentials=session.refresh_credentials)
        return export


class ExternalDocsProvider(Provider):
    kind = 'external-docs'

    def plan(self, root, state, tree, pages, site):
        return [_record('external-docs', doc.url, doc)
                for doc in ext.iter_external_documents(root, tree=tree, pages=pages, site_config=site)]

    def build(self, ctx, resource):
        doc = resource['source']
        def download(stage):
            return ext.download_document(replace(doc, course_root=stage), runner=ctx.runner)
        return download


class YouTubeProvider(Provider):
    kind = 'youtube'

    def plan(self, root, state, tree, pages, site):
        return [_record('youtube', yt.canonical_viewer_url(link.url), link)
                for link in yt.iter_youtube_links(root, pages=pages)]

    def build(self, ctx, resource):
        link = resource['source']
        def video(stage):
            canonical = yt.canonical_viewer_url(link.url)
            configured_browser = ctx.request.youtube_cookies_browser or None
            runner = ctx.runner
            try:
                meta = yt.fetch_video_metadata(canonical, cookies_browser=None, runner=runner)
            except yt.CommandExecutionError:
                if not configured_browser:
                    raise
                meta = yt.fetch_video_metadata(canonical, cookies_browser=configured_browser, runner=runner)
            return yt.save_transcripts(replace(link, course_root=stage), meta,
                                       languages=['en'], cookies_browser=configured_browser, runner=runner)
        return video


class PanoptoProvider(Provider):
    kind = 'panopto'

    def plan(self, root, state, tree, pages, site):
        return [_record('panopto', pan.canonical_viewer_url(link.url, site_config=site), link)
                for link in pan.iter_panopto_links(root, pages=pages, site_config=site)]

    @staticmethod
    def discovery_resource(tree, pages, site):
        sources = [page.content_id for page in pages if any(
            link.get('kind') == 'lti_launch' and str(link.get('handle', '')).lower() == 'panopto'
            for link in page.external_links)]
        return _record('panopto-discovery', canonical_ultra_course_url(tree['course_id'], site_config=site),
                       None, {'source_ids': sources})

    def configured(self, ctx):
        return bool(ctx.site.panopto_origin)

    def unconfigured_notice(self, ctx):
        resource = self.discovery_resource(ctx.tree, ctx.pages, ctx.site)
        if resource['metadata']['source_ids']:
            return {'asset_id': resource['asset_id'], 'kind': resource['kind'],
                    'url': resource['url'], 'status': 'unavailable',
                    'reason': 'site-config-missing-panopto-origin'}
        return {'asset_id': 'panopto:unconfigured', 'kind': 'panopto',
                'url': None, 'status': 'unavailable',
                'reason': 'site-config-missing-panopto-origin'}

    def expand(self, ctx, seeds, *, catalog, decisions):
        """Dynamic course-menu discovery for recordings the tree cannot list.

        Planned links seed the recording set; a cached discovery run reuses
        its recorded links, otherwise the Blackboard course menu is walked
        live and the outcome persisted as its own ``panopto-discovery`` asset.
        """
        results = []
        links = {item['url']: item['source'] for item in seeds}
        discovery = self.discovery_resource(ctx.tree, ctx.pages, ctx.site)
        discovery_url, discovery_key = discovery['url'], discovery['asset_id']
        selected = list(dict.fromkeys(ctx.request.assets or ()))
        selected_panopto = {key for key in selected if key in catalog and catalog[key]['kind'] == 'panopto'}
        for key in sorted(selected_panopto - set(decisions)):
            url = catalog[key]['url']
            links.setdefault(url, pan.PanoptoLink(url, course_root=ctx.root))
        # A selected attachment (or a recording with an acquisition decision) must not
        # trigger dynamic menu discovery.  Discovery itself is a network
        # operation and is therefore gated before it can create a state
        # record or call the browser.
        need_discovery = not selected or discovery_key in selected or bool(selected_panopto - set(decisions))
        discovery_decision = decisions.get(discovery_key)
        if discovery_decision:
            results.append({'asset_id': discovery_key, 'kind': 'panopto-discovery',
                            'url': discovery_url,
                            'status': discovery_decision['decision'], 'reason': discovery_decision['reason']})
            need_discovery = False
        if not ctx.request.dry_run and need_discovery:
            with Store(ctx.root, create=True) as discovery_db:
                discovery_key = discovery_db.asset('panopto-discovery', discovery_url)
                cached = None if ctx.request.refresh else discovery_db.reusable(discovery_key, ctx.snapshot)
                if cached:
                    for item in discovery_db.success(discovery_key).get('links', []):
                        link = pan.PanoptoLink(item['url'], course_root=ctx.root, link_title=item.get('title'))
                        links.setdefault(pan.canonical_viewer_url(link.url, site_config=ctx.site), link)
                else:
                    attempt = discovery_db.start(discovery_key, ctx.snapshot)
                    try:
                        browser, _ = ctx.session_factory().transport()
                        discovered = pan.discover_course_menu_panopto_links(
                            ctx.root, browser, wait_seconds=2, retry_count=2,
                            course_id=ctx.tree['course_id'], pages=ctx.pages, site_config=ctx.site)
                        for link in discovered:
                            links.setdefault(pan.canonical_viewer_url(link.url, site_config=ctx.site), link)
                        discovery_db.finish(attempt, 'complete', {
                            'links': [{'url': pan.canonical_viewer_url(l.url, site_config=ctx.site),
                                       'title': l.link_title} for l in discovered]})
                    except Exception as exc:
                        discovery_db.finish(attempt, 'failed', {'error': str(exc)})
                        results.append({'asset_id': discovery_key, 'status': 'failed',
                                        'error': clean(str(exc)),
                                        'needs_action': _needs_action(exc)})
        if discovery_key in selected and not discovery_decision and not any(r['status'] == 'failed' for r in results):
            if not ctx.request.dry_run:
                # Discovery-only retries must still expose unacquired recordings.
                with Store(ctx.root, create=True) as inventory_db:
                    for url in links:
                        inventory_db.asset('panopto', url)
            results.append({'asset_id': discovery_key, 'kind': 'panopto-discovery',
                            'url': discovery_url,
                            'status': 'planned' if ctx.request.dry_run else 'complete'})
        return [_record('panopto', url, link) for url, link in links.items()], results

    def build(self, ctx, resource):
        link = resource['source']
        def recording(stage):
            browser, _ = ctx.session_factory().transport()
            browser.open(link.url)
            time.sleep(2)
            data = browser.eval_json(pan.delivery_script(link.video_id, link.is_tid))
            return pan.save_transcripts(replace(link, course_root=stage), data, site_config=ctx.site)
        return recording


# Registry order is execution order: it fixes the order planned resources are
# yielded in and the order unconfigured notices appear in fetch results.
PANOPTO = PanoptoProvider()
REGISTRY: dict[str, Provider] = {
    provider.kind: provider
    for provider in (AttachmentsProvider(), ReadingListProvider(), ExternalDocsProvider(),
                     YouTubeProvider(), PANOPTO)
}


def resource_kind(key):
    kind = key.split(':', 1)[0]
    return 'panopto' if kind == 'panopto-discovery' else kind


def requested_kinds(request):
    kinds = list(request.kinds or REGISTRY)
    if request.assets:
        selected = {resource_kind(key) for key in request.assets}
        kinds = [kind for kind in kinds if kind in selected]
    return kinds


def plan_resources(root, state, tree, pages, site, kinds=None) -> list[StaticResource]:
    """Deduplicated planned resources for *kinds*, in registry execution order."""
    selected = set(kinds) if kinds is not None else None
    records: list[StaticResource] = []
    for kind, provider in REGISTRY.items():
        if selected is not None and kind not in selected:
            continue
        records.extend(provider.plan(root, state, tree, pages, site))
    seen = set()
    unique = []
    for record in records:
        if record['asset_id'] in seen:
            continue
        seen.add(record['asset_id'])
        unique.append(record)
    return unique


def plan_catalog(root, state, tree, pages, site, kinds=None, *, resources=None) -> list[dict[str, Any]]:
    """Enumerate archived resources without browser, authentication or network."""
    resources = resources if resources is not None else plan_resources(root, state, tree, pages, site, kinds)
    catalog = [{'asset_id': item['asset_id'], 'kind': item['kind'], 'url': item['url'], 'metadata': item['metadata']}
               for item in resources]
    discovery = PANOPTO.discovery_resource(tree, pages, site)
    if discovery['metadata']['source_ids'] and (kinds is None or 'panopto' in kinds):
        catalog.append({key: value for key, value in discovery.items() if key != 'source'})
    return catalog
