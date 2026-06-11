# Frontline-Hiring Signal Scraper

## What this project does
Takes a list of companies (an Apollo export) and outputs, **per company**:
- `frontline_role_count` — how many open FRONTLINE / hourly roles they have right now
- `frontline_roles` — the list of those open roles (`title | location`)

This is a sales-qualification signal for **Nova** (AI phone-screening for high-volume
hourly hiring). A high frontline-role count = strong Nova ICP. The count is the
deliverable; tiering happens manually downstream, so there is **no LLM in the data path**.

## Read these first
- `@SPEC.md` — full spec: architecture, resolver design, fetcher endpoints, matching
  engine, acceptance criteria, known test companies.
- `roles.txt` — the frontline role list, one term per line. The matcher loads this at
  runtime. **Do not hardcode roles in source** — read the file.

## Architecture (4 stages)
1. **Resolver** — company → (`ats_platform`, `ats_slug`). The hard part. *Read* the slug
   from the company's careers page; never guess it.
2. **Fetcher** — pull open roles from the ATS's public JSON. Plain HTTP; no Apify needed
   for the main platforms.
3. **Filter** — keep only frontline roles (word-prefix match against `roles.txt`, minus an
   exclude list, with an allow-list for hourly leads).
4. **Output** — write count + role list per company; flag anything unresolved.

## Hard rules
- **Never guess a slug.** Slugs are opaque (especially Greenhouse / Workday). Extract the
  exact slug from the careers page or its redirect, then **verify by hitting the endpoint**
  before trusting it.
- **Never emit a false zero.** A company we couldn't resolve is `status=unresolved`, NOT
  `frontline_role_count=0`. Zero means "resolved, and genuinely has none."
- **Apollo `Technologies` is a hint, not truth** — frequently empty or vague. Use it to
  prioritize which platform to try first; never as the sole source of the platform.
- **Matching = word-prefix at a word boundary** (`\bterm`, no *trailing* boundary):
  `clean` → cleaner / cleaning / clean-up; `serv` → server / service. The leading `\b`
  avoids false friends (obSERVer, reSORT, poRTER, CAREer). Then drop anything on the
  exclude list **unless** it's an allowed hourly lead. Full logic in SPEC.
- **Cache every resolved slug** (platform + slug + verified-at) so re-runs skip resolution.
- **Locations may be nested objects**, not strings — extract city/state defensively,
  never `str()` the whole object.

## Reuse, don't reinvent (clone + read these before writing fetch/resolve code)
- `plibither8/jobber` (MIT) — working fetchers for Workable, Greenhouse, Lever, Ashby,
  BambooHR; normalizes to `{title, location, link}`. Basis for stage 2.
- `Ramcharan747/careerscout` (MIT) — resolver blueprint: ATS-detection patterns
  (17 platforms), slug probing, static→headless tiers. Basis for stage 1.
- `YvetteZheng0812/ats-job-scraper` — adds SmartRecruiters / Rippling / Workday endpoints,
  a slug-cache pattern, and SerpAPI-based discovery.

## Build order (prove before scaling)
- **Phase 0** — ONE Workable company end-to-end (Spark Car Wash → `apply.workable.com/spark-car-wash`).
  Hardcode the slug; confirm the count is accurate and complete BEFORE building the resolver.
- **Phase 1** — Build the resolver (careers-page fetch → detect ATS → extract slug →
  verify → cache). Add Indeed-apply-URL mining + search fallback for stragglers.
- **Phase 2** — Add Eightfold, Greenhouse, Lever, Ashby fetchers.
- **Phase 3** — Decide whether iCIMS / Dayforce (locked-down, JS-heavy) are worth a
  headless/Apify path, based on how much of the real list actually sits there.

## Conventions
- Python 3.11+. `requests` for HTTP. Add a headless fetcher (Playwright) only when Phase 1
  needs it for JS-only career pages.
- One module per platform under `fetchers/` (e.g. `fetchers/workable.py`), each returning the
  same normalized shape: `{"title": str, "location": str}`.
- Resolver and fetchers are idempotent and cached; the whole run is safe to re-run.
- Log every unresolved company with the reason (`no_careers_page`, `unknown_ats`,
  `slug_unverified`, etc.).
