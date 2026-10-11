"""Synthetic deployment for offline REST and integration tests."""
from blackboard.site_config import SiteConfig

EXAMPLE_SITE = SiteConfig(
    origin="https://learn.example.edu",
    trusted_host_suffixes=("example.edu",),
    leganto_origin="https://example.leganto.exlibrisgroup.com",
    panopto_origin="https://example.au.panopto.com",
    display_timezone="Australia/Sydney",
)
