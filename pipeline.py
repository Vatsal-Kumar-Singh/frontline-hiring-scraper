"""End-to-end pipeline: Apollo CSV in -> enriched CSV out.

Per company: resolve -> fetch -> filter -> count. Idempotent and cached; safe to
re-run. Never emits a false zero — unresolved/locked-down/error leave the count
empty; 0 is reserved for "resolved and genuinely none".

Usage:
    python pipeline.py input.csv [output.csv]
    python pipeline.py input.csv --limit 25
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import budget
import config

# Company names contain non-Latin characters (e.g. "Ōno", "Café"). Windows'
# default console codec is cp1252 and a bare print() of such a name raises
# UnicodeEncodeError, which would kill a worker thread (and the whole run). Force
# UTF-8 output with safe replacement so output can never crash the pipeline.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_ROOT = os.path.dirname(os.path.abspath(__file__))

WORKERS = 40  # Phase 1 is network-bound; resolve companies concurrently.
SINCE_CHOICES = ["1", "7", "15", "30", "all"]

# Structured progress for the web UI. Off by default (clean CLI); the UI sets
# PROGRESS_JSON=1 and parses these `PROGRESS|{...}` lines from stdout.
_PROGRESS_ON = os.environ.get("PROGRESS_JSON") == "1"
_emit_lock = threading.Lock()


def _emit(**event):
    if not _PROGRESS_ON:
        return
    with _emit_lock:
        try:
            print("PROGRESS|" + json.dumps(event), flush=True)
        except Exception:
            pass


def _twentyplus(n):
    """'yes' if the frontline count meets the threshold (strong signal), else 'no'."""
    return "yes" if n >= config.FRONTLINE_THRESHOLD else "no"


def _date_window(since):
    """since in {1,7,15,30,'all'} -> (date_after 'YYYY-MM-DD' or None, cutoff date
    or None, all_time bool)."""
    if since in (None, "all", "0"):
        return None, None, True
    days = int(since)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    return cutoff.isoformat(), cutoff, False


def _date_ok(date_posted, cutoff):
    """Client-side recency filter for dedicated actors (no server date input)."""
    if cutoff is None:
        return True
    s = (date_posted or "")[:10]
    if not s:
        return True  # keep undated rather than silently drop
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() >= cutoff
    except ValueError:
        return True

import fetchers
from fetchers import apify_actor, apify_base, google_jobs, indeed, linkedin
from matcher import filter_roles
from resolver import load_cache, resolve, resolve_headless_batch, save_cache

# Output columns appended to every input row (SPEC §3, + ats_url for click-through
# / Apify input, + count_method showing how the count was derived).
OUT_COLS = [
    "frontline_role_count", "frontline_20plus", "frontline_roles", "ats_platform",
    "ats_slug", "ats_url", "status", "count_method",
]

ROLE_SEP = "\n"  # within the frontline_roles cell (CSV-quoted)


def _find_col(fieldnames, *candidates) -> str | None:
    """Lenient header detection — case/space-insensitive contains match."""
    norm = {fn.lower().strip(): fn for fn in fieldnames}
    # exact-ish first
    for c in candidates:
        if c.lower() in norm:
            return norm[c.lower()]
    # contains
    for c in candidates:
        for low, original in norm.items():
            if c.lower() in low:
                return original
    return None


def process_row(row, cols, cache, log):
    website = row.get(cols["website"], "") if cols["website"] else ""
    tech = row.get(cols["tech"], "") if cols["tech"] else ""
    name = row.get(cols["name"], "") if cols["name"] else website

    res = resolve(website, tech, cache)
    platform, slug, status, reason = (
        res["platform"], res["slug"], res["status"], res["reason"],
    )

    ats_url = res.get("ats_url", "")
    out = {
        "frontline_role_count": "",
        "frontline_20plus": "",
        "frontline_roles": "",
        "ats_platform": platform,
        "ats_slug": slug,
        "ats_url": ats_url,
        "status": "unresolved",
        "count_method": "",
    }

    def _emit(jobs, src):
        frontline = filter_roles(jobs)
        out["frontline_role_count"] = len(frontline)  # real count, may be 0
        out["frontline_20plus"] = _twentyplus(len(frontline))
        out["frontline_roles"] = ROLE_SEP.join(
            f"{j['title']} — {j['location']}".rstrip(" —") for j in frontline
        )
        out["status"] = "ok"
        out["count_method"] = src
        log(f"OK ({src})    {name!r}: {platform} count={len(frontline)} "
            f"20+={out['frontline_20plus']}")
        return out

    if status != "ok":
        # Left unresolved here; an Apify batch pass (run()) may still fill it.
        log(f"UNRESOLVED  {name!r}: {reason}")
        return out

    try:
        jobs = fetchers.fetch(platform, slug)
    except Exception as e:  # network / shape error — not a zero
        log(f"ERROR       {name!r}: fetch {platform}/{slug}: {e}")
        out["status"] = "error"
        return out

    return _emit(jobs, "http")


def run(input_path, output_path, limit=None, since="7", headless_enabled=True,
        apify_enabled=False, indeed_enabled=False, linkedin_enabled=False,
        harvest_enabled=False):
    cache = load_cache()
    budget.begin_run()  # pin per-run $15 hard-stop baseline (covers all paid phases)

    with open(input_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    cols = {
        "name": _find_col(fieldnames, "company name", "company", "name"),
        "website": _find_col(fieldnames, "website", "company website", "domain", "url"),
        "tech": _find_col(fieldnames, "technologies", "technology", "tech"),
    }
    if not cols["website"]:
        sys.exit("ERROR: could not find a Website column in the input CSV.")

    out_fields = fieldnames + [c for c in OUT_COLS if c not in fieldnames]

    if limit:
        rows = rows[:limit]

    def log(msg):
        print(msg, file=sys.stderr)
        _emit(type="log", message=str(msg))

    total = len(rows)

    # Resume: reuse already-resolved rows from a prior (interrupted) run writing the
    # SAME output file, so repeated launches accumulate progress across crashes /
    # machine sleeps. Match by website value.
    # Key by (company, website) — website alone collapses/loses blank-website rows
    # (companies resolved by name-search), which a re-run would then reprocess.
    def _rkey(r):
        nm = (r.get(cols["name"], "") if cols["name"] else "").strip().lower()
        st = (r.get(cols["website"], "") if cols["website"] else "").strip().lower()
        return (nm, st)

    resume = {}
    if os.path.exists(output_path):
        try:
            with open(output_path, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    if r.get("status") == "ok":
                        resume[_rkey(r)] = {k: r.get(k, "") for k in OUT_COLS}
        except (OSError, ValueError):
            resume = {}
    if resume:
        log(f"RESUME      skipping {len(resume)} already-resolved companies "
            f"from previous run")

    ck_lock = threading.Lock()

    def checkpoint():
        """Atomically persist current results — called periodically and after each
        phase / headless chunk so a crash or machine-sleep never loses progress."""
        with ck_lock:
            _write_output(rows, results, out_fields, output_path)

    # ---- Phase 1: resolve + plain-HTTP fetch (concurrent, buffered) ----
    _emit(type="phase", phase="Static read", detail="reading careers pages & ATS APIs",
          current=0, total=total)
    results = [None] * total
    # Pre-fill resumed rows so EVERY checkpoint includes them (a later death can't
    # regress previously-resolved rows that this run hasn't re-reached yet).
    for idx, row in enumerate(rows):
        key = _rkey(row)
        if key in resume:
            results[idx] = dict(resume[key])
    done = {"n": 0}
    lock = threading.Lock()

    def work(idx):
        if results[idx] is None:                       # not already resumed
            results[idx] = process_row(rows[idx], cols, cache, log)
        res = results[idx]
        name = rows[idx].get(cols["name"], "") if cols["name"] else ""
        with lock:
            done["n"] += 1
            n = done["n"]
        print(f"[{n}/{total}] {name[:38]:38} {res['status']:11} "
              f"{res.get('ats_platform', ''):14} count={res['frontline_role_count']}")
        _emit(type="progress", phase="Static read", current=n, total=total,
              message=f"{name[:40]} — {res['status']}")
        if n % 50 == 0:
            checkpoint()  # intra-phase checkpoint (survive a mid-static crash)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(work, range(total)))
    save_cache(cache)
    checkpoint()  # static results persisted immediately

    # Resolution tiers run CHEAPEST-FIRST so each tier shrinks the work (and spend)
    # of the next. Validated cost/resolution on the 781-co Apollo set:
    #   static/headless $0 · Indeed ~$0.0016 · dedicated Apify ~$0.13 · catch-all ~$0.14
    # Indeed is ~80x cheaper per resolution than paid Apify and very high-yield, so
    # it runs BEFORE the slow headless and the expensive Apify tiers.

    # ---- Phase 2: Indeed sweep — PAID (cheap, opt-in). Resolves the bulk. ----
    if indeed_enabled:
        _indeed_fill(rows, results, cols, log, since, checkpoint=checkpoint)
        checkpoint()

    # ---- Phase 2.5: slug-harvest — read real ATS free for low-count Indeed cos, ----
    #      cache slugs so future runs skip the paid lookup entirely.
    if indeed_enabled and harvest_enabled:
        _harvest_fill(rows, results, cols, cache, log, since, checkpoint=checkpoint)
        save_cache(cache)
        checkpoint()

    # ---- Phase 3: FREE headless render fallback for still-unresolved rows ----
    if headless_enabled:
        _headless_fill(rows, results, cols, cache, log, checkpoint=checkpoint)
        save_cache(cache)
        checkpoint()

    # ---- Phase 4: LinkedIn sweep — PAID (cheap, opt-in). Indeed-miss escalation. ----
    if linkedin_enabled:
        _linkedin_fill(rows, results, cols, log, since, checkpoint=checkpoint)
        checkpoint()

    # ---- Phase 5: Apify ATS actors — PAID, opt-in. Last resort (most expensive). ----
    if apify_enabled:
        _apify_fill(rows, results, cols, log, since)
        checkpoint()
    else:
        log("APIFY       skipped — most expensive tier, OFF by default.")
        _emit(type="phase", phase="Apify lookup", detail="skipped (paid step not enabled)",
              current=0, total=0)

    counts = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    strong = sum(1 for r in results if r.get("frontline_20plus") == "yes")
    print(f"\nDone. {summary}  ->  {output_path}")
    _emit(type="done", counts=counts, strong=strong, total=total, output=output_path)


INDEED_WORKERS = 8  # concurrent Indeed actor runs (cheap; bounded by Apify plan)


def _scraper_fill(rows, results, cols, log, since, *, fetch_fn, method,
                  cost_per_job, max_items, miss_path, label, checkpoint=None):
    """Generic name-search scraper tier (PAID, opt-in) shared by Indeed & LinkedIn.
    Sweeps every still-unresolved company by NAME, concurrently, budget-guarded.
    Never a false zero (no match -> stays unresolved). Writes a categorized miss
    report (not_found / name_mismatch / error) to `miss_path`."""
    if not apify_base.enabled():
        log(f"{label:11} skipped — no Apify token configured")
        return
    synced = budget.sync_from_apify()
    if synced is not None:
        log(f"{label:11} ledger reconciled to real Apify usage: ${synced:.2f}")
    targets = []
    for idx, res in enumerate(results):
        if res.get("status") == "ok":
            continue
        name = (rows[idx].get(cols["name"], "") if cols["name"] else "").strip()
        if name:
            targets.append((idx, name))
    n = len(targets)
    worst = max_items * cost_per_job
    _emit(type="phase", phase=f"{label.title()} lookup",
          detail=f"name-search sweep ({method})", current=0, total=n)
    log(f"{label:11} sweeping {n} unresolved companies by name "
        f"(<= {max_items} jobs each @ ${cost_per_job}); "
        f"budget ${budget.spent():.2f}/${budget.cap():.0f}")
    blk = threading.Lock()
    st = {"filled": 0, "done": 0, "stop": False}
    misses = []

    def _website(idx):
        return (rows[idx].get(cols["website"], "") if cols["website"] else "")

    def work(item):
        idx, name = item
        with blk:
            if st["stop"] or budget.remaining() < worst:
                st["stop"] = True
                return
        try:
            jobs, raw, found_names = fetch_fn(name, since, row=rows[idx])
        except Exception as e:
            with blk:
                st["done"] += 1
                misses.append((name, _website(idx), "error", str(e)[:120]))
            return
        frontline = filter_roles(jobs)
        with blk:
            st["done"] += 1
            if jobs:  # got matching data -> resolved (true zero allowed)
                res = results[idx]
                res["frontline_role_count"] = len(frontline)
                res["frontline_20plus"] = _twentyplus(len(frontline))
                res["frontline_roles"] = ROLE_SEP.join(
                    f"{j['title']} — {j['location']}".rstrip(" —") for j in frontline)
                if not res.get("ats_platform"):
                    res["ats_platform"] = method
                res["status"] = "ok"
                res["count_method"] = method
                if frontline:
                    st["filled"] += 1
            elif raw == 0:
                misses.append((name, _website(idx), f"not_found_on_{method}", ""))
            else:
                misses.append((name, _website(idx), "name_mismatch",
                               "; ".join(found_names[:5])))
            done = st["done"]
        _emit(type="progress", phase=f"{label.title()} lookup", current=done,
              total=n, message=f"{name[:40]}")
        if checkpoint and done % 50 == 0:
            checkpoint()

    with ThreadPoolExecutor(max_workers=INDEED_WORKERS) as ex:
        list(ex.map(work, targets))
    if st["stop"]:
        log(f"{label:11} stopped early — budget cap reached (no false zeros)")
    try:
        with open(miss_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["company", "website", "reason", "detail"])
            w.writerows(sorted(misses, key=lambda m: (m[2], m[0].lower())))
    except OSError:
        pass
    by_reason = {}
    for _, _, reason, _ in misses:
        by_reason[reason] = by_reason.get(reason, 0) + 1
    log(f"{label:11} filled {st['filled']} companies with frontline roles; "
        f"misses -> {by_reason} (see {miss_path}) (${budget.spent():.2f} spent)")


def _indeed_fill(rows, results, cols, log, since="7", checkpoint=None):
    _scraper_fill(rows, results, cols, log, since, fetch_fn=indeed.fetch_company,
                  method="indeed", cost_per_job=budget.COST_INDEED,
                  max_items=indeed.MAX_ITEMS, miss_path="indeed_misses.csv",
                  label="INDEED", checkpoint=checkpoint)


def _linkedin_fill(rows, results, cols, log, since="7", checkpoint=None):
    _scraper_fill(rows, results, cols, log, since, fetch_fn=linkedin.fetch_company,
                  method="linkedin", cost_per_job=budget.COST_LINKEDIN,
                  max_items=linkedin.MAX_ITEMS, miss_path="linkedin_misses.csv",
                  label="LINKEDIN", checkpoint=checkpoint)


def _harvest_fill(rows, results, cols, cache, log, since="7", checkpoint=None):
    """#4 slug-harvest (PAID, but pennies): for companies Indeed resolved with a
    LOW frontline count, read the REAL ATS (discovered from Indeed's apply URLs) for
    an accurate full count, and CACHE the slug so future runs read it FREE. Turns a
    ~$0.0008 Indeed probe into a permanent free ATS read; can upgrade count=0
    companies to strong. Budget-guarded, concurrent."""
    if not apify_base.enabled():
        log("HARVEST     skipped — no Apify token")
        return
    synced = budget.sync_from_apify()
    if synced is not None:
        log(f"HARVEST     ledger reconciled to real Apify usage: ${synced:.2f}")
    from resolver import normalize_website, now_iso
    thr = config.FRONTLINE_THRESHOLD
    targets = []
    for idx, res in enumerate(results):
        if res.get("count_method") != "indeed":
            continue  # only Indeed-resolved companies expose apply URLs to harvest
        try:
            cnt = int(res.get("frontline_role_count") or 0)
        except (ValueError, TypeError):
            cnt = 0
        if cnt >= thr:
            continue  # already strong via Indeed — full board not needed
        name = (rows[idx].get(cols["name"], "") if cols["name"] else "").strip()
        site = (rows[idx].get(cols["website"], "") if cols["website"] else "").strip()
        if name and site:
            targets.append((idx, name, site, cnt))
    n = len(targets)
    _emit(type="phase", phase="Slug harvest", detail="discover+read real ATS free",
          current=0, total=n)
    log(f"HARVEST     probing {n} low-count Indeed companies for a readable ATS "
        f"(budget ${budget.spent():.2f}/${budget.cap():.0f})")
    worst = 10 * budget.COST_INDEED
    blk = threading.Lock()
    st = {"upgraded": 0, "cached": 0, "done": 0, "stop": False}

    def work(item):
        idx, name, site, cnt = item
        with blk:
            if st["stop"] or budget.remaining() < worst:
                st["stop"] = True
                return
        try:
            hit = indeed.discover_ats(name, since)
        except Exception:
            hit = None
        if hit:
            platform, slug, ats_url = hit
            try:
                jobs = fetchers.fetch(platform, slug)
            except Exception:
                jobs = None
            if jobs is not None:
                frontline = filter_roles(jobs)
                with blk:
                    host = normalize_website(site)
                    if host:
                        cache[host] = {"platform": platform, "slug": slug,
                                       "ats_url": ats_url, "verified_at": now_iso()}
                        st["cached"] += 1
                    if len(frontline) > cnt:
                        res = results[idx]
                        res["frontline_role_count"] = len(frontline)
                        res["frontline_20plus"] = _twentyplus(len(frontline))
                        res["frontline_roles"] = ROLE_SEP.join(
                            f"{j['title']} — {j['location']}".rstrip(" —")
                            for j in frontline)
                        res["ats_platform"] = platform
                        res["ats_slug"] = slug
                        res["ats_url"] = ats_url
                        res["count_method"] = f"{platform}+indeed"
                        st["upgraded"] += 1
        with blk:
            st["done"] += 1
            done = st["done"]
        _emit(type="progress", phase="Slug harvest", current=done, total=n,
              message=f"{name[:40]}")
        if checkpoint and done % 40 == 0:
            checkpoint()

    with ThreadPoolExecutor(max_workers=INDEED_WORKERS) as ex:
        list(ex.map(work, targets))
    try:
        save_cache(cache)
    except Exception:
        pass
    if st["stop"]:
        log("HARVEST     stopped early — budget cap reached")
    log(f"HARVEST     cached {st['cached']} ATS slugs (FREE next run); "
        f"upgraded {st['upgraded']} counts (${budget.spent():.2f} spent)")


def run_harvest_only(input_path, output_path, since="7"):
    """Resume ONLY the #4 slug-harvest on Indeed-resolved low-count companies."""
    rows, cols, out_fields, results, log = _load_resume(input_path, output_path)
    cache = load_cache()
    log(f"HARVEST-ONLY loaded {len(results)} rows")
    budget.begin_run()  # pin per-run $15 hard-stop baseline
    ck_lock = threading.Lock()

    def checkpoint():
        with ck_lock:
            _write_output(rows, results, out_fields, output_path)

    _harvest_fill(rows, results, cols, cache, log, since, checkpoint=checkpoint)
    checkpoint()
    strong = sum(1 for r in results if r.get("frontline_20plus") == "yes")
    ok = sum(1 for r in results if r.get("status") == "ok")
    print(f"\nDone (harvest). ok={ok} | strong(20+)={strong}  ->  {output_path}")
    _emit(type="done", counts={"ok": ok}, strong=strong, total=len(rows),
          output=output_path)


GOOGLE_MIN_EMP = 100   # only worth $0.02/job on larger companies
GOOGLE_TIER_CAP = 6.0  # hard ceiling for this (pricey) tier, on top of the run cap


def _google_fill(rows, results, cols, log, since="7", checkpoint=None):
    """#2 Google Jobs (PAID, ~$0.02/job) — targeted at the LARGER still-unresolved
    companies only (>= GOOGLE_MIN_EMP employees), employer-filtered. Bounded by its
    own tier cap AND the per-run/monthly budget. Never a false zero."""
    if not apify_base.enabled():
        log("GOOGLE      skipped — no Apify token")
        return
    synced = budget.sync_from_apify()
    if synced is not None:
        log(f"GOOGLE      ledger reconciled to real Apify usage: ${synced:.2f}")
    emp_col = next((c for c in (rows[0].keys() if rows else []) if "employee" in c.lower()), None)

    def emp(idx):
        try:
            return int((rows[idx].get(emp_col, "") or "0").replace(",", ""))
        except (ValueError, TypeError):
            return 0

    targets = []
    for idx, res in enumerate(results):
        if res.get("status") == "ok":
            continue
        name = (rows[idx].get(cols["name"], "") if cols["name"] else "").strip()
        if name and (not emp_col or emp(idx) >= GOOGLE_MIN_EMP):
            targets.append((idx, name))
    n = len(targets)
    tier_start = budget.spent()
    worst = 12 * budget.COST_GOOGLE  # ~one page of results
    _emit(type="phase", phase="Google Jobs lookup",
          detail="targeted high-value pass", current=0, total=n)
    log(f"GOOGLE      targeted pass on {n} larger (>={GOOGLE_MIN_EMP} emp) unresolved "
        f"companies (~$0.20 each, tier cap ${GOOGLE_TIER_CAP:.0f}); budget ${budget.spent():.2f}")
    blk = threading.Lock()
    st = {"filled": 0, "done": 0, "stop": False}

    def work(item):
        idx, name = item
        with blk:
            if (st["stop"] or budget.remaining() < worst
                    or budget.spent() - tier_start >= GOOGLE_TIER_CAP):
                st["stop"] = True
                return
        try:
            jobs, raw, found = google_jobs.fetch_company(name, since, row=rows[idx])
        except Exception as e:
            with blk:
                st["done"] += 1
            log(f"GOOGLE-FAIL {name[:30]}: {str(e)[:80]}")
            return
        frontline = filter_roles(jobs)
        with blk:
            st["done"] += 1
            if jobs:
                res = results[idx]
                res["frontline_role_count"] = len(frontline)
                res["frontline_20plus"] = _twentyplus(len(frontline))
                res["frontline_roles"] = ROLE_SEP.join(
                    f"{j['title']} — {j['location']}".rstrip(" —") for j in frontline)
                if not res.get("ats_platform"):
                    res["ats_platform"] = "google"
                res["status"] = "ok"
                res["count_method"] = "google"
                if frontline:
                    st["filled"] += 1
            done = st["done"]
        _emit(type="progress", phase="Google Jobs lookup", current=done, total=n,
              message=f"{name[:40]}")
        if checkpoint and done % 20 == 0:
            checkpoint()

    with ThreadPoolExecutor(max_workers=6) as ex:  # lower concurrency — pricey tier
        list(ex.map(work, targets))
    if st["stop"]:
        log("GOOGLE      stopped early — tier/budget cap reached")
    log(f"GOOGLE      filled {st['filled']} companies "
        f"(${budget.spent() - tier_start:.2f} this tier, ${budget.spent():.2f} total)")


def run_google_only(input_path, output_path, since="7"):
    """Resume ONLY the targeted Google Jobs tier on larger unresolved companies."""
    _run_only(input_path, output_path, since, _google_fill, "GOOGLE-ONLY")


def _run_only(input_path, output_path, since, fill_fn, tag):
    """Shared driver for the single-tier resume paths (--indeed-only/--linkedin-only)."""
    rows, cols, out_fields, results, log = _load_resume(input_path, output_path)
    n_ok = sum(1 for r in results if r.get("status") == "ok")
    log(f"{tag} loaded {len(results)} rows ({n_ok} already resolved)")
    budget.begin_run()  # pin per-run $15 hard-stop baseline
    ck_lock = threading.Lock()

    def checkpoint():
        with ck_lock:
            _write_output(rows, results, out_fields, output_path)

    fill_fn(rows, results, cols, log, since, checkpoint=checkpoint)
    checkpoint()
    counts = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    strong = sum(1 for r in results if r.get("frontline_20plus") == "yes")
    print(f"\nDone ({tag.strip()}). {summary} | strong(20+)={strong}  ->  {output_path}")
    _emit(type="done", counts=counts, strong=strong, total=len(rows), output=output_path)


def run_indeed_only(input_path, output_path, since="7"):
    """Resume PAID Indeed sweep ONLY on an existing enriched output."""
    _run_only(input_path, output_path, since, _indeed_fill, "INDEED-ONLY")


def run_linkedin_only(input_path, output_path, since="7"):
    """Resume PAID LinkedIn sweep ONLY (escalation for Indeed-misses)."""
    _run_only(input_path, output_path, since, _linkedin_fill, "LINKEDIN-ONLY")


def _load_resume(input_path, output_path):
    """Shared loader for the *-only resume paths: read input rows + ALL prior
    results (ok and unresolved) so detected platforms/names survive without a
    re-scan. Returns (rows, cols, out_fields, results, log)."""
    with open(input_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)
    cols = {
        "name": _find_col(fieldnames, "company name", "company", "name"),
        "website": _find_col(fieldnames, "website", "company website", "domain", "url"),
        "tech": _find_col(fieldnames, "technologies", "technology", "tech"),
    }
    if not cols["website"]:
        sys.exit("ERROR: could not find a Website column in the input CSV.")
    if not os.path.exists(output_path):
        sys.exit(f"ERROR: resume needs an existing {output_path} from a prior run.")
    out_fields = fieldnames + [c for c in OUT_COLS if c not in fieldnames]

    def log(msg):
        print(msg, file=sys.stderr)
        _emit(type="log", message=str(msg))

    # Key by (company, website) — NOT website alone. Many Apollo exports have
    # blank or duplicated Website cells (e.g. companies resolved by name-search),
    # and a website-only key collapses/loses those rows on reload.
    def _key(r):
        nm = (r.get(cols["name"], "") if cols["name"] else "").strip().lower()
        st = (r.get(cols["website"], "") if cols["website"] else "").strip().lower()
        return (nm, st)

    prior = {}
    with open(output_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            prior[_key(r)] = {k: r.get(k, "") for k in OUT_COLS}

    def _blank():
        return {k: "" for k in OUT_COLS} | {"status": "unresolved",
                                            "frontline_role_count": ""}
    results = [dict(prior.get(_key(row), _blank())) for row in rows]
    return rows, cols, out_fields, results, log


def run_apify_only(input_path, output_path, since="7"):
    """Resume the PAID Apify phase ONLY — load an existing enriched output (with
    every company's already-detected platform) and run _apify_fill directly,
    skipping the free static/headless re-scan. Use after a full free run when you
    want to opt into Apify for the still-unresolved locked-platform companies."""
    rows, cols, out_fields, results, log = _load_resume(input_path, output_path)
    n_ok = sum(1 for r in results if r.get("status") == "ok")
    budget.begin_run()  # pin per-run $15 hard-stop baseline
    log(f"APIFY-ONLY  loaded {len(results)} rows ({n_ok} already resolved); "
        f"filling the rest via paid Apify")
    _apify_fill(rows, results, cols, log, since)
    _write_output(rows, results, out_fields, output_path)

    counts = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    strong = sum(1 for r in results if r.get("frontline_20plus") == "yes")
    print(f"\nDone (apify-only). {summary} | strong(20+)={strong}  ->  {output_path}")
    _emit(type="done", counts=counts, strong=strong, total=len(rows), output=output_path)


def _write_output(rows, results, out_fields, output_path):
    """Atomically write rows+results to output_path (write tmp, then replace)."""
    tmp = str(output_path) + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        for row, result in zip(rows, results):
            if result is None:
                continue
            out_row = dict(row)
            out_row.update({k: result.get(k, "") for k in OUT_COLS})
            writer.writerow(out_row)
    os.replace(tmp, output_path)


HEADLESS_CHUNK = 30           # companies per isolated worker subprocess
HEADLESS_CHUNK_TIMEOUT = 300  # hard wall-clock per chunk; a hung worker is killed


def headless_resolve_companies(websites, cache):
    """Render the given companies (headless) and return {website: result-dict} for
    those that RESOLVE (ATS found, or custom-page generic jobs with >=1 frontline).
    Runs INSIDE the isolated worker subprocess (headless_worker.py)."""
    out = {}
    resolved = resolve_headless_batch(websites, cache)
    for site, res in resolved.items():
        if res.get("status") == "ok":                 # real fetchable ATS found
            try:
                jobs = fetchers.fetch(res["platform"], res["slug"])
            except Exception:
                continue
            frontline = filter_roles(jobs)
            platform, method, slug = res["platform"], "headless", res.get("slug", "")
        elif res.get("generic_jobs"):                 # Option B: custom page links
            frontline = filter_roles(res["generic_jobs"])
            if not frontline:
                continue
            platform, method, slug = "custom", "generic", ""
        else:
            continue
        out[site] = {
            "frontline_role_count": len(frontline),
            "frontline_20plus": _twentyplus(len(frontline)),
            "frontline_roles": ROLE_SEP.join(
                f"{j['title']} — {j['location']}".rstrip(" —") for j in frontline),
            "ats_platform": platform, "ats_slug": slug,
            "ats_url": res.get("ats_url", ""), "status": "ok", "count_method": method,
        }
    return out


def _kill_tree(proc):
    """Force-kill a subprocess and ALL its children (e.g. headless Chrome),
    cross-platform: taskkill /T on Windows, process-group kill on macOS/Linux."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # whole session group
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _headless_fill(rows, results, cols, cache, log, checkpoint=None):
    """FREE fallback for JS/custom careers pages. Each chunk runs in an ISOLATED
    subprocess (headless_worker.py) with a hard timeout: if the browser freezes,
    the whole worker is force-killed and the run moves on — a single bad site can
    never stall the job. Zero Apify/LLM cost."""
    import headless as _hl
    if not _hl.available():
        log("HEADLESS    skipped — Playwright not installed "
            "(pip install playwright && python -m playwright install chromium)")
        return
    idx_by_site = {}
    for idx, res in enumerate(results):
        if res["status"] == "ok":
            continue
        site = rows[idx].get(cols["website"], "") if cols["website"] else ""
        if site:
            idx_by_site.setdefault(site, []).append(idx)
    if not idx_by_site:
        return

    sites = list(idx_by_site)
    nsites = len(sites)
    nchunks = (nsites + HEADLESS_CHUNK - 1) // HEADLESS_CHUNK
    log(f"HEADLESS    {nsites} companies in {nchunks} isolated chunks of "
        f"{HEADLESS_CHUNK} (a chunk is killed if it hangs > {HEADLESS_CHUNK_TIMEOUT}s)")
    _emit(type="phase", phase="Headless render",
          detail="rendering JS/custom pages in crash-proof worker subprocesses",
          current=0, total=nsites)

    worker = os.path.join(_ROOT, "headless_worker.py")
    filled = 0
    for ci in range(nchunks):
        chunk = sites[ci * HEADLESS_CHUNK:(ci + 1) * HEADLESS_CHUNK]
        in_fd, in_path = tempfile.mkstemp(suffix=".json", dir=_ROOT)
        out_path = in_path + ".out"
        with os.fdopen(in_fd, "w", encoding="utf-8") as f:
            json.dump({"websites": chunk, "threshold": config.FRONTLINE_THRESHOLD,
                       "output": out_path}, f)
        # start_new_session (POSIX): put the worker + its Chromium children in a
        # fresh process group so _kill_tree can kill them all together.
        proc = subprocess.Popen([sys.executable, "-u", worker, in_path], cwd=_ROOT,
                                start_new_session=(os.name != "nt"))
        try:
            proc.wait(timeout=HEADLESS_CHUNK_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            log(f"HEADLESS    chunk {ci+1}/{nchunks} hung -> force-killed, moving on")
        resolved = {}
        try:
            if os.path.exists(out_path):
                with open(out_path, encoding="utf-8") as f:
                    resolved = json.load(f)
        except (OSError, ValueError):
            resolved = {}
        for p in (in_path, out_path):
            try:
                os.remove(p)
            except OSError:
                pass
        for site, r in resolved.items():
            for idx in idx_by_site.get(site, []):
                results[idx] = r
                filled += 1
        _emit(type="progress", phase="Headless render",
              current=min((ci + 1) * HEADLESS_CHUNK, nsites), total=nsites,
              message=f"chunk {ci+1}/{nchunks} done ({filled} resolved)")
        log(f"HEADLESS    chunk {ci+1}/{nchunks} done — {filled} resolved so far")
        if checkpoint:
            checkpoint()  # persist after every chunk
    log(f"HEADLESS    resolved {filled} previously-unresolved companies (free)")


def _apify_fill(rows, results, cols, log, since="7"):
    """Fill still-unresolved rows by batch-querying the configured Apify actors
    by company domain. Dedicated actors (full boards) run first with a client-side
    date window; the catch-all aggregator mops up with a server-side date filter.
    Guarded by the monthly budget cap."""
    platforms = apify_actor.enabled_platforms()
    if not platforms:
        return

    n_unres = sum(1 for r in results if r["status"] != "ok")
    _emit(type="phase", phase="Apify lookup",
          detail="paid catch-all for locked platforms (Paycom/Dayforce/...)",
          current=0, total=n_unres)
    synced = budget.sync_from_apify()
    if synced is not None:
        log(f"APIFY       ledger reconciled to real Apify usage: ${synced:.2f}")
    date_after, cutoff, all_time = _date_window(since)
    window = "all time" if all_time else f"posted on/after {date_after}"
    log(f"APIFY       date window: {window} | budget: ${budget.spent():.2f} spent, "
        f"${budget.remaining():.2f} of ${budget.cap():.0f} left")
    if not budget.can_continue():
        log("APIFY       monthly budget cap reached — skipping Apify (no false zeros)")
        return

    # Catch-all only mops up DETECTED-but-locked platforms that have no dedicated
    # actor. We deliberately do NOT blast no-signature companies through the
    # $0.012/job catch-all (uncertain yield x expensive = wasted spend).
    catchall_platforms = fetchers.LOCKED_DOWN - set(apify_actor.ACTORS)

    # registrable domain -> list of still-unresolved result indices, optionally
    # restricted to rows whose DETECTED platform is in `only_platforms`.
    def unresolved_domains(only_platforms=None):
        dmap = {}
        for idx, res in enumerate(results):
            if res["status"] == "ok":
                continue
            if only_platforms is not None and res.get("ats_platform") not in only_platforms:
                continue
            site = rows[idx].get(cols["website"], "") if cols["website"] else ""
            dom = apify_actor.registrable_domain(site)
            if dom:
                dmap.setdefault(dom, []).append(idx)
        return dmap

    def _fill(idx, jobs, platform, method):
        res = results[idx]
        frontline = filter_roles(jobs)
        res["frontline_role_count"] = len(frontline)
        res["frontline_20plus"] = _twentyplus(len(frontline))
        res["frontline_roles"] = ROLE_SEP.join(
            f"{j['title']} — {j['location']}".rstrip(" —") for j in frontline
        )
        res["ats_platform"] = platform
        res["status"] = "ok"
        res["count_method"] = method

    # ---- 2a: dedicated per-source actors (full boards, accurate) ----
    for platform in platforms:
        if not budget.can_continue():
            break
        dmap = unresolved_domains({platform})  # only companies DETECTED as this ATS
        if not dmap:
            continue
        domains = list(dmap)
        log(f"APIFY       {platform}: querying {len(domains)} detected domains "
            f"(<= {apify_actor.per_company_limit()} jobs each @ $0.002)...")
        try:
            by_domain, truncated, budget_hit = apify_actor.batch_fetch(platform, domains)
        except Exception as e:
            log(f"APIFY-FAIL  {platform}: {e}")
            continue
        if truncated:
            log(f"APIFY WARN  {platform}: hit per-run job cap — counts may undercount")
        if budget_hit:
            log(f"APIFY       {platform}: stopped early — budget cap reached")
        filled = 0
        for dom, jobs in by_domain.items():
            recent = [j for j in jobs if _date_ok(j.get("date_posted"), cutoff)]
            for idx in dmap.get(dom, []):
                if results[idx]["status"] == "ok":
                    continue
                _fill(idx, recent, platform, f"apify-full:{platform}")
                filled += 1
        log(f"APIFY       {platform}: filled {filled} companies "
            f"(${budget.spent():.2f} spent)")

    # ---- 2b: catch-all aggregator (covers paycom/dayforce/etc; lower confidence) ----
    if apify_actor.catchall_enabled() and budget.can_continue():
        dmap = unresolved_domains(catchall_platforms)  # detected-locked only
        if dmap:
            log(f"APIFY       catch-all: querying {len(dmap)} detected-locked domains "
                f"(<= {apify_actor.per_company_limit()} jobs each @ $0.012)...")
            try:
                by_domain, truncated, budget_hit = apify_actor.batch_fetch_catchall(
                    list(dmap), date_after=date_after, all_time=all_time)
                if truncated:
                    log("APIFY WARN  catch-all: hit per-run job cap — may undercount")
                if budget_hit:
                    log("APIFY       catch-all: stopped early — budget cap reached")
                filled = 0
                for dom, entry in by_domain.items():
                    # server-side date-filtered already; client-side as belt-and-braces
                    recent = [j for j in entry["jobs"]
                              if _date_ok(j.get("date_posted"), cutoff)]
                    for idx in dmap.get(dom, []):
                        if results[idx]["status"] == "ok":
                            continue
                        _fill(idx, recent, entry["source"], "apify-aggregator")
                        filled += 1
                log(f"APIFY       catch-all: filled {filled} companies "
                    f"(${budget.spent():.2f} spent)")
            except Exception as e:
                log(f"APIFY-FAIL  catch-all: {e}")
    log(f"APIFY       month-to-date spend: ${budget.spent():.2f} / ${budget.cap():.0f}")


def main():
    ap = argparse.ArgumentParser(description="Frontline-hiring signal scraper")
    ap.add_argument("input", help="Apollo CSV export")
    ap.add_argument("output", nargs="?", help="output CSV (default: <input>.enriched.csv)")
    ap.add_argument("--limit", type=int, default=None, help="process first N rows")
    ap.add_argument("--since", choices=SINCE_CHOICES, default="7",
                    help="Apify recency window in days (or 'all'). Default 7.")
    ap.add_argument("--threshold", type=int, default=config.FRONTLINE_THRESHOLD,
                    help=f"frontline count we care about (>= this = strong). "
                         f"Default {config.FRONTLINE_THRESHOLD}.")
    ap.add_argument("--no-headless", action="store_true",
                    help="skip the free Playwright render fallback for JS-only careers pages")
    ap.add_argument("--apify", action="store_true",
                    help="enable the PAID Apify ATS actors (most expensive tier; last resort)")
    ap.add_argument("--indeed", action="store_true",
                    help="enable the PAID Indeed sweep (cheap ~$0.00008/job; runs before headless)")
    ap.add_argument("--linkedin", action="store_true",
                    help="enable the PAID LinkedIn sweep (cheap ~$0.0009/job; Indeed-miss escalation)")
    ap.add_argument("--harvest", action="store_true",
                    help="after Indeed, read each low-count company's real ATS free and "
                         "cache the slug (use with --indeed)")
    ap.add_argument("--apify-only", action="store_true",
                    help="skip the free static/headless scan; run ONLY the paid Apify "
                         "phase on an existing enriched output (resume the paid step)")
    ap.add_argument("--indeed-only", action="store_true",
                    help="run ONLY the PAID Indeed sweep (by company name, ~$0.00008/job) "
                         "on still-unresolved companies in an existing enriched output")
    ap.add_argument("--linkedin-only", action="store_true",
                    help="run ONLY the PAID LinkedIn sweep (~$0.0009/job) on still-"
                         "unresolved companies — escalation for Indeed-misses")
    ap.add_argument("--harvest-only", action="store_true",
                    help="run ONLY the #4 slug-harvest: read the real ATS (free) for "
                         "low-count Indeed companies and cache slugs for future runs")
    ap.add_argument("--google-only", action="store_true",
                    help="run ONLY the targeted Google Jobs tier (PAID ~$0.02/job) on "
                         "larger still-unresolved companies")
    args = ap.parse_args()
    config.FRONTLINE_THRESHOLD = args.threshold

    output = args.output
    if not output:
        if args.input.lower().endswith(".csv"):
            output = args.input[:-4] + ".enriched.csv"
        else:
            output = args.input + ".enriched.csv"

    if args.google_only:
        run_google_only(args.input, output, since=args.since)
    elif args.harvest_only:
        run_harvest_only(args.input, output, since=args.since)
    elif args.linkedin_only:
        run_linkedin_only(args.input, output, since=args.since)
    elif args.indeed_only:
        run_indeed_only(args.input, output, since=args.since)
    elif args.apify_only:
        run_apify_only(args.input, output, since=args.since)
    else:
        run(args.input, output, limit=args.limit, since=args.since,
            headless_enabled=not args.no_headless, apify_enabled=args.apify,
            indeed_enabled=args.indeed, linkedin_enabled=args.linkedin,
            harvest_enabled=args.harvest)


if __name__ == "__main__":
    main()
