"""Isolated headless worker — renders ONE chunk of companies and writes the
resolved results to JSON. Launched as a subprocess by pipeline._headless_fill so
that a frozen/crashed browser can be force-killed (taskkill /T) without stalling
the main run. Never run directly in normal use.

Input  (argv[1]): JSON file {"websites": [...], "threshold": N, "output": "<path>"}
Output (data["output"]): JSON {website: result-dict} for companies that resolved.
"""
import json
import sys

import config


def main():
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    config.FRONTLINE_THRESHOLD = int(data.get("threshold", 20))

    import pipeline
    from resolver import load_cache, save_cache

    cache = load_cache()
    results = pipeline.headless_resolve_companies(data["websites"], cache)
    try:
        save_cache(cache)
    except Exception:
        pass
    with open(data["output"], "w", encoding="utf-8") as f:
        json.dump(results, f)


if __name__ == "__main__":
    main()
