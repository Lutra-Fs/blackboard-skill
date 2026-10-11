# Browser Authentication

Prepare login externally in the browser profile used by Blackboard. Pin
`--browser-backend` for acquisition when needed; dependencies are checked on use.
`--browser-session` names an automation group, not a cookie profile. Closing an
Ego task group keeps profile cookies for other groups.

Protected requests reuse the browser's authentication. A detected login failure
stops acquisition with `blackboard.browser_session_required`; an inaccessible
browser reports `browser.unavailable`. Other resource failures are reported
individually. Public document/caption acquisition needs no Blackboard browser.

Follow the JSON error's `solution`: restore authentication externally, then run
its quoted command. Completed acquisitions are retained. External batch runners
can stop on the authentication error code.
