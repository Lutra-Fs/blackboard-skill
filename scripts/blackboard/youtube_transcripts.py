#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse

from .archive_io import (
    atomic_write_json,
    atomic_write_text,
    safe_artifact_name,
    utc_now_iso,
)
from . import captions
from .runtime import CommandExecutionError, CommandRunner
from .url_policy import is_video_transcript_url

from .sync_models import Page

# Default English includes regional tags and the original automatic track.
# Select one available track per requested language, preferring manual captions;
# do not fetch every translation or infer absence from an exact-tag mismatch.
DEFAULT_LANGUAGES = ("en",)

# yt-dlp is the single subprocess boundary for YouTube capture. It reads the
# browser cookie jar in-memory (no file is written) and exits with a non-zero
# code on bot-detection or a missing video.
YT_DLP_BIN = shutil.which("yt-dlp") or "yt-dlp"


@dataclass(frozen=True)
class YouTubeLink:
    url: str
    course_root: Path
    page_id: str | None = None
    page_title: str | None = None
    link_title: str | None = None
    source_type: str = "page-link"

    @property
    def video_id(self) -> str:
        parsed = urlparse(self.url)
        host = (parsed.hostname or "").lower()
        if host == "youtu.be":
            return parsed.path.strip("/").split("/", 1)[0]
        if host.endswith("ted.com"):
            # /talks/<slug> — slug is the stable talk identifier.
            segments = [segment for segment in parsed.path.split("/") if segment]
            if "talks" in segments:
                idx = segments.index("talks")
                if idx + 1 < len(segments):
                    return segments[idx + 1]
            return ""
        query = parse_qs(parsed.query)
        if query.get("v"):
            return query["v"][0]
        # /embed/<id> and /shorts/<id> expose the id on the path.
        segments = [segment for segment in parsed.path.split("/") if segment]
        for prefix in ("embed", "shorts", "v"):
            if prefix in segments:
                idx = segments.index(prefix)
                if idx + 1 < len(segments):
                    return segments[idx + 1]
        return ""


def is_viewer_url(url: str) -> bool:
    if not is_video_transcript_url(url):
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        return bool(parsed.path.strip("/"))
    if host.endswith("ted.com"):
        segments = [segment for segment in parsed.path.split("/") if segment]
        return "talks" in segments
    segments = [segment for segment in parsed.path.split("/") if segment]
    query = parse_qs(parsed.query)
    if query.get("v"):
        return True
    return any(prefix in segments for prefix in ("embed", "shorts", "v"))


def canonical_viewer_url(url: str) -> str:
    if not is_viewer_url(url):
        raise ValueError("Unsupported video viewer URL")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith("ted.com"):
        host = "www.ted.com"
        slug = YouTubeLink(url, course_root=Path(".")).video_id
        return urlunparse(("https", host, f"/talks/{slug}", "", "", ""))
    if host == "youtu.be" or host.endswith("youtube-nocookie.com"):
        host = "www.youtube.com"
    video_id = YouTubeLink(url, course_root=Path(".")).video_id
    return urlunparse(("https", host, "/watch", "", f"v={video_id}", ""))


def add_unique_link(links: dict[str, YouTubeLink], link: YouTubeLink) -> None:
    try:
        canonical = canonical_viewer_url(link.url)
    except ValueError:
        return
    links.setdefault(canonical, replace(link, url=canonical))


def iter_youtube_links(root: Path, *, pages: list[Page]) -> list[YouTubeLink]:
    links: dict[str, YouTubeLink] = {}
    for page in pages:
        for item in page.external_links + [vars(a) for a in page.attachments] + page.child_links:
            add_unique_link(links, YouTubeLink(
                item.get("href") or "", course_root=root.resolve(),
                page_id=page.content_id, page_title=page.title,
                link_title=item.get("text") or item.get("title"),
                source_type="archived-page-link",
            ))
    return list(links.values())


def _ytdlp_base(cookies_browser: str | None, canonical_url: str) -> list[str]:
    cmd = [YT_DLP_BIN, "--no-warnings", "--no-playlist"]
    if urlparse(canonical_url).hostname == "www.ted.com":
        # A per-command plugin bridges the proven TED schema change.
        # Global yt-dlp and unrelated YouTube invocations stay untouched.
        plugins = Path(__file__).with_name("ytdlp-plugins")
        cmd.extend(["--plugin-dirs", str(plugins)])
    if cookies_browser:
        cmd.extend(["--cookies-from-browser", cookies_browser])
    return cmd


def fetch_video_metadata(canonical_url: str, *, cookies_browser: str | None, runner: CommandRunner | None = None) -> dict[str, Any]:
    """Return yt-dlp's full --dump-json payload for a canonical video URL.

    The raw payload is the untrusted upstream response; callers consume it only
    through :func:`safe_video_summary`, which projects it onto an allowlist.
    yt-dlp selects the correct extractor (YouTube or TED) from the URL host.
    """
    if not canonical_url:
        raise CommandExecutionError("youtube fetch requested with empty canonical URL")
    runner = runner or CommandRunner()
    result = runner.run(_ytdlp_base(cookies_browser, canonical_url) + ["--skip-download", "--dump-json", canonical_url], check=True, timeout=120)
    try:
        return json.loads(result.stdout.splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise CommandExecutionError(f"yt-dlp returned no JSON for {canonical_url}: {exc}") from exc


def fetch_subtitle_languages(meta: dict[str, Any]) -> tuple[list[str], list[str]]:
    manual = list((meta.get("subtitles") or {}).keys())
    automatic = list((meta.get("automatic_captions") or {}).keys())
    return manual, automatic


def fetch_transcript(
    canonical_url: str,
    language: str,
    *,
    cookies_browser: str | None,
    runner: CommandRunner | None = None,
) -> tuple[str, str] | None:
    """Download one language track and return ``(format, vtt_body)``.

    Prefers a manual caption track over auto-generated captions. Returns None
    when yt-dlp cannot produce a VTT body for the requested language.
    """
    runner = runner or CommandRunner()
    with tempfile.TemporaryDirectory(prefix="youtube-transcript-") as workdir:
        out_template = str(Path(workdir) / "%(title)s [%(id)s]")
        result = runner.run(
            _ytdlp_base(cookies_browser, canonical_url)
            + [
                "--skip-download",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs", language,
                "--sub-format", "vtt",
                "-o", out_template,
                canonical_url,
            ],
            check=False,
            timeout=120,
        )
        if result.returncode != 0:
            return None
        candidates = sorted(Path(workdir).glob("*.vtt"))
        for candidate in candidates:
            body = candidate.read_text(encoding="utf-8", errors="replace")
            if body.strip():
                return "vtt", body
    return None


_VTT_TIMESTAMP = re.compile(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})")
_VTT_TAG = re.compile(r"<[^>]+>")


def vtt_to_captions(vtt_body: str) -> list[dict[str, Any]]:
    """Project a WebVTT transcript onto the same caption schema as Panopto."""
    captions: list[dict[str, Any]] = []
    pending_start: float | None = None
    pending_end: float | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal pending_start, pending_end, buffer
        if pending_start is None or not buffer:
            pending_start = pending_end = None
            buffer = []
            return
        text = " ".join(part.strip() for part in buffer if part.strip())
        text = _VTT_TAG.sub("", text).replace("&amp;", "&").replace("&#39;", "'").replace("&gt;", ">").replace("&lt;", "<")
        text = re.sub(r"\s+", " ", text).strip()
        duration = max(0.0, (pending_end or pending_start) - pending_start)
        if text:
            captions.append({"Caption": text, "Time": pending_start, "CaptionDuration": duration})
        pending_start = pending_end = None
        buffer = []

    for raw_line in vtt_body.splitlines():
        line = raw_line.rstrip("\n")
        if not line.strip():
            flush()
            continue
        if line.strip().startswith("WEBVTT") or line.strip().startswith("NOTE"):
            continue
        if "-->" in line:
            flush()
            times = [t for t in _VTT_TIMESTAMP.findall(line)]
            if len(times) >= 2:
                h, m, s, ms = times[0]
                pending_start = int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0
                h, m, s, ms = times[1]
                pending_end = int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0
            continue
        if line.strip().isdigit() and pending_start is None and not buffer:
            continue
        if _VTT_TIMESTAMP.fullmatch(line.strip().replace(",", ".")):
            continue
        buffer.append(line)
    flush()
    return captions


def captions_to_srt(captions_list: list[dict[str, Any]]) -> str:
    return captions.captions_to_srt(captions_list)


def captions_to_markdown(title: str, source_url: str, language: str, captions_list: list[dict[str, Any]]) -> str:
    return captions.captions_to_markdown(title, source_url, language, captions_list)


def safe_caption_items(captions_list: Any) -> list[dict[str, Any]]:
    return captions.safe_caption_items(captions_list)


def safe_video_summary(meta: dict[str, Any], caption_languages: list[str] | None = None) -> dict[str, Any]:
    video: dict[str, Any] = {}
    for source_key, output_key, cast in (
        ("title", "title", str),
        ("uploader", "channel", str),
        ("uploader_id", "channel_id", str),
        ("duration", "duration_seconds", lambda v: int(v) if isinstance(v, (int, float)) and v >= 0 else None),
        ("upload_date", "uploaded_at", str),
        ("view_count", "view_count", lambda v: int(v) if isinstance(v, (int, float)) and v >= 0 else None),
        ("like_count", "like_count", lambda v: int(v) if isinstance(v, (int, float)) and v >= 0 else None),
    ):
        value = meta.get(source_key)
        try:
            cast_value = cast(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            cast_value = None
        if cast_value not in ("", None):
            video[output_key] = cast_value
    languages = caption_languages or []
    return {
        "schema_version": 1,
        "kind": "youtube-video-summary",
        "video": video,
        "caption_languages": sorted(set(language for language in languages if language)),
    }



def select_caption_tracks(requested: list[str], manual: list[str], automatic: list[str]) -> list[dict[str, str]]:
    """Choose one safe language tag per request, manual before automatic.

    A base language such as en accepts its variants. An explicit regional tag
    stays exact. Original automatic captions precede generic/translated ones.
    """
    tracks: list[dict[str, str]] = []
    for language in requested:
        def candidates(available: list[str]) -> list[str]:
            return sorted(tag for tag in available
                          if isinstance(tag, str)
                          and re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*", tag)
                          and (tag == language or ("-" not in language and tag.startswith(language + "-"))))
        manual_matches = candidates(manual)
        auto_matches = candidates(automatic)
        tag = next(iter(sorted(manual_matches, key=lambda t: (t != language, t))), None)
        source = "manual"
        if tag is None:
            tag = next(iter(sorted(auto_matches, key=lambda t: (t != language + "-orig", t != language, t))), None)
            source = "automatic"
        if tag is not None and all(row["language"] != tag for row in tracks):
            tracks.append({"language": tag, "source": source})
    return tracks


def save_transcripts(
    link: YouTubeLink,
    meta: dict[str, Any],
    *,
    languages: list[str],
    cookies_browser: str | None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    canonical_url = canonical_viewer_url(link.url)
    video_id = link.video_id
    title = str(meta.get("title") or link.link_title or link.page_title or video_id)
    out_dir = link.course_root / "_blackboard" / "youtube" / safe_artifact_name(
        f"{title} {video_id}",
        fallback="youtube",
    )
    manual_langs, auto_langs = fetch_subtitle_languages(meta)
    requested = languages or list(DEFAULT_LANGUAGES)
    planned_tracks = select_caption_tracks(requested, manual_langs, auto_langs)
    planned_captions = [track["language"] for track in planned_tracks]
    checked_at = utc_now_iso()
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out_dir / "source.json", {
        "source_url": canonical_url,
        "video_id": video_id,
        "course_root": str(link.course_root),
        "blackboard_page_id": link.page_id,
        "blackboard_page_title": link.page_title,
        "link_title": link.link_title,
        "source_type": link.source_type,
        "captured_at": utc_now_iso(),
    })
    written: list[str] = []
    captured_languages: list[str] = []
    for language in planned_captions:
        fetched = fetch_transcript(canonical_url, language, cookies_browser=None, runner=runner)
        if fetched is None and cookies_browser:
            fetched = fetch_transcript(
                canonical_url, language, cookies_browser=cookies_browser, runner=runner
            )
        if not fetched:
            continue
        captions = safe_caption_items(vtt_to_captions(fetched[1]))
        if not captions:
            continue
        safe_language = safe_artifact_name(language, fallback="unknown")
        vtt_path = out_dir / f"transcript_{safe_language}.vtt"
        srt_path = out_dir / f"captions_{safe_language}.srt"
        json_path = out_dir / f"captions_{safe_language}.json"
        md_path = out_dir / f"transcript_{safe_language}.md"
        atomic_write_text(vtt_path, fetched[1])
        atomic_write_text(srt_path, captions_to_srt(captions))
        atomic_write_json(json_path, captions)
        atomic_write_text(md_path, captions_to_markdown(title, canonical_url, language, captions))
        written.extend([str(vtt_path), str(srt_path), str(json_path), str(md_path)])
        captured_languages.append(language)
    status = "downloaded" if written else ("failed" if planned_captions else "no-captions")
    reason = ("captured" if written else "capture-failed" if planned_captions
              else "no-requested-language" if manual_langs or auto_langs else "no-tracks")
    summary = safe_video_summary(meta, captured_languages)
    summary["caption_tracks"] = [track for track in planned_tracks if track["language"] in captured_languages]
    summary["caption_probe"] = {
        "requested_languages": requested,
        "available_manual_languages": sorted(manual_langs),
        "available_automatic_languages": sorted(auto_langs),
        "selected_tracks": planned_tracks,
        "checked_at": checked_at,
        "reason": reason,
    }
    atomic_write_json(out_dir / "video.json", summary)
    return {
        "video_id": video_id,
        "url": canonical_url,
        "status": status,
        "error": "Could not capture advertised subtitle track(s): " + ", ".join(planned_captions) if status == "failed" else None,
        "title": title,
        "out_dir": str(out_dir),
        "files": written,
    }
