# Site Configuration

Ordinary use needs a full course URL, or a course ID with `--site URL`, and an
already signed-in browser. There is no institutional preset.

Use `--site-config PATH` instead of `--site` for optional integration settings:

```json
{
  "origin": "https://learn.example.edu",
  "trusted_hosts": ["files.example.edu"],
  "leganto_origin": "https://library.example.edu",
  "panopto_origin": "https://video.example.edu",
  "display_timezone": "Europe/London"
}
```

Only `origin` is required. The site's own host is trusted for attachments.
`trusted_host_suffixes` optionally authorizes an explicit multi-label suffix.
Missing integration origins leave those integrations unavailable; ordinary
course capture remains usable. The default display timezone is UTC.
`ultra_supported: false` disables Ultra-specific discovery and route shells.

Explicit sites and trusted attachment hosts may be internal, including IPv4
and IPv6 addresses. TLS certificate verification remains enabled. An explicitly
selected HTTP site uses HTTP; prefer HTTPS where available. Selecting one site
or file host does not authorize unrelated internal redirect targets.

Later commands reuse the archive's site context.
