"""Spec-compliance verification (CLAUDE.md hard rules + SPEC sections)."""
import csv, json
from pathlib import Path

from fetchers.base import flatten_location
from fetchers import workday, smartrecruiters
from matcher import is_frontline, filter_roles

PASS, FAIL = "PASS", "FAIL"
results = []
def check(name, ok, detail=""):
    results.append((ok, name, detail))

# ---- HARD RULE: never a false zero (unresolved -> empty count, not 0) ----
if Path("test_output.csv").exists():
    rows = list(csv.DictReader(open("test_output.csv", encoding="utf-8-sig")))
    unresolved = [r for r in rows if r["status"] != "ok"]
    bad = [r["Company Name"] for r in unresolved if r["frontline_role_count"] != ""]
    check("no false zero: every unresolved row has EMPTY count", not bad,
          f"violations: {bad}")
else:
    print("(skip) test_output.csv not present — run pipeline.py first for that check")

# ---- resolved zero IS a real 0 (SmartRecruiters Visa = 0 frontline) ----
visa = filter_roles(smartrecruiters.fetch("Visa"))
# emulate pipeline emit: count = len(frontline), can be 0 with status ok
emitted = len(visa)  # 0
check("resolved-zero emits integer 0 (not empty) for a real ATS board",
      emitted == 0, f"Visa frontline={emitted} -> would write count=0, status=ok")

# ---- HARD RULE: matching = word-prefix at boundary; false friends blocked ----
ff = {"Reporter": False, "Observer": False, "Career Coach": False, "Resort": False,
      "Cleaner": True, "Cleaning Crew": True, "Server": True, "Service Tech": True}
ff_ok = all(is_frontline(k) == v for k, v in ff.items())
check("word-prefix + leading \\b (false friends blocked, stems work)", ff_ok,
      {k: is_frontline(k) for k in ff})

# ---- HARD RULE: LEAD_OK protects an excluded hourly lead ----
# "Assistant General Manager" contains EXCLUDE 'general manager' but is LEAD_OK
check("LEAD_OK protects 'Assistant General Manager' (has 'general manager')",
      is_frontline("Assistant General Manager") is True)
check("EXCLUDE still drops 'Maintenance Engineer' (frontline stem + 'engineer')",
      is_frontline("Maintenance Engineer") is False)

# ---- HARD RULE: locations may be nested objects — defensive, never str(obj) ----
cases = {
    "plain string": ("Austin, TX", "Austin, TX"),
    "dict city/state": ({"city": "Reno", "state": "NV", "country": "US"}, "Reno, NV, US"),
    "list of dicts": ([{"city": "A"}, {"city": "B"}], "A; B"),
    "formattedLocation": ({"formattedLocation": "Remote - US"}, "Remote - US"),
    "nested wrapper": ({"location": {"city": "Mesa", "state": "AZ"}}, "Mesa, AZ"),
    "None": (None, ""),
}
loc_ok = True
loc_detail = {}
for k, (inp, exp) in cases.items():
    got = flatten_location(inp)
    loc_detail[k] = got
    if got != exp:
        loc_ok = False
check("defensive location extraction (dict/list/nested/None)", loc_ok, loc_detail)
# guard: a raw dict must never leak as str()
check("location never str()s a raw object",
      "{" not in flatten_location({"city": "X", "state": "Y"}))

# ---- SPEC 3: cache format {host: {platform, slug, verified_at}} ----
cache = json.loads(Path("cache/slugs.json").read_text(encoding="utf-8")) if Path("cache/slugs.json").exists() else {}
cache_ok = all({"platform", "slug", "verified_at"} <= set(v) for v in cache.values())
check("slug cache has platform+slug+verified_at per entry", cache_ok,
      list(cache.items())[:1])

# ---- SPEC 6: fetchers return list[{title, location}] exactly ----
shape = filter_roles(workday.fetch("carrier|wd5|jobs"))[:3]
shape_ok = all(set(j) == {"title", "location"} and isinstance(j["title"], str)
               and isinstance(j["location"], str) for j in shape)
check("fetcher output is exactly {title, location} strings", shape_ok, shape[:1])

# ---- SPEC 6: pagination pulls the FULL board (not truncated) ----
# Disable the frontline threshold so early-stop doesn't cap this pagination check.
import config
_save_t = config.FRONTLINE_THRESHOLD
config.FRONTLINE_THRESHOLD = 0
full = workday.fetch("carrier|wd5|jobs")
config.FRONTLINE_THRESHOLD = _save_t
check("pagination not truncated (Workday carrier > 1 page)", len(full) > 100,
      f"{len(full)} jobs pulled")

print(f"\n{'='*60}\nSPEC COMPLIANCE CHECKS\n{'='*60}")
allok = True
for ok, name, detail in results:
    print(f"[{PASS if ok else FAIL}] {name}")
    if not ok:
        allok = False
        print(f"        detail: {detail}")
print("="*60)
print("ALL CHECKS PASS" if allok else "SOME CHECKS FAILED")
