"""Phase 2 integration smoke test (NETWORK REQUIRED).

Exercises fetch -> normalize -> filter routing for every supported platform
against a known-good live slug. Asserts the normalized shape; prints frontline
counts. Tech-company boards legitimately yield few/zero frontline roles — that
is a correct resolved answer, not a failure.

Run: python test_fetchers.py
"""
import fetchers
from matcher import filter_roles

# (platform, live_slug) pairs verified against the live endpoint.
# Composite slugs: workday=tenant|dc|site, ultipro=host|tenant|guid, cornerstone=tenant|siteId.
PROBES = [
    ("workable", "spark-car-wash"),
    ("greenhouse", "stripe"),
    ("lever", "leverdemo"),
    ("ashby", "Ramp"),
    ("smartrecruiters", "Visa"),
    ("workday", "carrier|wd5|jobs"),
    ("ultipro", "recruiting2.ultipro.com|SAL1002|bcc2e2d1-d94c-2041-4126-28086417eb0a"),
    ("cornerstone", "cornerstone|2"),
    ("jazzhr", "dtexsystems"),
]
# Not in the required set: iCIMS (often AWS-WAF'd from datacenter IPs -> 405) and
# jsonld (slug is a URL, site-specific). Both are exercised opportunistically.


def run():
    failures = []
    for platform, slug in PROBES:
        try:
            jobs = fetchers.fetch(platform, slug)
        except Exception as e:
            failures.append(f"{platform}/{slug}: fetch raised {e}")
            continue

        if not isinstance(jobs, list) or not jobs:
            failures.append(f"{platform}/{slug}: returned no jobs")
            continue

        bad = [j for j in jobs if set(j) != {"title", "location"}
               or not isinstance(j["title"], str) or not isinstance(j["location"], str)]
        if bad:
            failures.append(f"{platform}/{slug}: {len(bad)} jobs have wrong shape, e.g. {bad[0]}")
            continue

        frontline = filter_roles(jobs)
        print(f"  {platform:16} {slug:16} total={len(jobs):4} frontline={len(frontline)}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("\nALL FETCHERS OK")


if __name__ == "__main__":
    run()
