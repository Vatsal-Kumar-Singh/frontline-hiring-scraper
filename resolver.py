"""Stage 1 — resolve a company website to (ats_platform, ats_slug), verified.

Order (SPEC §5): cache -> careers-page fetch + signature detection (follow
redirects) -> Apollo Technologies hint reorders the guesses -> verify against
the live endpoint -> cache. Search/headless fallbacks are stubbed (Phase 1+).

Hard rule: never guess a slug. Every accepted slug is read from a page/redirect
AND verified by hitting the platform endpoint.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

import fetchers
from fetchers import jsonld
from fetchers.base import USER_AGENT, strip_html

CACHE_PATH = Path(__file__).with_name("cache") / "slugs.json"
CACHE_TTL_DAYS = 30
# Curated permanent discoveries (from the web-agent / manual). Checked before
# live resolution; verified each run but never expires. Committed to source.
SEEDS_PATH = Path(__file__).with_name("seeds.json")

# Careers-page URL candidates appended to the company root. Trimmed to the
# highest-yield few for speed — the homepage scan discovers non-standard careers
# links (e.g. /join-team) anyway, so brute-forcing every path isn't needed.
CAREERS_PATHS = [
    "/careers", "/careers/", "/career", "/jobs", "/join-us",
    "/work-with-us", "/opportunities", "/employment",
]

# --- composite-slug builders (multi-group matches -> one "a|b|c" slug) --------
def _b_workday(m):
    site = m.group("site")
    if site.lower() in {"wday", "en", "job"}:
        return None
    return f"{m.group('tenant')}|{m.group('dc')}|{site}"


def _b_cornerstone(m):
    return f"{m.group('tenant')}|{m.group('site')}"


def _b_ultipro(m):
    return f"{m.group('host')}|{m.group('tenant')}|{m.group('guid')}"


def _b_taleo(m):
    # capture sub+site from host/path; pull org & cws from the matched URL chunk
    s = m.group(0)
    org = re.search(r"org=([A-Za-z0-9]+)", s, re.I)
    cws = re.search(r"cws=(\d+)", s, re.I)
    if org and cws:
        return f"{m.group('sub')}|{m.group('site')}|{org.group(1)}|{cws.group(1)}"
    return None


# ATS signature patterns: (platform, compiled_pattern, builder_or_None).
# builder(m) -> slug string (or None to skip). When builder is None: use named
# group `s` if present, else the platform name as a detect-only placeholder.
# Ordered roughly by how clean/unambiguous the signal is.
SIGNATURES = [
    # ---- fetchable, hosted ATS (high confidence) ----
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/(?P<s>[a-z0-9\-]+)", re.I), None),
    ("greenhouse", re.compile(r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?(?P<s>[a-z0-9\-]+)", re.I), None),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board\?for=(?P<s>[a-z0-9\-]+)", re.I), None),
    ("lever", re.compile(r"jobs\.lever\.co/(?P<s>[a-z0-9\-]+)", re.I), None),
    ("ashby", re.compile(r"(?:jobs\.ashbyhq\.com|api\.ashbyhq\.com/posting-api/job-board)/(?P<s>[a-z0-9\-]+)", re.I), None),
    ("workable", re.compile(r"apply\.workable\.com/(?:api/v3/accounts/)?(?P<s>[a-z0-9\-]+)", re.I), None),
    ("workable", re.compile(r"whr_embed\(\s*(?P<s>\d+)", re.I), None),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/(?P<s>[A-Za-z0-9\-]+)", re.I), None),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/(?P<s>[A-Za-z0-9\-]+)", re.I), None),
    ("icims", re.compile(r"(?:careers-)?(?P<s>[a-z0-9\-]+)\.icims\.com", re.I), None),
    ("workday", re.compile(
        r"(?P<tenant>[a-z0-9][a-z0-9\-]*)\.(?P<dc>wd\d+)\.myworkdayjobs\.com/"
        r"(?:[a-z]{2}-[A-Za-z]{2}/)?(?P<site>[A-Za-z0-9_]+)", re.I), _b_workday),
    ("cornerstone", re.compile(
        r"(?P<tenant>[a-z0-9\-]+)\.csod\.com/ux/ats/careersite/(?P<site>\d+)", re.I), _b_cornerstone),
    ("ultipro", re.compile(
        r"(?P<host>recruiting\d?\.ultipro\.com)/(?P<tenant>[A-Za-z0-9]+)/JobBoard/"
        r"(?P<guid>[0-9a-fA-F\-]{36})", re.I), _b_ultipro),
    ("jazzhr", re.compile(r"(?P<s>[a-z0-9\-]+)\.applytojob\.com", re.I), None),
    ("easyapply", re.compile(r"(?P<s>[a-z0-9\-]+)\.easyapply\.co", re.I), None),
    ("taleo", re.compile(
        r"(?P<sub>[a-z0-9]+)\.tbe\.taleo\.net/(?P<site>[a-z0-9]+)/[^\s\"']*", re.I), _b_taleo),
    ("culinaryagents", re.compile(r"culinaryagents\.com/groups/(?P<s>[a-z0-9\-]+)", re.I), None),
    # detect-only (JS/SPA -> web-agent or Apify):
    ("jobvite", re.compile(r"jobs\.jobvite\.com/(?P<s>[a-z0-9\-]+)", re.I), None),
    ("higherme", re.compile(r"(?:app\.)?higherme\.com", re.I), None),
    ("clearcompany", re.compile(r"(?P<s>[a-z0-9\-]+)\.clearcompany\.com", re.I), None),

    # ---- detect-only: needs headless / Apify (see fetchers.LOCKED_DOWN) ----
    ("dayforce", re.compile(
        r"dayforcehcm\.com/(?:CandidatePortal/)?(?:[a-z]{2}-[a-z]{2}/)?(?P<s>[A-Za-z0-9_]+)", re.I), None),
    ("successfactors", re.compile(r"successfactors\.(?:com|eu)/[^\"'\s]*?[?&]company=(?P<s>[A-Za-z0-9_]+)", re.I), None),
    ("successfactors", re.compile(r"career\d*\.successfactors\.(?:com|eu)", re.I), None),
    ("paycom", re.compile(r"paycomonline\.net/[^\"'\s]*?(?:clientkey=|portal/)(?P<s>[0-9A-Fa-f]{32})", re.I), None),
    ("adp", re.compile(r"myjobs\.adp\.com/(?P<s>[a-z0-9]+)/", re.I), None),
    ("adp", re.compile(r"workforcenow\.adp\.com", re.I), None),
    ("brassring", re.compile(r"brassring\.com/[^\"'\s]*?partnerid=(?P<s>\d+)", re.I), None),
    ("brassring", re.compile(r"sjobs\.brassring\.com", re.I), None),
    ("avature", re.compile(r"(?P<s>[a-z0-9\-]+)\.avature\.net", re.I), None),
    ("hirebridge", re.compile(r"hirebridge\.com/[^\"'\s]*?[?&]cid=(?P<s>\d+)", re.I), None),
    ("hirebridge", re.compile(r"(?:jobs\.)?hirebridge\.com", re.I), None),
    ("harri", re.compile(r"harri\.com/(?P<s>[A-Za-z0-9\-]+)", re.I), None),
    ("paradox", re.compile(r"(?P<s>[a-z0-9\-]+)\.olivia\.paradox\.ai", re.I), None),
    ("paradox", re.compile(r"\bparadox\.ai\b", re.I), None),
    ("talentreef", re.compile(r"(?P<s>[a-z0-9\-]+)\.talentreef\.com", re.I), None),
    ("talentreef", re.compile(r"(?:recruiting\.talentreef\.com|jobappnetwork\.com)", re.I), None),
    ("workstream", re.compile(r"(?:app\.)?workstream\.(?:us|io)", re.I), None),
]

# Per-platform reserved tokens that are infrastructure paths, not company slugs.
_RESERVED = {
    "workable": {"api", "www", "apply", "j", "i", "assets", "cdn"},
    "dayforce": {"candidateportal", "www", "jobs", "en", "client"},
    "icims": {"www", "careers", "jobs"},
    "jazzhr": {"www", "app"},
    "easyapply": {"www", "app"},
    "clearcompany": {"www", "app", "cc-client-cdn"},
    "harri": {"www", "about", "pricing", "login", "blog", "contact", "product", "demo"},
    "avature": {"www"},
}

EIGHTFOLD_MARKERS = re.compile(
    r"eightfold|EFSmartApply|/api/apply/v2/jobs|pcsdomain", re.I
)
# Generic schema.org JobPosting embedded in a custom ("native") career page.
JSONLD_MARKERS = re.compile(r'application/ld\+json', re.I)
JOBPOSTING_MARKER = re.compile(r'"@type"\s*:\s*\[?\s*"JobPosting"', re.I)

# Links on a homepage that likely lead to the careers/ATS page. Matches the
# common non-standard paths (/join-team, /work-for-us, ...) the fixed candidate
# list misses.
_CAREERS_LINK = re.compile(
    r"career|join|hiring|opportunit|employ|/jobs|work-with|work-for|work-at",
    re.I,
)
MAX_FETCH = 10  # bound total HTTP fetches per company
# Hard cap on HTML fed to ANY regex/parse. A few pages are multi-MB (huge inline
# JSON-LD, generated markup) and pin a CPU core for minutes — and since regex
# matching holds the GIL, ONE such page freezes the whole concurrent run. Careers/
# home pages with an ATS link are far smaller than this, so capping is safe.
TEXT_CAP = 300_000


def _careers_links(html: str, base_url: str, host: str) -> list[str]:
    """Same-host hrefs that look like careers/jobs links, absolutized."""
    out, seen = [], set()
    for href in re.findall(r'href=["\']([^"\']+)["\']', html[:TEXT_CAP], re.I):
        if not _CAREERS_LINK.search(href):
            continue
        absolute = urljoin(base_url, href.strip())
        h = (urlparse(absolute).hostname or "").lower()
        h = h[4:] if h.startswith("www.") else h  # literal prefix, not lstrip charset
        if host not in h:  # stay on the company's own domain
            continue
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- cache --------
def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


@lru_cache(maxsize=1)
def load_seeds() -> dict:
    """Curated permanent discoveries: {host: {platform, slug, ats_url}}."""
    if SEEDS_PATH.exists():
        try:
            return json.loads(SEEDS_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def _is_fresh(entry: dict) -> bool:
    ts = entry.get("verified_at")
    if not ts:
        return False
    try:
        when = datetime.fromisoformat(ts)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).days < CACHE_TTL_DAYS


# ------------------------------------------------------------ url helpers ------
def normalize_website(website: str) -> str | None:
    """-> bare registrable host, lowercased (no scheme, no www, no path)."""
    if not website or not str(website).strip():
        return None
    w = str(website).strip()
    if "//" not in w:
        w = "https://" + w
    host = (urlparse(w).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def candidate_urls(host: str) -> list[str]:
    roots = [f"https://{host}", f"https://www.{host}"]
    urls = list(roots)  # homepage(s) — footer often links to careers/ATS
    for root in roots:
        for path in CAREERS_PATHS:
            urls.append(root + path)
    # careers./jobs. subdomains
    urls.append(f"https://careers.{host}")
    urls.append(f"https://jobs.{host}")
    # de-dup preserving order
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# --------------------------------------------------------- signature scan ------
def detect_signatures(text: str, final_url: str = "") -> list[tuple[str, str, str]]:
    """Scan HTML + final URL for ATS signatures. Returns ordered, de-duped
    (platform, slug) candidates."""
    text = (text or "")[:TEXT_CAP]  # bound ALL regex work below (GIL-safe)
    haystack = f"{final_url}\n{text}"
    found: list[tuple[str, str, str]] = []  # (platform, slug, source_url)
    seen = set()
    for platform, pat, builder in SIGNATURES:
        for m in pat.finditer(haystack):
            if builder is not None:
                slug = builder(m)
            elif "s" in pat.groupindex:
                slug = m.group("s")
            else:
                slug = platform  # marker-only detect (no extractable id)
            if not slug:
                continue
            if slug.lower() in _RESERVED.get(platform, set()):
                continue
            key = (platform, slug.lower())
            if key not in seen:
                seen.add(key)
                found.append((platform, slug, final_url))

    # Eightfold runs on the company's own domain — detect by markers, slug=host.
    if EIGHTFOLD_MARKERS.search(haystack):
        host = urlparse(final_url).hostname if final_url else ""
        if host:
            key = ("eightfold", host.lower())
            if key not in seen:
                seen.add(key)
                found.append(("eightfold", host, final_url))

    # Generic JobPosting JSON-LD on a custom career page — slug = the page URL.
    if final_url and JSONLD_MARKERS.search(text) and JOBPOSTING_MARKER.search(text):
        key = ("jsonld", final_url.lower())
        if key not in seen:
            seen.add(key)
            found.append(("jsonld", final_url, final_url))
    return found


def _hint_platform(technologies: str | None) -> str | None:
    if not technologies:
        return None
    t = technologies.lower()
    for p in ("workable", "greenhouse", "lever", "ashby", "smartrecruiters",
              "eightfold", "icims", "workday", "cornerstone", "ultipro", "jazzhr",
              "dayforce", "successfactors", "paycom", "adp", "brassring", "avature",
              "hirebridge", "harri", "paradox", "talentreef", "workstream"):
        if p in t:
            return p
    if "ashbyhq" in t:
        return "ashby"
    if "ultipro" in t or "ukg" in t:
        return "ultipro"
    if "csod" in t:
        return "cornerstone"
    if "myworkday" in t:
        return "workday"
    if "jazz" in t:
        return "jazzhr"
    return None


def _rank(platform: str) -> int:
    """0 = real hosted ATS (fetch now), 1 = generic/own-domain fallback
    (eightfold/jsonld), 2 = detect-only (needs headless)."""
    if platform in fetchers.LOCKED_DOWN:
        return 2
    if platform in fetchers.GENERIC:
        return 1
    return 0


def _reorder(cands: list[tuple[str, str]], hint: str | None) -> list[tuple[str, str]]:
    """Hinted platform first, then real hosted ATS, then generic fallbacks, then
    detect-only — a fetchable count beats a deferred one when both are present."""
    return sorted(cands, key=lambda c: (0 if hint and c[0] == hint else 1, _rank(c[0])))


# --------------------------------------------------------------- fetch ---------
def _fetch(url: str, timeout: int = 4):
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    return requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)


# -------------------------------------------------------------- resolve --------
def _sitemap_careers_urls(host: str, get_page, limit: int = 6) -> list[str]:
    """Discover careers/jobs pages from the site's sitemap.xml — catches the
    non-standard paths a fixed candidate list misses (common on small/custom
    hospitality sites). Bounded to a few sitemap fetches; never raises."""
    found: list[str] = []
    seen: set[str] = set()
    sitemaps = [f"https://{host}/sitemap.xml", f"https://{host}/sitemap_index.xml"]
    child_budget = 2  # follow at most 2 nested sitemaps
    while sitemaps and len(found) < limit:
        sm = sitemaps.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        page = get_page(sm)
        if not page:
            continue
        for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", page[0][:TEXT_CAP], re.I):
            if u.lower().endswith(".xml") and child_budget > 0 and u not in seen:
                sitemaps.append(u)
                child_budget -= 1
            elif _CAREERS_LINK.search(u):
                h = (urlparse(u).hostname or "").lower()
                h = h[4:] if h.startswith("www.") else h
                if host in h and u not in found:
                    found.append(u)
                    if len(found) >= limit:
                        break
    return found


def _scan_pages(host: str, get_page):
    """Walk candidate + discovered careers URLs via get_page(url) -> (text,
    final_url) | None. Returns (all_candidates, any_page)."""
    all_candidates: list[tuple[str, str, str]] = []
    seen_cand = set()
    any_page = False
    cand = candidate_urls(host)
    # homepage first, then sitemap-confirmed careers pages, then guessed paths
    queue = cand[:1] + _sitemap_careers_urls(host, get_page) + cand[1:]
    visited: set[str] = set()
    fetches = 0
    while queue and fetches < MAX_FETCH:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        page = get_page(url)
        if not page:
            continue
        text, final_url = page
        fetches += 1
        any_page = True
        for cand in detect_signatures(text, final_url):
            ckey = (cand[0], cand[1].lower())  # dedupe on (platform, slug)
            if ckey not in seen_cand:
                seen_cand.add(ckey)
                all_candidates.append(cand)
        # A real hosted-ATS signature is unambiguous — stop. Generic/detect-only
        # are lower confidence, so keep scanning to prefer a real ATS.
        if any(_rank(c[0]) == 0 for c in all_candidates):
            break
        discovered = [
            d for d in _careers_links(text, final_url, host) if d not in visited
        ]
        queue = discovered + queue
    return all_candidates, any_page


def _collapse_multiboard(cands):
    """For EVERY platform, merge multiple detected boards of the same platform into
    ONE candidate with a MULTI_SEP-joined slug, so a company with several boards
    (Harri NY+NJ, RAM's 28 EasyApply portals, two Workday sites, ...) is summed
    instead of counting just the first. Single-board platforms pass through."""
    grouped: dict[str, dict] = {}
    for platform, slug, src in cands:
        g = grouped.setdefault(platform, {"slugs": [], "src": src})
        if slug not in g["slugs"]:
            g["slugs"].append(slug)
    out = []
    for platform, g in grouped.items():
        slug = (g["slugs"][0] if len(g["slugs"]) == 1
                else fetchers.MULTI_SEP.join(sorted(g["slugs"])))
        out.append((platform, slug, g["src"]))
    return out


def _finalize(host, all_candidates, any_page, hint, cache) -> dict:
    """Reorder candidates, verify each fetchable one, cache + return. Never a
    false zero: unresolved keeps the best candidate for coverage."""
    if not all_candidates:
        reason = "no_ats_signature" if any_page else "no_careers_page"
        return {"platform": "", "slug": "", "ats_url": "",
                "status": "unresolved", "reason": reason}
    all_candidates = _collapse_multiboard(all_candidates)
    ordered = _reorder(all_candidates, hint)
    for platform, slug, src in ordered:
        if platform in fetchers.LOCKED_DOWN:
            continue  # detect-only — can't fetch a count with plain HTTP
        if fetchers.verify(platform, slug):
            cache[host] = {"platform": platform, "slug": slug, "ats_url": src,
                           "verified_at": now_iso()}
            return {"platform": platform, "slug": slug, "ats_url": src,
                    "status": "ok", "reason": "verified"}
    p, s, src = ordered[0]
    if p in fetchers.LOCKED_DOWN:
        reason = "needs_headless"
    elif all(c[0] == "eightfold" for c in all_candidates):
        reason = "eightfold_api_locked"
    else:
        reason = "slug_unverified"
    return {"platform": p, "slug": s, "ats_url": src,
            "status": "unresolved", "reason": reason}


def resolve(website: str, technologies: str | None = None, cache: dict | None = None) -> dict:
    """Return {platform, slug, ats_url, status, reason}. On ok, the slug is
    verified against the live endpoint."""
    cache = cache if cache is not None else {}
    host = normalize_website(website)
    if not host:
        return {"platform": "", "slug": "", "ats_url": "",
                "status": "unresolved", "reason": "no_website"}

    entry = cache.get(host)
    if entry and _is_fresh(entry):
        return {"platform": entry["platform"], "slug": entry["slug"],
                "ats_url": entry.get("ats_url", ""), "status": "ok", "reason": "cache"}

    # Permanent curated discovery (web-agent/manual) — verify, then trust.
    seed = load_seeds().get(host)
    if seed and seed.get("platform") in fetchers.SUPPORTED:
        if fetchers.verify(seed["platform"], seed["slug"]):
            cache[host] = {"platform": seed["platform"], "slug": seed["slug"],
                           "ats_url": seed.get("ats_url", ""), "verified_at": now_iso()}
            return {"platform": seed["platform"], "slug": seed["slug"],
                    "ats_url": seed.get("ats_url", ""), "status": "ok", "reason": "seed"}

    def get_page(url):
        try:
            r = _fetch(url)
        except requests.RequestException:
            return None
        if r.status_code >= 400:
            return None
        return (r.text, str(r.url))

    all_candidates, any_page = _scan_pages(host, get_page)
    return _finalize(host, all_candidates, any_page, _hint_platform(technologies), cache)


# Focused URL set for the (slower) headless tier — homepage + the few highest-hit
# careers paths, incl. /career (singular).
def headless_candidate_urls(host: str) -> list[str]:
    # Kept small for speed — homepage footer usually links the ATS, and /careers
    # /career cover the rest. More pages come from discovered careers links.
    return [
        f"https://{host}",
        f"https://{host}/careers",
    ]


def _headless_targets(website):
    """Pick the URLs worth rendering for one company: the standard careers paths
    PLUS any careers links discovered in the static homepage (that's where a
    JS-injected ATS widget usually lives). One cheap static fetch."""
    host = normalize_website(website)
    if not host:
        return None
    urls = list(headless_candidate_urls(host))
    try:
        r = _fetch(f"https://{host}")
        if r.status_code < 400:
            for link in _careers_links(r.text, str(r.url), host):
                if link not in urls:
                    urls.append(link)
    except requests.RequestException:
        pass
    return (website, host, urls[:3])  # cap renders per company (homepage + careers + 1 discovered)


# --- Option B: generic job extraction from a rendered custom careers page ------
_JOB_HREF = re.compile(
    r'<a[^>]+href="[^"]*(?:/job|/jobs/|/position|/opening|/vacanc|/apply|/careers/)'
    r'[^"]*"[^>]*>(.*?)</a>', re.I | re.S)


def extract_generic_jobs(html: str) -> list[dict]:
    """Best-effort job extraction from an arbitrary rendered careers page (no
    known ATS): schema.org JobPosting + job-detail links ONLY. We deliberately do
    NOT harvest bare headings — restaurant/brand names (e.g. 'Italian Farmhouse',
    'Town Docks') trip role-stems (farm/dock) and create false positives. Links
    pointing at a job/apply URL are real postings; the matcher gates them too.
    De-duped by title."""
    html = (html or "")[:TEXT_CAP]  # bound regex/JSON-LD parse (GIL-safe)
    out: list[dict] = []
    seen: set[str] = set()

    def add(title, location=""):
        t = (title or "").strip()
        if 3 <= len(t) <= 90 and t.lower() not in seen:
            seen.add(t.lower())
            out.append({"title": t, "location": location})

    for j in jsonld.extract(html):       # structured JobPosting blocks (highest trust)
        add(j.get("title", ""), j.get("location", ""))
    for m in _JOB_HREF.finditer(html):   # anchors pointing at a job/apply URL
        add(strip_html(m.group(1)))
    return out


def resolve_headless_batch(websites, cache: dict, concurrency: int = 10, on_page=None) -> dict:
    """FREE JS-rendering fallback: render each unresolved company's careers pages
    (standard paths + statically-discovered careers links) with a headless
    browser, re-scan with the same signatures, verify, cache. Returns
    {website: result-dict}. on_page(host, url) fires per rendered page (progress).
    No-op (all unresolved) if Playwright isn't installed."""
    import headless
    from concurrent.futures import ThreadPoolExecutor

    hosts = {}
    tasks = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for res in ex.map(_headless_targets, websites):
            if not res:
                continue
            website, host, urls = res
            hosts[website] = host
            for u in urls:
                tasks.append((host, u))

    rendered = headless.render_many(tasks, concurrency=concurrency, on_page=on_page)

    results = {}
    for w, host in hosts.items():
        pages = rendered.get(host, [])
        all_candidates: list[tuple[str, str, str]] = []
        seen = set()
        for final_url, html in pages:
            for cand in detect_signatures(html, final_url):
                ckey = (cand[0], cand[1].lower())
                if ckey not in seen:
                    seen.add(ckey)
                    all_candidates.append(cand)
        res = _finalize(host, all_candidates, bool(pages), None, cache)
        # Option B: if no fetchable ATS resolved, harvest generic job candidates
        # from the rendered HTML (the matcher gates these downstream).
        if res["status"] != "ok" and pages:
            combined = "\n".join(html for _, html in pages)
            generic = extract_generic_jobs(combined)
            if generic:
                res["generic_jobs"] = generic
        results[w] = res
    return results
