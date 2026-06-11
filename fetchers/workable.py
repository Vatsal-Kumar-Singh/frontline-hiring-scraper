"""Workable fetcher — handles two board flavors:

1. Hosted board (text slug): POST apply.workable.com/api/v3/accounts/<slug>/jobs
   Paginates via a `token` cursor (10/page); `nextPage` is the next token.
   Verified live against apply.workable.com/spark-car-wash (Phase 0).
2. JS-embed board (numeric account id, from `whr_embed(<id>)` on a careers page):
   GET www.workable.com/api/accounts/<id> — returns the full board in one shot.
   This avoids a headless browser for the very common embed.js case.
"""
from __future__ import annotations

from .base import MAX_PAGES, flatten_location, get_json, post_json, reached_threshold

PLATFORM = "workable"


def _apply_url(slug: str) -> str:
    return f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"


def _account_url(account_id: str) -> str:
    return f"https://www.workable.com/api/accounts/{account_id}"


def _is_account_id(slug: str) -> bool:
    return slug.isdigit()


def _job_location(j: dict) -> str:
    loc = j.get("location") or j.get("locations")
    if loc:
        return flatten_location(loc)
    # account-endpoint jobs carry flat city/state/country fields
    return flatten_location(
        {"city": j.get("city"), "region": j.get("state"), "country": j.get("country")}
    )


def fetch(slug: str) -> list[dict]:
    if _is_account_id(slug):
        data = get_json(_account_url(slug))
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        return [
            {"title": (j.get("title") or "").strip(), "location": _job_location(j)}
            for j in jobs
        ]

    url = _apply_url(slug)
    out: list[dict] = []
    token = None
    for _ in range(MAX_PAGES):
        body = {"token": token} if token else {}
        data = post_json(url, json=body)
        for j in data.get("results", []):
            out.append(
                {"title": (j.get("title") or "").strip(), "location": _job_location(j)}
            )
        token = data.get("nextPage")
        if not token or reached_threshold(out):
            break
    return out


def verify(slug: str) -> bool:
    """Cheap check: does the slug resolve to a board with a usable shape?"""
    if _is_account_id(slug):
        data = get_json(_account_url(slug))
        return isinstance(data, dict) and "jobs" in data
    data = post_json(_apply_url(slug), json={})
    return isinstance(data, dict) and "results" in data
