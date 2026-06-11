"""Workday fetcher.

Public, unauthenticated JSON POST:
    POST https://<tenant>.<dc>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
    body {"appliedFacets":{},"limit":20,"offset":0,"searchText":""}
Paginates via offset/limit (max 20/page); total is the top-level `total`.
Verified live against carrier.wd5.myworkdayjobs.com (total ~1095).

Slug is composite: "<tenant>|<dc>|<site>" (e.g. "carrier|wd5|jobs").
"""
from __future__ import annotations

from .base import MAX_PAGES, FetchError, flatten_location, post_json, reached_threshold

PLATFORM = "workday"
PAGE = 20


def _parse(slug: str):
    parts = slug.split("|")
    if len(parts) != 3:
        raise FetchError(f"workday slug must be tenant|dc|site, got {slug!r}")
    return parts[0], parts[1], parts[2]


def _url(tenant: str, dc: str, site: str) -> str:
    return f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"


def fetch(slug: str) -> list[dict]:
    tenant, dc, site = _parse(slug)
    url = _url(tenant, dc, site)
    out: list[dict] = []
    offset = 0
    total = None  # Workday reports `total` only on the FIRST page; capture it once.
    for _ in range(MAX_PAGES):
        data = post_json(
            url, json={"appliedFacets": {}, "limit": PAGE, "offset": offset, "searchText": ""}
        )
        posts = data.get("jobPostings", []) if isinstance(data, dict) else []
        if not posts:
            break
        for j in posts:
            out.append(
                {
                    "title": (j.get("title") or "").strip(),
                    # locationsText is a string; may read "N Locations" for multi-site.
                    "location": flatten_location(j.get("locationsText")),
                }
            )
        if total is None:
            total = data.get("total", 0) if isinstance(data, dict) else 0
        offset += PAGE
        if (total and offset >= total) or reached_threshold(out):
            break
    return out


def verify(slug: str) -> bool:
    try:
        tenant, dc, site = _parse(slug)
    except FetchError:
        return False
    data = post_json(
        _url(tenant, dc, site),
        json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
    )
    return isinstance(data, dict) and "jobPostings" in data
