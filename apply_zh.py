# -*- coding: utf-8 -*-
"""Merge an authored translation batch into data/zh_cache.json.

The daily agent step translates the strings listed in data/queue.json and records
them here. Keeping translations in a separate cache keyed by content hash means
(a) repeated items across days cost nothing, (b) the English original is never
mutated, and (c) an incomplete pass degrades to a visible "待译" marker instead of
silently dropping content.

Usage:
  python apply_zh.py batch1.json          # {"<sha1-16>": "中文", ...}
  python apply_zh.py batch1.json batch2.json
  python apply_zh.py --stats              # coverage report
"""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "zh_cache.json")
QUEUE = os.path.join(HERE, "data", "queue.json")

cache = {}
if os.path.exists(CACHE):
    with io.open(CACHE, encoding="utf-8") as fh:
        cache = json.load(fh)

if "--stats" in sys.argv:
    q = []
    if os.path.exists(QUEUE):
        with io.open(QUEUE, encoding="utf-8") as fh:
            q = json.load(fh)
    done = sum(1 for x in q if x["key"] in cache)
    print("cache entries: %d | queue pending: %d/%d translated" % (len(cache), done, len(q)))
    sys.exit(0)

added = 0
for path in sys.argv[1:]:
    with io.open(path, encoding="utf-8") as fh:
        batch = json.load(fh)
    for k, v in batch.items():
        v = (v or "").strip()
        if not k or not v:
            continue
        if cache.get(k) != v:
            added += 1
        cache[k] = v

with io.open(CACHE, "w", encoding="utf-8") as fh:
    json.dump(cache, fh, ensure_ascii=False, indent=1, sort_keys=True)

print("cache now holds %d entries (%d added/changed this run)" % (len(cache), added))
if os.path.exists(QUEUE):
    with io.open(QUEUE, encoding="utf-8") as fh:
        q = json.load(fh)
    miss = [x for x in q if x["key"] not in cache]
    print("queue: %d total, %d still missing" % (len(q), len(miss)))
    for x in miss[:8]:
        print("   MISS %s %s | %s" % (x["key"], x["field"], x["en"][:70]))
