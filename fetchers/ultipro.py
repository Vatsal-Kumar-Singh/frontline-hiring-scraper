"""UKG / UltiPro Recruiting fetcher.

Public JSON POST:
    POST https://<host>/<TENANT>/JobBoard/<guid>/JobBoardView/LoadSearchResults
    body {opportunitySearch:{Top,Skip,...}, matchCriteria:{...}}
<host> is recruiting.ultipro.com OR recruiting2.ultipro.com (tenant-dependent).
Paginates via Top/Skip; total is `totalCount`. Verified live against
recruiting2.ultipro.com/SAL1002 (Salvation Army, ~1051 jobs).

Slug is composite: "<host>|<TENANT>|<guid>".
"""
from __future__ import annotations

from .base import MAX_PAGES, FetchError, post_json, reached_threshold

PLATFORM = "ultipro"
PAGE = 50


def _body(top, skip):
    return {
        "opportunitySearch": {"Top": top, "Skip": skip, "QueryString": "",
                              "OrderBy": [], "Filters": []},
        "matchCriteria": {"PreferredJobs": [], "Educations": [],
                          "LicenseAndCertifications": [], "Skills": [],
                          "hasNoLicenses": False, "SkippedSkills": []},
    }


def _parse(slug: str):
    parts = slug.split("|")
    if len(parts) != 3:
        raise FetchError(f"ultipro slug must be host|tenant|guid, got {slug!r}")
    return parts[0], parts[1], parts[2]


def _url(host: str, tenant: str, guid: str) -> str:
    return f"https://{host}/{tenant}/JobBoard/{guid}/JobBoardView/LoadSearchResults"


def _location(j: dict) -> str:
    locs = j.get("Locations") or []
    parts = []
    for loc in locs:
        addr = loc.get("Address") or {}
        state = addr.get("State") or {}
        city = addr.get("City")
        st = state.get("Code") or state.get("Name")
        country = (addr.get("Country") or {}).get("Code")
        readable = ", ".join(p for p in (city, st, country) if p)
        parts.append(readable or loc.get("LocalizedName") or "")
    return "; ".join(p for p in parts if p)


def fetch(slug: str) -> list[dict]:
    host, tenant, guid = _parse(slug)
    url = _url(host, tenant, guid)
    out: list[dict] = []
    skip = 0
    for _ in range(MAX_PAGES):
        data = post_json(url, json=_body(PAGE, skip))
        opps = data.get("opportunities", []) if isinstance(data, dict) else []
        for j in opps:
            out.append({"title": (j.get("Title") or "").strip(), "location": _location(j)})
        total = data.get("totalCount", 0) if isinstance(data, dict) else 0
        skip += len(opps) if opps else PAGE
        if not opps or skip >= total or reached_threshold(out):
            break
    return out


def verify(slug: str) -> bool:
    try:
        host, tenant, guid = _parse(slug)
    except FetchError:
        return False
    data = post_json(_url(host, tenant, guid), json=_body(1, 0))
    return isinstance(data, dict) and "opportunities" in data
