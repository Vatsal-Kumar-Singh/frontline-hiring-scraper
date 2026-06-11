"""Indeed tier (Apify: kaix/indeed-scraper) — the cheap fallback for companies
with NO readable ATS signature.

Why: many 'no signature' companies still post every hourly opening to Indeed.
Indeed is searched by company NAME (not domain), at ~$0.00008/job — roughly 150x
cheaper than the career-site catch-all ($0.012/job) — so sweeping all unresolved
companies costs a few dollars. We use Indeed's `company:"..."` filter for a tight
employer match, then a local name-similarity guard drops jobs from look-alike
employers, and filter_roles() (caller side) keeps only frontline titles.

PAID + opt-in. Requires APIFY_TOKEN. Never emits a false zero: if Indeed returns
no matching jobs for a company, the company is left UNRESOLVED (not 0).
"""
from __future__ import annotations

import re

import budget
from . import apify_base

ACTOR = "kaix/indeed-scraper"
MAX_ITEMS = 50            # enough to confirm 20+; bounds per-company cost
# pipeline `since` (days) -> kaix fromDays enum ('', '1','3','7','14'); 14 is max.
_FROMDAYS = {"1": "1", "3": "3", "7": "7", "14": "14", "15": "14", "30": "14",
             "all": ""}

_SUFFIX = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|"
    r"group|holdings|enterprises|the|and|&|of|services|hospitality|management)\b",
    re.I)
_NONWORD = re.compile(r"[^a-z0-9\s]")


def fromdays(since: str) -> str:
    return _FROMDAYS.get(str(since), "7")


def _norm_name(name: str) -> set[str]:
    s = _NONWORD.sub(" ", (name or "").lower())
    s = _SUFFIX.sub(" ", s)
    return {t for t in s.split() if len(t) > 1}


_SEARCH_STRIP = re.compile(
    r"[,]?\s+(inc|inc\.|incorporated|llc|l\.l\.c\.?|ltd|ltd\.|limited|corp|"
    r"corp\.|corporation|co|co\.|lp|llp|pllc|plc)\.?\s*$", re.I)


def search_name(name: str) -> str:
    """Clean a company name for a job-board search: drop trailing legal-entity
    suffixes ('Rush Enterprises, Inc' -> 'Rush Enterprises') that break exact
    company search. Applied repeatedly for stacked suffixes ('X, Inc. LLC')."""
    s = (name or "").strip()
    for _ in range(3):
        new = _SEARCH_STRIP.sub("", s).strip().rstrip(",")
        if new == s:
            break
        s = new
    return s or (name or "").strip()


def company_matches(target: str, found: str) -> bool:
    """True if `found` (Indeed's employer name) plausibly IS `target`. Token-based
    so 'Splash Car Wash' matches 'Splash Car Wash, Detail & Oil' but not an
    unrelated employer that merely shares one generic word."""
    t, f = _norm_name(target), _norm_name(found)
    if not t or not f:
        return False
    common = t & f
    # all of the shorter name's tokens present, or strong overlap both ways
    return common == t or common == f or (
        len(common) >= 2 and len(common) / len(t | f) >= 0.5)


def _title_text(it: dict) -> str:
    t = it.get("title")
    if isinstance(t, dict):
        return t.get("text") or t.get("normalized") or ""
    return t or ""


def _company_name(it: dict) -> str:
    c = it.get("company")
    if isinstance(c, dict):
        return c.get("name") or c.get("displayName") or ""
    return c or ""


def _location(it: dict) -> str:
    loc = it.get("location")
    if isinstance(loc, dict):
        parts = [loc.get("city"), loc.get("state"), loc.get("formatted"),
                 loc.get("text")]
        return ", ".join(p for p in parts if p) or ""
    if isinstance(loc, list):
        return "; ".join(str(x) for x in loc)
    return str(loc or "")


def _external_urls(it: dict) -> list[str]:
    u = it.get("urls")
    if isinstance(u, dict):
        return [u.get("external") or "", u.get("apply") or ""]
    return []


def discover_ats(name: str, since: str = "7", probe: int = 10):
    """Cheap ATS discovery: pull a few Indeed jobs for `name`, read each job's real
    apply URL (urls.external) and detect the ATS platform+slug from it. Returns
    (platform, slug, ats_url) for the first READABLE (free-fetchable) ATS, else None.
    This turns a ~$0.0008 Indeed probe into a permanently cached FREE ATS read."""
    import fetchers
    from resolver import detect_signatures
    items = apify_base.run_actor(
        ACTOR,
        {"keyword": f'company:"{search_name(name)}"', "maxItems": int(probe),
         "fromDays": "", "sort": "date", "country": "US"},
    )
    budget.record_jobs(len(items), budget.COST_INDEED)
    readable = (fetchers.SUPPORTED - fetchers.LOCKED_DOWN - fetchers.GENERIC)
    for it in items:
        for url in _external_urls(it):
            if not url:
                continue
            for platform, slug, src in detect_signatures("", url):
                if platform in readable and slug:
                    return platform, slug, (src or url)
    return None


def fetch_company(name: str, since: str = "7", max_items: int = MAX_ITEMS):
    """Return (jobs, raw_count, found_names) for `name` on Indeed.
      jobs       = [{title, location, date_posted, company}] whose employer matches
      raw_count  = total jobs Indeed returned BEFORE the name-match filter
      found_names= distinct employer names Indeed returned (for diagnosing misses)
    raw_count==0  -> Indeed has nothing for that name (board/name doesn't exist)
    raw_count>0 & jobs==[] -> only look-alike employers (name likely wrong)
    Raises on actor error (caller treats as unresolved — never a false zero)."""
    # Query BOTH the raw name and the suffix-cleaned name (when they differ) and
    # MERGE — cleaning usually helps but can occasionally break a match, so trying
    # both can only gain companies, never lose them.
    queries = [name.strip()]
    cleaned = search_name(name)
    if cleaned and cleaned.lower() != name.strip().lower():
        queries.append(cleaned)
    items, seen = [], set()
    for q in queries:
        raw = apify_base.run_actor(
            ACTOR,
            {"keyword": f'company:"{q}"', "maxItems": int(max_items),
             "fromDays": fromdays(since), "sort": "date", "country": "US"},
        )
        budget.record_jobs(len(raw), budget.COST_INDEED)
        for it in raw:
            jid = it.get("id") or it.get("refNum") or \
                f"{_title_text(it)}|{_company_name(it)}"
            if jid not in seen:
                seen.add(jid)
                items.append(it)
    out, found_names = [], set()
    for it in items:
        found = _company_name(it)
        if found:
            found_names.add(found)
        if found and not company_matches(name, found):
            continue  # look-alike employer — drop
        out.append({
            "title": _title_text(it),
            "location": _location(it),
            "date_posted": "",
            "company": found,
        })
    return out, len(items), sorted(found_names)
