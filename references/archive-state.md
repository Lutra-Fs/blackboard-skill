# Archive state and recovery

## Continue or retry

Read this section after an interrupted operation or a resource failure.

Use the returned facts when they are current.
If required information is missing or stale, examine `status`.
Use the command in `errors[].solution` when an error gives recovery instructions.

| Need | Action |
|---|---|
| Get missing or failed resources in the default scope | Use `fetch --root DIR`. Completed files stay reusable. |
| Retry selected resources | Add `--asset ID`. |
| Recheck a completed resource | Add `--refresh`. Earlier versions stay available. |
| Retry an unavailable or excluded resource | Apply a `fetch` decision before acquisition. Read [change plans](change-plan.md). |
| Restore protected access | Read [authentication](authentication.md). |

A new acquisition failure can leave partial coverage and a nonzero exit code.
Confirmed unavailable resources stay gaps.
Scope exclusions appear separately.
Completed files stay available after later failures.
Unfinished work can restart.
Read commands and dry-runs examine or preview work without recovery actions.

Gap entries include `asset_id`, `decision`, `reason`, source-page paths, and `last_error`.
When available, `last_error` includes `error_code` and `message`.
Use these facts to select a retry, replacement, or unavailable decision.

## State ownership

Read this section when archive identity or registered state is missing or damaged.

Keep each archive with its original site and course.
Restore a full archive or capture into a new course directory.
Use the CLI to maintain file registrations and resource decisions.

Incomplete capture preserves the current snapshot.
A withdrawn source does not remove local files.
For reviewed file deletion, read [change plans](change-plan.md).

`validate` checks archive integrity and acquisition coverage.
`metadata_ok` can be true with no integrity `issues`, while missing acquisitions still cause validation failure.
Examine coverage against the requested task.
Missing registered files and damaged identity stay errors.

## Caption gaps

Read this section when YouTube or TED captions are missing.

Acquisition requests source-provided English captions.
Use `files` to locate `video.json`.
Its `caption_probe` records the check time and available language tags.

| Result | Meaning and next action |
|---|---|
| `no-captions` | The check completed without an acquired transcript. Examine the probe reason. |
| `no-tracks` | The source advertised no tracks at the recorded check time. |
| `no-requested-language` | The source advertised tracks, but none matched the requested language. |
| `failed` | Use the current returned error. Correct its cause before a targeted retry. |

After you correct an acquisition failure, use `fetch --root DIR --asset ID`.
If the source or caption support changes, recheck with `fetch --root DIR --asset ID --refresh`.
A retry cannot supply captions that the source does not give.
