"""Generic schema.org JobPosting fetcher (for "native" / custom career pages).

Many custom career pages with no third-party ATS still embed
`<script type="application/ld+json">` JobPosting blocks. This fetcher GETs a
careers URL and extracts any JobPosting JSON-LD it finds. Lower-confidence than
a real ATS API: a listing page may only embed a subset of postings (detail-page
JSON-LD isn't followed), so counts can undercount. Used only as a last-resort
fallback by the resolver.

Slug = the careers page URL to scan.
"""
from __future__ import annotations

import json
import re

from .base import flatten_location, get_text, strip_html

PLATFORM = "jsonld"

_LD_BLOCK = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)


def _iter_objects(data):
    """Yield dict objects from arbitrary JSON-LD (object, list, or @graph)."""
    if isinstance(data, list):
        for x in data:
            yield from _iter_objects(x)
    elif isinstance(data, dict):
        yield data
        if "@graph" in data:
            yield from _iter_objects(data["@graph"])


def _is_jobposting(obj: dict) -> bool:
    t = obj.get("@type")
    if isinstance(t, list):
        return any(str(x).lower() == "jobposting" for x in t)
    return str(t).lower() == "jobposting"


def _location(obj: dict) -> str:
    loc = obj.get("jobLocation")
    # jobLocation -> (list of) {address: {addressLocality, addressRegion, ...}}
    if isinstance(loc, list):
        return "; ".join(filter(None, (_location({"jobLocation": x}) for x in loc)))
    if isinstance(loc, dict):
        addr = loc.get("address") or loc
        if isinstance(addr, dict):
            return flatten_location(
                {
                    "city": addr.get("addressLocality"),
                    "region": addr.get("addressRegion"),
                    "country": addr.get("addressCountry"),
                }
            )
    if isinstance(obj.get("applicantLocationRequirements"), dict):
        return flatten_location(obj["applicantLocationRequirements"].get("name"))
    return ""


def extract(html: str) -> list[dict]:
    out: list[dict] = []
    seen = set()
    for block in _LD_BLOCK.findall(html):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for obj in _iter_objects(data):
            if not _is_jobposting(obj):
                continue
            title = strip_html(str(obj.get("title", ""))).strip()
            if not title:
                continue
            key = (title.lower(), obj.get("url") or obj.get("identifier") or "")
            if key in seen:
                continue
            seen.add(key)
            out.append({"title": title, "location": _location(obj)})
    return out


def fetch(slug: str) -> list[dict]:
    """`slug` is the careers page URL to scan."""
    return extract(get_text(slug))


def verify(slug: str) -> bool:
    try:
        return len(extract(get_text(slug))) > 0
    except Exception:
        return False
