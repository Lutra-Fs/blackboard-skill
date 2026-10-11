"""Blackboard command implementation; use the public skill launcher."""
from __future__ import annotations
import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from .archive_io import ArchiveIntegrityError
from .course_lock import LOCK_CONFLICT_EXIT_CODE, CourseLock, LockBusyError
from .resource_store import clean
from .site_config import load_site_config, from_origin
from .browser_backend import BrowserUnavailable, SessionRequired
from .cli_requests import CLI_PATH, FetchRequest, BrowserOptions, UsageError
from . import course_service as service

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


EPILOG = f"""\
Choose an operation for the task; inspection and change review are on demand.

local    read archived pages/files when sufficient; archive inspection is offline
acquire  capture (pages only), sync (pages + files), fetch (fill existing gaps)
inspect  status (coverage + gaps), files (paged inventory), validate (integrity + coverage)
change   diff (review facts), apply PLAN.json (reviewed decisions/file disposition)

Complete against the requested scope: metadata publication is not file acquisition.
Protected network requests use an externally authenticated browser session.

exit codes: 0 command complete | 1 failed/partial | 2 invalid input | 3 blocked integrity
            | 4 dependency/session action | {LOCK_CONFLICT_EXIT_CODE} writer-lock busy | 130 interrupted
browser: BLACKBOARD_BROWSER_BACKEND or --browser-backend selects the authenticated backend
"""


def build_parser():
    p = Parser(
        prog="blackboard",
        description="Blackboard Learn course evidence and acquisition; run directly from the skill directory.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--version', action='version', version='blackboard 2.0')
    commands = p.add_subparsers(dest='command', required=True)
    help_text = {
        'capture': 'Capture and publish a complete configured-site snapshot, without downloading files',
        'sync': 'Capture a configured course, then fetch files from the published snapshot',
        'fetch': 'Fetch missing/failed resources from the current configured-site snapshot',
        'status': 'Read current snapshot and recent acquisition outcomes',
        'files': 'Query registered files and stable resource identities',
        'change': 'Inspect changes or apply explicit resource/file decisions',
        'validate': 'Validate snapshot, state and registered files offline',
        'request': 'Raw read-only GET of a site-relative JSON path through the session cookie chain',
    }
    for name, description in help_text.items():
        sub = commands.add_parser(name, help=description, description=description)
        if name != 'change':
            site = sub.add_mutually_exclusive_group()
            site.add_argument('--site', help='Blackboard origin; unnecessary with a course URL or existing archive')
            site.add_argument('--site-config', type=Path,
                             help='JSON site config; custom sites use an existing authenticated browser session')
            sub.add_argument('--root', type=Path, required=True, help='Existing course directory')
        if name == 'files':
            sub.add_argument('--format', choices=('json','data','paths'), default='json',
                             help='Output format: json envelope (default), data (indented data JSON, no envelope), paths (every present file across all pages; missing count on stderr)')
        elif name != 'change':
            sub.add_argument('--format', choices=('json','data'), default='json',
                             help='Output format: json envelope (default), data (indented data JSON, no envelope)')
        if name in ('capture','sync'):
            sub.add_argument('course', help='Configured Blackboard course URL or Blackboard ID')
        if name in ('capture','fetch'):
            sub.add_argument('--dry-run', action='store_true', help='Preview without persistent changes')
        if name == 'sync':
            sub.add_argument('--dry-run', action='store_true',
                             help='Preview the capture phase only; the fetch phase is not previewed')
        if name in ('capture','sync','fetch'):
            sub.add_argument('--browser-session',
                             help='Browser session (task-space) name; course commands derive it from the course ID')
            sub.add_argument('--browser-backend',
                             help='Pin the browser backend: ego, browser-use, kimi, auto, or a comma-separated priority list')
        if name in ('sync','fetch'):
            sub.add_argument('--kind', choices=service.KINDS, action='append', help='Restrict resource kinds; repeatable; default is all')
            sub.add_argument('--asset', action='append', help='Stable resource ID; repeat for multiple (mutually exclusive with --url)')
            sub.add_argument('--refresh', action='store_true', help='Recheck even successful resources; preserves old files')
            sub.add_argument('--url', help='Fetch one explicitly known trusted attachment URL (mutually exclusive with --asset)')
            sub.add_argument('--out', type=Path, help='Course-relative destination for --url')
            sub.add_argument('--youtube-cookies-browser', metavar='BROWSER',
                             help='Optional yt-dlp browser name used only after a public-caption attempt fails')
        if name == 'change':
            actions = sub.add_subparsers(dest='change_action', required=True)
            for action in ('diff', 'apply'):
                child = actions.add_parser(action, help='Inspect change facts' if action == 'diff' else 'Apply a change plan')
                child.add_argument('--root', type=Path, required=True, help='Existing course directory')
                child.add_argument('--format', choices=('json', 'data'), default='json')
                if action == 'apply':
                    child.add_argument('plan', type=Path, help='Change plan JSON file')
                    child.add_argument('--dry-run', action='store_true', help='Validate and preview without writes')
        if name == 'files':
            sub.add_argument('--asset', help='Filter to one stable resource ID')
            sub.add_argument('--limit', type=int, default=100, help='Page size (1..10000); follow data.next_offset for more pages')
            sub.add_argument('--offset', type=int, default=0, help='Page start; nonnegative')
        if name == 'request':
            sub.add_argument('method', choices=('get',), help='Only GET is allowed')
            sub.add_argument('path', help='Site-relative path, e.g. /learn/api/public/v1/courses/_123_1')
    return p


EXIT_BY_OUTCOME = {'complete': 0, 'partial': 1, 'blocked': 3, 'needs-action': 4, 'failed': 1}


def execute(args) -> tuple[dict[str, Any], int]:
    command = args.command
    requested_backend = getattr(args, 'browser_backend', None)
    if requested_backend:
        from .browser_backend import effective_backend_chain
        effective_backend_chain(requested_backend)
    origin = getattr(args, 'site', None)
    try:
        if origin:
            args.site = from_origin(origin)
        elif getattr(args, 'site_config', None):
            args.site = load_site_config(args.site_config)
        else:
            args.site = None
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    # argparse marks --root required for every remaining command, so root is
    # always a concrete directory from here on.
    root = args.root.expanduser().resolve()
    if command in ('fetch','sync') and bool(args.url) != bool(args.out):
        raise UsageError('--url and --out must be supplied together')
    if command in ('fetch','sync') and args.url and args.asset:
        raise UsageError('--url and --asset are mutually exclusive')
    if command in ('fetch','sync') and args.out:
        from .archive_io import safe_archive_path
        safe_archive_path(root,args.out,label='attachment destination')
    if command == 'change':
        from .course_changes import change_diff, apply_plan
        if args.change_action == 'diff':
            return change_diff(root), 0
        try:
            plan = json.loads(args.plan.expanduser().read_text())
        except (OSError, ValueError) as exc:
            raise UsageError(f'Cannot read change plan: {exc}') from exc
        if args.dry_run:
            return apply_plan(root, plan, dry_run=True), 0
        service.ensure_writable(root)
        with CourseLock(root, operation='change apply'):
            return apply_plan(root, plan), 0
    if command == 'files':
        if args.limit < 1 or args.limit > 10000 or args.offset < 0:
            raise UsageError('--limit must be 1..10000 and --offset nonnegative')
        files = service.read_files(root, args.asset)
        if args.format == 'paths':
            missing = sum(1 for f in files if not f['present'])
            if missing:
                print(f'{missing} registered file(s) missing on disk', file=sys.stderr)
            return {'files': files, 'total': len(files), 'next_offset': None}, 0
        end = args.offset + args.limit
        return {'files': files[args.offset:end], 'total': len(files), 'next_offset': end if end < len(files) else None}, 0
    if command == 'request':
        return service.request_get(root, args.path, site_config=args.site), 0
    if command == 'status':
        result = service.status(root, site_config=args.site)
        return result, 0
    if command == 'validate':
        result = service.validate(root, site_config=args.site)
        pending = result.get('pending_attempts', result.get('prepared_attempts', 0))
        return result, 0 if result['ok'] else (3 if result['issues'] or pending else 1)
    service.ensure_writable(root)
    def write():
        from .archive_repository import ArchiveState
        course = args.course if command in ('capture','sync') else ArchiveState(root).existing_tree.get('course_id')
        # fetch lazily creates a session only when a default-scope provider needs
        # authentication.  A decided-only fetch therefore does not open the
        # browser at all, including in a live (non-dry-run) invocation.
        tree_context = ArchiveState(root).existing_tree if command in ('capture', 'sync', 'fetch') else None
        site = service.resolve_site(args.site, tree_context, course=course)
        options = BrowserOptions(browser_session=args.browser_session,
                                 browser_backend=args.browser_backend)
        session = None if command == 'fetch' else service.Session(service.resolve_course(course, site), options, site_config=site)
        try:
            if command in ('capture','sync'):
                result = service.capture(root, course, session, dry_run=args.dry_run, site_config=site)
                if command == 'capture' or args.dry_run or result.get('outcome') != 'complete':
                    return result, EXIT_BY_OUTCOME.get(result.get('outcome') or 'failed', 1)
            fetch_request = FetchRequest(kinds=tuple(args.kind or ()),
                                         assets=tuple(args.asset or ()),
                                         url=args.url, out=args.out,
                                         refresh=args.refresh, dry_run=args.dry_run,
                                         youtube_cookies_browser=args.youtube_cookies_browser,
                                         browser_session=args.browser_session,
                                         browser_backend=args.browser_backend)
            try:
                result = service.fetch(root, fetch_request, session, site_config=site)
            except SessionRequired as exc:
                exc.solution = auth_solution(args, 'fetch')
                raise
            except KeyboardInterrupt as exc:
                exc.solution = 'Interrupted; resume acquisition with:\n' + recovery_invocation(args, 'fetch')
                raise
            return result, EXIT_BY_OUTCOME.get(str(result.get('outcome') or 'failed'), 1)
        finally:
            if session is not None:
                session.close()
    if args.dry_run:
        return write()
    with CourseLock(root, operation=command):
        return write()


def recovery_invocation(args, recovery_command=None):
    """Reproduce the interrupted operation without forcing successful files to refresh."""
    command = recovery_command or args.command
    argv = [sys.executable, str(CLI_PATH), command]
    if command in ('capture', 'sync') and getattr(args, 'course', None):
        argv.append(args.course)
    if command == 'request':
        argv.extend([args.method, args.path])
    if command == 'change':
        argv.append(args.change_action)
        if args.change_action == 'apply':
            argv.append(str(args.plan.expanduser().resolve()))
    if getattr(args, 'site', None) and not getattr(args, 'site_config', None):
        site = args.site.origin if hasattr(args.site, 'origin') else args.site
        argv.extend(['--site', site])
    for name in ('root', 'site_config', 'browser_session', 'browser_backend'):
        value = getattr(args, name, None)
        if value is not None:
            if name in ('root', 'site_config'):
                value = Path(value).expanduser().resolve()
            argv.extend(['--' + name.replace('_', '-'), str(value)])
    if command in ('fetch', 'sync'):
        for name in ('kind', 'asset'):
            for value in getattr(args, name, None) or ():
                argv.extend(['--' + name, value])
        for name in ('url', 'out', 'youtube_cookies_browser'):
            value = getattr(args, name, None)
            if value is not None:
                argv.extend(['--' + name.replace('_', '-'), str(value)])
    if getattr(args, 'dry_run', False):
        argv.append('--dry-run')
    return shlex.join(argv)


def auth_solution(args, recovery_command=None):
    """Keep recovery choices here so callers need not infer acquisition phases."""
    command = recovery_command or args.command
    reuse = ' Valid completed files will be reused; --refresh is intentionally omitted.' if command == 'fetch' else ' Interrupted metadata enumeration will restart.' if command in ('capture', 'sync') else ''
    return f'Restore authentication externally in the same browser cookie profile, then run:\n{recovery_invocation(args, command)}\n{reuse.strip()}'


def main(argv=None):
    command = 'unknown'; args = None
    data: dict[str, Any] = {}
    try:
        args = build_parser().parse_args(argv)
        command = args.command + (' ' + args.change_action if args.command == 'change' else '')
        data, code = execute(args)
        errors = [] if code == 0 else [{'code': f"{command}.{'blocked' if code == 3 else 'needs-action' if code == 4 else 'incomplete'}", 'message': 'Inspect data for blocked or failed items'}]
        if code == 1 and args.command in ('fetch', 'sync'):
            errors[0]['solution'] = 'Inspect data.results and data.gaps, repair the failed acquisition or review resource decisions, then rerun fetch. Confirmed unavailable items still remain coverage gaps.'
        if code == 3 and args.command in ('capture', 'sync'):
            errors[0]['solution'] = 'Resolve data.crawl.reasons, restore authentication externally if needed, then rerun capture or sync. The previous snapshot was preserved.'
        if data.get('outcome') == 'needs-action' and any(row.get('needs_action') for row in data.get('results', [])):
            errors = [{'code': 'blackboard.browser_session_required',
                       'message': 'Authentication is required; acquisition stopped.',
                       'solution': auth_solution(args, 'fetch')}]
    except KeyboardInterrupt as exc:
        solution = getattr(exc, 'solution', None) or ('Rerun the interrupted operation:\n' + recovery_invocation(args) if args else 'Rerun the requested command.')
        data, code, errors = getattr(exc, 'data', {}), 130, [{'code': 'interrupted', 'message': 'Operation interrupted', 'solution': solution}]
    except Exception as exc:
        if isinstance(exc, LockBusyError): code = LOCK_CONFLICT_EXIT_CODE
        elif isinstance(exc, ArchiveIntegrityError): code = 3
        elif isinstance(exc, (UsageError, argparse.ArgumentError)): code = 2
        elif isinstance(exc, BrowserUnavailable): code = 4
        elif isinstance(exc, SessionRequired): code = 4 if exc.retryable else 1
        else: code = 1
        data = {'readiness': 'damaged', 'ready': False} if isinstance(exc, ArchiveIntegrityError) and command in ('status','validate') else {}
        data = getattr(exc, 'data', data)
        errors = [{'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}]
        if getattr(exc, 'solution', None):
            errors[0]['solution'] = exc.solution
        if isinstance(exc, SessionRequired) and args is not None:
            errors[0]['solution'] = getattr(exc, 'solution', None) or auth_solution(args)
    payload = clean({'schema_version': 2, 'command': command, 'ok': code == 0,
                     'status': 'complete' if code == 0 else {3:'blocked',4:'needs-action',LOCK_CONFLICT_EXIT_CODE:'locked',130:'interrupted'}.get(code,'failed'),
                     'data': data, 'errors': [{k: v for k, v in e.items() if k != 'code'} for e in errors]})
    # The shared secret-key vocabulary treats a literal 'code' key as an
    # OAuth grant parameter; these codes are short machine taxonomy tokens,
    # not secrets, so they are re-attached after the envelope scrub.
    for entry, error in zip(payload['errors'], errors):
        entry['code'] = str(error['code'])
    envelope = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        if args and args.format == 'paths' and command == 'files' and code == 0:
            print('\n'.join(f['absolute_path'] for f in data['files'] if f['present']))
        elif args and args.format == 'data' and code == 0:
            print(json.dumps(payload['data'], ensure_ascii=False, indent=2))
        elif args and args.format in ('data', 'paths') and code != 0:
            print(envelope, file=sys.stderr)
        else:
            print(envelope)
        sys.stdout.flush()
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 141
    return code


if __name__ == '__main__':
    raise SystemExit(main())
