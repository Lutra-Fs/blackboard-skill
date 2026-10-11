"""Typed request objects for the CLI → service boundary.

The CLI parses argv into these frozen values at the edge (one construction
site per command) and hands them to :mod:`course_service`; the service reads
only the fields it consumes and never sees argparse names. This module is
deliberately dependency-free data plus the boundary's input-contract
exception so the CLI and the service can share it without coupling to each
other.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CLI_PATH = Path(__file__).resolve().parents[1] / 'blackboard_cli.py'


class UsageError(ValueError):
    """Invalid caller input: bad request-field values or combinations.

    Raised by the CLI for argparse-level grammar and by the service for
    request preconditions argparse cannot see; the CLI maps it to exit 2.
    """

    code = "invalid-input"


@dataclass(frozen=True)
class BrowserOptions:
    """Browser-session options shared by session/capture/sync/fetch."""

    browser_session: str | None = None
    browser_backend: str | None = None


@dataclass(frozen=True)
class FetchRequest:
    """One fetch invocation; root-agnostic — the archive root is the caller's.

    ``url``/``out`` select a single explicit attachment (mutually exclusive
    with ``assets``); ``kinds``/``assets`` narrow scope; the rest steer the
    acquisition providers.
    """

    kinds: tuple[str, ...] | None = None
    assets: tuple[str, ...] | None = None
    url: str | None = None
    out: Path | None = None
    refresh: bool = False
    dry_run: bool = False
    youtube_cookies_browser: str | None = None
    browser_session: str | None = None
    browser_backend: str | None = None
