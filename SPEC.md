# Acquisition contract

Blackboard gets course pages, their hierarchy, and linked resources.
The caller handles authentication and interprets the acquired content.
Coursework submission, grades, and independent bibliographic acquisition are outside this contract.

## Terms

Use these terms in code descriptions, documents, and contributions.
Use exact command names and JSON keys when you refer to the CLI.

| Term | Meaning |
|---|---|
| Archive | Local evidence and state for one site and course. |
| Snapshot | A published record of captured course pages and hierarchy. |
| Capture | Page acquisition and enumeration before snapshot publication. |
| Resource | An item recorded for acquisition. `asset_id` identifies the resource. |
| Registered file | A local file version recorded for a resource. Registration alone does not prove file presence. |
| Source mapping | The connection between a page, resource, and registered file. |
| Acquisition | The process that obtains resource content. |
| Coverage | The reported state of resources within the requested scope. |
| Gap | An unresolved acquisition or discovery requirement within scope. |
| Exclusion | A recorded decision that places a resource outside acquisition scope. |
| Change plan | Reviewed resource decisions and local file actions. |
| Context handoff | The task information that lets another contributor continue work. |

## Inputs and operations

Live course acquisition needs a course URL or a site address with a course ID.
It also needs an archive directory and a signed-in browser.
Explicit internal sites are supported.
Each archive belongs to one site and course.
Offline inspection needs no browser.

| Command | Behavior |
|---|---|
| `capture` | Get page text and hierarchy. Publish only a full capture. |
| `sync` | Capture pages, then get resources from the published snapshot. |
| `fetch` | Get resources from an existing snapshot. Reuse completed files. |
| `status` | Report the current snapshot and acquisition state. |
| `files` | Report registered files, source mappings, and file presence. |
| `validate` | Examine archive integrity, registered files, and acquisition coverage. |
| `change diff` | Report snapshot changes, file facts, and resource gaps. |
| `change apply` | Apply a reviewed change plan. |
| `request get` | Read raw site JSON without archive changes. |

Read [SKILL.md](SKILL.md) for the workflow.
Read command help for options.

## Results

The default output is one schema-v2 JSON envelope on stdout.
Progress messages go to stderr.
`--format data` prints the payload.
`files --format paths` prints present absolute file paths.
Errors can still return a JSON envelope with these formats.

A full capture becomes the current snapshot.
Incomplete enumeration preserves the previous snapshot.
Source withdrawals appear as changes and do not delete local files.
Acquisition preserves earlier file versions and local annotations.

Command success does not prove complete acquisition.
Unavailable resources stay gaps.
Excluded resources stay visible outside scope.
`validate` checks registered files even when a resource decision changes its scope.
A decision cannot hide a missing or damaged registered file.

| Preview | Result |
|---|---|
| `capture --dry-run` | Preview capture metadata without publication. |
| `sync --dry-run` | Preview capture metadata only. |
| `fetch --dry-run` | Preview acquisition without file acquisition. |
| `change apply --dry-run` | Examine and preview the plan without archive changes. |

| Exit code | Meaning |
|---|---|
| 0 | Command success. Examine coverage separately. |
| 1 | Failed operation or partial acquisition. |
| 2 | Invalid input. |
| 3 | Integrity failure or stale plan blocks the operation. |
| 4 | Browser, authentication, or dependency action required. |
| 75 | Another writer holds the archive lock. |
| 130 | Interrupted operation. |

## Recovery and reviewed changes

Protected authentication failures return `blackboard.browser_session_required`.
The caller signs in again and uses the command in `errors[].solution`.
Other resource failures appear individually and can make acquisition partial.
Read [archive recovery](references/archive-state.md) for retry conditions.

The caller supplies reasons for each change plan entry.
Apply checks the reviewed snapshot and actual file hashes.
Apply does not get files or judge their content.
Interrupted operations return recovery instructions.
Completed file actions can stay after a later action fails.
Read [change plans](references/change-plan.md) for the plan format and execution rules.
