"""Browser automation through ego-browser, browser-use or kimi-webbridge.

BrowserSession provides open, goto, eval_text, eval_json, cookies and close.
An explicit backend or BLACKBOARD_BROWSER_BACKEND pins/reorders the chain;
otherwise the first healthy backend is selected. Probes are cached for 60s.
Transport errors are scrubbed before reaching logs.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .redaction import redact_urls_for_error
from .runtime import CliResult, CommandRunner

# Test/back-compat seam: the URL-redaction implementation lives in
# ``redaction``; backends call it through this private name.
_redact_urls_for_error = redact_urls_for_error


# ---------------------------------------------------------------------------
# Backend chain configuration
# ---------------------------------------------------------------------------

from .cli_requests import UsageError

KIMI_DAEMON_URL = "http://127.0.0.1:10086/command"

DEFAULT_BACKEND_CHAIN: tuple[str, ...] = ("ego", "browser-use", "kimi")

#: Environment override: a single backend name pins it, a comma-separated
#: list reorders the default chain ("auto" or empty means "use the default").
BACKEND_ENV_VAR = "BLACKBOARD_BROWSER_BACKEND"


_BACKEND_ALIASES: dict[str, str] = {
    "ego": "ego",
    "ego-browser": "ego",
    "ego-lite": "ego",
    "ego_lite": "ego",
    "browser-use": "browser-use",
    "browseruse": "browser-use",
    "bu": "browser-use",
    "kimi": "kimi",
    "kimi-webbridge": "kimi",
    "webbridge": "kimi",
}

_UNSET = object()


def normalize_backend_name(raw: str) -> str:
    """Map a user-supplied backend name to its canonical chain name.

    Raises ``ValueError`` for unknown names so typos in the constructor or in
    ``BLACKBOARD_BROWSER_BACKEND`` fail loudly instead of silently.
    """
    compact = str(raw or "").strip().casefold()
    canonical = _BACKEND_ALIASES.get(compact)
    if canonical is None:
        valid = ", ".join(_BACKEND_ALIASES)
        raise UsageError(f"unknown browser backend {raw!r}; valid names: {valid}")
    return canonical


def effective_backend_chain(requested: Any = None, *, env: Any = _UNSET) -> tuple[str, ...]:
    """Return the backend priority order for this process.

    Precedence: an explicit ``requested`` name, then ``BLACKBOARD_BROWSER_BACKEND``
    (single name = pin, comma list = reordered chain), then
    :data:`DEFAULT_BACKEND_CHAIN`. ``"auto"``/empty at a level defers to the
    next level. A one-element chain means "pinned, no health probing".
    """
    fallback = DEFAULT_BACKEND_CHAIN
    raw: str | None = None
    requested_text = "" if requested is None else str(requested).strip()
    if requested_text and requested_text.casefold() != "auto":
        raw = requested_text
    else:
        env_text = os.environ.get(BACKEND_ENV_VAR, "") if env is _UNSET else str(env or "")
        env_text = env_text.strip()
        if env_text and env_text.casefold() != "auto":
            raw = env_text
    if raw is None:
        return fallback
    names: list[str] = []
    for part in raw.split(","):
        if not part.strip():
            continue
        name = normalize_backend_name(part)
        if name not in names:
            names.append(name)
    return tuple(names) or fallback


class BrowserUnavailable(RuntimeError):
    """No usable browser backend: resolution found none healthy, or the
    pinned backend's tool is missing. The CLI maps this to exit 4
    (dependency action needed) rather than a generic failure."""

    code = "browser.unavailable"


class SessionRequired(RuntimeError):
    """The browser session is not signed in to the site.

    Sign in externally in the same browser cookie profile, then re-run. ``retryable``
    marks it as a needs-action condition rather than a hard failure.
    """

    def __init__(self, message: str, *, code: str = "blackboard.browser_session_required", retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def resolve_session_backend(chain: tuple[str, ...]) -> str:
    """Pick the first backend in ``chain`` whose health probe passes.

    Probe failures fall through with a stderr note. If nothing is healthy,
    :class:`BrowserUnavailable` carries the per-backend reasons so the
    caller learns what to fix instead of failing later at first use.
    """
    attempts: list[str] = []
    for name in chain:
        health = probe_backend_health(name)
        if health.get("ok"):
            return name
        detail = health.get("detail", "probe failed")
        attempts.append(f"{name}: {detail}")
        _note(f"browser backend '{name}' unavailable: {detail}; trying next backend")
    raise BrowserUnavailable(
        "no healthy browser backend in chain "
        + ", ".join(chain)
        + " — " + " | ".join(attempts)
    )


def _note(message: str) -> None:
    """Progress note for resolution fallbacks; stderr, like the CLI's progress."""
    try:
        print(f"[browser-backend] {message}", file=sys.stderr)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Subprocess plumbing for the CLI backends (ego / browser-use).
# The single subprocess boundary is runtime.CommandRunner; the helpers below
# are thin seams so probes and impls (and their tests) share one call site.
# ---------------------------------------------------------------------------

# Back-compat/test seam: the result shape is owned by runtime.
_CliResult = CliResult

_COMMAND_RUNNER = CommandRunner()


def _run_cli(cmd: list[str], *, timeout: float, input_text: str | None = None) -> _CliResult:
    """Run one CLI command without raising; launch/timeout failures become results."""
    return _COMMAND_RUNNER.run_cli(cmd, timeout=timeout, input_text=input_text)


def _excerpt(text: str | None, limit: int = 400) -> str:
    cleaned = " ".join(str(text or "").split())
    return cleaned[:limit]


def _parse_cli_payload(text: str) -> Any:
    """Best-effort JSON extraction from CLI stdout (whole text, then last JSON line)."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    for line in reversed(stripped.splitlines()):
        candidate = line.strip()
        if not candidate.startswith(("{", "[")):
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


_TOOL_SPECS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ego": ("ego-browser", (".local/bin/ego-browser",)),
    "browser-use": ("browser-use", (".browser-use-env/bin/browser-use",)),
}
_TOOL_BIN_CACHE: dict[str, str | None] = {}


def _resolve_tool(name: str) -> str | None:
    """Locate a CLI binary on PATH, falling back to its known install path."""
    if name in _TOOL_BIN_CACHE:
        return _TOOL_BIN_CACHE[name]
    binary, fallbacks = _TOOL_SPECS[name]
    found = shutil.which(binary)
    if not found:
        for relative in fallbacks:
            candidate = Path.home() / relative
            if candidate.is_file():
                found = str(candidate)
                break
    _TOOL_BIN_CACHE[name] = found
    return found


def _extract_marker(stdout: str, marker: str) -> tuple[bool, Any]:
    """Find the last ``marker``-prefixed output line and parse its JSON tail."""
    for line in reversed((stdout or "").splitlines()):
        stripped = line.strip()
        if stripped.startswith(marker):
            raw = stripped[len(marker):].strip()
            try:
                return True, json.loads(raw)
            except json.JSONDecodeError:
                return True, None
    return False, None


def _combined_output(result: _CliResult) -> str:
    """ego-browser's nodejs mode routes script ``console.log`` to stderr, so
    marker/result scanning always covers both streams."""
    return (result.stdout or "") + "\n" + (result.stderr or "")


# ---------------------------------------------------------------------------
# Health probes
# ---------------------------------------------------------------------------

_PROBE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_PROBE_TTL_SECONDS = 60.0

_EGO_RESULT_MARKER = "__BB_RESULT__ "
_EGO_PROBE_MARKER = "__BB_PROBE__"
_EGO_PROBE_SCRIPT = (
    'const task = await taskSpace("blackboard-probe");\n'
    f'console.log("{_EGO_PROBE_MARKER} ego-ok");\n'
    "try { await task.finish({ keep: [] }); } catch (e) {}\n"
)


def probe_kimi_health() -> dict[str, Any]:
    """Check that the kimi-webbridge daemon and Chrome extension are connected."""
    try:
        body = json.dumps({"action": "list_tabs"}).encode("utf-8")
        request = urllib.request.Request(
            KIMI_DAEMON_URL, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        ok = isinstance(payload, dict) and payload.get("ok", False)
        return {
            "ok": ok,
            "daemon": True,
            "extension": ok,
        }
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return {"ok": False, "daemon": False, "extension": False}


def probe_ego_health() -> dict[str, Any]:
    """Roundtrip a minimal ego-browser script (create/reuse a probe task space).

    Healthy = clean exit plus the probe marker (``console.log`` lands on
    stderr in ego-browser's nodejs mode, so both streams are scanned).
    """
    tool = _resolve_tool("ego")
    if not tool:
        return {"ok": False, "detail": "ego-browser CLI not found"}
    result = _run_cli([tool, "nodejs"], timeout=20, input_text=_EGO_PROBE_SCRIPT)
    combined = _combined_output(result)
    ok = result.returncode == 0 and _EGO_PROBE_MARKER in combined
    detail = "roundtrip ok" if ok else _excerpt(combined or f"exit {result.returncode}")
    return {"ok": ok, "bin": tool, "detail": detail}


def probe_browser_use_health() -> dict[str, Any]:
    """Probe ``browser-use --json state`` against a throwaway session.

    Local mode attaches to the user's running Chrome via CDP; the first ever
    attach may surface a one-time Chrome remote-debugging approval prompt,
    which is why the timeout is generous (25s).
    """
    tool = _resolve_tool("browser-use")
    if not tool:
        return {"ok": False, "detail": "browser-use CLI not found"}
    result = _run_cli(
        [tool, "--session", "blackboard-probe", "--json", "state"],
        timeout=25,
    )
    if result.returncode != 0:
        return {
            "ok": False,
            "bin": tool,
            "detail": _excerpt(result.stderr or result.stdout or f"exit {result.returncode}"),
        }
    payload = _parse_cli_payload(result.stdout)
    if payload is None:
        return {"ok": False, "bin": tool, "detail": f"non-JSON output: {_excerpt(result.stdout)}"}
    return {"ok": True, "bin": tool, "detail": "state roundtrip ok"}


_PROBE_FUNCTIONS: dict[str, Any] = {
    "ego": probe_ego_health,
    "browser-use": probe_browser_use_health,
    "kimi": probe_kimi_health,
}


def probe_backend_health(name: str) -> dict[str, Any]:
    """Probe one backend by canonical name, with a module-level TTL cache.

    Results (healthy or not) are cached for ~60s so repeated ``auto``
    construction and backend selection stay cheap.
    """
    canonical = normalize_backend_name(name)
    now = time.monotonic()
    cached = _PROBE_CACHE.get(canonical)
    if cached and (now - cached[0]) < _PROBE_TTL_SECONDS:
        return dict(cached[1])
    probe = _PROBE_FUNCTIONS[canonical]()
    _PROBE_CACHE[canonical] = (now, probe)
    return dict(probe)


def resolve_backend(requested: str | None = "auto") -> str:
    """Resolve the backend name through the chain (selection rule).

    A pinned name (single-element chain) is returned without probing;
    otherwise the first healthy backend in the (capability-filtered, when
    defaulted) chain wins.
    """
    chain = effective_backend_chain(requested)
    if len(chain) == 1:
        return chain[0]
    return resolve_session_backend(chain)


# ---------------------------------------------------------------------------
# ego-browser backend (primary)
# ---------------------------------------------------------------------------

class _EgoImpl:
    """Drive ego-browser: one ``ego-browser nodejs`` subprocess per operation.

    Each script re-acquires ``taskSpace(<session>)`` and the tracked page
    label; results are printed as a ``__BB_RESULT__ `` JSON marker line.
    ego-browser's nodejs mode routes script ``console.log`` to stderr, so
    markers are scanned across both output streams and unrelated noise cannot
    corrupt them.
    """

    name = "ego"
    default_label = "p1"

    def __init__(self, *, session: str) -> None:
        self.session = session
        self.label = self.default_label

    def _script(self, body: str) -> str:
        return (
            f"const task = await taskSpace({json.dumps(self.session)});\n"
            f"const page = task.page({json.dumps(self.label)});\n"
            f"{body}"
        )

    def _run(self, op: str, body: str, *, timeout: float = 90) -> dict[str, Any]:
        tool = _resolve_tool("ego")
        if not tool:
            raise RuntimeError("ego-browser CLI not found on PATH or in ~/.local/bin")
        result = _run_cli([tool, "nodejs"], timeout=timeout, input_text=self._script(body))
        combined = _combined_output(result)
        if result.returncode != 0:
            raise RuntimeError(
                f"ego-browser {op} failed: "
                f"{_redact_urls_for_error(_excerpt(combined))}"
            )
        found, payload = _extract_marker(combined, _EGO_RESULT_MARKER)
        if not found or not isinstance(payload, dict):
            raise RuntimeError(
                f"ego-browser {op} produced no result marker: "
                f"{_redact_urls_for_error(_excerpt(combined))}"
            )
        if not payload.get("ok", True):
            raise RuntimeError(
                f"ego-browser {op} failed: {_redact_urls_for_error(_excerpt(payload.get('error')))}"
            )
        return payload

    def open(self, url: str) -> str:
        body = (
            f"await page.goto({json.dumps(url)}, "
            '{timeout: 30000, waitUntil: "domcontentloaded"});\n'
            'console.log("__BB_RESULT__ " + JSON.stringify({ok: true, url: String(await page.url())}));\n'
        )
        self._run("open", body, timeout=60)
        return self.label

    def goto(self, url: str) -> None:
        self.open(url)

    def eval_text(self, script: str) -> str:
        body = (
            f"const value = await page.evaluate({json.dumps(script)});\n"
            'console.log("__BB_RESULT__ " + JSON.stringify({ok: true, value: value === undefined ? null : value}));\n'
        )
        payload = self._run("eval", body, timeout=90)
        return _jsonish_to_text(payload.get("value"))

    def close(self) -> None:
        body = (
            "try { await task.finish({keep: []}); } catch (e) {}\n"
            'console.log("__BB_RESULT__ " + JSON.stringify({ok: true}));\n'
        )
        self._run("close", body, timeout=30)

    def cookies(self, urls: list[str]) -> list[dict[str, Any]]:
        """Read cookies for ``urls`` via the page's CDP passthrough."""
        body = (
            f"const res = await page.cdp('Network.getCookies', {{urls: {json.dumps(list(urls))}}});\n"
            f'console.log("{_EGO_RESULT_MARKER}" + JSON.stringify({{ok: true, cookies: (res && res.cookies) || []}}));\n'
        )
        payload = self._run("cookie fetch", body, timeout=30)
        cookies = payload.get("cookies")
        if not isinstance(cookies, list):
            raise RuntimeError(
                f"ego-browser cookie fetch returned no cookies: "
                f"{_redact_urls_for_error(_excerpt(json.dumps(payload)))}"
            )
        valid = [c for c in cookies if isinstance(c, dict) and c.get("name") and c.get("value")]
        return valid


def _jsonish_to_text(value: Any) -> str:
    """Stringify an eval result the way the kimi evaluate action did."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value)


# ---------------------------------------------------------------------------
# browser-use backend
# ---------------------------------------------------------------------------

class _BrowserUseImpl:
    """Drive the ``browser-use`` CLI with ``--session`` / ``--json``.

    Output shapes vary between versions, so every payload is parsed
    defensively: JSON (whole stdout or last JSON line) when available, plain
    text tolerated for open/navigation, and genuinely ambiguous eval output
    raises ``RuntimeError`` with a redacted excerpt. ``close`` is a no-op:
    the CLI's ``close`` subcommand stops the shared daemon (and may close the
    user's Chrome), while ``--session`` persistence is exactly what we want.
    """

    name = "browser-use"
    fallback_tab = "browser-use"
    _TAB_ID_KEYS = ("tab_id", "tabId", "tab_index", "tabIndex", "targetId")

    def __init__(self, *, session: str) -> None:
        self.session = session
        self._tab = self.fallback_tab

    def _cli(self, args: list[str], *, timeout: float = 90) -> _CliResult:
        tool = _resolve_tool("browser-use")
        if not tool:
            raise RuntimeError("browser-use CLI not found on PATH or in ~/.browser-use-env")
        return _run_cli(
            [tool, "--session", self.session, "--json", *args],
            timeout=timeout,
        )

    def _fail(self, op: str, result: _CliResult) -> RuntimeError:
        excerpt = _excerpt(result.stderr or result.stdout or f"exit {result.returncode}")
        return RuntimeError(
            f"browser-use {op} failed: {_redact_urls_for_error(excerpt)}"
        )

    def _find_tab_id(self, payload: Any) -> str | None:
        scopes: list[Any] = [payload]
        if isinstance(payload, dict):
            scopes.extend(payload.get(key) for key in ("data", "result"))
        for scope in scopes:
            if not isinstance(scope, dict):
                continue
            for key in self._TAB_ID_KEYS:
                value = scope.get(key)
                if value is not None and str(value):
                    return str(value)
        return None

    def open(self, url: str) -> str:
        result = self._cli(["open", url], timeout=60)
        if result.returncode != 0:
            raise self._fail("open", result)
        self._tab = self._find_tab_id(_parse_cli_payload(result.stdout)) or self.fallback_tab
        return self._tab

    def goto(self, url: str) -> None:
        self.open(url)

    def eval_text(self, script: str) -> str:
        result = self._cli(["eval", script], timeout=90)
        if result.returncode != 0:
            raise self._fail("eval", result)
        payload = _parse_cli_payload(result.stdout)
        if payload is None:
            text = result.stdout.strip()
            if text:
                return text
            raise RuntimeError(
                f"browser-use eval returned unparseable output: "
                f"{_redact_urls_for_error(_excerpt(result.stdout or result.stderr))}"
            )
        if isinstance(payload, dict):
            # Observed shape: {"id", "success", "data": {"result": <value>}};
            # older/newer builds may hoist the value to the top level instead.
            inner = payload.get("data") if isinstance(payload.get("data"), dict) else None
            for scope in ([inner] if inner else []) + [payload]:
                for key in ("result", "value", "output", "data"):
                    if key in scope:
                        return _jsonish_to_text(scope[key])
            raise RuntimeError(
                f"browser-use eval payload had an unrecognized shape: "
                f"{_redact_urls_for_error(_excerpt(result.stdout))}"
            )
        return _jsonish_to_text(payload)

    def close(self) -> None:
        return None

    def cookies(self, urls: list[str]) -> list[dict[str, Any]]:
        """Fetch cookies via ``browser-use cookies get`` and normalize to CDP shape.

        The CLI has no URL filter, so ``urls`` is accepted for interface parity
        but the whole session cookie jar is returned; the per-request origin
        filter in :class:`cookie_store.CookieStore` narrows it per call.
        """
        result = self._cli(["cookies", "get"], timeout=30)
        if result.returncode != 0:
            raise self._fail("cookies get", result)
        payload = _parse_cli_payload(result.stdout)
        cookies: list[Any] | None = None
        if isinstance(payload, list):
            cookies = payload
        elif isinstance(payload, dict):
            # Observed shape: {"id", "success", "data": {"cookies": [...]}}.
            inner = payload.get("data") if isinstance(payload.get("data"), dict) else None
            for scope in ([inner] if inner else []) + [payload]:
                for key in ("cookies", "result", "data"):
                    value = scope.get(key)
                    if isinstance(value, list):
                        cookies = value
                        break
                if cookies is not None:
                    break
        if cookies is None:
            raise RuntimeError(
                "browser-use cookies get returned an unrecognized payload: "
                f"{_redact_urls_for_error(_excerpt(result.stdout))}"
            )
        normalized = [
            normalized
            for normalized in (
                _normalize_cookie(raw) for raw in cookies if isinstance(raw, dict)
            )
            if normalized is not None
        ]
        return normalized


def _normalize_cookie(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize any CLI cookie shape into the raw CDP-style dict the filter uses.

    The per-request origin filter depends on ``name``, ``value``, ``domain``,
    ``path``, ``secure``, and ``hostOnly``; browser-use (Playwright-style)
    output spells some of these differently or omits them.
    """
    name = str(raw.get("name") or "")
    value = str(raw.get("value") or "")
    if not name or not value:
        return None
    cookie = dict(raw)
    domain = raw.get("domain")
    if not domain and raw.get("url"):
        domain = urlparse(str(raw.get("url"))).hostname
    cookie["name"] = name
    cookie["value"] = value
    cookie["domain"] = str(domain or "")
    cookie["path"] = str(raw.get("path") or "/")
    cookie["secure"] = bool(raw.get("secure"))
    if "hostOnly" not in cookie and "host_only" in raw:
        cookie["hostOnly"] = raw.get("host_only")
    return cookie


# ---------------------------------------------------------------------------
# kimi-webbridge backend
# ---------------------------------------------------------------------------

class _KimiImpl:
    """Chrome tab control via the kimi-webbridge CDP HTTP daemon."""

    name = "kimi"

    def __init__(self, *, session: str) -> None:
        self.session = session

    def _command(self, action: str, **kwargs: Any) -> Any:
        """POST a kimi-webbridge action and return the parsed response data.

        Raises ``RuntimeError`` if the daemon is unreachable or the action
        failed, with the URL redacted from any error message. The private
        ``_timeout`` keyword is the urlopen timeout only — it is popped
        before serialization so it never reaches the request body.
        """
        timeout = kwargs.pop("_timeout", 90)
        payload: dict[str, Any] = {"action": action, "session": self.session}
        payload.update(kwargs)
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            KIMI_DAEMON_URL, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(
                f"kimi-webbridge daemon unreachable at {KIMI_DAEMON_URL}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"kimi-webbridge returned non-JSON: {exc}") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            err = result.get("error") if isinstance(result, dict) else None
            message = err.get("message", str(err)) if isinstance(err, dict) else str(err or result)
            raise RuntimeError(_redact_urls_for_error(message))
        return result.get("data")

    def open(self, url: str) -> str:
        data = self._command("navigate", url=url, _timeout=30)
        return str(data.get("tabId", "")) if isinstance(data, dict) else ""

    def goto(self, url: str) -> None:
        self._command("navigate", url=url, _timeout=30)

    def eval_text(self, script: str) -> str:
        """Evaluate a JS expression in the tab and return its string value."""
        data = self._command("evaluate", args={"code": script}, _timeout=90)
        if isinstance(data, dict):
            return str(data.get("value", ""))
        return str(data)

    def cookies(self, urls: list[str]) -> list[dict[str, Any]]:
        """POST ``Network.getCookies`` to kimi-webbridge and return raw cookies."""
        data = self._command(
            "cdp",
            args={"method": "Network.getCookies", "params": {"urls": list(urls)}},
            _timeout=15,
        )
        cookies = data.get("cookies") if isinstance(data, dict) else None
        if not isinstance(cookies, list):
            raise RuntimeError(f"kimi-webbridge CDP getCookies returned no cookies: {data!r}")
        valid = [c for c in cookies if isinstance(c, dict) and c.get("name") and c.get("value")]
        return valid

    def close(self) -> None:
        self._command("close_session", _timeout=10)


_IMPL_FACTORIES = {
    "ego": lambda *, session: _EgoImpl(session=session),
    "browser-use": lambda *, session: _BrowserUseImpl(session=session),
    "kimi": lambda *, session: _KimiImpl(session=session),
}


def _make_impl(name: str, *, session: str) -> Any:
    factory = _IMPL_FACTORIES.get(name)
    if factory is None:
        raise ValueError(f"unknown browser backend: {name!r}")
    return factory(session=session)


# ---------------------------------------------------------------------------
# BrowserSession — backend-agnostic facade
# ---------------------------------------------------------------------------

class BrowserSession:
    """Chrome tab control through the resolved backend-chain implementation.

    Provides the narrowed interface callers use (``open``, ``goto``,
    ``eval_text``, ``eval_json``, ``cookies``, ``close``) so integration callers
    (Panopto, Leganto) need no changes beyond dropping the old tab-id
    plumbing. ``backend="auto"`` (default) resolves through the chain
    ego -> browser-use -> kimi unless pinned by the
    constructor argument or ``BLACKBOARD_BROWSER_BACKEND``; the resolved name
    is exposed as ``self.backend``.

    ``borrowed=True`` marks a non-owning session: one wrapped around a tab
    the caller already had, so ``close()`` will not close it.
    """

    def __init__(
        self,
        *,
        backend: str | None = "auto",
        session: str = "blackboard-sync",
        borrowed: bool = False,
    ) -> None:
        self._owns_session = not borrowed
        self._closed = False
        self.session = session
        chain = effective_backend_chain(backend)
        if len(chain) == 1:
            self.backend = chain[0]
        else:
            self.backend = resolve_session_backend(chain)
        self._impl = _make_impl(self.backend, session=session)

    # -- public interface ---------------------------------------------------

    def open(self, url: str) -> str:
        """Navigate to ``url`` and return the backend's tab/page id."""
        return self._impl.open(url)

    def goto(self, url: str) -> None:
        """Navigate the current tab to a new URL."""
        self._impl.goto(url)

    def eval_text(self, script: str) -> str:
        """Evaluate a JS expression in the tab and return its string value."""
        return self._impl.eval_text(script)

    def eval_json(self, script: str) -> Any:
        """Evaluate a JS expression that returns JSON and parse it."""
        stdout = self.eval_text(script)
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Could not parse browser eval JSON: {_redact_urls_for_error(stdout[:500])}"
            ) from exc

    def cookies(self, urls: list[str]) -> list[dict[str, Any]]:
        """Read cookies for ``urls`` via CDP ``Network.getCookies`` on this backend.

        The resolved backend is fixed at construction, so cookies always come
        from the same browser the session navigates.
        """
        return self._impl.cookies(list(urls))

    def close(self) -> None:
        if self._closed or not self._owns_session:
            return
        impl = getattr(self, "_impl", None)
        if impl is not None:
            try:
                impl.close()
            except RuntimeError:
                pass
        self._closed = True
