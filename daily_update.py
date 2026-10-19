# -*- coding: utf-8 -*-
"""Daily orchestrator for the Africa Eyes monitor.

Runs the deterministic half of the refresh so the scheduled job is one command:

  1. fetch_news.py    -> data/raw/tavily_news_*.json   (media reporting)
  2. fetch_articles.py-> data/raw/article_*.json       (full article text)
  3. fetch_social.py  -> data/raw/firecrawl_*.json     (LinkedIn/X/FB/IG)
  4. build_data.py    -> data.js                       (site data layer)
                       -> data/queue.json              (strings needing Chinese)

Then the AGENT step, which this script cannot do by itself:
  4. read data/queue.json, translate the "en" values, write data/zh_cache.json
     (via apply_zh.py), and re-run build_data.py
  5. republish the directory with the App Publishing tool

Translations are cached by content hash, so after the first full pass a daily
run only has to translate genuinely new items.

Usage:
  python daily_update.py           # full refresh
  python daily_update.py --quick   # smoke test (1 news angle, 2 social platforms)
"""
import io
import os
import re
import subprocess
import sys
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
QUICK = "--quick" in sys.argv


def run(script, extra=None):
    print("\n" + "=" * 62 + "\n>>> " + script + (" " + " ".join(extra or []) if extra else ""))
    cmd = [PY, os.path.join(HERE, script)] + (extra or [])
    p = subprocess.run(cmd, cwd=HERE)
    if p.returncode != 0:
        print("!! %s exited with %d" % (script, p.returncode))
    return p.returncode


rc1b = 0
if QUICK:
    # Trim the news plan to a single query by shadowing the module constant.
    src = io.open(os.path.join(HERE, "fetch_news.py"), encoding="utf-8").read()
    src = src.replace("for tag, q, domains, n in PLAN:",
                      "for tag, q, domains, n in PLAN[:1]:")
    tmp = os.path.join(HERE, "_fetch_news_quick.py")
    io.open(tmp, "w", encoding="utf-8").write(src)
    rc1 = run("_fetch_news_quick.py")
    rc2 = run("fetch_social.py", ["--quick"])
else:
    rc1 = run("fetch_news.py")
    # Runs between the news and social steps: it reads the news snapshots to
    # decide what to fetch, and build_data.py then prefers this full text over
    # the search index's `content` when writing summaries.
    rc1b = run("fetch_articles.py")
    rc2 = run("fetch_social.py")

# LinkedIn via Bright Data. Its own script because it needs a zone that the
# Firecrawl path does not; a missing zone prints a one-line explanation and the
# pipeline carries on with the other platforms.
if not QUICK:
    run("fetch_linkedin_bd.py")

# X people profiles: at most once per calendar day (each run costs ~$1 of xai
# tool calls). The first run of the day — the 7am one under the hourly schedule
# — produces the snapshot; later hourly runs see the file and skip.
prof_today = os.path.join(HERE, "data", "raw",
                          "xprofiles_%s.json" % datetime.now().strftime("%Y%m%d"))
if not QUICK and not os.path.exists(prof_today):
    run("fetch_people_x.py")

rc3 = run("build_data.py")

# Translation. Until now this needed an agent in the loop, which is exactly why
# an unattended hourly refresh could never finish on its own. translate_xai.py
# does the same job through xAI chat completions and feeds apply_zh.py, so the
# pipeline is self-contained end to end.
rc4 = run("translate_xai.py")
if rc4 == 0:
    rc5 = run("build_data.py")     # rebuild so the new Chinese lands in data.js
else:
    rc5 = 0
    print("!! translation step failed — data.js keeps 待译 markers")

# report
dj = os.path.join(HERE, "data.js")
try:
    txt = io.open(dj, encoding="utf-8").read()
    m = re.search(r'"generatedAt":\s*"([^"]+)"', txt)
    print("\n" + "=" * 62)
    print("data.js generated at:", m.group(1) if m else "?")
    print("size: %d bytes" % len(txt))
except Exception as e:
    print("could not read data.js:", e)

rc6 = run("make_digest.py")
sys.exit(1 if (rc1 or rc1b or rc2 or rc3 or rc4 or rc5 or rc6) else 0)
