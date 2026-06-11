"""EasyApply fetcher (easyapply.co).

Each company/property has its own portal `<slug>.easyapply.co` that
server-renders its jobs as `easyapply.co/job/<title>-<id>` links — parseable
with plain HTTP, no API needed. Multi-location employers (e.g. RAM Hotels = 31
hotel portals) are handled by passing a pipe-joined slug; counts are summed and
de-duped across all portals.

Slug: "<sub>" or "<sub1>|<sub2>|...". Title is derived from the job slug.
"""
from __future__ import annotations

import re

from .base import FetchError, get_text, reached_threshold

PLATFORM = "easyapply"
_JOB = re.compile(r"easyapply\.co/job/([a-z0-9][a-z0-9\-]*)", re.I)
_TRAIL_ID = re.compile(r"-\d+$")


def _url(sub: str) -> str:
    return f"https://{sub}.easyapply.co"


def _title_from_slug(job_slug: str) -> str:
    return _TRAIL_ID.sub("", job_slug).replace("-", " ").strip().title()


def fetch(slug: str) -> list[dict]:
    subs = [s.strip() for s in slug.split("|") if s.strip()]
    out: list[dict] = []
    seen: set[str] = set()
    errors = 0
    for sub in subs:
        try:
            html = get_text(_url(sub))
        except FetchError:
            errors += 1
            continue
        for m in _JOB.finditer(html):
            jid = m.group(1).lower()
            if jid in seen:
                continue
            seen.add(jid)
            out.append({"title": _title_from_slug(jid), "location": sub})
        # enough to confirm 20+ — don't fetch the remaining portals
        if reached_threshold(out):
            break
    if subs and errors == len(subs):
        raise FetchError("easyapply: all portals failed")
    return out


def verify(slug: str) -> bool:
    sub = slug.split("|")[0].strip()
    try:
        html = get_text(_url(sub))
    except Exception:
        return False
    return "easyapply" in html.lower()
