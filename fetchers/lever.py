"""Lever fetcher.

Public postings API: api.lever.co/v0/postings/<slug>?mode=json
Returns a flat list; location lives in `categories.location`.
"""
from __future__ import annotations

from .base import flatten_location, get_json

PLATFORM = "lever"


def _url(slug: str) -> str:
    return f"https://api.lever.co/v0/postings/{slug}"


def fetch(slug: str) -> list[dict]:
    data = get_json(_url(slug), params={"mode": "json"})
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for j in data:
        cats = j.get("categories") or {}
        loc = cats.get("allLocations") or cats.get("location")
        out.append(
            {
                "title": (j.get("text") or "").strip(),
                "location": flatten_location(loc),
            }
        )
    return out


def verify(slug: str) -> bool:
    data = get_json(_url(slug), params={"mode": "json"})
    return isinstance(data, list)
