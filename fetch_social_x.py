# -*- coding: utf-8 -*-
"""X social collection that works *inside the cloud service*.

fetch_social.py reaches X through the local Firecrawl connector proxy and the
local xsearch.py CLI, neither of which exists in the deployed sandbox — so the
cloud page used to lose its social cards after the 5-day pool ran dry. This
script is the server-safe replacement: it calls xAI's Responses API directly
(via sources.xai_posts), gets real post text plus Chinese translation, and
writes the same firecrawl_*.json shape build_data.py already reads.

Cost control: one call per query, and the whole script refuses to run more than
once every SOCIAL_X_EVERY_MIN minutes (default 180), because x_search is billed
per fetched post. Six angles every three hours is roughly $4-5/day; running it
hourly would multiply that by three for very little extra coverage.
"""
import io
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "raw")
os.makedirs(OUT, exist_ok=True)

EVERY_MIN = int(os.environ.get("SOCIAL_X_EVERY_MIN", "180"))
MAX_QUERIES = int(os.environ.get("SOCIAL_X_QUERIES", "6"))

QUERIES = [
    ("soc_ng_gov", "Nigeria Federal Government ministry announcement policy"),
    ("soc_ng_security", "Nigeria security politics government"),
    ("soc_ng_energy", "Nigeria NNPC refinery oil gas pipeline electricity"),
    ("soc_ng_mining", "Nigeria mining lithium gold minerals policy"),
    ("soc_sahel", "Niger Mali Sahel junta uranium military"),
    ("soc_gh", "Ghana government GoldBod mining gold economy policy"),
    ("soc_corridor", "Nigeria Morocco gas pipeline West Africa corridor project"),
    ("soc_china", "Nigeria Ghana China Chinese company project investment"),
]


def recent_run():
    """Minutes since the last X collection, or None if never run."""
    files = [os.path.join(OUT, f) for f in os.listdir(OUT)
             if f.startswith("firecrawl_xai_") and f.endswith(".json")]
    if not files:
        return None
    return (time.time() - max(os.path.getmtime(f) for f in files)) / 60.0


def first_sentence(text, limit=140):
    t = " ".join((text or "").split())
    if not t:
        return ""
    for sep in (". ", "? ", "! ", "。", "！", "？"):
        i = t.find(sep)
        if 25 <= i <= limit:
            return t[:i + len(sep)].strip()
    return t[:limit].strip()


def main():
    age = recent_run()
    if age is not None and age < EVERY_MIN and "--force" not in sys.argv:
        print("X social: last run %.0f min ago (< %d) — skipping" % (age, EVERY_MIN))
        return

    import sources
    if not sources.xai_key():
        print("X social: no xAI key — skipping")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    total, cost_note = 0, 0
    for tag, q in QUERIES[:MAX_QUERIES]:
        try:
            posts = sources.xai_posts(q, days=3, max_results=10)
        except Exception as e:
            print("  !! %s: %s" % (tag, str(e)[:140]))
            continue
        results = []
        for p in posts:
            text = p.get("text") or ""
            if not text:
                continue
            title = first_sentence(text) or p.get("summaryZh") or q
            results.append({
                "url": p["url"],
                "title": title,
                "description": text,
                "snippet": text,
                "source": "xai-xsearch",
                "grok_summary": False,
                "author": p.get("author") or "",
                "handle": p.get("handle") or "",
            })
        if not results:
            print("  -- %s: no posts" % tag)
            continue
        doc = {"query": q,
               "platforms": [{
                   "name": "x", "label": "X", "kind": "social", "fulltext": True,
                   "engine": "xai-xsearch", "model": os.environ.get("XAI_SEARCH_MODEL", "grok-4.7"),
                   "results": results,
               }]}
        path = os.path.join(OUT, "firecrawl_xai_%s_%s.json" % (tag, stamp))
        with io.open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
        total += len(results)
        print("  %s: %d posts" % (tag, len(results)))
        time.sleep(4)
    print("X social: %d posts written" % total)


if __name__ == "__main__":
    main()
