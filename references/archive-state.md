# Archive State And Recovery

## Continue Or Retry

Reuse returned facts; query `status` when information needed for the next action
is missing or stale. `fetch` retries default-scope missing/failed resources and
reuses completed files. Use `--asset ID` for targeted work or `--refresh` to
recheck completed exports while retaining prior versions.

Follow `errors[].solution` after a failure. A new acquisition failure remains
partial/nonzero. Confirmed unavailable resources remain coverage gaps; scope
exclusions are reported separately. Use [change plans](change-plan.md) to revise
those decisions before retrying. For login failures, see
[authentication](authentication.md).

Completed files are reused on retry; unfinished work may restart. Read commands
and dry-runs inspect or preview rather than resume work.

## State Ownership

Keep each archive tied to its original site and course. For missing or damaged
archive state, restore a complete archive or capture into a new course directory.
Use the CLI to maintain file registrations and resource decisions.

An incomplete capture leaves the previous archive current. A withdrawn source
does not delete local files, and later failures do not erase earlier downloads.
Use [change plans](change-plan.md) for reviewed file deletion. Follow returned
recovery instructions before further changes.

`validate` checks archive integrity and in-scope acquisition. `metadata_ok` with
no `issues` can still fail for undownloaded resources; interpret coverage against
the requested task. Missing registered files or damaged identity remain errors.

## Caption Gaps

YouTube/TED acquisition fetches source-provided English captions.

- `no-captions` is a completed check, not an acquired transcript. Locate
  `video.json` through `files`; its `caption_probe` records the check time and
  available tags. `no-tracks` means none were advertised at that check;
  `no-requested-language` means tracks exist but none match the request.
- For `failed`, use the current returned error rather than an earlier probe.
  After fixing the reported cause, retry with `fetch --root DIR --asset ID`.
- To recheck `no-captions` after the source or caption support changes, use
  `fetch --root DIR --asset ID --refresh`. Retrying cannot
  supply a transcript when the source has no matching captions.
