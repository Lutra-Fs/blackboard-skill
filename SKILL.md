---
name: blackboard
description: Capture Blackboard Learn course pages and linked files, examine acquisition gaps, or apply reviewed archive changes.
---

# Blackboard

Get course evidence and keep its source mappings.
Use `scripts/blackboard_cli.py` with Python 3.11 or later.
Replace `<SKILL_ROOT>` with the installed skill directory in the commands below.
The caller supplies authentication and interprets course content.

## 1. Choose the operation

Use sufficient local evidence before you request new data.
Use one existing archive directory for each course.

| Requested result | Operation | Completion condition |
|---|---|---|
| Read existing material | Read archived pages and use `files` | Identify readable evidence and any missing or stale sources. |
| Get current pages | `capture` | `data.published` and `data.crawl.complete` are true. Files can stay undownloaded. |
| Get current pages and files | `sync` | Get requested resources or report partial coverage with gaps and exclusions. |
| Fill archive gaps | `fetch` | Get selected resources or report their remaining failures. |
| Apply reviewed archive changes | `change diff`, then `change apply` | Record the reviewed resource decisions and file actions. |

Use a course URL for the first capture or sync:

```bash
python3 "<SKILL_ROOT>/scripts/blackboard_cli.py" sync \
  https://learn.example.edu/ultra/courses/_123_1/outline --root DIR
```

For a course ID, supply a site on the first capture:

```bash
python3 "<SKILL_ROOT>/scripts/blackboard_cli.py" capture \
  _123_1 --site https://learn.example.edu --root DIR
```

For an existing snapshot, use `fetch --root DIR`.
Later commands use the archive's site context.
Offline inspection needs neither a browser nor a course argument.

## 2. Prepare access

For protected requests, use a signed-in `ego-browser`, `browser-use`, or `kimi-webbridge` browser.
If automatic selection chooses another browser, set `--browser-backend`.
For browser profiles or login recovery, read [authentication](references/authentication.md).
For optional services or trusted hosts, read [site configuration](references/site-configuration.md).

## 3. Do the operation and examine the result

For a first full archive, examine `status`, `files`, and `validate`.
Examine completion against the requested scope.
A successful command can leave unavailable resources in `data.gaps`.
Full acquisition requires `data.coverage.complete`.
For a selected resource, examine its result and registered file presence.
Unrelated gaps do not block completion of a targeted task.

Use `data.coverage`, `data.gaps`, and `data.excluded` in the acquisition report.
Exclusions describe scope, not acquired files.
Use `files` to examine registered paths and the `present` field.
For JSON pages, continue with `data.next_offset` until it is null.
For page hierarchy and file sources, read [source mappings](references/hierarchy-sync.md).

For `blackboard.browser_session_required`, repair login in the same browser profile.
Use the command in `errors[].solution` after access returns.
For interrupted work, damaged state, or caption gaps, read [archive recovery](references/archive-state.md).

## 4. Review changes when requested

Use `change diff --root DIR` to examine page changes, file hashes, local edits, and resource gaps.
Compare the actual source content before you select files to keep.
Read [change plans](references/change-plan.md) before you make or apply a plan.
Preview the plan with `change apply PLAN.json --root DIR --dry-run`.
Apply the plan within the user's authorized scope.

A missing source does not authorize local deletion.
Plan application records decisions and file actions.
Get files with `fetch` only when the task also requires acquisition.

## Result reference

For output fields, exit codes, and preview behavior, read [the CLI contract](SPEC.md#results).
Use command help for other options.
Use `request get` for raw JSON reads.
Keep coursework submission, grades, and citation resolution with the caller.
