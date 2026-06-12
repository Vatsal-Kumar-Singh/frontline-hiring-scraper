"""Best-effort monthly Apify spend guard.

Apify bills these actors pay-per-result, and the price differs by actor:
  dedicated *-jobs-api (adp/icims/paradox): $0.002/job
  career-site catch-all aggregator:         $0.012/job   (6x more!)
(Prices verified against the live actor pricing for FREE/BRONZE tiers — the
conservative high end; SILVER+ is cheaper, so we never UNDER-count spend.)

This tracks spend in a local monthly ledger and lets the pipeline stop before
exceeding the cap. It is *soft* — a single in-flight run can overshoot by its
own size (bounded small because we query one company at a time) — so the REAL
hard wall is the Apify account monthly limit (Settings -> Limits / set via API).

Cap default $10; override with APIFY_MONTHLY_CAP_USD in secrets.local.json.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

# Per-source price (USD/job), conservative FREE/BRONZE-tier rates.
COST_DEDICATED = 0.002    # adp / icims / paradox dedicated actors
COST_CATCHALL = 0.012     # career-site catch-all aggregator
COST_INDEED = 0.00008     # kaix/indeed-scraper — ~150x cheaper than catch-all
COST_LINKEDIN = 0.0009    # worldunboxer/rapid-linkedin-scraper (Indeed-miss escalation)
COST_GOOGLE = 0.02        # igview google-jobs-scraper — pricey; targeted use only
COST_PER_JOB = COST_DEDICATED  # back-compat default
DEFAULT_CAP = 65.0       # monthly backstop (override via APIFY_MONTHLY_CAP_USD)
DEFAULT_RUN_CAP = 15.0   # HARD STOP per single run (override via APIFY_RUN_CAP_USD)

# Set at the start of a run so the per-run cap measures only THIS run's spend.
# This run's spend, tracked IN MEMORY so the per-run cap can't be defeated by the
# ledger file being reset/edited mid-run (observed in practice).
_run_spent: float = 0.0

_DIR = Path(__file__).resolve().parent
_LEDGER = _DIR / "apify_spend.json"
_SECRETS = _DIR / "secrets.local.json"


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _secret(key, default):
    try:
        return float(json.loads(_SECRETS.read_text(encoding="utf-8")).get(key, default))
    except (OSError, ValueError, TypeError):
        return default


def cap() -> float:
    """Monthly spend ceiling (backstop)."""
    return _secret("APIFY_MONTHLY_CAP_USD", DEFAULT_CAP)


def run_cap() -> float:
    """Hard stop for a SINGLE run — the primary guard for a 7-day run."""
    return _secret("APIFY_RUN_CAP_USD", DEFAULT_RUN_CAP)


def begin_run() -> None:
    """Mark the start of a run. Reconciles the ledger to real Apify usage, then
    pins a baseline so the per-run cap counts only spend made AFTER this point."""
    global _run_spent
    sync_from_apify()
    _run_spent = 0.0


def _read() -> dict:
    try:
        d = json.loads(_LEDGER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    if d.get("month") != _month():  # new month -> reset
        d = {"month": _month(), "spent_usd": 0.0}
    return d


def _write(d: dict) -> None:
    _LEDGER.write_text(json.dumps(d, indent=2), encoding="utf-8")


def spent() -> float:
    return round(_read()["spent_usd"], 4)


def remaining() -> float:
    """Spend headroom = the TIGHTER of the monthly backstop and the per-run cap.
    All guards use this, so a run can never exceed $15 (or the monthly ceiling)."""
    monthly = max(0.0, cap() - spent())
    per_run = max(0.0, run_cap() - _run_spent)  # in-memory; reset-proof
    return round(min(monthly, per_run), 4)


def can_continue() -> bool:
    return remaining() > 0


def _apify_token() -> str:
    tok = os.environ.get("APIFY_TOKEN", "").strip()
    if not tok:
        try:
            tok = json.loads(_SECRETS.read_text(encoding="utf-8")).get(
                "APIFY_TOKEN", "").strip()
        except (OSError, ValueError):
            tok = ""
    return tok


def sync_from_apify() -> float | None:
    """Reconcile the local ledger with Apify's REAL monthly usage (the source of
    truth). A lost/reset/edited local file can otherwise blind the budget guard and
    risk overspend. Takes the MAX of local vs real so we never UNDER-count. Returns
    the synced total, or None if the usage couldn't be fetched (best-effort)."""
    tok = _apify_token()
    if not tok:
        return None
    try:
        r = requests.get("https://api.apify.com/v2/users/me/limits",
                         params={"token": tok}, timeout=20)
        usd = r.json().get("data", {}).get("current", {}).get("monthlyUsageUsd")
    except (requests.RequestException, ValueError):
        return None
    if usd is None:
        return None
    d = _read()
    d["spent_usd"] = round(max(d.get("spent_usd", 0.0), float(usd)), 4)
    _write(d)
    return d["spent_usd"]


def record_jobs(n_jobs: int, cost_per_job: float = COST_PER_JOB) -> float:
    """Add n_jobs * cost_per_job to this month's ledger AND the in-memory per-run
    counter; return the new monthly total. Pass COST_CATCHALL for the catch-all
    actor, COST_DEDICATED for the rest."""
    global _run_spent
    amount = max(0, n_jobs) * float(cost_per_job)
    _run_spent += amount
    d = _read()
    d["spent_usd"] = round(d["spent_usd"] + amount, 4)
    _write(d)
    return d["spent_usd"]
