"""Blackboard Ultra course URL helpers.

URL-parsing and LTI-launch helpers shared by the REST crawler and the course
service.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .site_config import SiteConfig
from .url_policy import is_blackboard_url, require_blackboard_id


def parse_course_id(
    course_url: str,
    explicit: str | None,
    *,
    site_config: SiteConfig,
) -> str:
    if not is_blackboard_url(course_url, site_config=site_config):
        raise RuntimeError(f"Course URL must use {site_config.origin}")
    inferred: str | None = None
    for pattern in (r"/courses/([^/]+)", r"[?&]course_id=([^&]+)"):
        match = re.search(pattern, course_url)
        if match:
            inferred = match.group(1)
            break
    if explicit:
        explicit = require_blackboard_id(explicit, label="Blackboard course id")
        if inferred and inferred != explicit:
            raise RuntimeError(f"Explicit course id {explicit} does not match the course URL id {inferred}")
        return explicit
    if inferred:
        return require_blackboard_id(inferred, label="Blackboard course id")
    raise RuntimeError("Could not infer course id from the course URL, e.g. https://<origin>/ultra/courses/_58445_1/outline")


def canonical_ultra_course_url(course_id: str, *, site_config: SiteConfig) -> str:
    """Return the current native Ultra outline route (without the legacy /cl segment)."""
    return f"{site_config.origin}/ultra/courses/{course_id}/outline"


def build_lti_launch_url(
    launch_link: str,
    course_id: str,
    *,
    site_config: SiteConfig,
) -> str | None:
    """Resolve a placement ``launchLink`` to a canonical launch URL.

    Joins the (often site-relative) ``launchLink`` to the Blackboard origin,
    requires it to be on the ``/webapps/blackboard/execute/blti/launchPlacement``
    path, and augments its query with ``course_id`` and ``from_ultra=true``.
    Returns ``None`` when the link is missing or not a valid Blackboard launch
    placement URL. Used by the REST Books & Tools crawl.
    """
    if not launch_link:
        return None
    launch_url = urljoin(site_config.origin, launch_link)
    if not is_blackboard_url(
        launch_url,
        prefixes=("/webapps/blackboard/execute/blti/launchPlacement",),
        site_config=site_config,
    ):
        return None
    launch = urlparse(launch_url)
    query = dict(parse_qsl(launch.query, keep_blank_values=True))
    query.update({"course_id": course_id, "from_ultra": "true"})
    return urlunparse(launch._replace(query=urlencode(query)))
