"""Oracle Taleo fetcher (via the public RSS feed).

Taleo career boards are JS/session-bound, but every board exposes a public RSS
feed that lists ALL requisitions with title + taleo:location:
    https://<sub>.tbe.taleo.net/<site>/ats/servlet/Rss?org=<ORG>&cws=<N>
Verified live against BlueStar Resort & Golf (phf/phf02/SHEA/cws=53 -> 130 jobs).

Slug is composite: "<sub>|<site>|<org>|<cws>"  (e.g. "phf|phf02|SHEA|53").
"""
from __future__ import annotations

import html
import re

from .base import FetchError, get_text

PLATFORM = "taleo"
_ITEM = re.compile(r"<item>(.*?)</item>", re.S | re.I)
_TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_LOC = re.compile(r"<taleo:location>(.*?)</taleo:location>", re.S | re.I)


def _parse(slug: str):
    parts = slug.split("|")
    if len(parts) != 4:
        raise FetchError(f"taleo slug must be sub|site|org|cws, got {slug!r}")
    return parts


def _url(sub, site, org, cws) -> str:
    return f"https://{sub}.tbe.taleo.net/{site}/ats/servlet/Rss?org={org}&cws={cws}"


def fetch(slug: str) -> list[dict]:
    sub, site, org, cws = _parse(slug)
    xml = get_text(_url(sub, site, org, cws))
    out: list[dict] = []
    for item in _ITEM.findall(xml):
        tm = _TITLE.search(item)
        if not tm:
            continue
        title = html.unescape(tm.group(1)).strip()
        lm = _LOC.search(item)
        loc = html.unescape(lm.group(1)).strip() if lm else ""
        out.append({"title": title, "location": loc})
    return out


def verify(slug: str) -> bool:
    try:
        sub, site, org, cws = _parse(slug)
        xml = get_text(_url(sub, site, org, cws))
    except Exception:
        return False
    return "<item>" in xml or "<rss" in xml.lower()
