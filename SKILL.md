---
name: blackboard
description: Capture Blackboard Learn course pages and linked files, inspect acquisition gaps, or apply reviewed archive changes.
---

# Blackboard

Acquire traceable local course evidence and maintain its source/file mappings.
Use `scripts/blackboard_cli.py` with Python 3.11+. Protected network requests need
an already signed-in browser (`ego-browser`, `browser-use` or kimi-webbridge);
authentication belongs to the caller. Offline archive inspection needs no live
browser or course argument. There is no school preset.

## Choose The Operation

| Goal | Operation | Completion evidence |
|---|---|---|
| Read existing course material | Read archived pages; use `files` for source/file mappings | Relevant local evidence is readable; identify missing or stale sources |
| Get current pages without downloading files | `capture` | `data.published` and `data.crawl.complete` are true; file coverage is not a requirement |
| Get current pages and linked files | `sync` | Requested in-scope files are acquired; otherwise report partial coverage and gaps/exclusions |
| Fill gaps in an existing snapshot | `fetch`, optionally `--asset ID` | Selected resources are present; otherwise report their remaining failures. Unrelated gaps do not block a targeted task |

Choose the operation that matches the request. Reuse sufficient local evidence;
query only facts needed for the next action. Use one existing directory per
course. These are alternative entry points:

```bash
python3 "<SKILL_ROOT>/scripts/blackboard_cli.py" sync \
  https://learn.example.edu/ultra/courses/_123_1/outline --root DIR

# A course ID needs a site on the first capture.
python3 "<SKILL_ROOT>/scripts/blackboard_cli.py" capture \
  _123_1 --site https://learn.example.edu --root DIR

python3 "<SKILL_ROOT>/scripts/blackboard_cli.py" fetch --root DIR
```

A course URL supplies the site; later commands reuse the archived site context.
Explicit internal hosts/IPs are supported. Pin `--browser-backend` when automatic
selection chooses a different browser. `--browser-session` names a task group,
not a cookie profile; closing an Ego group retains profile authentication.
Optional integrations use [site configuration](references/site-configuration.md).
Missing integration configuration is reported only for requested/evidenced use.

Incomplete enumeration leaves the current snapshot unchanged. Remote withdrawals
are reported as changes, not automatic local deletions. `fetch --asset ID` narrows
work, including reported discovery gaps; `--refresh` rechecks completed resources
and preserves prior versions.
A capture/sync dry-run previews metadata only; fetch dry-run previews acquisition.

For a first full archive, inspect `status`, `files` and `validate` to verify its
coverage and registered paths. A metadata-only request ends at complete page
publication; undownloaded attachments are not a capture failure. Dry-runs preview
work, not publication or acquisition.

On `blackboard.browser_session_required`, repair login externally in the same
browser profile, then follow `errors[].solution`. Published metadata resumes with
fetch; interrupted enumeration repeats capture. See [authentication](references/authentication.md).

## Inspect And Decide

`status` reports actual coverage, `gaps` and `excluded`; `files` lists registered
paths (`present` matters, JSON pages use `next_offset`). `validate` checks registered
files and in-scope coverage. Command success does not imply complete coverage:
confirmed unavailable resources remain gaps even when ordinary fetch exits zero.
For acquisition handoffs, use returned results, `data.coverage`, `data.gaps` and
`data.excluded`, not just `ok` or fetch counts. Full in-scope acquisition requires
`data.coverage.complete`; targeted tasks need evidence for the selected resources.
Gap entries carry `asset_id`, `decision`, `reason`, source-page paths and
`last_error` (`error_code`/`message` when present). These
locate the evidence for deciding what to retry, replace or leave unavailable;
exclusions describe scope, not successful downloads.

Use `change diff --root DIR` for page changes, file fingerprints/local edits, and
acquisition gaps when review is needed. Compare the actual sources, then write
the resource decisions or file disposition described in [change plans](references/change-plan.md).
Preview with `change apply PLAN.json --root DIR --dry-run`, then apply within the
user's authorized scope. Decisions persist until explicitly changed; a missing
source is not proof that its local file should be deleted.
To retry a resource marked unavailable/excluded, apply a `fetch` decision first,
then run a targeted fetch. Applying a plan records decisions and file disposition;
it neither acquires files nor publishes an incomplete capture. A disposition task
ends when the reviewed decisions/file actions are applied; fetch only if the
request also calls for acquisition.

Output defaults to one schema-v2 JSON envelope; progress goes to stderr.
`--format data` prints the payload; `files --format paths` prints present absolute
paths. Use command help for remaining options. Raw JSON reads use `request get`.

- Retry or interrupted work: [archive recovery](references/archive-state.md#continue-or-retry)
- Damaged/mismatched archive: [state ownership](references/archive-state.md#state-ownership)
- Missing YouTube/TED captions: [caption gaps](references/archive-state.md#caption-gaps)
- Page hierarchy and file handoff: [hierarchy sync](references/hierarchy-sync.md)

Scope is course reading/acquisition, not submissions, grades or citation resolution.
Exit codes: 0 command success, 1 failed/partial, 2 invalid input, 3 integrity/stale
plan blocked, 4 browser/auth/dependency action, 75 writer lock busy, 130 interrupted.
