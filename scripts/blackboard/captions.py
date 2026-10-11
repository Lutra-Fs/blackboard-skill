"""Shared caption projection and formatting.

Panopto and YouTube transcripts project onto the same caption schema
(``{Caption, Time, CaptionDuration}``) and produce the same SRT/Markdown
output. This module owns that shared logic so both stages — and any future
video-transcript stage — use one definition instead of two parallel copies.
"""
from __future__ import annotations

from typing import Any


def caption_timestamp(seconds: float) -> str:
    """Format seconds as ``HH:MM:SS,mmm`` (SRT cue timing)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole_seconds = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:
        whole_seconds += 1
        millis = 0
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{millis:03}"


def captions_to_srt(captions: list[dict[str, Any]]) -> str:
    """Render captions as an SRT subtitle document."""
    lines: list[str] = []
    counter = 1
    for item in captions:
        caption = str(item.get("Caption") or "").strip()
        if not caption:
            continue
        start = float(item.get("Time") or 0)
        duration = float(item.get("CaptionDuration") or 0)
        end = start + duration
        lines.extend([str(counter), f"{caption_timestamp(start)} --> {caption_timestamp(end)}", caption, ""])
        counter += 1
    return "\n".join(lines).rstrip() + "\n"


def captions_to_markdown(title: str, source_url: str, language: str, captions: list[dict[str, Any]]) -> str:
    """Render captions as a readable Markdown transcript."""
    from .archive_io import utc_now_iso
    lines = [
        f"# {title}",
        "",
        f"- Source: {source_url}",
        f"- Language: `{language}`",
        f"- Captured at: `{utc_now_iso()}`",
        "",
        "## Transcript",
        "",
    ]
    for item in captions:
        caption = str(item.get("Caption") or "").strip()
        if not caption:
            continue
        start = float(item.get("Time") or 0)
        lines.append(f"- `{caption_timestamp(start).replace(',', '.')}` {caption}")
    return "\n".join(lines).rstrip() + "\n"


def safe_caption_items(captions: Any) -> list[dict[str, Any]]:
    """Project untrusted caption data onto the allowlisted schema.

    Drops malformed rows (non-dict, empty caption, non-numeric time) so the
    persisted rows contain only Caption, Time, and CaptionDuration.
    """
    safe: list[dict[str, Any]] = []
    if not isinstance(captions, list):
        return safe
    for item in captions:
        if not isinstance(item, dict):
            continue
        caption = str(item.get("Caption") or "").strip()
        if not caption:
            continue
        try:
            start = float(item.get("Time") or 0)
            duration = float(item.get("CaptionDuration") or 0)
        except (TypeError, ValueError):
            continue
        safe.append({"Caption": caption, "Time": start, "CaptionDuration": duration})
    return safe
