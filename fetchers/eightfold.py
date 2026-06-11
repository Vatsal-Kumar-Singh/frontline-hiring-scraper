"""Eightfold fetcher.

Eightfold boards run on the company's OWN domain (e.g. careers.<company>.com)
with a public positions API:
    https://<host>/api/apply/v2/jobs?domain=<tenant>&start=<n>&num=10
`tenant` is usually the registrable company domain (host minus a careers./jobs.
prefix). Response: {"positions": [...], "count": N}. Paginated via start/num.

The resolver stores the careers HOST as the slug for this platform.
"""
from __future__ import annotations

from .base import MAX_PAGES, flatten_location, get_json, reached_threshold

PLATFORM = "eightfold"
PAGE = 10


def _tenant_candidates(host: str) -> list[str]:
    host = host.strip().lower().rstrip("/")
    for scheme in ("https://", "http://"):
        if host.startswith(scheme):
            host = host[len(scheme):]
    cands = [host]
    for prefix in ("careers.", "jobs.", "www."):
        if host.startswith(prefix):
            cands.append(host[len(prefix):])
    # de-dup, preserve order
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _fetch_with_tenant(host: str, tenant: str) -> list[dict] | None:
    url = f"https://{host}/api/apply/v2/jobs"
    out: list[dict] = []
    start = 0
    for _ in range(MAX_PAGES):
        data = get_json(
            url,
            params={"domain": tenant, "start": start, "num": PAGE, "sort_by": "relevance"},
        )
        if not isinstance(data, dict) or "positions" not in data:
            return None
        positions = data.get("positions") or []
        for j in positions:
            loc = j.get("location") or j.get("locations") or j.get("display_job_location")
            out.append(
                {
                    "title": (j.get("name") or j.get("title") or "").strip(),
                    "location": flatten_location(loc),
                }
            )
        count = data.get("count", 0)
        start += PAGE
        if start >= count or not positions or reached_threshold(out):
            break
    return out


def fetch(slug: str) -> list[dict]:
    """`slug` is the careers host (e.g. careers.bimbobakeriesusa.com)."""
    for tenant in _tenant_candidates(slug):
        result = _fetch_with_tenant(slug, tenant)
        if result is not None:
            return result
    return []


def verify(slug: str) -> bool:
    for tenant in _tenant_candidates(slug):
        try:
            if _fetch_with_tenant(slug, tenant) is not None:
                return True
        except Exception:
            continue
    return False
