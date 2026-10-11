# Change plans

Read this document before you make or apply resource decisions and file actions.

## Examine the evidence

Use `change diff --root DIR`.
The result reports:

- The current `base_snapshot` and changes from the previous published snapshot.
- Source-page paths.
- All kept registered file versions.
- Actual and original SHA-256 hashes, with local modification flags.
- Resource gaps and exclusions.

For an archive without recorded snapshot history, `before_snapshot` and `pages` are null.
For a known first capture, the baseline is empty and its nodes appear as additions.

Read page and file content.
Record whether a revision replaces or complements an earlier version.

## Write the plan

Replace every placeholder below with a reviewed value from the archive.
Use the actual file hashes reported by `change diff`.

```json
{
  "version": 1,
  "base_snapshot": "value from change diff",
  "resources": [
    {
      "asset_id": "known resource ID",
      "decision": "unavailable",
      "reason": "The source link is broken. No usable alternative exists."
    }
  ],
  "files": [
    {
      "old": {"path": "Week01/old.pdf", "sha256": "actual 64-digit lowercase SHA-256"},
      "new": {"path": "Week01/new.pdf", "sha256": "actual 64-digit lowercase SHA-256"},
      "action": "remove_old",
      "reason": "The new revision covers the old material. The old file has no unique annotations."
    }
  ]
}
```

`resources` and `files` are optional arrays.
Supply at least one entry.
Give each entry a nonempty reason.
Unlisted items stay unchanged.

### Resource decisions

| Decision | Effect |
|---|---|
| `fetch` | Restore normal acquisition attempts on the next fetch. |
| `unavailable` | Keep the requirement as a gap. Skip routine acquisition attempts. |
| `exclude` | Place the resource outside scope. Report it in `excluded`. |

A new decision replaces the previous decision.
Explicit asset selection and refresh respect these decisions.
To retry a decided resource, apply `fetch` first.
For a global authentication failure, restore access externally.
Use resource decisions only when evidence supports the individual resource decision.

### File actions

| Action | Effect |
|---|---|
| `keep_both` | Keep both files and record the review. |
| `remove_old` | Remove the reviewed old file and keep the new file. |

Both file references need registered paths relative to the course archive and reviewed actual SHA-256 hashes.
The new file must already exist.
The CLI checks all deletion entries before it changes files.

If a different resource replaces an active resource's only file version, explicitly exclude the superseded resource.
This decision prevents automatic acquisition of the superseded resource.
Kept replacements for the same resource stay reusable.

## Preview and apply

Replace `<SKILL_ROOT>` with the installed skill directory.

```bash
python3 "<SKILL_ROOT>/scripts/blackboard_cli.py" change apply plan.json --root DIR --dry-run
python3 "<SKILL_ROOT>/scripts/blackboard_cli.py" change apply plan.json --root DIR
```

Apply within the user's authorized scope.
Plan application records decisions and file actions.
It does not get resources or restore login.
A stale snapshot or file hash blocks execution.
Use the returned solution to examine the evidence again.

Before a new plan, recover unfinished acquisitions with `fetch`.
After a file cleanup failure, correct the cause.
Use the same `change apply` plan again.
The error reports completed and pending deletion entries.
Completed actions can stay when later cleanup fails.
A repeated plan finishes local cleanup without resource acquisition.
