"""Course-local acquisition facts; snapshots remain ordinary immutable files."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .archive_io import ArchiveIntegrityError, atomic_write_json, load_json_object, safe_archive_path
from .content_rules import sha256_file
# Redaction of persisted values (key-name and query based) lives in
# ``redaction`` with the single shared secret-key vocabulary; this module
# re-exports the names its callers use.
from .redaction import clean, stable_url


def dumps(value):
    return json.dumps(clean(value), ensure_ascii=False, sort_keys=True)


def asset_id(kind: str, url: str) -> str:
    return kind + ':' + hashlib.sha256(stable_url(url).encode()).hexdigest()[:24]


def file_evidence(path: Path, expected_name: str | None = None) -> dict:
    """Check signatures/containers rather than trusting a suffix or HTTP MIME."""
    import zipfile
    name = (expected_name or path.name).lower()
    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise ArchiveIntegrityError('Missing, empty or symlink file')
    with path.open('rb') as f:
        head = f.read(1024)
    if name.endswith('.pdf'):
        if b'%PDF-' not in head:
            raise ArchiveIntegrityError('Expected PDF signature')
        with path.open('rb') as f:
            f.seek(max(0, path.stat().st_size - 65536))
            if b'%%EOF' not in f.read():
                raise ArchiveIntegrityError('Incomplete PDF: EOF marker missing')
    elif name.endswith(('.xlsx', '.docx', '.pptx', '.zip')):
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                prefix = {'xlsx': 'xl/', 'docx': 'word/', 'pptx': 'ppt/'}.get(name.rsplit('.', 1)[-1])
                if z.testzip() or (prefix and ('[Content_Types].xml' not in names or not any(n.startswith(prefix) for n in names))):
                    raise ArchiveIntegrityError('Invalid Office/ZIP container')
        except zipfile.BadZipFile as exc:
            raise ArchiveIntegrityError('Expected Office/ZIP container') from exc
    elif name.endswith(('.doc', '.xls', '.ppt')) and not head.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
        raise ArchiveIntegrityError('Expected legacy Office signature')
    elif name.endswith('.ris'):
        body = path.read_text(encoding='utf-8-sig')
        if not re.search(r'^TY  - ', body, re.MULTILINE) or not re.search(r'^ER  -', body, re.MULTILINE):
            raise ArchiveIntegrityError('Invalid RIS records')
    elif name.endswith('.json'):
        json.loads(path.read_text())  # Lists are valid caption documents.
    elif head.lstrip().lower().startswith((b'<!doctype html', b'<html')) and not name.endswith(('.html', '.htm')):
        raise ArchiveIntegrityError('Unexpected HTML response')
    return {'sha256': sha256_file(path), 'size': path.stat().st_size, 'validation': 'signature-container'}


class Acquisition:
    """Handle for one in-flight provider download inside ``Store.acquisition``.

    ``stage`` is the empty per-attempt staging directory the provider writes
    into; the caller stores the provider's outcome dict in ``result``. When
    the context manager exits it fills ``outcome`` with the final fetch
    result row (published or failed) for the
    caller to return.
    """

    def __init__(self, attempt: str, stage: Path):
        self.attempt = attempt
        self.stage = stage
        self.result: dict[str, Any]
        self.outcome: dict[str, Any] | None = None


def _acquisition_errors() -> tuple[type[BaseException], ...]:
    """Provider/network failures that end an acquisition as 'failed'.

    Imported lazily so the store module keeps its minimal import graph;
    anything else propagates and leaves the attempt to recover().
    """
    from .browser_backend import BrowserUnavailable, SessionRequired
    from .safe_fetch import FetchError
    from .runtime import CommandExecutionError
    return (ArchiveIntegrityError, FetchError, SessionRequired, BrowserUnavailable, CommandExecutionError, OSError)


def _needs_action(exc: BaseException) -> bool:
    from .browser_backend import SessionRequired
    return isinstance(exc, SessionRequired) and exc.retryable


class Store:
    """One atomic course state. Writers hold CourseLock; reads never create files."""
    def __init__(self, root: Path, *, create=False):
        self.root = root.resolve()
        self.path = safe_archive_path(self.root, '_blackboard/state.json', label='course state')
        self.writable = create
        data = load_json_object(self.path)
        if data is None:
            if not create:
                raise ArchiveIntegrityError('State missing; restore an initialized archive or capture a new course')
            data = {'schema_version': 1, 'resources': {}, 'runs': []}
        # State is a validated JSON object; sub-structure is checked by
        # check(). The raw document is private: callers use the query/command
        # methods below instead of mutating the structure directly.
        self._data: dict[str, Any] = data
        resources = self._data.get('resources')
        if not isinstance(resources, dict):
            raise ArchiveIntegrityError('Invalid resources in course state')
        for resource in resources.values():
            if not isinstance(resource, dict):
                continue
            old_ignore = resource.pop('ignore', None)
            old_defer = resource.pop('defer', None)
            if old_ignore is not None and not isinstance(old_ignore, dict):
                raise ArchiveIntegrityError('Invalid legacy exclusion')
            if old_defer is not None and not isinstance(old_defer, dict):
                raise ArchiveIntegrityError('Invalid legacy retry note')
            if old_ignore and not isinstance(old_ignore.get('reason'), str):
                raise ArchiveIntegrityError('Legacy exclusion requires a reason')
            if old_ignore and 'decision' not in resource:
                resource['decision'] = {'decision': 'exclude', 'reason': old_ignore['reason'],
                                        'snapshot': old_ignore.get('snapshot', 'legacy')}
            if old_defer:
                resource['previous_retry_note'] = clean(old_defer.get('reason', ''))
        self.check()

    def identity(self) -> dict[str, str] | None:
        """The bound ``(origin, course_id)`` archive identity, if any."""
        return self._data.get('archive_identity')

    def pending_ids(self) -> list[str]:
        """Resource IDs with an unfinished (running/prepared) attempt, sorted."""
        return sorted(key for key, resource in self._data['resources'].items() if 'pending' in resource)

    def prepared_attempt_count(self) -> int:
        """Attempts persisted as prepared, i.e. crash-recoverable publications."""
        return sum(resource.get('pending', {}).get('status') == 'prepared'
                   for resource in self._data['resources'].values())

    def runs(self) -> list[dict[str, Any]]:
        """The bounded command-run history, oldest first."""
        return list(self._data['runs'])

    def registered_resources(self) -> dict[str, dict[str, Any]]:
        """Catalog row (kind, url, revision metadata) per registered resource."""
        return {key: {'asset_id': key, 'kind': resource['kind'], 'url': resource['url'],
                      'metadata': resource.get('revision', {})}
                for key, resource in self._data['resources'].items()}

    def check(self):
        def require(condition):
            if not condition:
                raise ValueError('State invariant failed')
        try:
            require(self._data['schema_version'] == 1)
            require(isinstance(self._data['resources'], dict) and isinstance(self._data['runs'], list))
            identity = self._data.get('archive_identity')
            if identity is not None:
                require(isinstance(identity, dict))
                require(isinstance(identity.get('origin'), str) and bool(identity['origin']))
                require(isinstance(identity.get('course_id'), str) and bool(identity['course_id']))
            for key, resource in self._data['resources'].items():
                require(key == asset_id(resource['kind'], resource['url']))
                require(resource['url'] == stable_url(resource['url']))
                require(isinstance(resource['files'], list))
                paths = set()
                for item in resource['files']:
                    safe_archive_path(self.root, item['path'])
                    require(item['path'] not in paths)
                    paths.add(item['path'])
                    require(re.fullmatch(r'[a-f0-9]{64}', item['sha256']))
                    require(isinstance(item['size'], int) and item['size'] >= 0)
                    require(all(isinstance(item[k], str) for k in ('validation', 'attempt_id', 'snapshot')))
                for slot in ('success', 'failure', 'pending'):
                    if slot not in resource:
                        continue
                    item = resource[slot]
                    require(all(isinstance(item[k], str) for k in ('id', 'snapshot', 'status', 'started_at')))
                    if slot == 'success':
                        require(item['status'] in ('complete', 'no-captions'))
                        require(isinstance(item['paths'], list))
                        require(all(path in paths for path in item['paths']))
                        for link in item.get('links', []):
                            require(isinstance(link['url'], str) and link['url'] == stable_url(link['url']))
                    elif slot == 'pending':
                        require(item['status'] in ('running', 'prepared'))
                        if item['status'] == 'prepared':
                            require(item['final_status'] in ('complete', 'no-captions'))
                            for f in item['files']:
                                safe_archive_path(self.root, f['source'])
                                safe_archive_path(self.root, f['path'])
                                require(re.fullmatch(r'[a-f0-9]{64}', f['sha256']))
                                require(isinstance(f['size'], int) and f['size'] >= 0)
                                require(isinstance(f['validation'], str))
                if 'decision' in resource:
                    info = resource['decision']
                    require(info['decision'] in ('fetch', 'unavailable', 'exclude'))
                    require(isinstance(info['reason'], str) and bool(info['reason'].strip()))
                    require(isinstance(info['snapshot'], str))
            require(isinstance(self._data.get('file_cleanup', []), list))
            for entry in self._data.get('file_cleanup', []):
                for side in ('old', 'new'):
                    safe_archive_path(self.root, entry[side]['path'])
                    require(re.fullmatch(r'[a-f0-9]{64}', entry[side]['sha256']))
            for run in self._data['runs']:
                require(all(isinstance(run[k], str) for k in ('id', 'command', 'started_at', 'status')))
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ArchiveIntegrityError('Invalid or unsupported course state; preserved without overwrite') from exc

    def save(self):
        if not self.writable:
            raise ArchiveIntegrityError('Read-only course state')
        self.check()
        try:
            atomic_write_json(self.path, self._data)
        except Exception:
            # Keep caller recovery decisions consistent with the durable state.
            self._data = load_json_object(self.path) or {'schema_version': 1, 'resources': {}, 'runs': []}
            raise

    def ensure_identity(self, origin: str, course_id: str) -> dict[str, str]:
        """Bind this state file to one site origin and course id.

        The state file remains the sole mutable acquisition authority. Existing
        identity is never silently replaced; a mismatch is a cross-site or
        cross-course integrity error.
        """
        identity = {"origin": str(origin).rstrip("/"), "course_id": str(course_id)}
        current = self._data.get('archive_identity')
        if current is not None and current != identity:
            raise ArchiveIntegrityError(
                'Course state identity does not match the requested site/course'
            )
        if current is None:
            self._data['archive_identity'] = identity
            self.save()
        return identity

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def asset(self, kind, url, metadata=None):
        key = asset_id(kind, url)
        resource = self._data['resources'].setdefault(key, {'kind': kind, 'url': stable_url(url), 'files': []})
        revision = {k: clean(v) for k,v in (metadata or {}).items() if k in ('cdn_path', 'checked_snapshot')}
        if revision:
            resource['revision'] = {**resource.get('revision', {}), **revision}
        self.save()
        return key

    def revision(self, key):
        return self._data['resources'][key].get('revision', {})

    def decision_info(self, key):
        return self._data['resources'].get(key, {}).get('decision')

    def set_decision(self, key, decision, reason, snapshot, *, kind, url):
        resource = self._data['resources'].setdefault(key, {'kind': kind, 'url': stable_url(url), 'files': []})
        resource['decision'] = {'decision': decision, 'reason': reason, 'snapshot': snapshot}

    def failure(self, key):
        return self._data['resources'].get(key, {}).get('failure')

    def cleanup_pending(self):
        return list(self._data.get('file_cleanup', []))

    def begin_cleanup(self, entries):
        self._data['file_cleanup'] = list(entries)
        self.save()

    def recover_cleanup(self):
        completed = []
        while self.cleanup_pending():
            entry = self.cleanup_pending()[0]
            old, new = entry['old'], entry['new']
            replacement = safe_archive_path(self.root, new['path'])
            target = safe_archive_path(self.root, old['path'])
            registered = {f['path'] for r in self._data['resources'].values() for f in r['files']}
            if old['path'] not in registered or new['path'] not in registered:
                raise ArchiveIntegrityError('Cleanup references unregistered files')
            if not replacement.is_file() or sha256_file(replacement) != new['sha256']:
                raise ArchiveIntegrityError('Cleanup replacement missing or changed; restore the reviewed file before retrying cleanup')
            if target.exists():
                if not target.is_file() or sha256_file(target) != old['sha256']:
                    raise ArchiveIntegrityError('Cleanup target changed; existing file preserved')
                target.unlink()
                descriptor = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            # The intent was saved before unlink, so a crash here is recoverable.
            for resource in self._data['resources'].values():
                resource['files'] = [f for f in resource['files'] if f['path'] != old['path']]
                success = resource.get('success')
                if success and old['path'] in success['paths']:
                    paths = [p for p in success['paths'] if p != old['path']]
                    if any(f['path'] == new['path'] for f in resource['files']):
                        paths.append(new['path'])
                    if paths:
                        success['paths'] = list(dict.fromkeys(paths))
                    else:
                        resource.pop('success', None)
            self._data['file_cleanup'].pop(0)
            self.save()
            completed.append(entry)
        return completed

    def success(self, key):
        return self._data['resources'].get(key, {}).get('success', {})

    def attempt(self, attempt):
        for key, resource in self._data['resources'].items():
            for slot in ('pending', 'success', 'failure'):
                if resource.get(slot, {}).get('id') == attempt:
                    return key, resource[slot]
        raise ArchiveIntegrityError('Unknown acquisition attempt: ' + attempt)

    def start(self, key, snapshot):
        resource = self._data['resources'][key]
        if 'pending' in resource:
            raise ArchiveIntegrityError('Recover unfinished acquisition before retrying')
        attempt = uuid.uuid4().hex
        resource['pending'] = {'id': attempt, 'snapshot': snapshot, 'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'running'}
        self.save()
        return attempt

    def finish(self, attempt, status, result):
        key, pending = self.attempt(attempt)
        resource = self._data['resources'][key]
        outcome = {k: pending[k] for k in ('id', 'snapshot', 'started_at')}
        outcome.update(status=status, finished_at=datetime.now(timezone.utc).isoformat())
        if status in ('complete', 'no-captions'):
            outcome['paths'] = [f['path'] for f in pending.get('files', [])]
            if resource['kind'] == 'panopto-discovery':
                outcome['links'] = [{'url': stable_url(i['url']), 'title': clean(i.get('title'))}
                                    for i in result.get('links', [])]
            resource['success'] = outcome
        else:
            outcome['error'] = clean(str(result.get('error') or result.get('message') or result.get('reason') or status)).replace(str(self.root), '<course>')[:2000]
            outcome['error_code'] = str(result.get('error_code', 'download.failed'))
            resource['failure'] = outcome
        resource.pop('pending', None)
        self.save()

    def reusable(self, key, snapshot=None):
        outcome = self.success(key)
        if not outcome or (snapshot is not None and outcome['snapshot'] != snapshot):
            return None
        if all(safe_archive_path(self.root, p).is_file() for p in outcome['paths']):
            return {'asset_id': key, 'status': 'skipped', 'files': outcome['paths'], 'previous_status': outcome['status']}
        return None

    def prepare(self, attempt, sources, *, status='complete', result=None):
        """Persist exact publication and completion intent before touching targets."""
        _key, pending = self.attempt(attempt)
        prepared = []
        for source, relative in sources:
            source = Path(source)
            evidence = file_evidence(source)
            target = safe_archive_path(self.root, relative, label='published file')
            registered = [f for r in self._data['resources'].values() for f in r['files'] if f['path'] == str(relative)]
            known_different = any(f['sha256'] != evidence['sha256'] for f in registered)
            if known_different or (target.exists() and (not target.is_file() or sha256_file(target) != evidence['sha256'])):
                stem = target.stem.encode('utf-8')[:150].decode('utf-8', errors='ignore')
                target = target.with_name(f'{stem} [{evidence["sha256"]}]{target.suffix}')
            if target.exists() and sha256_file(target) != evidence['sha256']:
                raise ArchiveIntegrityError('Content-addressed destination collision')
            prepared.append({**evidence, 'source': str(source.resolve().relative_to(self.root)), 'path': str(target.relative_to(self.root))})
        pending.update(status='prepared', files=prepared, final_status=status)
        if result and result.get('cdn_path'):
            pending['revision'] = {'cdn_path': stable_url(result['cdn_path']), 'checked_snapshot': pending['snapshot']}
        self.save()
        return prepared

    def publish(self, attempt):
        key, pending = self.attempt(attempt)
        if pending['status'] != 'prepared':
            raise ArchiveIntegrityError('Publication requires a prepared acquisition')
        prepared = pending['files']
        for item in prepared:
            source = safe_archive_path(self.root, item['source'])
            target = safe_archive_path(self.root, item['path'])
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if sha256_file(target) != item['sha256']:
                    raise ArchiveIntegrityError('Prepared destination changed; existing file preserved')
            else:
                if not source.is_file() or sha256_file(source) != item['sha256']:
                    raise ArchiveIntegrityError('Prepared source missing or changed')
                with source.open('rb') as f:
                    os.fsync(f.fileno())
                os.link(source, target)
                fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        resource = self._data['resources'][key]
        paths = {f['path']: f for f in resource['files']}
        for item in prepared:
            previous = paths.get(item['path'])
            if previous and previous['sha256'] != item['sha256']:
                raise ArchiveIntegrityError('Registered original hash disagrees with publication')
            if not previous:
                resource['files'].append({k: item[k] for k in ('path', 'sha256', 'size', 'validation')} |
                                         {'attempt_id': attempt, 'snapshot': pending['snapshot']})
        if 'revision' in pending:
            resource['revision'] = pending['revision']
        self.finish(attempt, pending['final_status'], {})
        return prepared

    @contextmanager
    def acquisition(self, key: str, snapshot: str) -> Iterator[Acquisition]:
        """Run one provider download as a prepare/publish transaction.

        Creates a fresh per-attempt staging directory and yields an
        :class:`Acquisition`. The caller writes provider files into
        ``acquisition.stage`` and stores the provider's result dict in
        ``acquisition.result``. On clean exit the staged files are collected
        (provider-embedded staging paths rewritten to course-relative
        finals), the exact publication intent is persisted with ``prepare()``,
        and ``publish()`` links the files into the course. A failed provider
        status, a provider error, or a file-less success finishes the attempt
        'failed'; an unexpected exception propagates after cleanup, leaving
        the running attempt to ``recover()``. A prepared attempt keeps its
        staging directory so ``recover()`` can settle it.
        """
        attempt = self.start(key, snapshot)
        stage = safe_archive_path(self.root, Path('_blackboard/staging') / attempt / self.root.name)
        stage.mkdir(parents=True, exist_ok=False)
        record = Acquisition(attempt, stage)
        try:
            yield record
            result = record.result
            status = result.get('status', 'failed')
            if status not in ('downloaded', 'skipped', 'no-captions'):
                self.finish(attempt, 'failed', result)
                record.outcome = {'asset_id': key, 'status': 'failed', 'result': clean(result)}
            else:
                sources = self._collect_stage_sources(stage)
                if not sources and status != 'no-captions':
                    raise ArchiveIntegrityError('Successful provider returned no files')
                report = json.loads(dumps(result).replace(str(stage), str(self.root)))
                self.prepare(attempt, sources,
                             status='no-captions' if status == 'no-captions' else 'complete', result=report)
                published = self.publish(attempt)
                record.outcome = {'asset_id': key, 'status': status, 'files': [p['path'] for p in published]}
        except _acquisition_errors() as exc:
            _, row = self.attempt(attempt)
            if row['status'] != 'prepared':
                self.finish(attempt, 'failed', {'error': str(exc)})
            record.outcome = {'asset_id': key, 'status': 'failed', 'error': clean(str(exc)),
                              'needs_action': _needs_action(exc)}
        finally:
            _, row = self.attempt(attempt)
            if row['status'] != 'prepared':
                shutil.rmtree(stage.parent)

    def _collect_stage_sources(self, stage: Path) -> list[tuple[Path, Path]]:
        """Gather provider output files, rewriting embedded staging paths.

        Provider metadata may embed temporary absolute staging paths; those
        are rewritten to final course-relative paths before registration.
        Partial downloads (``.part``/``.tmp``) and reading-list manifests are
        deliberately never published.
        """
        sources = []
        for path in sorted(stage.rglob('*')):
            if not path.is_file() or path.name.endswith(('.part', '.tmp')) or path.name == 'manifest.json':
                continue
            if path.suffix.lower() in ('.json', '.md', '.vtt', '.srt'):
                body = path.read_text(encoding='utf-8')
                body = body.replace(str(stage), str(self.root))
                if path.suffix.lower() == '.json':
                    body = dumps(json.loads(body)) + '\n'
                else:
                    body = clean(body)
                path.write_text(body, encoding='utf-8')
            sources.append((path, path.relative_to(stage)))
        return sources

    def recover(self):
        self.recover_cleanup()
        recovered = []
        for resource in list(self._data['resources'].values()):
            pending = resource.get('pending')
            if not pending:
                continue
            if pending['status'] == 'prepared':
                self.publish(pending['id'])
                recovered.append(pending['id'])
            else:
                self.finish(pending['id'], 'interrupted', {'error': 'Acquisition interrupted before preparation'})
        for run in self._data['runs']:
            if run['status'] == 'running':
                run.update(status='interrupted', finished_at=datetime.now(timezone.utc).isoformat())
        self.save()
        return recovered

    def run(self, command, data, *, run_id=None, status='complete'):
        key = run_id or uuid.uuid4().hex
        previous = next((r for r in self._data['runs'] if r['id'] == key), {})
        run = {'id': key, 'command': command, 'started_at': previous.get('started_at', datetime.now(timezone.utc).isoformat()),
               'finished_at': None if status == 'running' else datetime.now(timezone.utc).isoformat(), 'status': status}
        if data.get('snapshot'):
            run['snapshot'] = data['snapshot']
        if 'changes' in data:
            run['changes'] = clean(data['changes'])
        if 'results' in data:
            from collections import Counter
            run['counts'] = dict(Counter(r['status'] for r in data['results']))
        self._data['runs'] = [r for r in self._data['runs'] if r['id'] != key][-19:] + [run]
        self.save()
        return key

    def attempts(self):
        rows = []
        for key, resource in self._data['resources'].items():
            for slot in ('pending', 'success', 'failure'):
                if slot in resource:
                    rows.append({'asset_id': key, **{k:v for k,v in resource[slot].items()
                                                     if k in ('id', 'snapshot', 'status', 'started_at', 'finished_at', 'error')}})
        return sorted(rows, key=lambda r: r['started_at'], reverse=True)

    def files(self, *, key=None) -> list[dict[str, Any]]:
        out = []
        for identity, resource in self._data['resources'].items():
            if key and identity != key:
                continue
            for evidence in resource['files']:
                path = safe_archive_path(self.root, evidence['path'])
                present = path.is_file()
                out.append({**evidence, 'asset_id': identity, 'kind': resource['kind'], 'url': resource['url'],
                            'metadata': resource.get('revision', {}), 'present': present,
                            'status': 'available' if present else 'missing', 'absolute_path': str(path)})
        return sorted(out, key=lambda r: (r['path'], r['asset_id']))
