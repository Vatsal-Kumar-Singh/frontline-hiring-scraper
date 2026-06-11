"""Harri fetcher (hospitality ATS) — public career-portal API.

Two unauthenticated calls:
  1. GET  gateway.harri.com/core/api/v1/profile/slug/<SLUG>  -> data.id (brand id)
  2. POST gateway.harri.com/core/api/v1/harri_search/search_jobs
       body {"size":N,"from":offset,"source":"web","brand_level_ids":[<id>],
             "sort":["publish_date"],"sort_type":"desc","flow":"CAREER_PORTAL"}
     -> data.hits (total), data.results[].position.name (+ brand/store location)
Verified live against harri.com/HHospitality (Hogsalt, 53 jobs).

Slug = the path segment in harri.com/<SLUG>.
"""
from __future__ import annotations

from .base import MAX_PAGES, flatten_location, get_json, post_json, reached_threshold

PLATFORM = "harri"
GATEWAY = "https://gateway.harri.com/core/api/v1"
PAGE = 100
_HEADERS = {"Origin": "https://harri.com", "Referer": "https://harri.com/",
            "Accept": "application/json, text/plain, */*"}


def _brand_id(slug: str):
    data = get_json(f"{GATEWAY}/profile/slug/{slug}", headers=_HEADERS)
    return (data.get("data") or {}).get("id")


def _location(j: dict) -> str:
    store = j.get("store") or {}
    loc = flatten_location({
        "city": store.get("city") or j.get("city"),
        "region": store.get("state") or j.get("state"),
        "country": store.get("country"),
    })
    if loc:
        return loc
    brand = j.get("brand") or {}
    return brand.get("external_business_name") or brand.get("name") or ""


def _fetch_brand(bid, out, seen):
    """Append a brand's jobs to `out` (de-duped by job id); honors early-stop."""
    frm = 0
    for _ in range(MAX_PAGES):
        body = {"size": PAGE, "from": frm, "source": "web", "brand_level_ids": [bid],
                "sort": ["publish_date"], "sort_type": "desc", "flow": "CAREER_PORTAL"}
        data = post_json(f"{GATEWAY}/harri_search/search_jobs", json=body, headers=_HEADERS)
        block = data.get("data", {}) if isinstance(data, dict) else {}
        results = block.get("results") or []
        for j in results:
            jid = j.get("id")
            if jid is not None and jid in seen:
                continue
            if jid is not None:
                seen.add(jid)
            title = ((j.get("position") or {}).get("name") or j.get("title") or "").strip()
            if title:
                out.append({"title": title, "location": _location(j)})
        frm += len(results)
        if not results or frm >= block.get("hits", 0) or reached_threshold(out):
            break


def fetch(slug: str) -> list[dict]:
    # slug may be pipe-joined (multiple location/brand boards) -> sum across them.
    out: list[dict] = []
    seen: set = set()
    for one in [s.strip() for s in slug.split("|") if s.strip()]:
        bid = _brand_id(one)
        if bid:
            _fetch_brand(bid, out, seen)
        if reached_threshold(out):
            break
    return out


def verify(slug: str) -> bool:
    try:
        return any(_brand_id(s.strip()) for s in slug.split("|") if s.strip())
    except Exception:
        return False
