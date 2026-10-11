# Evidence And Sources

This is an evidence index, not a runtime procedure or a compatibility guarantee.
The public contract is [SPEC.md](SPEC.md); usage is in [SKILL.md](SKILL.md).
Implementation history remains in Git.

## Evidence Scope

Historical live checks used one university's Blackboard deployment and selected
linked services. They establish observed outcomes for those sources, not
compatibility with every institution, permission level or integration.
The deployment is an evidence source, not a required configuration or preset.

## Recorded Observations

- April-July 2026 course checks established that page text and module hierarchy
  contain evidence absent from attachments alone. Existing course-root reuse
  and same-name revisions also motivated preserving file/source mappings and
  local material.
- July 2026 checks verified native reading-list export and linked course files.
  A tool link alone did not establish successful acquisition of its contents.
- 2026-10-02 caption checks reproduced an English regional-tag mismatch.
  The repaired acquisition captured the advertised en-GB transcript; four
  other public-video assets also downloaded.
- 2026-10-02 TED verification acquired the 682-second Dan Meyer transcript.
  A linked external YouTube video was a different resource, not an equivalent
  replacement. These checks are historical, not a fresh service-health report.
- 2026-10-10 the reviewed CLI contract separated capture, acquisition and
  archive-change decisions. Confirmed unavailable resources remain visible
  gaps; local deletion requires reviewed file disposition.

## Verification Sources

The repository's tests cover observable capture completeness, source/file
mappings, acquisition gaps, authentication failures, reviewed changes and
interrupted-work recovery. Caption fixtures distinguish absent tracks, missing
requested language and capture failure. Offline regression success is not proof
of current live-service compatibility.

The [upstream yt-dlp TED extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/ted.py)
was the primary reference for the recorded TED compatibility repair.
