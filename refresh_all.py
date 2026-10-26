# -*- coding: utf-8 -*-
"""Full refresh for the long-term (library) page — one command, no agent step.

Replaces the older daily_update.py flow, which assumed a human/agent would
translate data/queue.json by hand and then republish to the sites sandbox.
Translation is now handled by translate_xai.py (which merges straight into
data/zh_cache.json), and the published artifact is the pre-rendered library
page, whose link never changes.

Steps
  1. fetch_news.py       media reporting (RSS first, Tavily/GDELT when available)
  2. fetch_articles.py   full article text
  3. fetch_social.py     X / LinkedIn / Facebook / Instagram public posts
                         (falls back to fetch_social_x.py when the local
                          Firecrawl proxy is unavailable)
  4. build_data.py       data.js + data/queue.json
  5. translate_xai.py    translate every missing string, merge into zh_cache
  6. build_data.py       rebuild with the new Chinese
  7. make_static_page.py render the JS-free page
  8. import + publish    overwrite the same library node -> same public link

Usage
  python refresh_all.py              # full cycle
  python refresh_all.py --publish-only   # rebuild the page from current data.js
  python refresh_all.py --skip-social    # skip step 3
"""
import io
import os
import re
import subprocess
import sys
import time
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

LIB = r"D:/workbuddy/WorkBuddyAI/resources/app.asar.unpacked/resources/plugins/workbuddy-builtin/skills/library"
NODE_ID = "pm8CGKciTycadAbqTkySuL"
PUBLIC = "https://workbuddy.cc/p/%s" % NODE_ID


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def run(script, extra=None, optional=False):
    name = script + (" " + " ".join(extra) if extra else "")
    log(">> " + name)
    t0 = time.time()
    p = subprocess.run([PY, os.path.join(HERE, script)] + (extra or []), cwd=HERE)
    dt = time.time() - t0
    ok = p.returncode == 0
    log("%s rc=%d %.0fs" % (name, p.returncode, dt))
    if not ok and not optional:
        raise SystemExit("stage failed: " + name)
    return ok


def build_stamp():
    """generatedAt recorded inside data.js."""
    p = os.path.join(HERE, "data.js")
    if not os.path.exists(p):
        return None
    m = re.search(r'"generatedAt":\s*"([^"]+)"',
                  io.open(p, encoding="utf-8").read(400))
    return m.group(1) if m else None


def artifact_stamp(path):
    """generatedAt embedded in the built HTML artifact."""
    try:
        raw = io.open(path, encoding="utf-8").read()
    except Exception:
        return None
    m = re.search(r'"generatedAt":\s*"([^"]+)"', raw)
    return m.group(1) if m else None


def publish():
    # The library page embeds the uploaded HTML in an iframe and its scripts DO
    # run there, so we ship the real interactive app (tabs, filters, sort,
    # fold) with the data inlined — not the pre-rendered fallback. Verified in a
    # headless browser: tab switching and the fold both work inside the iframe.
    log(">> build_static.py (bundle index.html + data.js)")
    subprocess.run([PY, os.path.join(HERE, "build_static.py")], cwd=HERE, check=True)
    # build_static.py writes dist/china-eyes.html. Importing any other path here
    # silently ships a stale artifact — that bug published 10-19 data on 10-22
    # while every script still exited 0. Hence the freshness assertion below.
    page = os.path.join(HERE, "dist", "china-eyes.html")
    want, got = build_stamp(), artifact_stamp(page)
    if not want or not got or want != got:
        raise SystemExit(
            "freshness guard: artifact %s has generatedAt=%r but data.js has %r "
            "— refusing to publish a stale page" % (os.path.basename(page), got, want))
    log("   freshness guard OK: artifact generatedAt=%s" % got)
    log(">> import_html.py (overwrite node %s)" % NODE_ID)
    subprocess.run([PY, os.path.join(LIB, "page", "import_html.py"), page,
                    "--file-name", "Africa Eyes 西非四国动态监测.html",
                    "--node-block-id", NODE_ID], check=True)
    log(">> publish_page.py")
    subprocess.run([PY, os.path.join(LIB, "page", "publish_page.py"),
                    "--node-id", NODE_ID], check=True)
    log("stable link: " + PUBLIC)


def data_age_min():
    """Minutes since the build stamp inside data.js (not the file mtime)."""
    p = os.path.join(HERE, "data.js")
    if not os.path.exists(p):
        return None
    head = io.open(p, encoding="utf-8").read(400)
    m = re.search(r'"generatedAt":\s*"([^"]+)"', head)
    if not m:
        return None
    try:
        return (datetime.now().astimezone()
                - datetime.fromisoformat(m.group(1))).total_seconds() / 60.0
    except Exception:
        return None


def min_age_arg():
    for i, a in enumerate(sys.argv):
        if a == "--min-age" and i + 1 < len(sys.argv):
            return float(sys.argv[i + 1])
        if a.startswith("--min-age="):
            return float(a.split("=", 1)[1])
    return 0.0


def main():
    need = min_age_arg()
    only = "--publish-only" in sys.argv
    if need > 0 and not only:
        age = data_age_min()
        if age is not None and age < need:
            log("data is %.0f min old (< %.0f) — skipping this slot" % (age, need))
            return
    if not only:
        run("fetch_news.py")
        run("fetch_articles.py", optional=True)
        if "--skip-social" not in sys.argv:
            if not run("fetch_social.py", optional=True):
                run("fetch_social_x.py", optional=True)
        run("build_data.py")
        run("translate_xai.py", ["--limit", "220"], optional=True)
        run("build_data.py")
    publish()
    log("done")


if __name__ == "__main__":
    main()
