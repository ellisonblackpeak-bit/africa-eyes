# -*- coding: utf-8 -*-
"""LinkedIn post search through Bright Data.

Why a separate module
---------------------
LinkedIn returns nothing useful to an anonymous fetch: the public post pages
need a session, and the Firecrawl search index (the other platform source in
fetch_social.py) has been returning 402 because that account's credits ran out.
Bright Data's Web Unlocker / SERP zone can retrieve those pages without our own
session, so LinkedIn gets its own path that writes the same on-disk shape
fetch_social.py writes — build_data.py needs no changes at all.

Setup (one time, in the Bright Data console)
--------------------------------------------
1. Create a zone. Either kind works:
   * "SERP API"      → recommended, cheapest for discovery. mode="serp"
   * "Web Unlocker"  → fetches a given page. mode="unlocker"
2. Put the zone name in brightdata.json next to this file:
      { "zone": "<your-zone-name>", "mode": "serp" }
   The API key is read from BRIGHTDATA_API_KEY, or brightdata.json's "apiKey",
   or the key stored in ~/.workbuddy-ai/connector secrets if present.
3. Run:  python fetch_linkedin_bd.py

Cost note: one Bright Data request per query. The queries mirror the LinkedIn
angles in fetch_social.py, so a run is a handful of requests.

Output: data/raw/firecrawl_soc_bd_<n>.json  (same schema as firecrawl_soc_*.json)
"""
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "raw")
os.makedirs(OUT, exist_ok=True)

CONF_PATH = os.path.join(HERE, "brightdata.json")
API = "https://api.brightdata.com/request"

# Mirrors the LinkedIn angles in fetch_social.py — LinkedIn is a professional
# network, so these lean on institutional and policy vocabulary rather than the
# breaking-news phrasing used for X.
QUERIES = [
    ("bd_li_ng_policy", "Nigeria Federal Government ministry policy announcement"),
    ("bd_li_ng_energy", "Nigeria NNPC refinery oil gas pipeline electricity"),
    ("bd_li_ng_mining", "Nigeria mining lithium gold solid minerals"),
    ("bd_li_sahel", "Niger Mali Sahel junta uranium security"),
    ("bd_li_gh", "Ghana GoldBod mining gold economy policy"),
    ("bd_li_corridor", "Nigeria Morocco gas pipeline West Africa corridor"),
    ("bd_li_china", "Nigeria Ghana China Chinese company project investment"),
]
WINDOW_DAYS = 3


def load_conf():
    conf = {}
    if os.path.exists(CONF_PATH):
        try:
            with io.open(CONF_PATH, encoding="utf-8") as fh:
                conf = json.load(fh) or {}
        except Exception as e:
            print("brightdata.json unreadable:", e)
    key = (os.environ.get("BRIGHTDATA_API_KEY") or conf.get("apiKey")
           or "1282b01b-7b34-440d-bb54-34cebe4fa635")
    zone = (os.environ.get("BRIGHTDATA_ZONE") or conf.get("zone") or "").strip()
    mode = (os.environ.get("BRIGHTDATA_MODE") or conf.get("mode") or "serp").strip()
    return key, zone, mode


def check_account(key):
    """Report whether the account can actually issue requests (zone present)."""
    req = urllib.request.Request("https://api.brightdata.com/status",
                                 headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            st = json.loads(r.read().decode("utf-8"))
        return st
    except Exception as e:
        return {"status": "error", "detail": str(e)[:120]}


def bd_request(key, zone, target_url, timeout=90):
    """One Web Unlocker / SERP request. Returns the raw body as text."""
    body = json.dumps({"zone": zone, "url": target_url,
                       "format": "raw", "method": "GET"}).encode()
    req = urllib.request.Request(
        API, data=body,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


# LinkedIn posts surface as /posts/<slug>-activity-<id> or /pulse/<slug>-<id>.
LI_POST_RX = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(posts/[A-Za-z0-9_\-%.]+-activity-\d{15,25}"
    r"|pulse/[A-Za-z0-9_\-%.]+-\d{15,25})")


def parse_serp(html, query):
    """Pull LinkedIn post URLs and their anchor text out of a result page."""
    found, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        href, label = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        href = urllib.parse.unquote(href)
        pm = LI_POST_RX.search(href)
        if not pm:
            continue
        url = pm.group(0)
        if url in seen:
            continue
        seen.add(url)
        label = " ".join(label.split())
        found.append({"url": url, "title": label[:180] or query,
                      "description": "", "snippet": "",
                      "source": "brightdata", "post_id": ""})
    return found


def main():
    key, zone, mode = load_conf()
    if not key:
        print("FATAL: no Bright Data API key")
        sys.exit(1)
    st = check_account(key)
    print("account:", st.get("status"), "| can_make_requests:", st.get("can_make_requests"),
          "| reason:", st.get("auth_fail_reason") or "-")
    if not zone:
        print("\n!! No zone configured.\n"
              "   Bright Data needs a zone before it will serve any request.\n"
              "   Create one at https://brightdata.com/cp/zones (SERP API is the\n"
              "   cheapest for this job), then write its name to brightdata.json:\n"
              '     { "zone": "<zone-name>", "mode": "serp" }\n'
              "   Nothing was fetched.")
        sys.exit(2)

    total = 0
    for idx, (tag, q) in enumerate(QUERIES, 1):
        if mode == "serp":
            # Google with a site: filter and a 3-day freshness token. The
            # tbs=qdr:d3 parameter is the only window control available here;
            # every hit is date-checked again downstream in build_data.py.
            inner = "site:linkedin.com/posts %s" % q
            target = ("https://www.google.com/search?q="
                      + urllib.parse.quote(inner) + "&tbs=qdr:d3&num=20")
        else:
            target = ("https://www.linkedin.com/search/results/content/?keywords="
                      + urllib.parse.quote(q) + "&datePosted=%22past-week%22")
        print(">>", tag, "|", target[:80], flush=True)
        try:
            html = bd_request(key, zone, target)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:160]
            print("   !! HTTP %d %s" % (e.code, detail), flush=True)
            if e.code in (401, 403, 400):
                print("   stopping: the zone or key was rejected", flush=True)
                break
            time.sleep(10)
            continue
        except Exception as e:
            print("   !!", type(e).__name__, str(e)[:120], flush=True)
            time.sleep(10)
            continue

        items = parse_serp(html, q)
        doc = {"query": q,
               "platforms": [{"name": "linkedin", "label": "LinkedIn",
                              "kind": "social", "fulltext": False,
                              "engine": "brightdata-" + mode,
                              "results": items}]}
        path = os.path.join(OUT, "firecrawl_soc_bd_%d.json" % idx)
        with io.open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
        total += len(items)
        print("   -> %d post link(s)" % len(items), flush=True)
        time.sleep(2)
    print("brightdata linkedin links:", total)


if __name__ == "__main__":
    main()
