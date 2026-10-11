"""Pytest configuration shared across all test modules.

Sets ``BB_REST_DELAY=0`` so the REST crawler's inter-call polite delay (0.1s in
production) is eliminated in tests. This is the single biggest test-suite
accelerator: 21 crawl tests × ~4 REST calls × 0.1s = ~8s of pure sleep that
serves no purpose against fake fetchers.

The env var is set before any test module imports ``rest_crawler``, so the
module-level constant reads 0.0 at import time.

``BLACKBOARD_BROWSER_BACKEND=kimi`` pins the browser-backend chain to the
kimi-webbridge transport for the whole suite, so ``BrowserSession``/
``CookieStore`` construction never probes (or launches) the ego-browser,
or browser-use CLIs. Tests that exercise chain resolution
override this with ``patch.dict``.
"""
import os

os.environ.setdefault("BB_REST_DELAY", "0")
os.environ["BLACKBOARD_BROWSER_BACKEND"] = "kimi"
