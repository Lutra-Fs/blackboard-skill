"""Offline change facts and explicit archive decisions; no plan lifecycle."""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

from .archive_io import ArchiveIntegrityError, safe_archive_path
from .archive_repository import ArchiveState
from .cli_requests import CLI_PATH, UsageError
from .content_rules import sha256_file
from .crawl_policy import tree_diff
from .resource_store import Store, clean, file_evidence


class ChangeStale(ArchiveIntegrityError):
    code = 'blackboard.change_stale'

    def __init__(self, message, root):
        super().__init__(message)
        cli = CLI_PATH
        command = shlex.join([sys.executable, str(cli), 'change', 'diff', '--root', str(root)])
        self.solution = f'Run {command}, review current files and resource facts, then update the plan.'


def change_diff(root):
    from .course_service import _coverage, resolve_site, verify_snapshot
    from . import providers

    state = ArchiveState(root)
    tree = state.existing_tree
    pages = verify_snapshot(state, tree)
    previous = tree.get('previous_snapshot')
    before = state.load_generation_tree(previous) if previous else None
    with Store(root) as db:
        files = db.files()
        pending_cleanup = db.cleanup_pending()
    for row in files:
        row['current_sha256'] = sha256_file(Path(row['absolute_path'])) if row['present'] else None
        row['locally_modified'] = row['present'] and row['current_sha256'] != row['sha256']
    planned = providers.plan_catalog(root, state, tree, pages, resolve_site(None, tree))
    inventory = _coverage(root, tree, pages, planned)
    return {'base_snapshot': tree.get('generation_id') or 'legacy',
            'before_snapshot': previous,
            'pages': tree_diff(before, tree) if 'previous_snapshot' in tree else None,
            'source_pages': [{'id': n['id'], 'title': n['title'],
                              'path': str(Path(n['page_dir']) / 'page.md'),
                              'attachment_urls': n.get('attachment_urls', [])}
                             for n in tree['nodes']],
            'files': files, 'coverage': inventory, 'gaps': inventory['gaps'],
            'excluded': inventory['excluded'], 'pending_file_cleanup': pending_cleanup}


def _reason(entry):
    value = entry.get('reason')
    if not isinstance(value, str) or not value.strip():
        raise UsageError('Every change requires a non-empty reason')
    return clean(value.strip())


def _file_ref(root, ref, registered, *, removal_saved=False):
    if not isinstance(ref, dict) or not isinstance(ref.get('path'), str):
        raise UsageError('File references require a course-relative path and sha256')
    if not isinstance(ref.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', ref['sha256']):
        raise UsageError('sha256 must be a lowercase 64-digit hexadecimal fingerprint')
    relative = Path(ref['path'])
    if relative.is_absolute() or '..' in relative.parts:
        raise UsageError('File paths must be relative to the course root')
    path = safe_archive_path(root, relative)
    if str(relative) not in registered and not (removal_saved and not path.exists()):
        raise UsageError('File is not registered: ' + str(relative))
    if not (removal_saved and not path.exists()) and (not path.is_file() or sha256_file(path) != ref['sha256']):
        raise ChangeStale('Reviewed file missing or changed: ' + str(relative), root)
    return {'path': str(relative), 'sha256': ref['sha256']}


def apply_plan(root, plan, *, dry_run=False):
    from .course_service import _resource_catalog, _verify_state_identity, resolve_site, verify_snapshot
    from . import providers

    if not isinstance(plan, dict) or type(plan.get('version')) is not int or plan['version'] != 1:
        raise UsageError('Change plan must be an object with version: 1')
    resource_changes, file_changes = plan.get('resources', []), plan.get('files', [])
    if not isinstance(resource_changes, list) or not isinstance(file_changes, list) or not (resource_changes or file_changes):
        raise UsageError('Plan requires a non-empty resources or files array')
    state = ArchiveState(root)
    tree = state.existing_tree
    pages = verify_snapshot(state, tree)
    snapshot = tree.get('generation_id') or 'legacy'
    if plan.get('base_snapshot') != snapshot:
        raise ChangeStale('Plan snapshot no longer matches the published archive', root)
    site = resolve_site(None, tree)
    with Store(root, create=not dry_run) as db:
        _verify_state_identity(db, {'origin': site.origin, 'course_id': str(tree['course_id'])})
        if db.pending_ids():
            raise ArchiveIntegrityError('Unfinished acquisitions exist; run fetch to recover before applying a new plan')
        pending = db.cleanup_pending()
        saved_removals = list(pending)
        # Reuse the existing review record when a previous apply deleted only
        # part of the batch. Missing old files are accepted only for saved intent.
        for run in db.runs():
            if run.get('command') == 'change apply' and run.get('snapshot') == snapshot:
                saved_removals.extend(row for row in run.get('changes', {}).get('files', [])
                                      if row.get('action') == 'remove_old')
        planned = providers.plan_catalog(root, state, tree, pages, site)
        planned_ids = {row['asset_id'] for row in planned}
        catalog = _resource_catalog(db, planned)
        files = db.files()
        registered = {}
        for row in files:
            registered.setdefault(row['path'], []).append(row)
        resources, dispositions, seen = [], [], set()
        for entry in resource_changes:
            if not isinstance(entry, dict):
                raise UsageError('Resource changes must be objects')
            key, decision = entry.get('asset_id'), entry.get('decision')
            if not isinstance(key, str) or key not in catalog or key in seen:
                raise UsageError('Resource must be known and occur once: ' + str(key))
            if decision not in ('fetch', 'unavailable', 'exclude'):
                raise UsageError('decision must be fetch, unavailable, or exclude')
            seen.add(key)
            resources.append({'asset_id': key, 'decision': decision, 'reason': _reason(entry)})
        decisions = {row['asset_id']: row['decision'] for row in resources}
        seen_paths = set()
        for entry in file_changes:
            if not isinstance(entry, dict) or entry.get('action') not in ('keep_both', 'remove_old'):
                raise UsageError('File action must be keep_both or remove_old')
            reason = _reason(entry)
            reviewed = clean({'old': entry.get('old'), 'new': entry.get('new'),
                              'action': entry['action'], 'reason': reason})
            removal_saved = reviewed in saved_removals and entry['action'] == 'remove_old'
            old = _file_ref(root, entry.get('old'), registered, removal_saved=removal_saved)
            new = _file_ref(root, entry.get('new'), registered)
            if old['path'] == new['path'] or old['path'] in seen_paths:
                raise UsageError('Old/new files must differ and each old file must occur once')
            seen_paths.add(old['path'])
            if entry['action'] == 'remove_old':
                file_evidence(safe_archive_path(root, new['path']))
                new_assets = {row['asset_id'] for row in registered[new['path']]}
                for row in registered.get(old['path'], []):
                    key = row['asset_id']
                    info = db.decision_info(key) or {'decision': 'fetch'}
                    if key in planned_ids and key not in new_assets and decisions.get(key, info['decision']) == 'fetch':
                        raise UsageError('Replacement belongs to another resource; explicitly exclude the superseded resource: ' + key)
            dispositions.append({'old': old, 'new': new, 'action': entry['action'], 'reason': reason})
        removals = [row for row in dispositions if row['action'] == 'remove_old']
        if any(row not in removals for row in pending):
            raise ArchiveIntegrityError('Unfinished file cleanup exists; rerun the original change apply plan to finish it')
        if {row['old']['path'] for row in removals} & {row['new']['path'] for row in dispositions}:
            raise UsageError('A replacement file cannot also be removed by the same plan')
        result = {'snapshot': snapshot, 'dry_run': dry_run, 'resources': resources,
                  'files': dispositions, 'completed': [], 'pending': []}
        if dry_run:
            return result
        for row in resources:
            source = catalog[row['asset_id']]
            db.set_decision(row['asset_id'], row['decision'], row['reason'], snapshot,
                            kind=source['kind'], url=source['url'])
        # One state write binds resource decisions and recoverable deletion intent.
        result['completed'] = [row for row in removals if row['old']['path'] not in registered]
        remaining = [row for row in removals if row not in result['completed']]
        db.begin_cleanup(remaining)
        try:
            db.run('change apply', {'snapshot': snapshot, 'changes': {'resources': resources, 'files': dispositions}})
            result['completed'].extend(db.recover_cleanup())
        except (Exception, KeyboardInterrupt) as exc:
            result['pending'] = Store(root).cleanup_pending()
            result['completed'] = [row for row in removals if row not in result['pending']]
            exc.data = result
            exc.solution = 'Resolve the reported filesystem problem, then rerun the same change apply plan to finish file cleanup without downloading resources.'
            raise
        return result
