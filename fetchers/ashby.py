"""Ashby fetcher.

Public job-board API: api.ashbyhq.com/posting-api/job-board/<slug>
Returns {"jobs": [...]}; each job has `title` and a `location` string plus
optional `secondaryLocations`/`address`.
"""
from __future__ import annotations

from .base import flatten_location, get_json

PLATFORM = "ashby"


def _url(slug: str) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{slug}"


def fetch(slug: str) -> list[dict]:
    data = get_json(_url(slug), params={"includeCompensation": "false"})
    out: list[dict] = []
    for j in data.get("jobs", []):
        loc = j.get("location") or j.get("address") or j.get("secondaryLocations")
        out.append(
            {
                "title": (j.get("title") or "").strip(),
                "location": flatten_location(loc),
            }
        )
    return out


def verify(slug: str) -> bool:
    data = get_json(_url(slug))
    return isinstance(data, dict) and "jobs" in data
