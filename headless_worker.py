"""Isolated headless worker — renders ONE chunk of companies and writes the
resolved results to JSON. Launched as a subprocess by pipeline._headless_fill so
that a frozen/crashed browser can be force-killed (taskkill /T) without stalling
the main run. Never run directly in normal use.

Input  (argv[1]): JSON file {"websites": [...], "threshold": N, "output": "<path>"}
Output (data["output"]): JSON {website: result-dict} for companies that resolved.
"""
import json
import os
import sys

import config


def main():
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    config.FRONTLINE_THRESHOLD = int(data.get("threshold", 20))
    out_path = data["output"]

    import pipeline
    from resolver import load_cache, save_cache

    cache = load_cache()

    # Stream each resolved company to a JSONL sink (flush+fsync per line) so a hard
    # SIGKILL (OS OOM-killer or the 300s force-kill) loses only the in-flight
    # company, never the whole chunk. The parent reads the consolidated .out first,
    # then falls back to this JSONL to salvage partial yield from a killed worker.
    jsonl_path = out_path + ".jsonl"
    try:
        sink = open(jsonl_path, "w", encoding="utf-8")
    except Exception:
        sink = None

    def emit(site, result):
        if sink is None:
            return
        try:
            sink.write(json.dumps({site: result}) + "\n")
            sink.flush()
            os.fsync(sink.fileno())
        except Exception:
            pass

    results = {}
    try:
        results = pipeline.headless_resolve_companies(data["websites"], cache, on_result=emit)
    except Exception:
        # Never die empty-handed: the JSONL already holds whatever resolved.
        pass
    finally:
        if sink is not None:
            try:
                sink.flush(); os.fsync(sink.fileno()); sink.close()
            except Exception:
                pass

    try:
        save_cache(cache)
    except Exception:
        pass
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f)
            f.flush()
            os.fsync(f.fileno())  # ensure the parent reads a COMPLETE file, not a buffered partial
    except Exception:
        pass


if __name__ == "__main__":
    main()
