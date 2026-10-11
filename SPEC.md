# Blackboard Acquisition Contract

Blackboard acquires traceable course pages, hierarchy and linked resources.
Authentication and interpretation of course content belong to the caller;
coursework submission, grades and independent bibliographic acquisition are
outside this contract.

## Inputs And Operations

Ordinary live use needs a course URL, or a site address and course ID, plus an
archive directory and an already signed-in browser. Explicit internal sites are
supported; there is no school preset. Each archive belongs to one site/course.
Offline inspection needs no browser.

- `capture` acquires current page text and hierarchy without downloading files.
- `sync` captures pages and acquires linked resources.
- `fetch` acquires resources from an existing capture, reusing completed files.
- `status`, `files` and `validate` inspect coverage, file mappings and integrity.
- `change diff/apply` reports changes and applies reviewed archive decisions.
- `request get` reads raw JSON without modifying the archive.

Usage and options are in [SKILL.md](SKILL.md) and command help.

## Results And Recovery

A complete capture becomes current; incomplete enumeration preserves the
previous capture. Source withdrawals are reported changes, not local deletions.
Acquisition preserves earlier versions and local annotations. Dry-runs preview
work without publishing or acquiring it.

Protected authentication failures stop acquisition and return
`blackboard.browser_session_required` with a readable `errors[].solution`.
The caller repairs login in the same browser profile and follows that solution.
Closing task groups retains profile authentication. Other resource failures are
reported individually and make acquisition partial/nonzero.

Command success and complete acquisition are distinct. Unavailable resources
remain reported gaps; excluded resources remain visible outside scope.
`validate` checks in-scope coverage and registered files; a resource decision
cannot hide a missing or damaged registered file.

## Reviewed Changes

The caller supplies reasons for resource decisions and reviewed file disposition
through [change plans](references/change-plan.md). Apply checks the reviewed
snapshot and file hashes; it neither downloads files nor decides their meaning.
Interrupted operations report recovery instructions, not an all-or-nothing
batch guarantee.
