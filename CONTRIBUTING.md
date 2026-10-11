# Contributing

Run the offline test suite described in [README.md](README.md). Keep changes
focused on Blackboard acquisition, source/file mappings and archive recovery;
the caller handles login and interpretation of course content.

Use synthetic course responses with example domains and invented identifiers.
Keep downloaded course content, account details, cookies, meeting access links,
tokens and local configuration outside the repository. Tests must run without a
live browser or network access. Describe any live verification separately from
offline regression results.

For bugs, include the command, Python version, operating system, browser backend
and relevant error code. Redact course URLs and archive paths before sharing
output. Explain the expected behavior and keep any reproduction synthetic.

Follow the existing implementation and public CLI contract. Add a focused
regression test when changing behavior; documentation edits do not need new
tests. Update the relevant reference when changing an acquisition or recovery
contract. Use conventional commit messages.

Before publishing, run Gitleaks on both the working tree and all Git refs:

```bash
gitleaks dir . --redact=100
gitleaks git . --log-opts="--all --reflog --full-history" --redact=100
```

Review personal information and course content separately: a secret scanner
does not recognize every meeting password or personal detail. Files deleted
from the working tree can still exist in history. Keep any private recovery
backup outside the repository and published artifacts.

Contributions are provided under this repository's `GPL-3.0-only` license.
