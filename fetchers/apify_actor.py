"""Apify adapters for detect-only / WAF'd platforms — batch-by-domain model.

The fantastic-jobs "*-jobs-api" actors are job DATABASES queried by company
domain (`domainFilter`), not per-page scrapers. So instead of one run per
company we run an actor ONCE for a batch of domains and group the results by
`domain_derived`. Cheap (only matching jobs are billed) and robust (catches
boards the careers-page resolver missed, e.g. iCIMS tenants behind WAF).

Two tiers (pipeline runs dedicated first, catch-all to mop up):
  Dedicated, FULL boards, accurate — paradox / adp / icims. No server-side date
    filter, so the date window is applied client-side (we return date_posted).
  Catch-all aggregator — covers many sources (paycom, dayforce, paylocity, ...).
    Supports SERVER-SIDE date filter (datePostedAfter / timeRange), so the date
    window cuts what you pay for. Lower confidence: it samples/de-dupes.

Every chunk is guarded by budget.can_continue() so a run can't blow the monthly
cap. Requires APIFY_TOKEN (see apify_base).
"""
from __future__ import annotations

from urllib.parse import urlparse

import budget
import config
import matcher
from .base import flatten_location
from . import apify_base

# Dedicated per-source actors. platform -> actor. Order = precedence.
ACTORS: dict[str, str] = {
    "paradox": "fantastic-jobs/paradox-ai-jobs-api",
    "adp": "fantastic-jobs/adp-jobs-api",
    "icims": "fantastic-jobs/icims-jobs-api",
}

# Catch-all aggregator (lower confidence; server-side date filter supported).
CATCHALL_ACTOR = "fantastic-jobs/career-site-job-listing-api"

# COST CONTROL — we query ONE company per actor run (domainFilter = [one domain]).
# The actor enforces a HARD minimum `limit` of 200, so we can't cap returns at ~35;
# instead the cost lever is `titleSearch` (only frontline jobs are returned/billed)
# plus the date window on the catch-all. Worst case per company = 200 jobs, so the
# budget guard refuses to START a company it can't fully afford at that worst case
# => total spend can NEVER exceed the cap (no overshoot).
PER_COMPANY_LIMIT = 200  # actor minimum; also the worst-case jobs billed per company


def per_company_limit() -> int:
    return PER_COMPANY_LIMIT


def _title_search() -> list[str]:
    """Frontline vocabulary (roles.txt) used as a server-side title filter so the
    actor only RETURNS — and only BILLS — frontline-ish jobs. Local filter_roles()
    then applies the exclusions, so the final count matches a full-board scan while
    we pay for a fraction of the jobs."""
    return list(matcher.load_roles())

_TWO_LEVEL = {
    "co.uk", "org.uk", "ac.uk", "com.au", "net.au", "org.au", "co.nz", "co.in",
    "com.br", "co.za", "com.sg", "com.mx", "co.jp", "com.tr",
}


def enabled_platforms() -> list[str]:
    return list(ACTORS) if apify_base.enabled() else []


def catchall_enabled() -> bool:
    return apify_base.enabled()


def registrable_domain(value: str) -> str:
    """Reduce a host or URL to eTLD+1 (lowercase)."""
    if not value:
        return ""
    v = value.strip().lower()
    if "//" in v or "/" in v:
        host = urlparse(v if "//" in v else "//" + v).hostname or v
    else:
        host = v
    host = host.split("/")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    last2 = ".".join(parts[-2:])
    if last2 in _TWO_LEVEL and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last2


def _norm_item(it: dict) -> dict:
    title = (it.get("title") or "").strip()
    loc = (
        it.get("locations_derived")
        or it.get("cities_derived")
        or it.get("locations_raw")
    )
    return {
        "title": title,
        "location": flatten_location(loc),
        "date_posted": (it.get("date_posted") or "")[:10],  # YYYY-MM-DD
    }


def _run_per_company(actor, domains, extra_input, cost_per_job):
    """Query ONE company per actor run (domainFilter=[domain]) with a hard
    per-company `limit` and a frontline titleSearch. Checks the budget before
    every company so overshoot is at most one company's worth of jobs.
    Returns (items, truncated, budget_hit)."""
    uniq = sorted({d for d in domains if d})
    cap = per_company_limit()
    worst_case_cost = cap * cost_per_job  # 200 jobs * price; never overshoot this
    truncated = False
    budget_hit = False
    out_items = []
    for dom in uniq:
        # Hard guarantee: don't START a company unless we can afford its worst case.
        if budget.remaining() < worst_case_cost:
            budget_hit = True
            break
        items = apify_base.run_actor(
            actor,
            {"domainFilter": [dom], "limit": cap,
             "titleSearch": _title_search(),
             "includeCompanyDetails": False, "includeLinkedIn": False,
             **extra_input},
        )
        budget.record_jobs(len(items), cost_per_job)
        if len(items) >= cap:
            truncated = True   # company has AT LEAST `cap` frontline-ish roles
        out_items.extend(items)
    return out_items, truncated, budget_hit


def batch_fetch(platform: str, domains: list[str]):
    """Dedicated actor, one company per run (capped). Returns
    (by_domain, truncated, budget_hit) where
    by_domain[reg_domain] = [ {title, location, date_posted}, ... ]"""
    items, truncated, budget_hit = _run_per_company(
        ACTORS[platform], domains, {}, budget.COST_DEDICATED)
    by_domain: dict[str, list[dict]] = {}
    for it in items:
        d = registrable_domain(it.get("domain_derived") or "")
        if d:
            by_domain.setdefault(d, []).append(_norm_item(it))
    return by_domain, truncated, budget_hit


def batch_fetch_catchall(domains, date_after=None, all_time=False):
    """Catch-all aggregator, one company per run (capped) with a SERVER-SIDE date
    filter. date_after: 'YYYY-MM-DD' (jobs on/after). all_time: backfill active.
    Returns (by_domain, truncated, budget_hit) where
    by_domain[reg_domain] = {"jobs": [...], "source": <board>}."""
    extra = {}
    if date_after:
        extra["datePostedAfter"] = date_after
    elif all_time:
        extra["timeRange"] = "6m"
    items, truncated, budget_hit = _run_per_company(
        CATCHALL_ACTOR, domains, extra, budget.COST_CATCHALL)
    by_domain: dict[str, dict] = {}
    for it in items:
        d = registrable_domain(it.get("domain_derived") or "")
        if not d:
            continue
        entry = by_domain.setdefault(d, {"jobs": [], "_sources": {}})
        entry["jobs"].append(_norm_item(it))
        src = (it.get("source") or it.get("source_type") or "aggregator").lower()
        entry["_sources"][src] = entry["_sources"].get(src, 0) + 1
    for d, entry in by_domain.items():
        srcs = entry.pop("_sources")
        entry["source"] = max(srcs, key=srcs.get) if srcs else "aggregator"
    return by_domain, truncated, budget_hit
