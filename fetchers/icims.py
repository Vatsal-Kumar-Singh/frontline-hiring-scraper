"""iCIMS fetcher.

The default career portal is a JS shell, but `?in_iframe=1` returns
server-rendered HTML (the iframe the page loads itself):
    GET https://careers-<slug>.icims.com/jobs/search?ss=1&in_iframe=1&pr=<page>
Each job: an `iCIMS_Anchor` (title in the `title="<id> - <title>"` attr / <h3>)
and a `col-xs-6 header left` span (location). Paginates via pr=0,1,2,...; total
pages from "Page 1 of N".

NOTE: many iCIMS tenants sit behind AWS WAF and answer this endpoint with a 405
"Human Verification" challenge. Those raise FetchError and resolve as
unresolved (never a false zero) — they need a headless/Apify path or a
residential IP. Slug = the careers subdomain (without the `careers-` prefix).
"""
from __future__ import annotations

import re

from .base import MAX_PAGES, get_text, reached_threshold, strip_html

PLATFORM = "icims"

_ANCHOR = re.compile(
    r'<a[^>]*class="[^"]*iCIMS_Anchor[^"]*"[^>]*title="([^"]*)"[^>]*>(.*?)</a>',
    re.I | re.S,
)
_LOC = re.compile(r'col-xs-6 header left"\s*>\s*<span>(.*?)</span>', re.I | re.S)
_PAGES = re.compile(r"Page\s+\d+\s+of\s+(\d+)", re.I)


def _url(slug: str) -> str:
    return f"https://careers-{slug}.icims.com/jobs/search"


def _params(page: int) -> dict:
    return {"ss": 1, "in_iframe": 1, "pr": page}


def _parse_page(html: str) -> list[dict]:
    # Both anchors and location spans are in document order; walk a single cursor
    # over the locations, pairing each title with the nearest preceding location
    # span (location renders before the title in each row). O(anchors + locs).
    locs = [(m.start(), strip_html(m.group(1))) for m in _LOC.finditer(html)]
    out: list[dict] = []
    i = 0
    loc = ""
    for am in _ANCHOR.finditer(html):
        while i < len(locs) and locs[i][0] < am.start():
            loc = locs[i][1]
            i += 1
        attr_title = am.group(1) or ""
        # "<id> - <Title>"  ->  Title; fall back to the <h3> inner text
        title = attr_title.split(" - ", 1)[-1].strip() if " - " in attr_title else ""
        if not title:
            title = strip_html(am.group(2))
        out.append({"title": title, "location": loc})
    return out


def fetch(slug: str) -> list[dict]:
    first = get_text(_url(slug), params=_params(0))
    out = _parse_page(first)
    pm = _PAGES.search(first)
    total_pages = int(pm.group(1)) if pm else 1
    for pr in range(1, min(total_pages, MAX_PAGES)):
        html = get_text(_url(slug), params=_params(pr))
        page_jobs = _parse_page(html)
        if not page_jobs:
            break
        out.extend(page_jobs)
        if reached_threshold(out):
            break
    return out


def verify(slug: str) -> bool:
    try:
        html = get_text(_url(slug), params=_params(0))
    except Exception:
        return False
    return "iCIMS" in html and "jobs/search" in html
