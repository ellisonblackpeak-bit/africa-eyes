# -*- coding: utf-8 -*-
"""Full-text backfill for the Africa Eyes monitor.

Why this step exists
--------------------
The news index returns a `content` field that is often only the page's
navigation chrome and a "related headlines" rail, with none of the article's own
prose. When that happens the aggregator has nothing to build a summary from and
the card ships with a headline and no summary at all — which is what the reader
reported. This step fetches the real article body for the in-window items so the
summary can be written from actual reporting.

Cost
----
Tavily extract on `advanced` depth is billed at roughly 2 credits per 5 URLs, so
a typical run of ~30 articles costs about 12 credits. Results are cached per URL
in data/raw/article_<sha1>.json, so a URL is only ever paid for once; re-runs
only fetch genuinely new items.

Proxy note
----------
The sandbox's system HTTP proxy breaks Tavily's /extract endpoint (the request
is a large POST that the proxy resets mid-flight, surfacing as
"SSL: UNEXPECTED_EOF_WHILE_READING"). /search is small enough to pass through
untouched, which is why fetch_news.py is unaffected. So this script — and only
this script — drops the proxy environment variables for its own process.
"""
import glob
import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

# Drop the system proxy BEFORE the client is constructed. Scoped to this
# process only; fetch_news.py and fetch_social.py keep their environment.
for _v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_v, None)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    from tavily import TavilyClient  # noqa: E402
except Exception:                     # optional — the direct-scrape path below
    TavilyClient = None               # is what runs in a bare cloud sandbox

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(ROOT, "data", "raw")
WINDOW_DAYS = 3
BATCH = 5                 # URLs per extract call (keeps each call under the 120 s cap)
DEPTH = "advanced"        # 2 credits / 5 URLs, materially better prose extraction
MAX_URLS = 60             # hard ceiling per run, so a runaway index cannot bill us
TIMEOUT = 115

# Same article-URL test the aggregator uses: skip section fronts and category
# pages, which return the site's own nav and nothing about the story.
SECTION_RX = re.compile(
    r"^(news|politics|business|world|africa|section|category|tag|author)/?$", re.I)


def norm_url(u):
    return re.sub(r"[?#].*$", "", str(u or "")).rstrip("/").lower()


def is_article_url(u):
    path = re.sub(r"^https?://[^/]+", "", u or "").strip("/")
    if not path:
        return False
    return not SECTION_RX.match(path)


def parse_pub(s):
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s)
    except Exception:
        return None


KEY = os.environ.get("TAVILY_API_KEY", "")
if not KEY:
    # The cloud sandbox has no ~/.workbuddy-ai, so this file lookup must not be
    # fatal — it used to raise FileNotFoundError and kill the whole stage, which
    # is why cloud builds shipped without article bodies.
    mcp = os.path.join(os.path.expanduser("~"), ".workbuddy-ai", "mcp.json")
    try:
        with io.open(mcp, encoding="utf-8") as fh:
            m = re.search(r"tvly-[A-Za-z0-9_\-]+", fh.read())
        KEY = m.group(0) if m else ""
    except Exception:
        KEY = ""
if not KEY:
    try:
        import sources
        _ks = sources.tavily_keys()
        KEY = _ks[0] if _ks else ""
    except Exception:
        KEY = ""
if not KEY:
    print("FATAL: no Tavily key")
    sys.exit(1)

# ── candidate URLs: in-window items from the roster the news step actually used ──
# The roster is reconstructed from the snapshots' own `_domains` field rather than
# duplicated here, so the two scripts cannot drift apart.
now_utc = datetime.now(timezone.utc)
cutoff = now_utc - timedelta(days=WINDOW_DAYS)

roster, cands = set(), {}
news_files = sorted(glob.glob(os.path.join(RAW, "tavily_news_*.json")))
news_files += sorted(glob.glob(os.path.join(RAW, "gdelt_news_*.json")))
for fn in news_files:
    if fn.endswith("_all.json"):
        continue
    try:
        with io.open(fn, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception:
        continue
    roster |= set(doc.get("_domains") or [])
    for it in doc.get("results", []):
        u = it.get("url")
        if not u or not is_article_url(u):
            continue
        dom = re.sub(r"^www\.", "", re.sub(r"^https?://", "", u).split("/")[0])
        if roster and dom not in roster:
            continue
        dt = parse_pub(it.get("published_date") or "")
        if dt is None or dt < cutoff:
            continue
        cands.setdefault(norm_url(u), (u, dt, it.get("content") or ""))

print("roster domains:", len(roster), "| in-window candidate URLs:", len(cands))

# ── cache ──
def cache_path(url):
    return os.path.join(RAW, "article_%s.json" % hashlib.sha1(norm_url(url).encode()).hexdigest()[:16])


cached = set()
for p in glob.glob(os.path.join(RAW, "article_*.json")):
    try:
        with io.open(p, encoding="utf-8") as fh:
            rec = json.load(fh)
    except Exception:
        continue
    u = rec.get("url")
    if u:
        cached.add(norm_url(u))

todo = [u for u in cands if u not in cached]
todo.sort(key=lambda u: cands[u][1], reverse=True)   # newest first
todo = todo[:MAX_URLS]
print("already cached: %d | to fetch: %d" % (len(cached), len(todo)))

client = TavilyClient(api_key=KEY) if TavilyClient else None
# When the news step already hit the Tavily usage limit, extract will fail the
# same way — skip it entirely and let the Firecrawl fallback do the fetching.
tavily_dead = (os.path.exists(os.path.join(RAW, "_tavily_quota_dead"))
               or client is None)
if tavily_dead:
    print("tavily unavailable (no package, or quota marker) — direct scrape only")
got = failed = 0
for i in range(0, len(todo), BATCH):
    if tavily_dead:
        break
    chunk = todo[i:i + BATCH]
    urls = [cands[u][0] for u in chunk]
    print(">> extract %d/%d (%d urls)" % (i + 1, len(todo), len(urls)), flush=True)
    try:
        res = client.extract(urls=urls, extract_depth=DEPTH, timeout=TIMEOUT)
    except Exception as e:
        print("   !! extract failed:", str(e)[:120], flush=True)
        if re.search(r"usage limit|exceeds your plan|upgrade your plan", str(e), re.I):
            print("   !! quota exhausted — remaining urls go to Firecrawl", flush=True)
            tavily_dead = True
            break
        # one retry after a pause: the endpoint occasionally resets a batch
        time.sleep(8)
        try:
            res = client.extract(urls=urls, extract_depth=DEPTH, timeout=TIMEOUT)
        except Exception as e2:
            print("   !! retry failed:", str(e2)[:120], flush=True)
            continue

    by_url = {}
    for x in res.get("results", []):
        by_url[norm_url(x.get("url"))] = x.get("raw_content") or ""
    for u in chunk:
        text = by_url.get(u)
        rec = {"url": cands[u][0], "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "chars": len(text or ""), "ok": bool(text and len(text) > 400)}
        if text:
            rec["text"] = text
        with io.open(cache_path(u), "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
        if rec["ok"]:
            got += 1
        else:
            failed += 1
    time.sleep(1.0)

# ── Firecrawl fallback ────────────────────────────────────────────────────────
# Some West African publishers refuse Tavily's fetcher outright — every
# thenationonlineng.net URL comes back "Failed to fetch url" with 0 characters.
# Firecrawl renders those pages successfully, so anything still unusable gets a
# second attempt there. Cost is about 1 credit per page and only the leftovers
# are ever retried, so a normal run pays for a handful of pages at most.
FIRECRAWL_ENV = r"C:/Users/A/WorkBuddy AI/2026-10-14-10-42-35/firecrawl-search/.env"
FIRECRAWL_API = "https://api.firecrawl.dev/v2/scrape"

# The fallback is deliberately restricted to the publishers it is FOR: the West
# African newsrooms and mining trade press that block Tavily's fetcher. Wire
# services are excluded because (a) they are usually paywalled, so both
# extractors fail and the credit is wasted, and (b) a 3-day Reuters query
# returns mostly world news that the relevance gate then discards — measured at
# eight off-topic pages fetched and thrown away in a single run.
FC_DOMAINS = {
    "thenationonlineng.net", "punchng.com", "vanguardng.com", "guardian.ng",
    "thisdaylive.com", "leadership.ng", "tribuneonlineng.com", "sunnewsonline.com",
    "dailytrust.com", "premiumtimesng.com", "saharareporters.com", "businessday.ng",
    "thecable.ng", "myjoyonline.com", "graphic.com.gh", "citinewsroom.com",
    "ghanaweb.com", "3news.com", "adomonline.com", "peacefmonline.com",
    "mining.com", "im-mining.com", "mining-journal.com", "mining-technology.com",
}


def firecrawl_key():
    k = (os.environ.get("FIRECRAWL_API_KEY") or "").strip()
    if k and not k.startswith("__REPLACE"):
        return k
    if os.path.exists(FIRECRAWL_ENV):
        try:
            with io.open(FIRECRAWL_ENV, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip().startswith("FIRECRAWL_API_KEY="):
                        v = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if v and not v.startswith("__REPLACE"):
                            return v
        except Exception:
            pass
    mcp = os.path.join(os.path.expanduser("~"), ".workbuddy-ai", "mcp.json")
    try:
        with io.open(mcp, encoding="utf-8") as fh:
            auth = ((json.load(fh).get("mcpServers") or {}).get("firecrawl") or {}).get(
                "headers", {}).get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth.split(" ", 1)[1].strip()
    except Exception:
        pass
    return ""


def firecrawl_scrape(url, key, tries=2):
    """Scrape one page to Markdown, backing off on the per-minute rate limit."""
    import urllib.request
    import urllib.error
    body = json.dumps({"url": url, "formats": ["markdown"],
                       "onlyMainContent": True}).encode()
    for attempt in range(tries):
        req = urllib.request.Request(
            FIRECRAWL_API, data=body,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return ((json.load(resp).get("data") or {}).get("markdown") or "")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:
                print("      rate limited, waiting 65s", flush=True)
                time.sleep(65)
                continue
            raise


# Anything in the window whose cache is missing or unusable — including entries
# left over from earlier runs — gets the Firecrawl attempt. URLs already tried
# and failed through Firecrawl are not retried, so a publisher that blocks both
# extractors does not cost credits on every run.
retry = []
for u in sorted(cands, key=lambda x: cands[x][1], reverse=True):
    dom = re.sub(r"^www\.", "", re.sub(r"^https?://", "", u).split("/")[0])
    if dom not in FC_DOMAINS:
        continue
    p = cache_path(u)
    if not os.path.exists(p):
        retry.append(u)
        continue
    try:
        with io.open(p, encoding="utf-8") as fh:
            rec = json.load(fh)
        if not rec.get("ok") and rec.get("source") != "firecrawl":
            retry.append(u)
    except Exception:
        retry.append(u)

if retry:
    key = firecrawl_key()
    print("firecrawl fallback: %d url(s)%s" % (len(retry), "" if key else " — NO KEY, skipped"))
    if key:
        fc_ok = 0
        fc_dead = False
        for i, u in enumerate(retry, 1):
            if fc_dead:
                break
            print("   [%d/%d] %s" % (i, len(retry), u[:70]), flush=True)
            try:
                md = firecrawl_scrape(cands[u][0], key)
            except Exception as e:
                msg = str(e)
                print("      !! ", msg[:100], flush=True)
                # 402 = account out of credits; every further call fails the
                # same way, so stop instead of burning one request per URL.
                if "402" in msg or "Insufficient credits" in msg:
                    print("      !! Firecrawl credits exhausted — stopping the "
                          "fallback for this run", flush=True)
                    fc_dead = True
                continue
            rec = {"url": cands[u][0], "source": "firecrawl",
                   "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "chars": len(md), "ok": bool(md and len(md) > 400)}
            if md:
                rec["text"] = md
            with io.open(cache_path(u), "w", encoding="utf-8") as fh:
                json.dump(rec, fh, ensure_ascii=False)
            fc_ok += 1 if rec["ok"] else 0
            time.sleep(1.2)
        print("firecrawl recovered: %d/%d" % (fc_ok, len(retry)))

# ── Direct-scrape fallback (zero API cost) ───────────────────────────────────
# When Tavily is quota-dead AND Firecrawl is credit-dead, both paid paths are
# gone but the show must go on: the West African roster is mostly plain
# WordPress, so a direct HTTP fetch + paragraph extraction recovers the body.
# Direct connection first (the shared proxy IP gets rate-limited), env proxy as
# fallback. Politeness: 1.5s between requests, browser UA.
import html as _html
import urllib.error
import urllib.request

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_TAG_RX = re.compile(r"<[^>]+>")
_P_RX = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)
_STRIP_RX = re.compile(
    r"<(script|style|noscript|header|footer|nav|aside|form|figure)[^>]*>.*?</\1>",
    re.S | re.I)


def direct_scrape(url, tries=2):
    """Fetch a page directly and return its paragraph text, or ''.

    Tries a proxy-free opener first (the shared env-proxy egress IP gets
    rate-limited by several of these publishers), then the default opener which
    honours the env proxy.
    """
    last = None
    # urllib.request is a module, not an opener — it has urlopen, not open, so it
    # cannot go in an opener tuple. Wrap it.
    _DEFAULT = urllib.request.build_opener()
    for attempt in range(tries):
        for opener in (_DIRECT, _DEFAULT):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/126.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,*/*",
                    "Accept-Language": "en-US,en;q=0.9"})
                with opener.open(req, timeout=45) as resp:
                    raw = resp.read(2_500_000)
                    ct = resp.headers.get("Content-Type", "")
                enc = "utf-8"
                m = re.search(r"charset=([\w\-]+)", ct, re.I)
                if m:
                    enc = m.group(1)
                doc = raw.decode(enc, "replace")
                doc = _STRIP_RX.sub(" ", doc)
                paras = []
                for p in _P_RX.findall(doc):
                    t = _html.unescape(_TAG_RX.sub(" ", p))
                    t = " ".join(t.split())
                    if len(t) >= 60:
                        paras.append(t)
                return "\n".join(paras)
            except Exception as e:
                last = e
                continue
        time.sleep(2)
    print("      !! direct:", str(last)[:90], flush=True)
    return ""


# Candidates whose cache is still unusable after Tavily + Firecrawl. Unlike the
# Firecrawl list this one is not limited to FC_DOMAINS — direct fetching is
# free, so agency/mining URLs get a try too. URLs that already failed a direct
# scrape are not retried this run.
d_retry = []
for u in sorted(cands, key=lambda x: cands[x][1], reverse=True):
    p = cache_path(u)
    if not os.path.exists(p):
        d_retry.append(u)
        continue
    try:
        with io.open(p, encoding="utf-8") as fh:
            rec = json.load(fh)
        if not rec.get("ok") and rec.get("source") != "direct":
            d_retry.append(u)
    except Exception:
        d_retry.append(u)

if d_retry:
    print("direct-scrape fallback: %d url(s)" % len(d_retry))
    d_ok = 0
    for i, u in enumerate(d_retry[:60], 1):   # cap: keep the run bounded
        print("   [%d/%d] %s" % (i, min(60, len(d_retry)), cands[u][0][:70]), flush=True)
        text = direct_scrape(cands[u][0])
        rec = {"url": cands[u][0], "source": "direct",
               "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "chars": len(text), "ok": bool(text and len(text) > 400)}
        if text:
            rec["text"] = text
        with io.open(cache_path(u), "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
        d_ok += 1 if rec["ok"] else 0
        time.sleep(1.5)
    print("direct-scrape recovered: %d/%d" % (d_ok, min(60, len(d_retry))))

# Report the state of the whole cache, not just this run's slice: on a re-run the
# Tavily loop fetches nothing and a per-run counter would misleadingly print 0.
usable = total_cached = 0
for p in glob.glob(os.path.join(RAW, "article_*.json")):
    total_cached += 1
    try:
        with io.open(p, encoding="utf-8") as fh:
            if json.load(fh).get("ok"):
                usable += 1
    except Exception:
        pass
print("this run: tavily=%d | cache: %d usable of %d files" % (got, usable, total_cached))
