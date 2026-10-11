from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Keep standalone (`python3 tests/test_browser_backend.py`) runs offline too:
# pin the chain to the kimi transport, whose I/O the fakes below intercept.
os.environ.setdefault("BLACKBOARD_BROWSER_BACKEND", "kimi")

from blackboard import browser_backend
from blackboard.browser_backend import BrowserSession, probe_kimi_health


def _ok(data) -> bytes:
    return json.dumps({"ok": True, "data": data}).encode("utf-8")


from site_fixture import EXAMPLE_SITE

class BrowserSessionTransportTests(unittest.TestCase):
    def _session(self) -> BrowserSession:
        return BrowserSession(session="t", backend="kimi")

    def test_open_timeout_is_not_serialized_into_request_body(self) -> None:
        # _command's private _timeout kwarg must only set the urlopen timeout,
        # never leak into the JSON body sent to the daemon.
        with patch("blackboard.browser_backend.urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = _ok({"tabId": "tab-9"})
            tab = self._session().open("https://learn.example.edu/ultra/")
        self.assertEqual(tab, "tab-9")
        request = mock_open.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertNotIn("_timeout", body)
        self.assertEqual(mock_open.call_args.kwargs["timeout"], 30)

    def test_eval_json_parses_json_value(self) -> None:
        with patch("blackboard.browser_backend.urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = _ok(
                {"value": '{"authenticated": true}'}
            )
            value = self._session().eval_json("JSON.stringify({authenticated: true})")
        self.assertEqual(value, {"authenticated": True})

    def test_non_ok_error_raises_redacted_message(self) -> None:
        err = json.dumps({
            "ok": False,
            "error": {"message": "evaluate blew up at https://learn.example.edu/p?token=secret&sig=abc"},
        }).encode("utf-8")
        with patch("blackboard.browser_backend.urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = err
            with self.assertRaises(RuntimeError) as ctx:
                self._session().eval_text("x")
        message = str(ctx.exception)
        self.assertIn("evaluate blew up", message)
        self.assertIn("https://learn.example.edu/p", message)
        self.assertNotIn("secret", message)
        self.assertNotIn("abc", message)


class ProbeHealthTests(unittest.TestCase):
    def test_probe_ok_when_daemon_returns_ok(self) -> None:
        with patch("blackboard.browser_backend.urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = _ok([])
            health = probe_kimi_health()
        self.assertTrue(health["ok"])

    def test_probe_unreachable_returns_false(self) -> None:
        with patch("blackboard.browser_backend.urllib.request.urlopen", side_effect=OSError("refused")):
            health = probe_kimi_health()
        self.assertFalse(health["ok"])
        self.assertFalse(health["daemon"])


class ChainResolutionTests(unittest.TestCase):
    AUTO_CHAIN = ("ego", "browser-use", "kimi")

    def test_effective_backend_chain_resolution(self) -> None:
        cases = [
            ("", self.AUTO_CHAIN),
            ("auto", self.AUTO_CHAIN),
            ("kimi", ("kimi",)),
            ("Kimi-Webbridge", ("kimi",)),
            ("browser-use,kimi", ("browser-use", "kimi")),
            ("kimi,webbridge", ("kimi",)),
        ]
        for env_value, expected in cases:
            with self.subTest(env=env_value):
                with patch.dict(os.environ, {"BLACKBOARD_BROWSER_BACKEND": env_value}):
                    self.assertEqual(browser_backend.effective_backend_chain(), expected)
        # A constructor argument takes precedence over the environment.
        with patch.dict(os.environ, {"BLACKBOARD_BROWSER_BACKEND": "kimi"}):
            self.assertEqual(browser_backend.effective_backend_chain("ego"), ("ego",))
        with self.assertRaises(ValueError):
            browser_backend.effective_backend_chain("mosaic")

    def test_pinned_backend_selection_skips_probing(self) -> None:
        # Both pin levels (constructor argument, single-name env var) select
        # the backend without health probing.
        with patch.object(browser_backend, "probe_backend_health") as probe:
            session = BrowserSession(session="t", backend="ego")
        self.assertEqual(session.backend, "ego")
        probe.assert_not_called()
        with patch.dict(os.environ, {"BLACKBOARD_BROWSER_BACKEND": "kimi"}), \
                patch.object(browser_backend, "probe_backend_health") as probe:
            session = BrowserSession(session="t")
        self.assertEqual(session.backend, "kimi")
        probe.assert_not_called()

    def test_unknown_backend_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            BrowserSession(session="t", backend="netscape")

    def test_auto_resolution_walks_default_chain_order(self) -> None:
        probed: list[str] = []

        def fake_probe(name: str) -> dict:
            probed.append(name)
            return {"ok": name == "kimi"}

        with patch.dict(os.environ, {"BLACKBOARD_BROWSER_BACKEND": "auto"}), \
                patch.object(browser_backend, "probe_backend_health", side_effect=fake_probe):
            session = BrowserSession(session="t")
        self.assertEqual(session.backend, "kimi")
        self.assertEqual(probed, ["ego", "browser-use", "kimi"])

    def test_auto_resolution_reports_unavailable(self) -> None:
        with patch.dict(os.environ, {"BLACKBOARD_BROWSER_BACKEND": "auto"}), \
                patch.object(browser_backend, "probe_backend_health", return_value={"ok": False}) as probe:
            with self.assertRaises(browser_backend.BrowserUnavailable):
                BrowserSession(session="t")
        self.assertEqual([call.args[0] for call in probe.call_args_list], list(self.AUTO_CHAIN))

    def test_resolve_backend_returns_name_only(self) -> None:
        with patch.dict(os.environ, {"BLACKBOARD_BROWSER_BACKEND": "kimi"}):
            self.assertEqual(browser_backend.resolve_backend(None), "kimi")
        with patch.dict(os.environ, {"BLACKBOARD_BROWSER_BACKEND": "auto"}), \
                patch.object(browser_backend, "probe_backend_health",
                             side_effect=lambda name: {"ok": name == "browser-use"}):
            self.assertEqual(browser_backend.resolve_backend(None), "browser-use")


class ProbeCacheTests(unittest.TestCase):
    def test_probe_backend_health_caches_per_name(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return browser_backend._CliResult(0, '{"ok": true}\n', "")

        with patch.dict(browser_backend._PROBE_CACHE, clear=True), \
                patch.object(browser_backend, "_resolve_tool", return_value="/fake/browser-use"), \
                patch.object(browser_backend, "_run_cli", side_effect=fake_run):
            first = browser_backend.probe_backend_health("browser-use")
            second = browser_backend.probe_backend_health("browser-use")
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(len(calls), 1)


class _FakeCli:
    """Records _run_cli calls and replays canned results."""

    def __init__(self, results) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, cmd, *, timeout=None, input_text=None):
        self.calls.append({"cmd": cmd, "timeout": timeout, "input_text": input_text})
        result = self.results.pop(0) if self.results else browser_backend._CliResult(0, "", "")
        return result


class EgoBackendTests(unittest.TestCase):
    def _session(self) -> BrowserSession:
        return BrowserSession(session="t", backend="ego")

    def test_open_returns_page_label_and_navigates_target_url(self) -> None:
        fake = _FakeCli([browser_backend._CliResult(0, '__BB_RESULT__ {"ok": true, "url": "https://x/ultra"}\n', "")])
        with patch.object(browser_backend, "_resolve_tool", return_value="/fake/ego-browser"), \
                patch.object(browser_backend, "_run_cli", side_effect=fake):
            tab = self._session().open("https://learn.example.edu/ultra/")
        self.assertEqual(tab, "p1")
        self.assertEqual(fake.calls[0]["cmd"], ["/fake/ego-browser", "nodejs"])
        self.assertIn("https://learn.example.edu/ultra/", fake.calls[0]["input_text"])

    def test_eval_text_returns_string_value(self) -> None:
        # ego-browser routes console.log to stderr; the marker is scanned there too.
        fake = _FakeCli([browser_backend._CliResult(0, "", '__BB_RESULT__ {"ok": true, "value": "clicked"}\n')])
        with patch.object(browser_backend, "_resolve_tool", return_value="/fake/ego-browser"), \
                patch.object(browser_backend, "_run_cli", side_effect=fake):
            value = self._session().eval_text("(function(){return 'clicked';})();")
        self.assertEqual(value, "clicked")

    def test_missing_marker_raises(self) -> None:
        fake = _FakeCli([browser_backend._CliResult(0, "nothing here\n", "")])
        with patch.object(browser_backend, "_resolve_tool", return_value="/fake/ego-browser"), \
                patch.object(browser_backend, "_run_cli", side_effect=fake):
            with self.assertRaises(RuntimeError):
                self._session().eval_text("x")

    def test_close_finishes_task_and_swallows_errors(self) -> None:
        fake = _FakeCli([browser_backend._CliResult(1, "", "space gone")])
        with patch.object(browser_backend, "_resolve_tool", return_value="/fake/ego-browser"), \
                patch.object(browser_backend, "_run_cli", side_effect=fake):
            self._session().close()  # must not raise
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("task.finish({keep: []})", fake.calls[0]["input_text"])
        self.assertEqual(fake.calls[0]["timeout"], 30)


class BrowserUseBackendTests(unittest.TestCase):
    def _session(self) -> BrowserSession:
        return BrowserSession(session="t", backend="browser-use")

    def test_open_parses_tab_id_and_falls_back_to_constant_tab(self) -> None:
        tab_json = browser_backend._CliResult(0, '{"tab_id": "tab-7"}\n', "")
        plain = browser_backend._CliResult(0, "navigated\n", "")
        for result, expected in ((tab_json, "tab-7"), (plain, "browser-use")):
            with self.subTest(stdout=result.stdout.strip()):
                fake = _FakeCli([result])
                with patch.object(browser_backend, "_resolve_tool", return_value="/fake/browser-use"), \
                        patch.object(browser_backend, "_run_cli", side_effect=fake):
                    tab = self._session().open("https://learn.example.edu/ultra/")
                self.assertEqual(tab, expected)
                self.assertEqual(
                    fake.calls[0]["cmd"],
                    ["/fake/browser-use", "--session", "t", "--json", "open", "https://learn.example.edu/ultra/"],
                )

    def test_eval_text_unwraps_payload_shapes(self) -> None:
        # Real observed shape is {"id", "success", "data": {"result": <value>}};
        # other builds hoist the value to the top level, print it as plain
        # text, or exit non-zero.
        cases = [
            ('{"id": "r1", "success": true, "data": {"result": "1"}}', "1"),
            ('{"success": true, "result": "1"}', "1"),
            ("0\n", "0"),
        ]
        for stdout, expected in cases:
            with self.subTest(stdout=stdout):
                fake = _FakeCli([browser_backend._CliResult(0, stdout, "")])
                with patch.object(browser_backend, "_resolve_tool", return_value="/fake/browser-use"), \
                        patch.object(browser_backend, "_run_cli", side_effect=fake):
                    self.assertEqual(self._session().eval_text("window.authenticated"), expected)

    def test_eval_text_unknown_payload_shape_raises(self) -> None:
        fake = _FakeCli([browser_backend._CliResult(0, '{"weird": 1}\n', "ignored")])
        with patch.object(browser_backend, "_resolve_tool", return_value="/fake/browser-use"), \
                patch.object(browser_backend, "_run_cli", side_effect=fake):
            with self.assertRaises(RuntimeError):
                self._session().eval_text("x")

    def test_close_is_a_noop(self) -> None:
        fake = _FakeCli([])
        with patch.object(browser_backend, "_resolve_tool", return_value="/fake/browser-use"), \
                patch.object(browser_backend, "_run_cli", side_effect=fake):
            self._session().close()
        self.assertEqual(fake.calls, [])


class KimiCookieTests(unittest.TestCase):
    def _session(self) -> BrowserSession:
        return BrowserSession(session="t", backend="kimi")

    def test_cookies_posts_cdp_getcookies_with_urls(self) -> None:
        urls = ["https://learn.example.edu/", "https://example.leganto.exlibrisgroup.com/"]
        with patch("blackboard.browser_backend.urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = _ok(
                {"cookies": [{"name": "BbRouter", "value": "x"}]}
            )
            cookies = self._session().cookies(urls)
        self.assertEqual(cookies, [{"name": "BbRouter", "value": "x"}])
        body = json.loads(mock_open.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(body["action"], "cdp")
        self.assertEqual(body["args"]["method"], "Network.getCookies")
        self.assertEqual(body["args"]["params"]["urls"], urls)
        self.assertEqual(body["session"], "t")
        self.assertEqual(mock_open.call_args.kwargs["timeout"], 15)

    def test_cookies_filters_entries_without_name_or_value(self) -> None:
        with patch("blackboard.browser_backend.urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = _ok(
                {"cookies": [
                    {"name": "good", "value": "v"},
                    {"name": "empty", "value": ""},
                    {"value": "no-name"},
                    {},
                    "not-a-dict",
                ]}
            )
            cookies = self._session().cookies(["https://learn.example.edu/"])
        self.assertEqual(cookies, [{"name": "good", "value": "v"}])

    def test_cookies_empty_set_is_a_valid_read(self) -> None:
        with patch("blackboard.browser_backend.urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = _ok({"cookies": []})
            self.assertEqual(self._session().cookies(["https://learn.example.edu/"]), [])

    def test_cookies_daemon_unreachable_raises(self) -> None:
        with patch("blackboard.browser_backend.urllib.request.urlopen", side_effect=OSError("refused")):
            with self.assertRaises(RuntimeError) as ctx:
                self._session().cookies(["https://learn.example.edu/"])
        self.assertIn("unreachable", str(ctx.exception))

    def test_cookies_missing_cookies_key_raises(self) -> None:
        with patch("blackboard.browser_backend.urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = _ok({"nomatch": 1})
            with self.assertRaises(RuntimeError) as ctx:
                self._session().cookies(["https://learn.example.edu/"])
        self.assertIn("returned no cookies", str(ctx.exception))


class EgoCookieTests(unittest.TestCase):
    def _session(self) -> BrowserSession:
        return BrowserSession(session="blackboard-sync", backend="ego")

    def test_cookies_parses_marker_cookies(self) -> None:
        fake = _FakeCli([browser_backend._CliResult(
            0,
            "",
            # ego-browser routes console.log to stderr; markers are scanned there too.
            '__BB_RESULT__ {"ok": true, "cookies": ['
            '{"name": "BbRouter", "value": "bb", "domain": "learn.example.edu", "path": "/", "secure": true}]}',
        )])
        with patch.object(browser_backend, "_resolve_tool", return_value="/fake/ego-browser"), \
                patch.object(browser_backend, "_run_cli", side_effect=fake):
            cookies = self._session().cookies(["https://learn.example.edu/"])
        self.assertEqual(cookies[0]["name"], "BbRouter")
        self.assertEqual(fake.calls[0]["cmd"], ["/fake/ego-browser", "nodejs"])
        script = fake.calls[0]["input_text"]
        self.assertIn('taskSpace("blackboard-sync")', script)
        self.assertIn('page("p1")', script)
        self.assertIn("Network.getCookies", script)
        self.assertIn('"https://learn.example.edu/"', script)

    def test_cookies_empty_set_is_a_valid_read(self) -> None:
        fake = _FakeCli([browser_backend._CliResult(0, '__BB_RESULT__ {"ok": true, "cookies": []}', "")])
        with patch.object(browser_backend, "_resolve_tool", return_value="/fake/ego-browser"), \
                patch.object(browser_backend, "_run_cli", side_effect=fake):
            self.assertEqual(self._session().cookies(["https://learn.example.edu/"]), [])

    def test_cookies_failure_redacts_signed_url(self) -> None:
        fake = _FakeCli([browser_backend._CliResult(
            1, "", "fetch failed at https://cdn.example.com/f?token=verysecret")])
        with patch.object(browser_backend, "_resolve_tool", return_value="/fake/ego-browser"), \
                patch.object(browser_backend, "_run_cli", side_effect=fake):
            with self.assertRaises(RuntimeError) as ctx:
                self._session().cookies(["https://learn.example.edu/"])
        self.assertIn("ego-browser cookie fetch failed", str(ctx.exception))
        self.assertNotIn("verysecret", str(ctx.exception))


class BrowserUseCookieTests(unittest.TestCase):
    def _session(self) -> BrowserSession:
        return BrowserSession(session="t", backend="browser-use")

    def test_cookies_normalizes_and_returns_cdp_shape(self) -> None:
        payload = {
            "id": "r1",
            "success": True,
            "data": {"cookies": [
                {"name": "BbRouter", "value": "bb", "domain": "learn.example.edu",
                 "path": "/", "secure": True, "host_only": False},
                {"name": "default", "value": "1"},
                {"name": "", "value": "1"},
                {"name": "empty", "value": ""},
            ]},
        }
        fake = _FakeCli([browser_backend._CliResult(0, json.dumps(payload), "")])
        with patch.object(browser_backend, "_resolve_tool", return_value="/fake/browser-use"), \
                patch.object(browser_backend, "_run_cli", side_effect=fake):
            cookies = self._session().cookies(["https://learn.example.edu/"])
        self.assertEqual(
            fake.calls[0]["cmd"],
            ["/fake/browser-use", "--session", "t", "--json", "cookies", "get"],
        )
        self.assertEqual(cookies[0]["hostOnly"], False)
        self.assertEqual(cookies[0]["path"], "/")
        self.assertIs(cookies[0]["secure"], True)
        self.assertEqual(cookies[1:], [{"name": "default", "value": "1", "domain": "", "path": "/", "secure": False}])

    def test_cookies_accepts_observed_payload_shapes(self) -> None:
        # Observed shape is {"id", "success", "data": {"cookies": [...]}};
        # other builds emit a top-level "cookies" key or a bare list.
        payloads = [
            json.dumps({"id": "r1", "success": True, "data": {"cookies": [
                {"name": "a", "value": "1", "domain": "learn.example.edu"},
            ]}}),
            json.dumps({"cookies": [
                {"name": "a", "value": "1", "domain": "learn.example.edu"},
            ]}),
            json.dumps([
                {"name": "a", "value": "1", "url": "https://learn.example.edu/ultra"},
            ]),
        ]
        for stdout in payloads:
            with self.subTest(stdout=stdout[:40]):
                fake = _FakeCli([browser_backend._CliResult(0, stdout, "")])
                with patch.object(browser_backend, "_resolve_tool", return_value="/fake/browser-use"), \
                        patch.object(browser_backend, "_run_cli", side_effect=fake):
                    cookies = self._session().cookies(["https://learn.example.edu/"])
                expected = {"name": "a", "value": "1", "domain": "learn.example.edu", "path": "/", "secure": False}
                if stdout.startswith("["):
                    expected["url"] = "https://learn.example.edu/ultra"
                self.assertEqual(cookies, [expected])

    def test_cookies_unrecognized_payload_raises(self) -> None:
        fake = _FakeCli([browser_backend._CliResult(0, '{"nope": true}', "")])
        with patch.object(browser_backend, "_resolve_tool", return_value="/fake/browser-use"), \
                patch.object(browser_backend, "_run_cli", side_effect=fake):
            with self.assertRaises(RuntimeError) as ctx:
                self._session().cookies(["https://learn.example.edu/"])
        self.assertIn("unrecognized payload", str(ctx.exception))

    def test_cookies_nonzero_exit_raises_redacted(self) -> None:
        fake = _FakeCli([browser_backend._CliResult(3, "", "cookies blew up at https://x/p?sig=abc123")])
        with patch.object(browser_backend, "_resolve_tool", return_value="/fake/browser-use"), \
                patch.object(browser_backend, "_run_cli", side_effect=fake):
            with self.assertRaises(RuntimeError) as ctx:
                self._session().cookies(["https://learn.example.edu/"])
        self.assertIn("browser-use cookies get failed", str(ctx.exception))
        self.assertNotIn("abc123", str(ctx.exception))

    def test_cookies_empty_set_is_a_valid_read(self) -> None:
        fake = _FakeCli([browser_backend._CliResult(0, '{"cookies": []}', "")])
        with patch.object(browser_backend, "_resolve_tool", return_value="/fake/browser-use"), \
                patch.object(browser_backend, "_run_cli", side_effect=fake):
            self.assertEqual(self._session().cookies(["https://learn.example.edu/"]), [])

class CliBackendFlagTests(unittest.TestCase):
    def test_browser_backend_flag_parses_on_browser_commands(self) -> None:
        from blackboard.cli import build_parser, main
        from blackboard import course_service as svc

        with tempfile.TemporaryDirectory() as tmp:
            cases = [
                (["capture", "_1_1", "--root", tmp, "--dry-run", "--browser-backend", "ego,browser-use"], "ego,browser-use"),
                (["sync", "_1_1", "--root", tmp, "--dry-run", "--browser-backend", "auto"], "auto"),
                (["fetch", "--root", tmp, "--dry-run", "--browser-backend", "kimi"], "kimi"),
            ]
            for argv, expected in cases:
                argv.extend(["--site", EXAMPLE_SITE.origin])
                with self.subTest(argv=argv):
                    args = build_parser().parse_args(argv)
                    self.assertEqual(args.browser_backend, expected)
                    session = MagicMock()
                    session.site = EXAMPLE_SITE
                    with patch.object(svc, "Session", return_value=session) as factory, \
                            patch.object(svc, "capture", return_value={"outcome": "complete"}) as capture, \
                            patch.object(svc, "fetch", return_value={"outcome": "complete"}) as fetch, \
                            contextlib.redirect_stdout(io.StringIO()):
                        code = main(argv)
                    self.assertEqual(code, 0)
                    if argv[0] in ("capture", "sync"):
                        self.assertIs(capture.call_args.args[2], session)
                        self.assertTrue(capture.call_args.kwargs['dry_run'])
                        self.assertEqual(factory.call_args.args[1].browser_backend, expected)
                        fetch.assert_not_called()  # sync --dry-run previews capture only
                    else:
                        self.assertEqual(fetch.call_args.args[1].browser_backend, expected)

    def test_invalid_browser_backend_exits_invalid_input(self) -> None:
        # Reject invalid pins before any archive or browser activity.
        from blackboard.cli import main
        for name in ("bogus", "browse-now", "browsenow"):
            with self.subTest(backend=name), \
                    patch.object(browser_backend, "probe_backend_health", return_value={"ok": True}) as probe, \
                    contextlib.redirect_stdout(io.StringIO()) as captured:
                code = main(["fetch", "--root", "/tmp/course", "--browser-backend", name])
            self.assertEqual(code, 2)
            payload = json.loads(captured.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIn(name, payload["errors"][0]["message"])
            probe.assert_not_called()

    def test_login_is_not_a_blackboard_command(self) -> None:
        from blackboard.cli import UsageError, build_parser
        with self.assertRaises(UsageError):
            build_parser().parse_args(["login"])

    def test_paths_format_rejected_for_non_files_command(self) -> None:
        from blackboard.cli import UsageError, build_parser
        with self.assertRaises(UsageError):
            build_parser().parse_args(["status", "--root", "/tmp/x", "--format", "paths"])


class OutcomeMappingTests(unittest.TestCase):
    def test_exit_code_by_reported_outcome(self) -> None:
        # main()'s exit code must follow the outcome the service reports.
        from blackboard.cli import main
        from blackboard import course_service as svc

        cases = [("complete", 0), ("partial", 1), ("blocked", 3), ("needs-action", 4), ("failed", 1)]
        with tempfile.TemporaryDirectory() as tmp:
            for outcome, expected in cases:
                with self.subTest(outcome=outcome):
                    with patch.object(svc, "fetch", return_value={"outcome": outcome}), \
                            contextlib.redirect_stdout(io.StringIO()):
                        code = main(["fetch", "--site", EXAMPLE_SITE.origin, "--root", tmp, "--dry-run"])
                    self.assertEqual(code, expected)

    def test_browser_unavailable_exits_needs_action(self) -> None:
        from blackboard.cli import main
        from blackboard import course_service as svc

        with patch.object(svc, "request_get", side_effect=browser_backend.BrowserUnavailable("chain down")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["request", "get", "/learn/api/public/v1/courses", "--root", "/tmp/course"])
        self.assertEqual(code, 4)
        self.assertIn("browser.unavailable", out.getvalue())

    def test_internal_value_error_is_not_invalid_input(self) -> None:
        from blackboard.cli import main
        from blackboard import course_service as svc

        with patch.object(svc, "request_get", side_effect=ValueError("boom")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["request", "get", "/learn/api/public/v1/courses", "--root", "/tmp/course"])
        self.assertEqual(code, 1)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["errors"][0]["code"], "ValueError")

    def test_session_required_maps_retryability_to_exit_code(self) -> None:
        from blackboard.cli import main
        from blackboard import course_service as svc

        for retryable, expected in ((True, 4), (False, 1)):
            with self.subTest(retryable=retryable):
                with patch.object(svc, "request_get", side_effect=browser_backend.SessionRequired("Sign in first", retryable=retryable)), contextlib.redirect_stdout(io.StringIO()) as out:
                    code = main(["request", "get", "/learn/api/public/v1/courses", "--root", "/tmp/course"])
                self.assertEqual(code, expected)
                payload = json.loads(out.getvalue())
                self.assertEqual(payload["errors"][0]["code"], "blackboard.browser_session_required")


if __name__ == "__main__":
    unittest.main()
