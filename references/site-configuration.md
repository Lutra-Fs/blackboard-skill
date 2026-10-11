# Site configuration

Read this document for optional service origins and trusted attachment hosts.

## Supply the site

Use a full course URL or a course ID with `--site URL`.
Protected requests need a signed-in browser.
The project has no institutional preset.
Later commands use the site context recorded in the archive.

For optional integrations, use `--site-config PATH` instead of `--site`:

```json
{
  "origin": "https://learn.example.edu",
  "trusted_hosts": ["files.example.edu"],
  "leganto_origin": "https://library.example.edu",
  "panopto_origin": "https://video.example.edu",
  "display_timezone": "Europe/London"
}
```

## Configure optional services

| Field | Behavior |
|---|---|
| `origin` | Required Blackboard site origin. The site's host is trusted for attachments. |
| `trusted_hosts` | Additional explicitly trusted attachment hosts. |
| `trusted_host_suffixes` | Explicitly trusted host suffixes with multiple labels. |
| `leganto_origin` | Origin for the optional reading-list integration. |
| `panopto_origin` | Origin for the optional Panopto integration. |
| `display_timezone` | Display timezone. The default is UTC. |
| `ultra_supported` | Set `false` to disable Ultra discovery and route shells. |

Missing integration origins leave those integrations unavailable.
Ordinary course capture stays usable.

## Select trusted hosts

Explicit sites and trusted hosts can use internal addresses, including IPv4 and IPv6.
TLS certificate verification stays enabled.
An explicitly selected HTTP site uses HTTP.
Prefer HTTPS where the site supports it.
Site or file host selection does not authorize unrelated internal redirect targets.
