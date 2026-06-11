"""Headless render tier (Playwright) — the FREE fallback for JS-only careers pages.

Many 'no ATS signature' companies are on a known ATS (Workday/iCIMS/Paylocity/
EasyApply/...) whose careers link is injected by JavaScript, so static HTML never
shows it. Rendering the page with a real browser and re-running the SAME signature
detection recovers them deterministically — zero Apify spend, zero LLM tokens,
just local compute. Renders many URLs concurrently in one browser.

Requires `pip install playwright` + `python -m playwright install chromium`.
If Playwright isn't installed, render_many() is a no-op (returns {}).
"""
from __future__ import annotations

import asyncio
import re

from fetchers.base import USER_AGENT

CONCURRENCY = 6
TIMEOUT_MS = 15000
SETTLE_MS = 800           # let JS inject the careers/ATS markup after DOM load
PER_PAGE_HARD_S = 35      # hard cap per page — abandon a hung/crashed render
CHUNK_HARD_S = 240        # internal cap per chunk (the worker subprocess has an
                          # outer hard kill-timeout as the real guarantee)

# Heuristic interaction patterns (deterministic, no LLM). Cost-bounded.
_CONSENT = re.compile(r"accept all|accept|agree|got it|allow all|i understand|continue", re.I)
_REVEAL = re.compile(
    r"view opening|view job|see opening|see job|view position|open position|"
    r"current opening|search job|search|browse job|view all|all jobs|"
    r"explore opportunit|see all|find job|view our job", re.I)
_MORE = re.compile(r"load more|show more|view more|see more|next", re.I)
_MAX_REVEAL_CLICKS = 2
_MAX_MORE_CLICKS = 6
_SCROLLS = 2


def available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def render_many(tasks, concurrency: int = CONCURRENCY, timeout_ms: int = TIMEOUT_MS,
                interact: bool = True, on_page=None):
    """tasks: list of (key, url). Returns {key: [(final_url, html), ...]}.
    interact=True runs heuristic clicks (cookie dismiss, reveal/load-more, scroll)
    and concatenates iframe content. on_page(key, url) fires after each render
    (progress). No-op ({}) if Playwright isn't installed."""
    if not tasks or not available():
        return {}
    return asyncio.run(_render_all(tasks, concurrency, timeout_ms, interact, on_page))


async def _click_first(page, pattern, timeout=2000):
    """Click the first visible button/link/role-button whose name matches pattern.
    Returns True if a click happened."""
    for getter in (
        lambda: page.get_by_role("button", name=pattern),
        lambda: page.get_by_role("link", name=pattern),
        lambda: page.locator("[role=button]", has_text=pattern),
    ):
        try:
            loc = getter()
            if await loc.count() and await loc.first.is_visible():
                await loc.first.click(timeout=timeout, no_wait_after=True)
                return True
        except Exception:
            continue
    return False


async def _interact(page):
    """Deterministic, cost-bounded interaction to reveal JS/click-gated jobs."""
    try:
        if await _click_first(page, _CONSENT):
            await page.wait_for_timeout(400)
    except Exception:
        pass
    clicks = 0
    while clicks < _MAX_REVEAL_CLICKS:
        if await _click_first(page, _REVEAL):
            clicks += 1
            await page.wait_for_timeout(1200)
        else:
            break
    for _ in range(_MAX_MORE_CLICKS):
        if await _click_first(page, _MORE):
            await page.wait_for_timeout(900)
        else:
            break
    for _ in range(_SCROLLS):
        try:
            await page.mouse.wheel(0, 25000)
            await page.wait_for_timeout(700)
        except Exception:
            break


async def _capture(page) -> str:
    """Main-frame HTML + all iframe contents (ATS widgets often live in iframes)."""
    parts = []
    try:
        parts.append(await page.content())
    except Exception:
        pass
    for frame in page.frames:
        try:
            parts.append(await frame.content())
        except Exception:
            continue
    return "\n".join(parts)


async def _render_all(tasks, concurrency, timeout_ms, interact, on_page=None):
    from playwright.async_api import async_playwright

    results: dict[str, list] = {}
    sem = asyncio.Semaphore(concurrency)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=USER_AGENT)

        async def work(key, url):
            async with sem:
                page = await ctx.new_page()
                try:
                    async def _do():
                        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                        await page.wait_for_timeout(SETTLE_MS)
                        if interact:
                            await _interact(page)
                        return page.url, await _capture(page)
                    # HARD per-page cap: a crashed/hung browser can't freeze the run.
                    final_url, html = await asyncio.wait_for(_do(), timeout=PER_PAGE_HARD_S)
                    results.setdefault(key, []).append((final_url, html))
                except Exception:
                    pass
                finally:
                    try:
                        await asyncio.wait_for(page.close(), timeout=5)
                    except Exception:
                        pass
                    if on_page:
                        try:
                            on_page(key, url)
                        except Exception:
                            pass

        # HARD per-chunk cap as a final backstop against any remaining hang.
        try:
            await asyncio.wait_for(
                asyncio.gather(*(work(k, u) for k, u in tasks)), timeout=CHUNK_HARD_S)
        except asyncio.TimeoutError:
            pass
        try:
            await asyncio.wait_for(browser.close(), timeout=10)
        except Exception:
            pass
    return results
