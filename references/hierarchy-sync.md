# Page hierarchy and source mappings

Read this document when you connect local evidence to its course source.

## Read source evidence

`_blackboard/course-tree.json` records the current captured pages.
Use parent identities and breadcrumbs to determine the source hierarchy.
A parent reference alone does not prove that the archive contains the parent's text.

| File in a page directory | Content |
|---|---|
| `page.md` | Readable text and source context. |
| `links.json` | Attachments, child links, external links, and unresolved assets. |
| `source.json` | Page identity and capture source information. |

Use `files` for registered paths and actual file presence.
Use recorded page and attachment identities to connect files to source pages.
A resource ID identifies an acquisition resource.
It does not describe the meaning of the content.
A tool launch link does not prove acquisition of its documents or captions.

## Preserve the course layout

Select the course root from its applicable instructions and existing layout.
The CLI controls file placement and filename collisions.
Ordinary fetch preserves registered paths and local annotations.
For reviewed changes or deletion, read [change plans](change-plan.md).
