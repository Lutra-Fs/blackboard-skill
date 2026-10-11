# Change Plans

`change diff --root DIR` reports the current `base_snapshot`, changes against its
previous published snapshot, source-page paths, all retained registered file
versions with actual/original SHA-256 and local-modification flags, and resource
gaps/exclusions. On an archive predating snapshot lineage, `before_snapshot` is
null and `pages` is null; no historical delta is invented. A known first capture
has an empty baseline and reports its nodes as added.
These are facts, not semantic replacement decisions. Read page/file content to
judge whether a revision supersedes or complements another version.

## Input

```json
{
  "version": 1,
  "base_snapshot": "value from change diff",
  "resources": [
    {
      "asset_id": "known resource ID",
      "decision": "unavailable",
      "reason": "Source link verified broken; no usable alternative found."
    }
  ],
  "files": [
    {
      "old": {"path": "Week01/old.pdf", "sha256": "actual 64-digit lowercase SHA-256"},
      "new": {"path": "Week01/new.pdf", "sha256": "actual 64-digit lowercase SHA-256"},
      "action": "remove_old",
      "reason": "New revision covers the old material; old file has no unique annotations."
    }
  ]
}
```

`resources` and `files` are optional arrays; at least one entry is required.
Every entry needs a nonempty reason. Unlisted items are unchanged.

Resource decisions replace one another, never stack:

- `fetch`: default acquisition; restores normal attempts on the next fetch.
- `unavailable`: still needed but confirmed unobtainable; skip routine attempts
  and continue reporting the actual missing resource in `gaps`.
- `exclude`: outside the chosen acquisition scope; report in `excluded`, not gaps.

Explicit asset selection and refresh respect these decisions. To retry a decided
resource, apply `fetch` first. Apply itself never downloads or logs in. A global
authentication error requires external repair, not bulk unavailable decisions.

File actions are `keep_both` (retain and record the review) or `remove_old`.
Both references require registered course-relative paths and reviewed actual
SHA-256; new must already exist. Deletion checks all entries before any mutation.
Deleting an active resource's only version in favor of a different resource also
requires an explicit exclusion for the superseded resource, avoiding silent
redownload. Same-resource retained replacements remain reusable.

## Execute

```bash
python3 "<SKILL_ROOT>/scripts/blackboard_cli.py" change apply plan.json --root DIR --dry-run
python3 "<SKILL_ROOT>/scripts/blackboard_cli.py" change apply plan.json --root DIR
```

Stale snapshots/files refuse execution and return a solution to review again.
After a file-cleanup failure, fix the reported cause and rerun the same
`change apply` plan; this finishes local cleanup without downloading resources.
Apply reports completed/pending deletion entries on execution failure; batch
execution is not an all-or-nothing filesystem transaction. Unfinished
acquisitions must recover with `fetch` before applying a new plan.
