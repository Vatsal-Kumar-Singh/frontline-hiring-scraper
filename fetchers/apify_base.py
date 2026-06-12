"""Generic Apify actor caller — the bridge for detect-only platforms.

The detect-only ATSes (Dayforce, Paradox, ADP, Paycom, SuccessFactors, etc.)
can't be read with plain HTTP, but Apify actors can render/scrape them. This
module runs an actor synchronously and returns its dataset items; per-platform
adapters (see apify_actor.py) build the actor input and normalize the output.

Auth: set the APIFY_TOKEN environment variable. No token => Apify disabled
(platforms stay detect-only / unresolved — never a false zero).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from .base import FetchError

APIFY_BASE = "https://api.apify.com/v2"
RUN_TIMEOUT = 300  # seconds; actors can be slow

# gitignored local secrets file (project root), used if APIFY_TOKEN env is unset.
_SECRETS_PATH = Path(__file__).resolve().parent.parent / "secrets.local.json"


_token_cache: str | None = None


def token() -> str:
    """APIFY_TOKEN from the environment, else from gitignored secrets.local.json.
    Resolved once and cached (it doesn't change within a process)."""
    global _token_cache
    if _token_cache is not None:
        return _token_cache
    tok = os.environ.get("APIFY_TOKEN", "").strip()
    if not tok:
        try:
            tok = json.loads(_SECRETS_PATH.read_text(encoding="utf-8")).get(
                "APIFY_TOKEN", ""
            ).strip()
        except (OSError, ValueError):
            tok = ""
    _token_cache = tok
    return tok


def enabled() -> bool:
    return bool(token())


def run_actor(actor_id: str, run_input: dict) -> list[dict]:
    """Run an actor synchronously and return its dataset items as list[dict].

    Uses run-sync-get-dataset-items so we get results in one blocking call.
    actor_id is the Apify actor, e.g. "apify/web-scraper" or "user~actor-name".
    """
    tok = token()
    if not tok:
        raise FetchError("APIFY_TOKEN not set — Apify actors disabled")
    # actor id in the path uses '~' between user and name
    actor_path = actor_id.replace("/", "~")
    url = f"{APIFY_BASE}/acts/{actor_path}/run-sync-get-dataset-items"
    # Retry transient network/5xx errors with backoff so a brief connectivity blip
    # can't error an entire tier (one outage once wiped a whole Indeed pass).
    last_exc = None
    for attempt in range(3):
        try:
            r = requests.post(
                url, params={"token": tok}, json=run_input, timeout=RUN_TIMEOUT + 15
            )
        except requests.RequestException as e:
            last_exc = e
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code >= 500 or r.status_code == 429:
            last_exc = FetchError(f"Apify {r.status_code}: {r.text[:120]}")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise FetchError(f"Apify actor {actor_id} -> {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
        except ValueError as e:
            raise FetchError(f"Apify returned non-JSON: {e}") from e
        return data if isinstance(data, list) else data.get("items", [])
    raise FetchError(f"Apify run failed after retries: {last_exc}")
