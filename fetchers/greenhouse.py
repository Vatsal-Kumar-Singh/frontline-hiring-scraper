"""Greenhouse fetcher.

Public board API: boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true
Returns all jobs in one payload (no pagination); `location` is {"name": ...}.
"""
from __future__ import annotations

from .base import flatten_location, get_json

PLATFORM = "greenhouse"


def _url(slug: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


def fetch(slug: str) -> list[dict]:
    data = get_json(_url(slug), params={"content": "true"})
    out: list[dict] = []
    for j in data.get("jobs", []):
        out.append(
            {
                "title": (j.get("title") or "").strip(),
                "location": flatten_location(j.get("location") or j.get("offices")),
            }
        )
    return out


def verify(slug: str) -> bool:
    data = get_json(_url(slug))
    return isinstance(data, dict) and "jobs" in data
