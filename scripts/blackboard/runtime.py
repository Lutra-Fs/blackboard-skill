"""Single subprocess boundary for every external command the skill runs.

Both subprocess entry points live on :class:`CommandRunner`: the raising
:meth:`CommandRunner.run` used by downloader/provider code (yt-dlp, ``file``,
helpers in external_documents/youtube_transcripts) and the
non-raising :meth:`CommandRunner.run_cli` used by the CLI browser backends in
:mod:`browser_backend` (ego-browser, browser-use). Launch and
timeout failures become ordinary values or exceptions at this one boundary,
and child environments are sanitized here once (:func:`_clean_child_env`) so
external interpreters never inherit this project's ``PYTHONPATH``/``PYTHONHOME``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NamedTuple


class CommandExecutionError(RuntimeError):
    """Raised by the single subprocess boundary on launch, timeout, or exit failure."""


class CliResult(NamedTuple):
    """One completed (or failed) CLI invocation; launch errors map to exit 127, timeouts to 124."""

    returncode: int
    stdout: str
    stderr: str


def _clean_child_env() -> dict[str, str]:
    """Environment for external binaries: drop interpreter-leaking variables.

    yt-dlp (Homebrew Python 3.14) and other external tools must not inherit
    this project's ``PYTHONPATH``/``PYTHONHOME``. When the sync runs inside a
    virtualenv (e.g. an agent session exporting PYTHONPATH to a 3.11 venv),
    the child Python loads the venv's pure-Python packages against the
    system interpreter's compiled backends — the classic symptom is a cffi
    version-mismatch crash inside yt-dlp's Cryptodome import.
    """
    env = dict(os.environ)
    for var in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(var, None)
    return env


class CommandRunner:
    """Single subprocess boundary used by every downloader entry point and CLI backend."""

    def run(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
                env=_clean_child_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CommandExecutionError(f"command could not complete: {cmd[0]}: {exc}") from exc
        if check and result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or f"command failed: {' '.join(cmd)}"
            raise CommandExecutionError(message)
        return result

    def run_cli(self, cmd: list[str], *, timeout: float, input_text: str | None = None) -> CliResult:
        """Run one CLI command without raising.

        Launch failures and timeouts become result rows (returncode 127/124)
        instead of exceptions, so the CLI browser backends can report a
        missing binary or a hung tool as an ordinary backend error.
        """
        try:
            proc = subprocess.run(
                cmd,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=_clean_child_env(),
            )
            return CliResult(proc.returncode, proc.stdout or "", proc.stderr or "")
        except subprocess.TimeoutExpired:
            return CliResult(124, "", f"timed out after {timeout}s: {cmd[0]}")
        except OSError as exc:
            return CliResult(127, "", f"could not launch {cmd[0]}: {exc}")
