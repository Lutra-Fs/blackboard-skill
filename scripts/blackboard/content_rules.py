from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

from .site_config import SiteConfig
from .url_policy import is_trusted_attachment_source_url


def normalize_inline(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\u00a0", " ")).strip()


def normalize_block(text: str | None) -> str:
    lines: list[str] = []
    previous_blank = False
    for raw_line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = normalize_inline(raw_line)
        if not line:
            if not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        lines.append(line)
        previous_blank = False
    return "\n".join(lines).strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_filename(name: str | None, href: str) -> str:
    candidate = normalize_inline(name) or Path(urlparse(href).path).name or "blackboard-file"
    candidate = re.sub(r"^Preview File\s+", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"[\\/:*?\"<>|]+", " - ", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip().strip(".")
    return candidate or "blackboard-file"


def is_unsafe_html_attachment_name(name: str | None) -> bool:
    return bool(re.search(r"\bUNSAFE_HTML_\d+\.html$", normalize_inline(name), re.IGNORECASE))


class _EmbeddedHtmlDownloadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        href = values.get("href", "").strip()
        label = values.get("aria-label", "").strip()
        if href:
            self.links.append((href, label))


def embedded_html_linked_download(path: Path, *, site_config: SiteConfig) -> tuple[str, str] | None:
    """Return a canonical linked file from an embedded-HTML wrapper.

    The linked document must come from the site's trusted attachment hosts —
    the same policy that authorizes the follow-up download.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not any(marker in text for marker in ("bb-embedded-html", "lt-ultra-iframe", "lt-ultra-aoc-embed")):
        return None

    parser = _EmbeddedHtmlDownloadParser()
    parser.feed(text)
    allowed_suffixes = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"}
    for href, label in parser.links:
        parsed = urlparse(href)
        suffix = Path(parsed.path).suffix.lower()
        if not is_trusted_attachment_source_url(href, site_config=site_config) or suffix not in allowed_suffixes:
            continue
        if label and "download" not in label.lower():
            continue
        filename = safe_filename(unquote(Path(parsed.path).name), href)
        return href, filename
    return None

def bucket_for_title(title: str) -> str:
    compact = normalize_inline(title)
    lower = compact.lower()

    assessment = re.search(r"assessment\s*(\d+)", lower)
    if assessment:
        return f"50_Assessments/Assessment {int(assessment.group(1))}"

    if lower in {"home", "student services"} or "subject outline" in lower or "learning guide" in lower:
        return "00_Admin"

    week = re.search(r"\bweek\s*(\d+)\b", lower)
    tutorial = re.search(r"\btutoria?l\s*(\d+)\b", lower)
    number_match = tutorial or week
    if number_match:
        number = int(number_match.group(1))
        return f"{10 + number:02d}_Week_{number:02d}"

    return "00_Admin"


def bucket_for_hierarchy(title: str, parent_title: str | None, breadcrumbs: list[str]) -> str:
    """Choose a study bucket from the closest meaningful hierarchy label."""
    labels = [normalize_inline(value) for value in [*breadcrumbs, parent_title or "", title] if normalize_inline(value)]
    lower_labels = [value.lower() for value in labels]

    # Subject-level guidance stays administrative even when its page title has
    # incidental numbers or words such as resources.
    if any(value in {"subject information", "subject info"} for value in lower_labels):
        return "00_Admin"

    for value in reversed(labels):
        assessment = re.search(r"assessment\s*(\d+)", value, re.IGNORECASE)
        if assessment:
            return f"50_Assessments/Assessment {int(assessment.group(1))}"

    for value in reversed(labels):
        week = re.search(r"\bweek\s*(\d+)\b", value, re.IGNORECASE)
        if week:
            number = int(week.group(1))
            return f"{10 + number:02d}_Week_{number:02d}"

    # TEAC5020 currently labels weekly units as Module 01, Module 02, etc.
    # Use a numerical module only when no explicit Week/Assessment ancestor was
    # available; generic modules continue to fall back to the page title rule.
    for value in reversed(labels):
        module = re.search(r"\bmodule\s*0*(\d+)\b", value, re.IGNORECASE)
        if module:
            number = int(module.group(1))
            return f"{10 + number:02d}_Week_{number:02d}"

    return bucket_for_title(title)


def assessment_number_for_title(title: str) -> int | None:
    match = re.search(r"assessment\s*(\d+)", normalize_inline(title).lower())
    return int(match.group(1)) if match else None


def node_type_for(title: str, depth: int) -> str:
    lower = title.lower()
    if "assessment" in lower:
        return "assessment"
    if depth == 0 or "module" in lower:
        return "module"
    return "page"
