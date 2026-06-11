"""JazzHR fetcher.

Public boards are server-rendered HTML at:
    GET https://<slug>.applytojob.com/apply/jobs
The listing table has one row per job: a `job_title_link` anchor (title) and a
trailing `<td>` (location). All jobs render on one page (no pagination).
Verified live against dtexsystems.applytojob.com. Slug = the subdomain.
"""
from __future__ import annotations

import re

from .base import get_text, strip_html

PLATFORM = "jazzhr"

_ROW = re.compile(r'<tr id="row_job_', re.I)
_ANCHOR = re.compile(r'<a[^>]*class="job_title_link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.I | re.S)
_CODE = re.compile(r"/details/([A-Za-z0-9]+)")


def _url(slug: str) -> str:
    return f"https://{slug}.applytojob.com/apply/jobs"


def fetch(slug: str) -> list[dict]:
    html = get_text(_url(slug))
    out: list[dict] = []
    seen = set()
    for chunk in _ROW.split(html)[1:]:
        am = _ANCHOR.search(chunk)
        if not am:
            continue
        href, title = am.group(1), strip_html(am.group(2))
        code_m = _CODE.search(href)
        code = code_m.group(1) if code_m else href
        if code in seen:
            continue
        seen.add(code)
        # location is the next <td> after the title cell
        tdm = _TD.search(chunk, am.end())
        location = strip_html(tdm.group(1)) if tdm else ""
        out.append({"title": title, "location": location})
    return out


def verify(slug: str) -> bool:
    try:
        html = get_text(_url(slug))
    except Exception:
        return False
    return "job_title_link" in html or "applytojob" in html
