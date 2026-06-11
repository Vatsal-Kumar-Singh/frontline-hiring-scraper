"""Cornerstone OnDemand (CSOD) fetcher.

Two-step, no login: the public career-site HTML embeds an anonymous JWT
(user:-101); scrape it, then call the JSON search API with it.
    GET  https://<tenant>.csod.com/ux/ats/careersite/<siteId>/home?c=<tenant>
         -> regex "token":"<jwt>"
    POST https://<tenant>.csod.com/services/x/career-site/v1/search   (Bearer)
Paginates via pageNumber/pageSize; total is data.totalCount. Verified live
against cornerstone.csod.com (siteId 2).

Slug is composite: "<tenant>|<siteId>".
"""
from __future__ import annotations

import re

from .base import MAX_PAGES, FetchError, flatten_location, get_text, post_json, reached_threshold

PLATFORM = "cornerstone"
PAGE = 50
_TOKEN_RE = re.compile(r'"token"\s*:\s*"([^"]+)"')


def _parse(slug: str):
    parts = slug.split("|")
    if len(parts) != 2:
        raise FetchError(f"cornerstone slug must be tenant|siteId, got {slug!r}")
    return parts[0], parts[1]


def _token(tenant: str, site_id: str) -> str:
    html = get_text(
        f"https://{tenant}.csod.com/ux/ats/careersite/{site_id}/home",
        params={"c": tenant},
    )
    m = _TOKEN_RE.search(html)
    if not m:
        raise FetchError("cornerstone: anonymous token not found in career-site HTML")
    return m.group(1)


def _search(tenant: str, token: str, site_id: str, page: int):
    return post_json(
        f"https://{tenant}.csod.com/services/x/career-site/v1/search",
        headers={"Authorization": "Bearer " + token},
        json={
            "careerSiteId": int(site_id), "cities": [], "states": [], "countryCodes": [],
            "keywords": "", "cultureId": 1, "cultureName": "en-US",
            "pageNumber": page, "pageSize": PAGE,
        },
    )


def _location(req: dict) -> str:
    return flatten_location(req.get("locations"))


def fetch(slug: str) -> list[dict]:
    tenant, site_id = _parse(slug)
    token = _token(tenant, site_id)
    out: list[dict] = []
    page = 1
    for _ in range(MAX_PAGES):
        data = _search(tenant, token, site_id, page)
        block = data.get("data", {}) if isinstance(data, dict) else {}
        reqs = block.get("requisitions", []) or []
        for r in reqs:
            out.append(
                {"title": (r.get("displayJobTitle") or "").strip(), "location": _location(r)}
            )
        total = block.get("totalCount", 0)
        if not reqs or len(out) >= total or reached_threshold(out):
            break
        page += 1
    return out


def verify(slug: str) -> bool:
    try:
        tenant, site_id = _parse(slug)
        token = _token(tenant, site_id)
        data = _search(tenant, token, site_id, 1)
    except Exception:
        return False
    return isinstance(data, dict) and isinstance(data.get("data"), dict)
