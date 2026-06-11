"""Culinary Agents fetcher (hospitality-specific job board).

Company group pages server-render job cards with clean data-attributes:
    https://culinaryagents.com/groups/<slug>/jobs[?page=N]
    <a class="ca-single-job-card" data-jobid=".." data-title=".." data-entity="<property>" ...>
Paginates via ?page=N. Verified live against 22-thomas-keller (46 jobs).

Slug = the group slug (e.g. "22-thomas-keller").
"""
from __future__ import annotations

import html
import re

from .base import MAX_PAGES, get_text, reached_threshold

PLATFORM = "culinaryagents"
_CARD = re.compile(r'<a[^>]*class="[^"]*ca-single-job-card[^"]*"[^>]*>', re.I)
_ATTR_RE = {
    name: re.compile(name + r'="([^"]*)"', re.I)
    for name in ("data-jobid", "data-title", "data-entity")
}


def _attr(tag: str, name: str) -> str:
    m = _ATTR_RE[name].search(tag)
    return html.unescape(m.group(1)).strip() if m else ""


PAGE = 24  # board returns 24 cards/page; paginate via ?offset=N


def _url(slug: str, offset: int) -> str:
    base = f"https://culinaryagents.com/groups/{slug}/jobs"
    return base if offset <= 0 else f"{base}?offset={offset}"


def fetch(slug: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for page in range(MAX_PAGES):
        html_text = get_text(_url(slug, page * PAGE))
        new = 0
        for tag in _CARD.finditer(html_text):
            t = tag.group(0)
            jid = _attr(t, "data-jobid")
            title = _attr(t, "data-title")
            if not title or (jid and jid in seen):
                continue
            if jid:
                seen.add(jid)
            new += 1
            out.append({"title": title, "location": _attr(t, "data-entity")})
        if new == 0 or reached_threshold(out):
            break
    return out


def verify(slug: str) -> bool:
    try:
        return "ca-single-job-card" in get_text(_url(slug, 0))
    except Exception:
        return False
