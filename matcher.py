"""Stage 3 — the frontline filter (SPEC §4).

Word-prefix match at a word boundary against roles.txt, minus EXCLUDE, with
LEAD_OK protecting hourly leads. No trailing boundary so one stem covers a
family (`clean` -> cleaner / cleaning / clean-up); the leading `\\b` blocks
false friends (obSERVer, reSORT, repoRTER, CAREer).
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from config import EXCLUDE, EXCLUDE_EXACT, LEAD_OK

ROLES_PATH = Path(__file__).with_name("roles.txt")


@lru_cache(maxsize=1)
def load_roles(path: str | None = None) -> tuple[str, ...]:
    """Load frontline terms from roles.txt, one per line, lowercased & stripped."""
    p = Path(path) if path else ROLES_PATH
    terms = []
    for line in p.read_text(encoding="utf-8").splitlines():
        t = line.strip().lower()
        if t:
            terms.append(t)
    return tuple(terms)


def _compile(terms, *, trailing_boundary=False) -> re.Pattern:
    """One big alternation: \\b(?:term1|term2|...) — prefix, no trailing boundary
    by default (so a stem covers a family). trailing_boundary=True makes it a
    whole-word match (\\b...\\b) for short acronyms that would otherwise collide.
    """
    alt = "|".join(re.escape(t) for t in terms)
    tail = r")\b" if trailing_boundary else r")"
    return re.compile(r"\b(?:" + alt + tail)


@lru_cache(maxsize=1)
def _roles_re() -> re.Pattern:
    return _compile(load_roles())


@lru_cache(maxsize=1)
def _exclude_re() -> re.Pattern:
    return _compile(tuple(EXCLUDE))


@lru_cache(maxsize=1)
def _exclude_exact_re() -> re.Pattern:
    return _compile(tuple(EXCLUDE_EXACT), trailing_boundary=True)


def is_frontline(title: str) -> bool:
    """True iff `title` is a frontline/hourly role per the SPEC §4 algorithm."""
    if not title:
        return False
    title_l = title.lower()

    # 1) match any frontline term at a word-start boundary?
    if not _roles_re().search(title_l):
        return False

    # 2) skilled/salaried role we must drop?
    excluded = bool(_exclude_re().search(title_l)) or bool(
        _exclude_exact_re().search(title_l)
    )

    # 3) if excluded, only an allowed hourly lead survives (skip LEAD_OK scan
    #    entirely on the common non-excluded path).
    if excluded and not any(term in title_l for term in LEAD_OK):
        return False

    return True


def filter_roles(jobs):
    """Given an iterable of {title, location}, return the frontline subset."""
    return [j for j in jobs if is_frontline(j.get("title", ""))]
