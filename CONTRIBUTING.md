# Contributing

Give a verified problem report, a minimal reproduction, compatibility evidence, documentation, or a patch.
A useful report is a contribution even when its code does not become part of the project.
The maintainer selects requests to accept.

## Choose the contribution

Keep work within the [acquisition contract](SPEC.md).
For a focused fix or document correction, open an issue or pull request (PR).
A patch is optional for a problem report.
For a new backend, archive format, or acquisition scope, discuss the expected behavior first.
Keep each PR focused on one problem.

## Hand over the task

Give the next contributor enough information to continue the work:

1. Give the task and its acceptance conditions.
2. Give actual and expected results.
3. Give a minimal reproduction with synthetic data.
4. Give the reason for the implementation choice when you supply a patch.
5. Record verification commands, observed results, and unverified cases.
6. Record useful failed attempts, remaining work, and related issue or commit links.

For bugs, include the Python version, operating system, browser backend, and relevant error code.
Report live observations separately from offline test results.
Make the reproduction usable without access to your institution's site.

## Supply agent context

Agent-assisted contributions are welcome.
The submitter is responsible for the task description and verification claims.
Give the task handoff whether you used an agent or worked manually.

An original task prompt or shared conversation can show the task's intent and earlier decisions.
These materials are optional.
Before publication, examine the selected task context and remove private information.
Mark removed sections as `[REDACTED]`.
Share a conversation only after the contributor confirms that the selected material can be public.
If you omit it, put the information needed to continue work in the PR.

Treat conversations as context.
Examine their claims against current code and verification results.
Use PR comments for new evidence, technical questions, and decisions.

## Expect maintainer takeover

The maintainer can merge, modify, partly use, take over, or independently implement a contribution.
The maintainer can replace all code in the original patch.
Takeover does not require repeated revision requests to the contributor.

At takeover, the maintainer records that decision in the issue or PR.
When work is completed, the maintainer records the outcome and a link to the final implementation.
Adopted problem reports, reproductions, and verification keep contributor credit and source links.
Use `Co-authored-by` only for actual joint authorship.
Record other contributions as reporting, reproduction, or verification credit.

## Do checks

Use the [offline test instructions](README.md#do-the-offline-tests).
Add a focused regression test when behavior changes.
Document changes need no new tests.
Keep tests independent of live browsers and network access.
Update the contract or relevant reference when behavior changes.
Use Conventional Commits for commit messages.

## Write readable documents

Use [ASD-STE100 Issue 9](https://www.asd-ste100.org/assets/files/ASD-STE100_ISSUE9.pdf) for technical English.
Use approved dictionary meanings and consistent [project terms](SPEC.md#terms).
Use exact identifiers for commands, options, paths, and JSON keys.
Use short sentences and active instructions.
Keep procedural sentences within 20 words and descriptive sentences within 25 words.
Put one instruction in each sentence.
Put a condition before its action.
Keep each paragraph on one topic.

Keep shared definitions in SPEC and operation steps in SKILL.
Put branch-specific detail in the relevant reference.
Give each reference a link and the condition that makes it useful.
Before submission, make sure that each command example works.
Make sure that each local document link points to the correct content.

## Protect private information

Use example domains, invented identifiers, and synthetic course responses.
Keep credentials, cookies, meeting access links, real course content, and personal configuration outside public contributions.
Replace private URLs and local paths before you share command output.
Examine prompts, attachments, and conversation links as well as patches.

Before publication, scan the working tree and all Git refs:

```bash
gitleaks dir . --redact=100
gitleaks git . --log-opts="--all --reflog --full-history" --redact=100
```

Examine personal information and course content separately.
A secret scanner does not detect every private detail.
Deleted files can stay in Git history.
Keep private recovery backups outside the repository and published artifacts.

Contributions use this repository's `GPL-3.0-only` license.
