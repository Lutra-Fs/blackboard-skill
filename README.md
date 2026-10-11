# Blackboard

Archive Blackboard Learn course pages and linked files while keeping their
source mappings, hierarchy and acquisition gaps visible. This repository is an
agent skill with a standalone Python CLI.

Use `capture` for pages, `sync` for pages and files, or `fetch` to fill gaps in an
existing archive. Offline commands inspect coverage and registered files.
Incomplete page enumeration preserves the current snapshot; file revisions
preserve previous versions. Reviewed change plans control resource decisions
and local file disposition.

## Requirements

- Python 3.11+ on macOS or Linux. Archive writer locks use POSIX `fcntl`.
- For protected requests, an installed and already signed-in browser backend:
  `ego-browser`, `browser-use`, or `kimi-webbridge`. Login is handled externally.
- For optional YouTube/TED captions, the `yt-dlp` executable on `PATH`.
- The system `file` command for external-document type detection.

The core CLI uses Python's standard library. Browser tools and caption tools
are separate integrations, checked when used. There is no institutional preset.
Recorded live checks cover one deployment and selected integrations; other
deployments need their own verification. See [SOURCES.md](SOURCES.md).

## Quick start

From a checkout of this repository, create an archive directory outside it and
sign in to your Blackboard site through your chosen browser backend:

```bash
mkdir -p ../course-archive
python3 scripts/blackboard_cli.py capture \
  https://learn.example.edu/ultra/courses/_123_1/outline \
  --root ../course-archive --browser-backend ego

# Acquire linked resources from the published capture.
python3 scripts/blackboard_cli.py fetch \
  --root ../course-archive --browser-backend ego

# Inspect the archive offline.
python3 scripts/blackboard_cli.py status --root ../course-archive
python3 scripts/blackboard_cli.py files --root ../course-archive --format paths
python3 scripts/blackboard_cli.py validate --root ../course-archive
```

Replace the example URL with your own course URL. A course ID alone needs
`--site https://learn.example.edu` on first capture. Later commands reuse the
archive's site context. Optional reading-list and Panopto integrations use
[site configuration](references/site-configuration.md).

For agent use, place this checkout at `blackboard/` inside your agent's skills
directory and invoke `$blackboard`. [SKILL.md](SKILL.md) describes the workflow;
[SPEC.md](SPEC.md) defines the acquisition contract. CLI options are available
through `python3 scripts/blackboard_cli.py --help` and each command's `--help`.

## Results and recovery

Output is a schema-v2 JSON envelope on stdout; progress goes to stderr. Success
is measured against the requested scope: a complete page capture may still have
undownloaded files. Full acquisition requires `data.coverage.complete`; inspect
`data.gaps` and `data.excluded` when coverage is partial.

Authentication failures return `blackboard.browser_session_required`. Restore
login in the same browser profile, then run the command in `errors[].solution`.
See [authentication](references/authentication.md) and
[archive recovery](references/archive-state.md).

## Development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Tests use synthetic responses and temporary archives; they need no course
access, browser session or credentials. Installing the development dependencies
also enables the TED plugin contract tests. CI runs the same suite on Linux and
macOS. See [CONTRIBUTING.md](CONTRIBUTING.md) before adding fixtures.

## License

Copyright (C) 2026 Blackboard contributors.

This project is licensed under the GNU General Public License version 3 only
(`GPL-3.0-only`); see [LICENSE](LICENSE). This license covers this project's code
and documentation. Acquired course material retains its own rights.
