"""LinkedIn tier (Apify: worldunboxer/rapid-linkedin-scraper) — the escalation
for companies Indeed can't find. LinkedIn's coverage differs from Indeed's, so it
recovers employers missing from Indeed (validated: Paragon Hotel, a no-Indeed
company, returned 12 frontline jobs).

Searched by the actor's dedicated `company_names` filter at ~$0.0009/job — ~13x
the Indeed price but still ~13x cheaper than the career-site catch-all. So we run
it ONLY on Indeed-misses, keeping spend tiny. Same contract as indeed.py:
company-name guard + never a false zero (no match -> stays unresolved).
"""
from __future__ import annotations

import budget
from . import apify_base
from .indeed import company_matches, search_name  # shared helpers

ACTOR = "worldunboxer/rapid-linkedin-scraper"
MAX_ITEMS = 50
# pipeline `since` (days) -> actor posted_within enum
_POSTED = {"1": "Past 24 hours", "3": "Past Week", "7": "Past Week",
           "14": "Past Month", "15": "Past Month", "30": "Past Month",
           "all": "Any Time"}


def posted_within(since: str) -> str:
    return _POSTED.get(str(since), "Past Week")


def _norm(it: dict) -> dict:
    return {
        "title": it.get("job_title") or it.get("title") or "",
        "location": it.get("location") or "",
        "date_posted": it.get("time_posted") or "",
        "company": it.get("company_name") or it.get("companyName") or "",
    }


def fetch_company(name: str, since: str = "7", max_items: int = MAX_ITEMS, row=None):
    """Return (jobs, raw_count, found_names) — same contract as indeed.fetch_company.
    Searches the raw name and the suffix-cleaned name. (We deliberately do NOT add
    the LinkedIn vanity name here: at $0.0009/job it generated mostly mismatched,
    paid-for noise on generic hospitality names. The vanity alias lives on the
    near-free Indeed tier instead.)"""
    terms = [name.strip()]
    cleaned = search_name(name)
    if cleaned and cleaned.lower() != name.strip().lower():
        terms.append(cleaned)
    items, seen = [], set()
    for q in terms:
        raw = apify_base.run_actor(
            ACTOR,
            {"company_names": [q], "jobs_entries": int(max_items),
             "posted_within": posted_within(since)},
        )
        budget.record_jobs(len(raw), budget.COST_LINKEDIN)
        for it in raw:
            jid = it.get("job_id") or it.get("job_url") or \
                f"{it.get('job_title')}|{it.get('company_name')}"
            if jid not in seen:
                seen.add(jid)
                items.append(it)
    out, found = [], set()
    for it in items:
        rec = _norm(it)
        if rec["company"]:
            found.add(rec["company"])
        if rec["company"] and not company_matches(name, rec["company"]):
            continue
        out.append(rec)
    return out, len(items), sorted(found)
