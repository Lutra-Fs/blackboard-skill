"""Site context selected by the caller; no institutional defaults."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse


def _normalise_origin(value: str, *, label: str = "site origin") -> str:
    value = str(value or "").strip()
    parsed = urlparse(value)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError(f"{label} must be an HTTP(S) origin without credentials or a path")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or any(c.isspace() for c in host):
        raise ValueError(f"{label} must contain a valid host")
    # Keep an explicitly supplied non-default port because it is part of the
    # origin; reject malformed ports without ever making a network request.
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid port") from exc
    if port == 0:
        raise ValueError(f"{label} has an invalid port")
    scheme = parsed.scheme.lower()
    netloc = f"[{host}]" if ":" in host else host
    if port not in (None, 443 if scheme == "https" else 80):
        netloc += f":{port}"
    return urlunparse((scheme, netloc, "", "", "", ""))


def _host(value: str, *, label: str) -> str:
    text = str(value or "").strip().lower().rstrip(".")
    if not text or "://" in text or "/" in text or "@" in text:
        raise ValueError(f"{label} must be a hostname, not a URL")
    return text


@dataclass(frozen=True)
class SiteConfig:
    """Validated, serialisable context for one Blackboard deployment."""

    origin: str
    auth_mode: str = "browser-session"
    trusted_hosts: tuple[str, ...] = ()
    trusted_host_suffixes: tuple[str, ...] = ()
    leganto_origin: str | None = None
    panopto_origin: str | None = None
    display_timezone: str = "UTC"
    ultra_supported: bool = True

    def __post_init__(self) -> None:
        origin = _normalise_origin(self.origin)
        if self.auth_mode not in {"browser-session", "wsu"}:
            raise ValueError("auth_mode must be 'browser-session'")
        # Older archives used this label for the same cookie transport.
        if self.auth_mode == "wsu":
            object.__setattr__(self, "auth_mode", "browser-session")
        if not isinstance(self.ultra_supported, bool):
            raise ValueError("ultra_supported must be boolean")
        if not isinstance(self.trusted_hosts, (tuple, list)) or not isinstance(self.trusted_host_suffixes, (tuple, list)):
            raise ValueError("trusted_hosts and trusted_host_suffixes must be arrays")
        trusted = tuple(dict.fromkeys(_host(item, label="trusted host") for item in self.trusted_hosts))
        suffixes = tuple(dict.fromkeys(_host(item, label="trusted host suffix") for item in self.trusted_host_suffixes))
        if any("." not in suffix for suffix in suffixes):
            raise ValueError("trusted host suffixes must be explicit multi-label suffixes (for example, example.edu)")
        # The Blackboard origin is always trusted for its own attachments and
        # redirects, even when a compact custom config omits it explicitly.
        origin_host = (urlparse(origin).hostname or "").lower()
        if origin_host not in trusted:
            trusted = (origin_host, *trusted)
        leganto = _normalise_origin(self.leganto_origin, label="leganto_origin") if self.leganto_origin else None
        panopto = _normalise_origin(self.panopto_origin, label="panopto_origin") if self.panopto_origin else None
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(self.display_timezone)
        except Exception as exc:
            raise ValueError(f"Unknown display_timezone: {self.display_timezone!r}") from exc
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "trusted_hosts", trusted)
        object.__setattr__(self, "trusted_host_suffixes", suffixes)
        object.__setattr__(self, "leganto_origin", leganto)
        object.__setattr__(self, "panopto_origin", panopto)

    @property
    def blackboard_host(self) -> str:
        return (urlparse(self.origin).hostname or "").lower()

    @property
    def leganto_host(self) -> str | None:
        return (urlparse(self.leganto_origin).hostname or "").lower() if self.leganto_origin else None

    @property
    def panopto_host(self) -> str | None:
        return (urlparse(self.panopto_origin).hostname or "").lower() if self.panopto_origin else None

    def url(self, path: str = "") -> str:
        return self.origin.rstrip("/") + "/" + str(path).lstrip("/") if path else self.origin

    def is_blackboard_host(self, host: str | None) -> bool:
        return bool(host and host.lower().rstrip(".") == self.blackboard_host)

    def matches_origin(self, url: str, origin: str | None = None) -> bool:
        """Return whether *url* has the exact configured HTTP(S) origin.

        Host-only checks are suitable for cookie domain and attachment
        allowlists, but a Blackboard API/course/login target is origin scoped.
        Preserve non-default ports so ``https://host`` and
        ``https://host:8443`` cannot be confused.
        """
        expected = urlparse(origin or self.origin)
        try:
            actual = urlparse(str(url or ""))
            actual_port = actual.port
        except ValueError:
            return False
        if actual.username or actual.password or actual.scheme.lower() != expected.scheme.lower():
            return False
        if (actual.hostname or "").lower().rstrip(".") != (expected.hostname or "").lower().rstrip("."):
            return False
        expected_port = expected.port
        default_port = 443 if expected.scheme == "https" else 80
        return (actual_port or default_port) == (expected_port or default_port)

    def is_trusted_host(self, host: str | None) -> bool:
        value = (host or "").lower().rstrip(".")
        return bool(value and (value in self.trusted_hosts or any(value == s or value.endswith("." + s) for s in self.trusted_host_suffixes)))

    def cookie_origins(self) -> tuple[str, ...]:
        values: list[str | None] = [self.origin]
        values.extend([self.leganto_origin, self.panopto_origin])
        return tuple(dict.fromkeys(value for value in values if value))

    def to_dict(self) -> dict[str, object]:
        return {
            "origin": self.origin,
            "auth_mode": self.auth_mode,
            "trusted_hosts": list(self.trusted_hosts),
            "trusted_host_suffixes": list(self.trusted_host_suffixes),
            "leganto_origin": self.leganto_origin,
            "panopto_origin": self.panopto_origin,
            "display_timezone": self.display_timezone,
            "ultra_supported": self.ultra_supported,
        }


def from_origin(origin: str, *, auth_mode: str = "browser-session") -> SiteConfig:
    """Build a minimal custom config when an archive stores only its origin."""
    return SiteConfig(origin=_normalise_origin(origin), auth_mode=auth_mode)


def _from_mapping(data: dict[str, object]) -> SiteConfig:
    allowed = {"origin", "auth_mode", "trusted_hosts", "trusted_host_suffixes", "leganto_origin", "panopto_origin", "display_timezone", "ultra_supported"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError("Site config contains unsupported fields: " + ", ".join(sorted(unknown)))
    origin = str(data.get("origin") or "").strip()
    if not origin:
        raise ValueError("Site config requires origin")
    auth_mode = str(data.get("auth_mode") or "browser-session")
    normalised = _normalise_origin(origin)
    trusted_hosts = data.get("trusted_hosts") or ()
    trusted_host_suffixes = data.get("trusted_host_suffixes") or ()
    if not isinstance(trusted_hosts, (list, tuple)) or not isinstance(trusted_host_suffixes, (list, tuple)):
        raise ValueError("trusted_hosts and trusted_host_suffixes must be arrays")
    ultra_supported = data.get("ultra_supported", True)
    if not isinstance(ultra_supported, bool):
        raise ValueError("ultra_supported must be boolean")
    leganto_origin = data.get("leganto_origin")
    panopto_origin = data.get("panopto_origin")
    return SiteConfig(
        origin=normalised,
        auth_mode=auth_mode,
        trusted_hosts=tuple(trusted_hosts),
        trusted_host_suffixes=tuple(trusted_host_suffixes),
        leganto_origin=str(leganto_origin) if leganto_origin else None,
        panopto_origin=str(panopto_origin) if panopto_origin else None,
        display_timezone=str(data.get("display_timezone") or "UTC"),
        ultra_supported=ultra_supported,
    )


def load_site_config(path: str | Path | None = None, *, fallback_origin: str | None = None) -> SiteConfig:
    """Load optional JSON settings or an explicitly supplied origin."""
    if path is None:
        if not fallback_origin:
            raise ValueError("Specify --site URL or a full course URL")
        return from_origin(fallback_origin)
    config_path = Path(path).expanduser().resolve()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Cannot read site config: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Site config is not valid JSON: {config_path}") from exc
    if not isinstance(data, dict):
        raise ValueError("Site config must be a JSON object")
    return _from_mapping(data)


def site_config_from_dict(data: dict[str, object]) -> SiteConfig:
    """Rehydrate sanitized config metadata stored in an immutable tree."""
    return _from_mapping(data)
