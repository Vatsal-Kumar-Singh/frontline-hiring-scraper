# Frontline-Hiring Signal Scraper

Takes an Apollo company export and outputs, **per company**, how many open
**frontline / hourly** roles they have right now (`frontline_role_count`) and
which ones (`frontline_roles`). A high count = strong [Nova](SPEC.md) ICP. No LLM
in the data path — the pipeline is deterministic and re-runnable.

See [`SPEC.md`](SPEC.md) for the full design and [`CLAUDE.md`](CLAUDE.md) for the
hard rules.

## Easiest way to use it (no coding)

**Step 1 — Install Python once** (skip if you already have it): get
[Python 3.11+](https://www.python.org/downloads/). On Windows, **tick "Add Python
to PATH"** on the first install screen.

**Step 2 — Download this app:** on its GitHub page, click the green **`Code`**
button → **`Download ZIP`**. Then **unzip** the file (right-click → Extract All).

**Step 3 — Start it:**
- **Windows:** double-click **`run.bat`**
- **Mac:** double-click **`run.command`** *(first time: right-click it → **Open**
  → **Open** to get past the security prompt).*
  **If double-click does nothing** (downloads can lose the "runnable" flag): open
  **Terminal** (press ⌘-Space, type *Terminal*, Enter), type `bash ` (with a
  space), then **drag the `run.command` file into the Terminal window** and press
  **Enter**. That runs it once; it then creates the Desktop icon for next time.
- **Linux:** in a terminal, `chmod +x run.sh && ./run.sh`

The first start installs everything automatically (a few minutes) **and puts a
"Frontline Hiring Scraper" icon on your Desktop** — after that, just use the
Desktop icon. Your browser opens at **http://127.0.0.1:5050**.

**Step 4 — On the page:**
1. **Upload** your Apollo company CSV (needs a *Company Name* and *Website* column).
2. Pick **how recent** the jobs should be (1 / 7 / 15 / 30 days, or all current).
3. Set the **minimum number of open roles** to flag a company as "strong" (default **20+**).
4. Choose **lookup tiers** — and **Yes/No on the paid Apify scraper** (off by default).
5. *(Optional)* Upload your own **ICP job-roles list** (a `.txt`, one role per line).
6. Paste your **Apify API token** once (free at [apify.com](https://apify.com) →
   Settings → Integrations) — only needed for the paid tiers. Saved locally, never uploaded.
7. Set your **spend cap** (default **$15 per run**).
8. Click **Run**, watch the live progress + log, then **Download** the results CSV.

**You can never overspend:** every run **stops the instant it would exceed your
$15 cap** — if a run needs more, you raise the number yourself first (nothing
spends past your cap without you deciding). The page shows your spend live.

> **Something not working (especially on a Mac)?** See
> **[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md)** — it lists every common issue
> (SSL certificate errors, the "unidentified developer" prompt, ports, Chromium,
> etc.) with the exact symptom and one-step fix. If you're stuck, hand that file +
> your error message to an AI assistant (Claude Code, Codex, ChatGPT) and it can
> resolve it immediately.

### The lookup tiers (cheapest first — leave the defaults)
| Tier | Cost | What it does |
|---|---|---|
| Static + sitemap + headless | **free** | Reads each company's own careers page / ATS (incl. careers pages found via `sitemap.xml`) |
| **Indeed sweep** | ~$0.0001/job | Finds companies by name (+ LinkedIn vanity name) — highest yield |
| **ATS slug harvest** | pennies | Reads a company's real ATS for free (via Indeed) + caches it |
| **LinkedIn sweep** | ~$0.0009/job | Recovers companies Indeed couldn't find |
| **Google Jobs** | ~$0.02/job | Broadest coverage; **targeted at larger companies only** (employer-filtered) |
| Apify ATS actors | most expensive | Only for locked platforms (Paycom/Dayforce/ADP/iCIMS) |

Tiers run cheapest-first, each shrinking the work for the next. A typical
500-company run resolves ~70–85% for **a few dollars**. (Lists of tiny local
businesses resolve lower — they're often not on any job board — see notes below.)

## Run from the command line (optional, for developers)
```
pip install -r requirements.txt
python -m playwright install chromium
python app.py                                   # the web UI

# or headless, full cheap pipeline:
python pipeline.py input.csv output.csv --since 7 --threshold 20 --indeed --harvest --linkedin
python pipeline.py input.csv output.csv --apify-only     # resume just the paid Apify tier
```

## The 20+ threshold (only the signal matters)
We only need to know whether a company has **≥20 open frontline roles** (a strong
Nova signal) — exact counts beyond that are unnecessary. So fetchers **early-stop**
once the threshold is hit (Workday Carrier: 200 rows instead of 1,095; RAM Hotels:
~5 EasyApply portals instead of 28), which saves time, Apify spend, and agent
tokens. `frontline_20plus` (`yes`/`no`) is the headline column; `frontline_role_count`
is the exact count when under the threshold, or a floor (≈20) when early-stopped.
Change it with `--threshold N` (default 20).

```powershell
pip install -r requirements.txt

# Tests:
python test_matcher.py     # filter regression: 35/35 match, 18/18 non-match
python test_fetchers.py    # live fetch+filter smoke test, every platform

# Full pipeline (Apollo CSV in -> enriched CSV out):
python pipeline.py input.csv output.csv
python pipeline.py input.csv --limit 25      # first 25 rows
```

Input column detection is lenient (case/space-insensitive). `Website` is the
primary key; `Technologies` is used only to *order* platform guesses.

## Output columns (appended to every input row)

| column | meaning |
|---|---|
| `frontline_role_count` | int; matched open roles. **Empty (never 0)** when unresolved/error. A floor (≈threshold) when early-stopped. |
| `frontline_20plus` | `yes`/`no` — does it meet the threshold (default 20)? The primary signal. Empty when unresolved. |
| `frontline_roles` | matched roles as `Title — Location`, newline-separated |
| `ats_platform` | resolved platform (`workable`, `greenhouse`, …) |
| `ats_slug` | the exact slug used to fetch |
| `status` | `ok` \| `unresolved` \| `error` |

`status=unresolved` ⇒ count empty. `0` is a real, resolved answer ("they have
none"); empty means "we couldn't read it." Resolved slugs are cached in
[`cache/slugs.json`](cache/) so re-runs skip resolution; the cache is checkpointed
after every row.

## Architecture (4 stages)

1. **Resolver** ([`resolver.py`](resolver.py)) — `website → (platform, slug)`,
   verified. Fetches the homepage + careers-page candidates, **follows redirects
   and discovers careers links** (e.g. `/join-team`), regexes the HTML/final URL
   for ATS signatures, reorders by the Apollo hint, then **verifies against the
   live endpoint** before accepting. Never guesses a slug.
2. **Fetcher** ([`fetchers/`](fetchers/)) — one module per platform, each
   returning the normalized shape `{"title", "location"}`. Pages through fully;
   extracts locations defensively (they're often nested objects/lists).
3. **Filter** ([`matcher.py`](matcher.py)) — word-prefix match at a word boundary
   against [`roles.txt`](roles.txt), minus `EXCLUDE`, with `LEAD_OK` protecting
   hourly leads ([`config.py`](config.py)).
4. **Output** ([`pipeline.py`](pipeline.py)) — count + role list per company;
   logs every unresolved company with a reason.

## Platform coverage

**Fetchable with plain HTTP (count + roles):**

| platform | slug format | endpoint |
|---|---|---|
| Workable | `<slug>` or numeric `<id>` | `apply.workable.com/api/v3/...`; JS embed via `www.workable.com/api/accounts/<id>` |
| Greenhouse | `<slug>` | `boards-api.greenhouse.io` |
| Lever | `<slug>` | `api.lever.co/v0/postings` |
| Ashby | `<slug>` | `posting-api/job-board` |
| SmartRecruiters | `<slug>` | paginated postings API |
| **Workday** | `tenant\|dc\|site` | `wday/cxs/<tenant>/<site>/jobs` JSON POST |
| **UltiPro / UKG** | `host\|tenant\|guid` | `JobBoard/<guid>/JobBoardView/LoadSearchResults` JSON POST |
| **Cornerstone (CSOD)** | `tenant\|siteId` | scrape anon JWT → `career-site/v1/search` |
| **JazzHR** | `<slug>` | `applytojob.com/apply/jobs` (server-rendered HTML) |
| **iCIMS** | `<slug>` | `careers-<slug>.icims.com/...?in_iframe=1` — see WAF note |
| **EasyApply** | `<sub1>\|<sub2>\|...` | `<sub>.easyapply.co` job links; sums across multi-property portals (e.g. RAM Hotels = 28 hotels → 203 frontline) |
| **Taleo** | `<sub>\|<site>\|<org>\|<cws>` | public RSS feed `…/ats/servlet/Rss?org=&cws=` (title + taleo:location) |
| **Culinary Agents** | `<group-slug>` | hospitality board `culinaryagents.com/groups/<slug>/jobs` (offset-paginated) |
| **Harri** | `<slug>` (from `harri.com/<slug>`) | hospitality ATS public API: slug→brand id → `search_jobs` (`brand_level_ids`) |
| **JSON-LD (generic / "native")** | careers URL | schema.org `JobPosting` in page HTML — last-resort fallback |
| Eightfold | careers host | own-domain v2 API (older tenants only — see note) |

EasyApply, Taleo, and Culinary Agents were discovered on companies that had no
static ATS signature, then turned into free deterministic fetchers so they're
re-runnable at zero cost.

### Resolution cascade (cheapest → most expensive; each tier feeds the next)
Every company stops at the first tier that resolves it, so paid tiers only ever
see what the free/cheap tiers couldn't crack:
1. **Seed + slug cache** ([`seeds.json`](seeds.json), `cache/slugs.json`) — curated
   and prior verified resolutions, re-verified but reused. **Free.**
2. **Static fetch + signatures** — homepage + careers paths **+ careers pages
   discovered from `sitemap.xml`** (catches non-standard paths like `/employment`),
   follow redirects, scan HTML for ATS signatures. **Free.**
3. **Indeed sweep** (`--indeed`) — finds companies by name (and by the **LinkedIn
   vanity name** from the Apollo `Company LinkedIn URL`) on Indeed (~$0.0001/job).
   Highest yield; resolves the bulk.
4. **Slug harvest** (`--harvest`) — reads a company's real ATS for **free** using
   the apply URL Indeed exposes, and caches the slug so future runs skip the paid
   step entirely. Tagged `count_method=<platform>+indeed`.
5. **Headless render** ([`headless.py`](headless.py), Playwright) — renders JS-only
   careers pages and re-scans with the same signatures. **Free** (local compute).
   Disable with `--no-headless`.
6. **LinkedIn sweep** (`--linkedin`) — recovers companies Indeed missed
   (~$0.0009/job).
7. **Google Jobs** (`--google`) — broadest coverage (a company's own JobPosting
   schema, niche boards). Pricey (~$0.02/job) and imprecise, so it's **employer-
   filtered and runs only on larger companies** (≥100 employees) under a $6 tier cap.
8. **Apify ATS actors** (`--apify`) — dedicated (ADP/iCIMS/Paradox) + catch-all
   (Paycom/Dayforce/…). Most expensive; last resort, opt-in.

Free + cheap tiers resolve the bulk; every discovery is cached/seedable so the
unresolved set shrinks permanently. Spend is hard-capped per run ($15 default)
(see the budget guard in [`budget.py`](budget.py)).

**Detect-only — resolved + slug recorded, but counts need headless/Apify** (never a false zero):
Dayforce, SAP SuccessFactors, Paycom, ADP, BrassRing (sjobs), Avature, Hirebridge, Harri, Paradox/Olivia, TalentReef, Workstream.

**Skipped (defunct/ambiguous):** PeopleMatter (folded into Snagajob ~2016), RapidHire.

### Notes on the harder platforms (verified live)
- **iCIMS** — the `?in_iframe=1` trick returns server-rendered HTML, *but* many tenants sit behind **AWS WAF** and answer with a `405 "Human Verification"` challenge from datacenter IPs. The fetcher is shipped and works on non-WAF networks (residential IPs / Apify proxies); WAF'd tenants resolve as `unresolved`, never a false zero.
- **Eightfold** — the public v2 API is now **PCSX-auth-gated** (`403`) on newer tenants; those resolve but don't count.
- **Dayforce, SuccessFactors, Paycom, ADP** — JS SPAs / session-bound or WAF'd APIs. Detected only.
- **Multi-ATS companies** — some employers expose two ATSes (e.g. MFA Oil = iCIMS for corporate + Dayforce for c-store/fuel). The resolver picks the highest-confidence *fetchable* one; the others are still detected.

### Apify integration (wired — batch-by-domain)
For platforms that plain HTTP can't read, the pipeline runs a **batch Apify pass**
after the HTTP phase. The fantastic-jobs `*-jobs-api` actors are job *databases*
queried by company domain (`domainFilter`), so we query an actor **once for a
batch of unresolved domains** and group results by `domain_derived` — cheap (only
matching jobs are billed) and robust (catches boards the resolver missed, e.g.
iCIMS tenants behind WAF).

Wired actors (`fetchers/apify_actor.py`):

| platform | actor | why |
|---|---|---|
| `paradox` | `fantastic-jobs/paradox-ai-jobs-api` | Paradox/Olivia (no public API) |
| `adp` | `fantastic-jobs/adp-jobs-api` | ADP Workforce Now (JS SPA) |
| `icims` | `fantastic-jobs/icims-jobs-api` | fills the AWS-WAF gap |

- **Auth:** `APIFY_TOKEN` env var, or `secrets.local.json` (gitignored).
- **Add an actor:** one line in `ACTORS` (`platform -> actor_id`). The default
  input (`domainFilter`) + normalizer (`title` + `locations_derived`) fit every
  fantastic-jobs `*-jobs-api` actor.
- **How it fills:** for each actor, all still-unresolved company domains are
  queried; any company with returned jobs gets `status=ok`, a real count, and
  `ats_platform` set to the board name. HTTP-resolved rows are never overridden.

**Catch-all fallback (covers everything else):** after the dedicated actors, a
final pass runs `fantastic-jobs/career-site-job-listing-api` — an aggregator
that returns jobs for ~all ATS sources by domain (Paycom, Dayforce, Paylocity,
isolved, SuccessFactors, TalentReef, ...). It fills any domain still unresolved
and names the board from each job's `source` field. **Lower confidence:** it
de-dupes/samples, so counts can *undercount* a full board (verified: iCIMS/ADP/
Paradox come back far smaller than their dedicated actors). That's exactly why
dedicated actors run first and the catch-all only mops up.

**`count_method` column** tells you how each count was derived, so you can weight
confidence when tiering:

| value | meaning | confidence |
|---|---|---|
| `http` | read directly from the ATS public API (incl. seed/static/headless-discovered) | highest |
| `headless` | ATS found by rendering a JS-only careers page, then read via its API | high |
| `<platform>+indeed` | ATS slug discovered from an Indeed apply URL, then read for free | high |
| `apify-full:<platform>` | dedicated actor, full board | high |
| `indeed` | company-name search on Indeed (7-day window) | high |
| `linkedin` | company-name search on LinkedIn (Indeed-miss escalation) | medium-high |
| `apify-aggregator` | catch-all aggregator | medium (may undercount) |
| `google` | Google Jobs, employer-filtered (larger companies only) | medium |
| `generic` | job links scraped from a custom (non-ATS) rendered careers page | medium (links+JSON-LD only; no false zeros) |
| *(empty)* | unresolved — count is empty, never 0 | — |

## Status vs. the SPEC phases

- **Phase 0** ✅ — Spark Car Wash → 8 frontline of 17 (GMs/Sales/Product Mgr/Exec
  Asst correctly dropped).
- **Phase 1** ✅ — resolver reads & verifies slugs from the careers page, caches,
  and flags the rest with a reason. Zero false zeros.
- **Phase 2** ✅ — Workable/Greenhouse/Lever/Ashby/SmartRecruiters fetch end-to-end.
- **Phase 3** (decision gate) — measure how much of the real list is
  Dayforce/iCIMS/Eightfold-PCSX or JS-only careers pages (Crew Carwash, Club
  Feast both need rendering). The resolver already records platform+slug for those,
  so a run over the real list gives the coverage numbers to decide whether a
  Playwright fallback is worth building.

## Findings worth knowing (from live verification)

- **Workable JS embed is resolvable without headless.** Marketing sites embed
  `www.workable.com/assets/embed.js` with a numeric account id (`whr_embed(678815)`);
  `GET www.workable.com/api/accounts/<id>` returns the full board. This covers
  the common "site doesn't link `apply.workable.com/...` statically" case.
- **Eightfold locked down its public API.** `app.eightfold.ai/api/apply/v2/jobs`
  returns `403 "Not authorized for PCSX"`; tenant careers hosts 404 that path.
  The page is server-rendered/personalized — fetching counts needs headless.
- **Dayforce slug = the client namespace, not the path prefix.** e.g.
  `us232.dayforcehcm.com/CandidatePortal/en-us/mfaoil` → `mfaoil`.
- **Some careers links use non-standard paths** (`/join-team`, `/apply-now`); the
  resolver discovers and follows same-host careers links found on the homepage.
