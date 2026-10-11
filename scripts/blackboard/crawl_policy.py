"""Completeness and explicit identity differences, independent of downloads."""
from __future__ import annotations
from typing import Any
from .resource_store import stable_url
from .content_rules import normalize_block, sha256_text
from .sync_models import Page

ULTRA_ROUTE_IDS = {'ultra-outline', 'ultra-announcements', 'ultra-calendar', 'ultra-books-and-tools'}


def tree_diff(old, new):
    before = {n['id']: n for n in (old or {}).get('nodes', [])}
    after = {n['id']: n for n in (new or {}).get('nodes', [])}
    removals = []
    def add(kind, value):
        removals.append({'kind': kind, 'identity': value})
    for key in sorted(before.keys() - after.keys()):
        add('node', key)
        removals[-1]['before'] = before[key]
    old_urls = {stable_url(u) for n in before.values() for u in n.get('attachment_urls', [])}
    new_urls = {stable_url(u) for n in after.values() for u in n.get('attachment_urls', [])}
    for url in sorted(old_urls - new_urls):
        add('attachment', url)
    changes = [{'id': key, 'before_parent': before[key].get('parent_id'), 'after_parent': after[key].get('parent_id'),
                'before_title': before[key].get('title'), 'after_title': after[key].get('title'),
                'before': before[key], 'after': after[key]}
               for key in sorted(before.keys() & after.keys())
               if any(before[key].get(f) != after[key].get(f) for f in ('parent_id', 'title', 'page_text_sha256', 'description_text_sha256', 'due_at'))]
    return {'removed': removals, 'added_nodes': sorted(after.keys() - before.keys()), 'changed_nodes': changes}


def evaluate_crawl_completeness(pages: list[Page], source_status: dict, existing_tree=None) -> dict[str, Any]:
    rest = source_status.get('rest') or {}
    reasons = []
    if not pages:
        reasons.append('no-pages')
    elif not any(p.node_type != 'route' and p.content_id not in ULTRA_ROUTE_IDS for p in pages):
        reasons.append('route-shell-only')
    if any(p.unresolved_assets for p in pages):
        reasons.append('unresolved-embedded-assets')
    if rest.get('pagination_truncated'):
        reasons.append('rest-pagination-truncated')
    if rest.get('errors'):
        reasons.append('rest-errors')
    ids = [p.content_id for p in pages]
    if len(ids) != len(set(ids)):
        reasons.append('duplicate-node-id')
    # Some recursive REST responses omit the enclosing course container. A top-level
    # record represents that boundary explicitly with depth 0, one breadcrumb,
    # and the unresolved parent ID as parent_title. Do not invent a page for it.
    boundaries = {p.parent_id for p in pages if p.parent_id and p.parent_id not in ids
                  and p.depth == 0 and len(p.breadcrumbs) == 1 and p.parent_title == p.parent_id}
    if any(p.parent_id and p.parent_id not in ids and p.parent_id not in boundaries for p in pages):
        reasons.append('missing-parent')
    parents = {p.content_id:p.parent_id for p in pages}
    for node in parents:
        seen = set()
        current = node
        while current in parents:
            if current in seen:
                reasons.append('parent-cycle')
                break
            seen.add(current)
            current = parents[current]
        if 'parent-cycle' in reasons:
            break
    tree = {'nodes': [{'id': p.content_id, 'parent_id': p.parent_id, 'title': p.title,
                       'attachment_urls': [a.href for a in p.attachments],
                       'page_text_sha256': p.text_hash,
                       'description_text_sha256': sha256_text(normalize_block(p.description_text)),
                       'due_at': p.due_at} for p in pages]}
    diff = tree_diff(existing_tree, tree)
    return {'status': 'blocked' if reasons else 'complete',
            'complete': not reasons, 'safe_to_replace': not reasons,
            'reasons': reasons, 'source_status': source_status, 'diff': diff, 'boundary_parent_ids':sorted(boundaries),
            'pages': len(pages), 'attachments': sum(len(p.attachments) for p in pages)}
