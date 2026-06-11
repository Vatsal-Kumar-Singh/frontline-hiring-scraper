"""Shared fetcher helpers: HTTP session, pagination guard, location flattening.

Every platform fetcher returns the same normalized shape:
    list[{"title": str, "location": str}]
"""
from __future__ import annotations

import html as _html
import re

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30
MAX_PAGES = 200  # backstop so a broken cursor can't loop forever


class FetchError(Exception):
    """Raised when an endpoint is unreachable or returns an unusable payload."""


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})


def get_json(url: str, *, params=None, headers=None, timeout=DEFAULT_TIMEOUT):
    try:
        r = _session.get(url, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise FetchError(f"GET {url} failed: {e}") from e
    if r.status_code == 404:
        raise FetchError(f"GET {url} -> 404 (slug not found)")
    if r.status_code >= 400:
        raise FetchError(f"GET {url} -> {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise FetchError(f"GET {url} returned non-JSON: {e}") from e


def post_json(url: str, *, json=None, headers=None, timeout=DEFAULT_TIMEOUT):
    try:
        r = _session.post(url, json=json or {}, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise FetchError(f"POST {url} failed: {e}") from e
    if r.status_code == 404:
        raise FetchError(f"POST {url} -> 404 (slug not found)")
    if r.status_code >= 400:
        raise FetchError(f"POST {url} -> {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise FetchError(f"POST {url} returned non-JSON: {e}") from e


def get_text(url: str, *, params=None, headers=None, timeout=DEFAULT_TIMEOUT) -> str:
    """GET returning raw text (HTML). Raises FetchError on any 4xx/5xx — note a
    405/403 here often means a WAF/bot challenge (e.g. iCIMS behind AWS WAF)."""
    hdrs = {"Accept": "text/html,application/xhtml+xml"}
    if headers:
        hdrs.update(headers)
    try:
        r = _session.get(url, params=params, headers=hdrs, timeout=timeout)
    except requests.RequestException as e:
        raise FetchError(f"GET {url} failed: {e}") from e
    if r.status_code >= 400:
        raise FetchError(f"GET {url} -> {r.status_code}")
    return r.text


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(s: str) -> str:
    """Strip tags, unescape HTML entities, collapse whitespace."""
    if not s:
        return ""
    return _WS_RE.sub(" ", _html.unescape(_TAG_RE.sub(" ", s))).strip()


def flatten_location(loc) -> str:
    """Locations are frequently nested objects or lists — never str() the raw object.

    Handles: plain strings; {city,state,region,country}; {name}; {formattedLocation};
    {location: {...}}; and lists of any of the above.
    """
    if loc is None:
        return ""
    if isinstance(loc, str):
        return loc.strip()
    if isinstance(loc, list):
        parts = [flatten_location(x) for x in loc]
        return "; ".join(p for p in parts if p)
    if isinstance(loc, dict):
        # Pre-formatted single fields first.
        for key in ("formattedLocation", "formatted_address", "name", "label", "text"):
            v = loc.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        # Nested wrapper.
        if isinstance(loc.get("location"), (dict, list)):
            nested = flatten_location(loc["location"])
            if nested:
                return nested
        if isinstance(loc.get("address"), (dict, list)):
            nested = flatten_location(loc["address"])
            if nested:
                return nested
        # Build "City, Region/State, Country" from components.
        city = loc.get("city") or loc.get("locality")
        region = (
            loc.get("region")
            or loc.get("state")
            or loc.get("province")
            or loc.get("administrative_area")
        )
        country = loc.get("country") or loc.get("countryCode")
        parts = [p for p in (city, region, country) if isinstance(p, str) and p.strip()]
        if parts:
            return ", ".join(parts)
    return ""


def reached_threshold(jobs) -> bool:
    """True once `jobs` contains >= config.FRONTLINE_THRESHOLD frontline roles, so
    paginating fetchers can early-stop (we only need to know if it's a strong
    20+ prospect; counting beyond is unnecessary). Lazy imports avoid cycles."""
    import config
    from matcher import is_frontline

    t = getattr(config, "FRONTLINE_THRESHOLD", 0)
    if not t:
        return False
    n = 0
    for j in jobs:
        if is_frontline(j.get("title", "")):
            n += 1
            if n >= t:
                return True
    return False
