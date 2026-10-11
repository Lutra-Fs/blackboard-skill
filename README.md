# Blackboard

Archive Blackboard Learn course pages and linked files.
Keep each file connected to its source page.
Report missing resources and preserve earlier file versions.

This repository contains an agent skill and a Python CLI.
The caller supplies a signed-in browser for protected requests.

## Install the skill

Install Node.js and npm before you use the skills CLI.
Use this command in your project directory:

```bash
npx skills@latest add Lutra-Fs/blackboard-skill --skill blackboard
```

Select your agent when the installer asks.
The [skills CLI](https://github.com/vercel-labs/skills) installs the skill in the current project by default.
For Codex in all projects, use this command:

```bash
npx skills@latest add Lutra-Fs/blackboard-skill --skill blackboard --agent codex --global
```

In Codex, give `$blackboard` a course URL and an archive directory:

```text
Use $blackboard to sync https://learn.example.edu/ultra/courses/_123_1/outline
into ~/course-archive using my signed-in browser.
```

To update the skill, run `npx skills@latest update blackboard`.
The installer includes scripts and reference documents.
Install browser tools and optional caption tools separately.

## Prepare the runtime

| Requirement | Use |
|---|---|
| Python 3.11 or later on macOS or Linux | Use the CLI. Archive writer locks use POSIX `fcntl`. |
| `ego-browser`, `browser-use`, or `kimi-webbridge` | Send protected requests through a signed-in browser. |
| `yt-dlp` on `PATH` | Get optional YouTube and TED captions. |
| System `file` command | Identify external document types. |

The core CLI uses the Python standard library.
The CLI checks optional tools when an operation needs them.
Each site needs its own configuration and verification.
See [recorded evidence](SOURCES.md) for the limits of previous live checks.

## Use the CLI

Use these commands from a repository checkout.
Replace the example URL with your course URL.
Make the archive outside the repository:

```bash
mkdir -p ../course-archive
python3 scripts/blackboard_cli.py capture \
  https://learn.example.edu/ultra/courses/_123_1/outline \
  --root ../course-archive --browser-backend ego
```

When capture publishes a full snapshot, get its linked resources:

```bash
python3 scripts/blackboard_cli.py fetch \
  --root ../course-archive --browser-backend ego
```

Examine the archive without a browser:

```bash
python3 scripts/blackboard_cli.py status --root ../course-archive
python3 scripts/blackboard_cli.py files --root ../course-archive --format paths
python3 scripts/blackboard_cli.py validate --root ../course-archive
```

For a course ID, supply `--site https://learn.example.edu` on the first capture.
Later commands use the site recorded in the archive.
Use `python3 scripts/blackboard_cli.py --help` to see available commands.
Use each command's `--help` to read its options.

## Find the instructions

| Task | Document |
|---|---|
| Choose an operation and examine completion | [Agent workflow](SKILL.md) |
| Interpret terms, results, and exit codes | [Acquisition contract](SPEC.md) |
| Restore browser access | [Authentication](references/authentication.md) |
| Continue interrupted work or examine caption gaps | [Archive recovery](references/archive-state.md) |
| Review resource decisions or remove an old file | [Change plans](references/change-plan.md) |
| Configure reading-list or Panopto services | [Site configuration](references/site-configuration.md) |
| Report a problem or hand over a contribution | [Contributing](CONTRIBUTING.md) |

## Do the offline tests

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Tests use synthetic responses and temporary archives.
The suite operates without course access, browser sessions, or credentials.
Development dependencies include the tools for TED contract tests.
CI runs the suite on Linux and macOS.

## License

Copyright (C) 2026 Blackboard contributors.

The project uses the GNU General Public License version 3 only (`GPL-3.0-only`).
See [LICENSE](LICENSE) for the license text.
The license covers this project's code and documentation.
Acquired course material keeps its own rights.
