from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, overload


class ArchiveIntegrityError(RuntimeError):
    """Raised when persisted archive state cannot be trusted safely."""


def as_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_artifact_name(text: str, *, fallback: str = "artifact") -> str:
    value = re.sub(r"[\\/:*?\"<>|]+", " - ", str(text or ""))
    value = re.sub(r"\s+", " ", value).strip().strip(".")
    return value or fallback


def atomic_write_text(path: Path, content: str) -> None:
    """Durably replace one text file without exposing a partial target."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, as_json(data))


def load_json_value(path: Path, *, required: bool = False) -> Any:
    """Load JSON once, failing closed when persisted state exists but is invalid."""
    path = Path(path)
    if not path.exists():
        if required:
            raise ArchiveIntegrityError(f"Required archive state is missing: {path}")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ArchiveIntegrityError(f"Archive state is unreadable; preserved without overwrite: {path}: {exc}") from exc
    if value is None:
        raise ArchiveIntegrityError(f"Archive state must not contain JSON null: {path}")
    return value


@overload
def load_json_object(path: Path, *, required: Literal[True]) -> dict[str, Any]: ...
@overload
def load_json_object(path: Path, *, required: bool = False) -> dict[str, Any] | None: ...


def load_json_object(path: Path, *, required: bool = False) -> dict[str, Any] | None:
    """Load an object-valued JSON file through the shared archive boundary.

    With ``required=True`` the result is always a dict; the missing-file case
    raises instead of returning ``None``.
    """
    value = load_json_value(path, required=required)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ArchiveIntegrityError(f"Archive state must contain a JSON object: {path}")
    return value


@overload
def load_json_list(path: Path, *, required: Literal[True]) -> list[Any]: ...
@overload
def load_json_list(path: Path, *, required: bool = False) -> list[Any] | None: ...


def load_json_list(path: Path, *, required: bool = False) -> list[Any] | None:
    """Load a list-valued JSON file through the shared archive boundary.

    With ``required=True`` the result is always a list; the missing-file case
    raises instead of returning ``None``.
    """
    value = load_json_value(path, required=required)
    if value is None:
        return None
    if not isinstance(value, list):
        raise ArchiveIntegrityError(f"Archive state must contain a JSON array: {path}")
    return value


@overload
def load_course_tree(path: Path, *, required: Literal[True]) -> dict[str, Any]: ...
@overload
def load_course_tree(path: Path, *, required: bool = False) -> dict[str, Any] | None: ...


def load_course_tree(path: Path, *, required: bool = False) -> dict[str, Any] | None:
    tree = load_json_object(path, required=required)
    if tree is None:
        return None
    nodes = tree.get("nodes")
    if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
        raise ArchiveIntegrityError(f"Published course tree has no valid nodes array: {path}")
    return tree


def safe_archive_path(root: Path, relative: str | Path, *, label: str = "archive path") -> Path:
    """Resolve an archive-relative path while rejecting traversal and symlink escapes."""
    root = Path(root).expanduser().resolve()
    raw = Path(relative)
    if raw.is_absolute() or not raw.parts or raw == Path("."):
        raise ArchiveIntegrityError(f"Invalid {label}: {relative!s}")
    current = root
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            raise ArchiveIntegrityError(f"{label} traverses a symlink: {relative!s}")
    candidate = (root / raw).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ArchiveIntegrityError(f"{label} escapes archive root: {relative!s}") from exc
    return candidate


def safe_path_component(value: str, *, label: str) -> str:
    value = str(value or "").strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise ArchiveIntegrityError(f"Invalid {label}: {value!r}")
    return value
