"""SmartRecruiters fetcher.

Public postings API: api.smartrecruiters.com/v1/companies/<slug>/postings
Paginates via limit/offset (max 100/page); location is a nested object.
"""
from __future__ import annotations

from .base import MAX_PAGES, flatten_location, get_json, reached_threshold

PLATFORM = "smartrecruiters"
PAGE = 100


def _url(slug: str) -> str:
    return f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"


def fetch(slug: str) -> list[dict]:
    url = _url(slug)
    out: list[dict] = []
    offset = 0
    for _ in range(MAX_PAGES):
        data = get_json(url, params={"limit": PAGE, "offset": offset})
        items = data.get("content", []) if isinstance(data, dict) else []
        for j in items:
            out.append(
                {
                    "title": (j.get("name") or "").strip(),
                    "location": flatten_location(j.get("location")),
                }
            )
        total = data.get("totalFound", 0) if isinstance(data, dict) else 0
        offset += PAGE
        if offset >= total or not items or reached_threshold(out):
            break
    return out


def verify(slug: str) -> bool:
    data = get_json(_url(slug), params={"limit": 1})
    return isinstance(data, dict) and "content" in data
