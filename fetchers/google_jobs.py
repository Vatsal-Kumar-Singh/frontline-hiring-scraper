"""Google Jobs tier (Apify: igview-owner/google-jobs-scraper) — broadest coverage
for the long tail (a company's own JobPosting schema, niche/local boards) that
Indeed and LinkedIn miss.

EXPENSIVE (~$0.02/result) and IMPRECISE: a name query returns loosely-related
postings from OTHER employers too, so we filter hard by employer name and only
ever run it on a small, high-value subset (see pipeline._google_fill). Same
contract as indeed/linkedin: employer-matched, never a false zero.
"""
from __future__ import annotations

import budget
from . import apify_base
from .indeed import company_matches, vanity_from_row

ACTOR = "igview-owner/google-jobs-scraper"
# pipeline `since` -> actor datePosted
_DATE = {"1": "today", "3": "3days", "7": "week", "14": "month", "15": "month",
         "30": "month", "all": ""}


def _date(since: str) -> str:
    return _DATE.get(str(since), "month")


def fetch_company(name: str, since: str = "7", row=None):
    """Return (jobs, raw_count, found_names). Queries one Google Jobs page and
    keeps only postings whose employer matches the company (by name or its
    LinkedIn vanity name)."""
    items = apify_base.run_actor(
        ACTOR,
        {"query": name, "page": 1, "country": "us", "datePosted": _date(since)},
    )
    budget.record_jobs(len(items), budget.COST_GOOGLE)
    vanity = vanity_from_row(row)
    match_terms = [name] + ([vanity] if vanity else [])
    out, found = [], set()
    for it in items:
        emp = (it.get("employerName") or it.get("companyName") or "").strip()
        title = (it.get("jobTitle") or it.get("title") or "").strip()
        if emp:
            found.add(emp)
        if emp and not any(company_matches(t, emp) for t in match_terms):
            continue
        out.append({"title": title, "location": it.get("location", ""),
                    "date_posted": "", "company": emp})
    return out, len(items), sorted(found)
