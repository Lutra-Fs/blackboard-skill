# Page Hierarchy And File Mappings

## Read Source Evidence

`_blackboard/course-tree.json` describes the current captured pages. Use their
parent identities and breadcrumbs for source hierarchy, not local filenames.
A parent reference without a captured page is not evidence of that page's text.

The recorded page directories contain:

- `page.md`: readable text and source context.
- `links.json`: attachments, child links, external links and unresolved assets.
- `source.json`: page identity and capture provenance.

Use `files` for registered file paths and actual presence. Connect files to their
source pages through recorded page/attachment identities. A resource ID identifies
an acquisition resource, not the meaning of its content. A tool launch link alone
does not establish that its documents or captions were acquired.

## Preserve The Course Layout

Choose the course root using its applicable instructions and existing layout.
The CLI handles file placement and filename collisions. Ordinary fetch preserves
registered paths and local annotations; it does not reorganize archived courses.
For reviewed changes or deletion, use [change plans](change-plan.md).
