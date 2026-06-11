# SPEC — Frontline-Hiring Signal Scraper

## 1. Context & purpose
Nova is an AI phone-screener for **high-volume hourly / frontline hiring** (QSR, retail,
grocery, warehouse, hospitality, senior care, car wash, logistics, etc.). A company is a
strong Nova prospect when it has **many open frontline roles** at once.

We start from an Apollo company export and need a hard signal: **how many open frontline
roles does each company have, and which ones.** That count drives manual tiering later —
so the pipeline itself stays deterministic (no LLM scoring in the data path).

A company's true frontline openings live on its **ATS career site**, most of which expose
public JSON. Job aggregators (Indeed/LinkedIn) under-count ATS-heavy employers, so the ATS
is the source of truth, with aggregators only as a fallback for unresolved companies.

---

## 2. Input
An Apollo CSV export. Relevant columns (names vary slightly by export — detect leniently):
- `Company Name`
- `Website` (primary key for resolution)
- `Company Linkedin Url`
- `Technologies` — **hint only**, often empty/stale. May name an ATS.
- (plus many enrichment columns we pass through untouched)

Scope: ~200–300 companies per run, not tens of thousands. Optimize for accuracy and
re-runnability over raw throughput.

---

## 3. Output
Emit the input rows **plus** these columns:

| column | meaning |
|---|---|
| `frontline_role_count` | integer; count of open roles matching the frontline filter |
| `frontline_roles` | the matched roles as `Title — Location`, newline- or `;`-separated |
| `ats_platform` | resolved platform (`workable`, `greenhouse`, `lever`, `ashby`, `eightfold`, `dayforce`, `icims`, …) |
| `ats_slug` | the exact slug used to fetch |
| `status` | `ok` \| `unresolved` \| `error` (with a reason logged) |

`status=unresolved` ⇒ leave `frontline_role_count` **empty**, never `0`. Zero is a real,
resolved answer; empty/unresolved is "we couldn't read it."

Also persist a slug cache (e.g. `cache/slugs.json`): `{website: {platform, slug, verified_at}}`.

---

## 4. Matching engine (the filter)
Load `roles.txt` (500 terms: full role names + broad word-prefix stems). For each job title:

```python
title_l = title.lower()

# 1) does it match any frontline term, at a word-start boundary?
matched = any(re.search(r'\b' + re.escape(term), title_l) for term in ROLES)
if not matched:
    skip

# 2) is it an allowed hourly lead? (protects it from the exclude list)
lead_ok = any(term in title_l for term in LEAD_OK)

# 3) is it a skilled/salaried role we must drop?
excluded = any(re.search(r'\b' + re.escape(x), title_l) for x in EXCLUDE)

if excluded and not lead_ok:
    skip

count it
```

**Why `\b` + prefix (no trailing boundary):** prefix lets one stem cover a family
(`clean` → cleaner/cleaning/clean-up). The leading word boundary blocks false friends:
`\bserv` matches *server* but not ob**serv**er; `\bsort` not re**sort**; `\bporter` not
re**porter**; and bare `care` is deliberately **not** a stem (it would hit *career*) —
the list uses `caregiv`, `care aide`, `home care`, etc. instead.

### EXCLUDE (starter list — skilled / salaried / professional)
Tune as real data comes in. Match these with the same `\b` prefix rule.
- **Salaried management:** `general manager`, `store manager`, `district manager`,
  `regional manager`, `area manager`, `operations manager`, `branch manager`,
  `division manager`, `plant manager`, `market manager`
- **Clinical / credentialed:** `registered nurse`, `rn`, `lpn`, `lvn`,
  `nurse practitioner`, `np`, `crna`, `charge nurse`, `physician`, `surgeon`, `doctor`,
  `pharmacist`, `dentist`, `physical therapist`, `occupational therapist`,
  `respiratory therapist`, `clinician`, `dietitian`, `nutritionist`, `social worker`
- **Technical / professional:** `engineer`, `architect`, `scientist`, `developer`,
  `programmer`, `analyst`, `accountant`, `attorney`, `lawyer`, `controller`
- **Leadership:** `director`, `vice president`, ` vp`, `president`, `chief`, `ceo`,
  `cfo`, `coo`, `cto`, `chro`, `head of`, `principal`, `superintendent`, `professor`,
  `consultant`

> Note: do **not** put bare `manager` or bare `supervisor` in EXCLUDE — that would kill
> the hourly leads below. Exclude only the specific salaried titles above.

### LEAD_OK (allow even if an EXCLUDE term is present) — these are hourly leads
`assistant manager`, `assistant store manager`, `assistant general manager`,
`assistant restaurant manager`, `shift manager`, `shift lead`, `shift leader`,
`shift supervisor`, `team lead`, `team leader`, `crew lead`, `crew leader`,
`floor supervisor`, `lead associate`, `key holder`, `opening crew`, `closing crew`.

### Regression set (rebuild this as a test before trusting the filter)
Prior manual validation: **35/35** should-match and **18/18** should-not.
- Must match: Crew Member, Line Cook, Warehouse Associate, Delivery Driver, CNA,
  Housekeeper, Security Guard, Shift Supervisor, Assistant Manager, Car Wash Attendant,
  Route Sales Representative, Sales Advisor, Stocker, …
- Must NOT match: Registered Nurse, General Manager, District Manager, Software Engineer,
  Data Analyst, Store Manager, Pharmacist, Marketing Director, Staff Accountant,
  and false friends Reporter (porter), Observer (server), Career Coach (care), Resort (sort).

---

## 5. Resolver (stage 1 — the hard part)
Goal: `website → (ats_platform, ats_slug)`, verified. Try in order; stop at first verified hit.

1. **Slug cache** — if `website` is cached and `verified_at` is fresh, use it.
2. **Indeed-apply mining (free pre-resolution)** — if a prior Indeed pull exists, the apply
   URLs already contain ATS domains + slugs for any company that syndicated even one job
   (e.g. an apply link to `apply.workable.com/<slug>`). Harvest these first; they're free.
3. **Careers-page fetch + signature detection (primary):**
   - Build candidate URLs from `website`: `/careers`, `/jobs`, `/company/careers`,
     `/join-us`, `/work-with-us`, plus `careers.<domain>` / `jobs.<domain>`.
   - Also fetch the homepage and scan the footer for a Careers/Jobs link.
   - **Follow redirects** — many `/careers` pages 301 straight to the ATS; the final URL
     often *is* the answer.
   - Regex the HTML + final URL for ATS signatures and capture the slug, e.g.:
     - `apply.workable.com/<slug>`
     - `boards.greenhouse.io/<slug>` / `job-boards.greenhouse.io/<slug>` / `grnhse`
     - `jobs.lever.co/<slug>`
     - `jobs.ashbyhq.com/<slug>`
     - `<slug>.icims.com` / `careers-<slug>.icims.com`
     - `dayforcehcm.com/.../<slug>/...`
     - Eightfold: usually `careers.<companydomain>` (own domain) with Eightfold markers
   - Port detection patterns from **careerscout**.
4. **Search fallback** — if the page fetch fails, query e.g. `"<Company>" careers workable`
   / `greenhouse` / `lever` and parse the ATS URL from results (SerpAPI, per ats-job-scraper).
5. **Apollo `Technologies` hint** — use any named ATS to *order* the guesses above, not to conclude.
6. **Verify** — hit the platform's JSON endpoint with the extracted slug. Valid jobs for the
   right company ⇒ accept + cache. 404 / wrong company ⇒ reject, mark `slug_unverified`.
7. **Headless fallback** — for JS-only career pages where static HTML has no signature, render
   with Playwright and re-scan (careerscout's Tier-2 approach). Only build this if Phase 1 needs it.

Anything still unresolved ⇒ `status=unresolved` (+ reason). Optionally fall back to an Indeed
count for coverage, clearly flagged as lower-confidence — never a silent zero.

---

## 6. Fetcher (stage 2 — the easy part)
Per platform, GET the public board JSON and normalize to `{title, location}`. **Verify each
endpoint against the repo source / a live slug before trusting** — patterns below are starting
points, not gospel.

| platform | public endpoint pattern (VERIFY) | source to lift from | notes |
|---|---|---|---|
| Workable | `apply.workable.com/api/v3/accounts/<slug>/jobs` (or widget JSON) | jobber | start here (Phase 0) |
| Greenhouse | `boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true` | jobber | cleanest API |
| Lever | `api.lever.co/v0/postings/<slug>?mode=json` | jobber | trivial, no auth |
| Ashby | `api.ashbyhq.com/posting-api/job-board/<slug>` | jobber | |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/<slug>/postings` | ats-job-scraper | |
| Workday | tenant-specific `…/wday/cxs/…` POST | ats-job-scraper | messy; defer |
| Eightfold | company-domain positions API | (verify live) | own-domain; Phase 2 |
| Dayforce / iCIMS | locked-down, JS/candidate-portal | — | Phase 3; headless/Apify only |

Normalization: each fetcher returns `list[{title, location}]`. Pull location defensively —
it's often a nested object (`{city, state}` or `{formattedLocation}`), sometimes a list of
locations; flatten to a readable string, don't `str()` the raw object.

---

## 7. Repos to leverage
Clone and **read** these before writing equivalent code; lift logic, don't depend on hosted services.
- **`plibither8/jobber`** (MIT) — fetcher for Workable/Greenhouse/Lever/Ashby/BambooHR,
  normalized output. The fetcher backbone.
- **`Ramcharan747/careerscout`** (MIT) — resolver engine: career-page discovery, 17-ATS
  detection, slug probing, static→headless→deeper tiers. The resolver blueprint.
- **`YvetteZheng0812/ats-job-scraper`** — 7-platform public-API fetcher + `discovered_slugs.json`
  cache + SerpAPI discovery. Source for the extra platforms and the cache/search patterns.
- (`quantamShade0337/jobs-radar` — an Apify actor aggregating Greenhouse/Ashby/Lever, if an
  Apify path is ever preferred for those three.)

---

## 8. Phases & acceptance criteria
**Phase 0 — prove the data (no resolver).**
Hardcode Spark Car Wash on Workable (`apply.workable.com/spark-car-wash`). Fetch → filter →
print count + roles. **Done when:** count reflects the true board (Indeed showed ~15 here; our
old Indeed scrape caught 1 — confirm we now get the full set) and the filter keeps frontline
roles while dropping any salaried/skilled ones.

**Phase 1 — resolver.**
**Done when:** given just `website`, the resolver returns a *verified* `(platform, slug)` for a
clear majority of a real sample, caches them, and flags the rest `unresolved` with a reason —
zero false zeros.

**Phase 2 — platform coverage.**
Add Eightfold, Greenhouse, Lever, Ashby fetchers behind the resolver's routing. **Done when:**
mixed-platform input produces correct counts per company end-to-end.

**Phase 3 — locked-down platforms (decision gate).**
Measure how much of the real list is iCIMS/Dayforce. Build headless/Apify for them only if the
coverage gain justifies it; otherwise leave them `unresolved`.

---

## 9. Known test companies (ground truth for dev)
| company | ATS | URL | expected |
|---|---|---|---|
| Spark Car Wash | Workable | `apply.workable.com/spark-car-wash` | STRONG (many frontline) |
| Club Feast | Workable | `apply.workable.com/club-feast` | weak (gig/contractor) |
| Bimbo Bakeries USA | Eightfold | `careers.bimbobakeriesusa.com` | STRONG (route sales, sanitation, maint techs) |
| MFA Oil | Dayforce | `jobs.dayforcehcm.com/en-US/mfaoil/...` | STRONG (c-store / Big O Tires / fuel) |
| Crew Carwash | Dayforce | `jobs.dayforcehcm.com/en-US/crewcarwash/...` | STRONG |
| TEC Equipment | iCIMS | `careers-tecequipment.icims.com` | borderline (skilled techs) |
| Flint Hills Resources | custom/Koch | `fhr.com/careers` | ~0 ICP is CORRECT (skilled refinery) |

Use the STRONG ones to confirm counts look right, and Flint Hills to confirm a real, resolved
**zero** is reported as `0` (not `unresolved`).

---

## 10. Gotchas
- **Slugs aren't derivable** from the company name on half the platforms (Greenhouse/Workday
  tokens are opaque). Always read-then-verify.
- **`Technologies` is mostly empty** — don't design around it.
- **`maxItems`-style global caps** and pagination differ per platform — page through fully so
  big employers aren't truncated.
- **Locations are objects**, frequently. Defensive extraction only.
- **Don't depend on hosted helpers** (e.g. jobber's public instance) for production runs — port
  the logic so the pipeline has no third-party uptime/rate-limit dependency.
- **iCIMS shipped its own "Frontline AI" (2026)** — iCIMS-hosted prospects are already being
  pitched frontline-hiring AI by their ATS; useful context for prioritizing, not for the scraper.
