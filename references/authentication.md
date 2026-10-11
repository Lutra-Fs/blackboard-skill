# Browser authentication

Read this document when you select a browser profile or recover protected access.

## Prepare access

Sign in through the browser profile that Blackboard will use.
If automatic selection chooses another browser, set `--browser-backend`.
The CLI checks browser dependencies when it needs them.

`--browser-session` identifies an automation group.
It does not identify a cookie profile.
Closing an Ego task group keeps the profile's authentication.

Protected requests use the browser's authentication.
Public document and caption acquisition need no Blackboard browser.

## Restore access

A detected login failure returns `blackboard.browser_session_required` and stops acquisition.
An inaccessible browser returns `browser.unavailable`.
Other resource failures appear individually.

1. Read the current error and `errors[].solution`.
2. Sign in externally in the same browser profile.
3. Use the command given in the solution.
4. Examine the result against the requested scope.

Completed acquisitions stay available.
External batch runners can stop when they detect the authentication error code.
For other retry conditions, read [archive recovery](archive-state.md#continue-or-retry).
