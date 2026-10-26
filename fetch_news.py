# -*- coding: utf-8 -*-
"""Batch news collection for the Africa Eyes monitor.

Uses Tavily against the designated West Africa / international / mining source
roster, across four target countries and five topic axes.

Window: the Tavily `days` parameter is set to LOOKBACK_DAYS, and the same 3-day
cut is applied again in build_data.py, so a stale index entry cannot widen the
window. Raw responses are written to data/raw/ for audit.
"""
import glob
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    from tavily import TavilyClient  # noqa: E402
except Exception:                     # tavily is optional: the RSS + GDELT path
    TavilyClient = None               # needs no package at all, which is what
                                      # lets this run in a bare cloud sandbox.

try:                                  # direct-HTTPS provider layer (sources.py)
    import sources as _sources
except Exception:
    _sources = None

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raw")
os.makedirs(OUT_DIR, exist_ok=True)

# Query window. Measured both ways: days=7 returns MORE rows (~8/query) but most
# are 3-7 days old and then get dropped, while days=3 returns fewer rows (~2-4)
# that are nearly all inside the window. Counter-intuitively the tight window
# yields MORE usable in-window items, because the index returns its freshest
# slice rather than a superset. build_data.py still enforces the 3-day cut.
LOOKBACK_DAYS = 3

# Key rotation. Tavily dev keys have a small monthly allowance and an hourly
# schedule burns it fast, so several keys are tried in order: the first one that
# still has quota serves the whole run. Priority: TAVILY_API_KEY (env) →
# tavily_keys.txt (explicit order) → mcp.json.
# Which keys are spent is remembered in _tavily_key_state.json so a later run
# starts on a live key instead of burning a request to rediscover a dead one.
KEYS = []
# TAVILY_API_KEYS carries the whole pool in one value (comma / semicolon /
# whitespace separated) — GitHub Secrets can only hold a single string, so this
# is how the cloud runner sees every key the reader added.
for _tok in re.split(r"[,;\s]+", os.environ.get("TAVILY_API_KEYS") or ""):
    _tok = _tok.strip()
    if _tok.startswith("tvly-") and _tok not in KEYS:
        KEYS.append(_tok)
env_key = (os.environ.get("TAVILY_API_KEY") or "").strip()
if env_key and env_key not in KEYS:
    KEYS.append(env_key)
_extra = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tavily_keys.txt")
if os.path.exists(_extra):
    try:
        with io.open(_extra, encoding="utf-8") as fh:
            for line in fh:
                k = line.strip()
                if k and not k.startswith("#") and k not in KEYS:
                    KEYS.append(k)
    except Exception:
        pass
_mcp = os.path.join(os.path.expanduser("~"), ".workbuddy-ai", "mcp.json")
try:
    with io.open(_mcp, encoding="utf-8") as fh:
        for k in re.findall(r"tvly-[A-Za-z0-9_\-]{16,}", fh.read()):
            if k not in KEYS:
                KEYS.append(k)
except Exception:
    pass

STATE_PATH = os.path.join(OUT_DIR, "_tavily_key_state.json")
_dead = {}
try:
    with io.open(STATE_PATH, encoding="utf-8") as fh:
        _dead = json.load(fh)
except Exception:
    _dead = {}
# Live keys first, spent keys last — but keep spent keys in the list, because a
# dev quota can reset and one of them may work again tomorrow.
KEYS = [k for _, k in sorted(
    enumerate(KEYS), key=lambda t: (1 if _dead.get(t[1][:14]) else 0, t[0]))]


def mark_dead(key):
    _dead[key[:14]] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with io.open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(_dead, fh, ensure_ascii=False, indent=1)
    except Exception:
        pass


KEY = KEYS[0] if KEYS else ""
if not KEY:
    print("FATAL: no Tavily key")
    sys.exit(1)
print("tavily keys: %d (live first: %s) | lookback: %d days"
      % (len(KEYS), ", ".join(k[:10] + ("*" if _dead.get(k[:14]) else "")
                              for k in KEYS), LOOKBACK_DAYS))

WA = [
    "premiumtimesng.com", "punchng.com", "vanguardng.com", "saharareporters.com",
    "businessday.ng", "thecable.ng", "myjoyonline.com", "graphic.com.gh",
    "citinewsroom.com", "thenationonlineng.net", "dailytrust.com", "guardian.ng",
    "thisdaylive.com", "leadership.ng", "tribuneonlineng.com", "sunnewsonline.com",
    "ghanaweb.com", "3news.com", "adomonline.com", "peacefmonline.com",
]
# Francophone sources: Niger and Mali are French-language media markets, and an
# English-only query set under-covers them badly.
FR = ["actuniger.com", "lesahel.org", "airinfo.org", "malijet.com", "maliweb.net", "aouaga.com"]
AGENCY = ["reuters.com", "africanews.com", "theafricareport.com", "allafrica.com", "bbc.com", "ecofinagency.com"]
MINING = ["mining.com", "mining-journal.com", "mining-technology.com", "im-mining.com"]

PLAN = [
    # ────────────────────────────────────────────────────────────────────────
    # MEASURED RULE (2026-10-20, 20 queries against the live API):
    #   short query (2–4 words) + NO include_domains  → 9–10 results, every time
    #   long query  (5–7 words) + include_domains     → 0–4 results, most times
    # The West-Africa-China angles were the worst case: with WA restricted they
    # returned cn_ml=0, cn_ng=4, cn_ne=2, cn_gh=1 while the unrestricted
    # periphery angles returned 10–12 each. That is the real reason the
    # 非洲四国 module looked thin — not a lack of coverage in the world, but a
    # filter that made the API answer "nothing found". Every angle below is now
    # short and unrestricted; build_data's country gate still rejects anything
    # that does not anchor to one of the twelve countries.
    # ─── Africa four — China angle (the largest yield axis) ────────────────
    ("cn_ng",   "China Nigeria investment", [], 12),
    ("cn_ng2",  "Chinese company Nigeria", [], 12),
    ("cn_gh",   "China Ghana investment", [], 12),
    ("cn_gh2",  "Chinese miners Ghana", [], 12),
    ("cn_ne",   "China Niger oil", [], 12),
    ("cn_ml",   "China Mali mining", [], 12),
    ("cn_wa",   "Chinese firm West Africa", [], 12),
    ("cn_af",   "Chinese investment Africa", [], 12),
    # ─── Africa four — China RISK events ────────────────────────────────────
    # Rewritten 2026-10-22 after the reader pointed out that several real
    # incidents were on the wire and not on the page. Two things were wrong:
    #   · the queries described the STORY ("Chinese workers attacked") instead
    #     of what the desk actually writes ("gunmen abduct", "police arrest",
    #     "traders protest");
    #   · they were all conditioned on the word "Chinese", so a story headlined
    #     "Nigerian traders' dispute with Chinese vendors" ranked low, and one
    #     headlined "18 arrested in Surigao illegal mining raid" not at all.
    # The angles below are the phrasings the local press uses for the same four
    # events, measured one by one on the live index.
    ("cn_kidnap",   "Chinese kidnapped Nigeria", [], 8),
    ("cn_abduct",   "foreign workers abducted Nigeria", [], 8),
    ("cn_arrest",   "Chinese arrested Africa mining", [], 8),
    ("cn_charged",  "Chinese national court Nigeria", [], 8),
    ("cn_mineatk",  "Chinese mine site attack", [], 8),
    ("cn_trader",   "Nigerian traders Chinese market", [], 8),
    ("cn_protest",  "Chinese investment protest Nigeria", [], 8),
    ("cn_fined",    "Chinese company fined", [], 8),
    ("cn_illegal",  "Chinese illegal mining arrested", [], 8),
    ("cn_galamsey", "Chinese galamsey Ghana", [], 8),
    ("cn_casualty", "Chinese killed Africa accident", [], 8),
    # ─── Niger: the oil complex ──────────────────────────────────────────────
    # The reader asked for CNPC Niger, the Zinder refinery, Agadem, the
    # Niger–Benin pipeline and the oil ministry by name, and got two cards.
    # Reason: the NAMES are not how the news is written. In the window that
    # mattered, every Niger oil story was a FUEL story — "Niger bans petrol and
    # diesel exports", "fuel queues form in Niamey", "oil well eruption".
    # "Zinder refinery SORAZ" returned Indian refineries and South African zinc;
    # "Niger fuel shortage refinery" returned eight on-topic items. So the
    # angles lead with the phenomenon and let the names catch the rest.
    ("ne_oil",      "Niger fuel shortage refinery", [], 8),
    ("ne_fuel",     "Niamey fuel", [], 8),
    ("ne_export",   "Niger bans petrol exports", [], 8),
    ("ne_ministry", "Niger petrol minister oil", [], 8),
    ("cn_cnpc",     "Agadem oil field", [], 8),
    ("cn_cnpc2",    "CNPC Niger oil", [], 8),
    ("cn_soraz",    "Zinder refinery Niger", [], 8),
    ("cn_pipe",     "Niger Benin pipeline", [], 8),
    ("cn_killed",   "Chinese national killed Africa", [], 8),
    ("cn_summon",   "Chinese company summoned government", [], 8),
    # ─── Niger: uranium ──────────────────────────────────────────────────────
    # Added 2026-10-26 after the reader asked for Niger uranium coverage by name
    # and the page had ZERO uranium cards. Measured the same day on the live
    # index: "Arlit uranium" 9, "Orano Niger uranium" 7, "Chinese uranium Niger"
    # 8 — all inside the 3-day window. "Niger uranium mine" was rejected as an
    # angle on purpose: it returned Mauritania, Kazakhstan and Tanzania mines,
    # which the country gate then throws away, i.e. paid for and discarded.
    # Arlit is the mining town, Orano the operator; between them they name the
    # asset without naming the country, which is exactly what the wire does.
    ("ne_arlit",    "Arlit uranium", [], 10),
    ("ne_orano",    "Orano Niger uranium", [], 8),
    ("cn_uranium",  "Chinese uranium Niger", [], 8),
    # ─── Africa four — US angle ─────────────────────────────────────────────
    ("us_ng",    "US Nigeria", [], 12),
    ("us_gh",    "US Ghana military", [], 12),
    ("us_ne_ml", "US Niger Mali", [], 12),
    ("us_biz",   "American company Nigeria energy", [], 12),
    # ─── Africa four — terror groups (MPLJ / FPL / JNIM / EIGS / ISIS / Lakurawa)
    # ONE GROUP PER ANGLE, each with a short query. The old combined strings
    # ("JNIM EIGS attack Niger Mali", "MPLJ FPL jihadist Sahel attack") returned a
    # handful of hits at best, and the FPL one returned Premier League FANTASY
    # FOOTBALL ("FPL player price changes — risers, fallers and top transfers"):
    # in a search index "FPL" is the fantasy game far more often than it is the
    # Front Patriotique de Libération. Measured 2026-10-20, per-group queries give
    # JNIM Niger 8 / MPLJ Niger 7 / EIGS 7 / Lakurawa 8 / Boko Haram 6 /
    # ISWAP 8 / Sahel jihadist 8 — roughly 50 usable items instead of 16.
    ("ter_jnim_ml", "JNIM Mali attack", [], 10),
    # Short form on purpose. The reader asked for JNIM-Mali coverage on
    # 2026-10-26; the angle above conditions on the word "attack", so the
    # group's blockade, taxation and governance stories never surfaced. Measured
    # the same day: "JNIM Mali" 10 / "Mali army attack" 10, versus 9 for the
    # long form, and the extra ones are the ones that matter — "Bamako is No
    # Longer Safe: JNIM Shifts Scale", "JNIM's entrenchment in southwestern Mali".
    ("ter_jnim_ml2", "JNIM Mali", [], 10),
    ("ter_mali_atk", "Mali army attack", [], 10),
    ("ter_jnim_ne", "JNIM Niger", [], 10),
    ("ter_eigs", "EIGS Islamic State Sahel", [], 10),
    ("ter_mplj", "MPLJ Niger", [], 10),
    ("ter_lakurawa", "Lakurawa Nigeria", [], 10),
    ("ter_boko", "Boko Haram attack", [], 10),
    ("ter_iswap", "ISWAP Nigeria", [], 10),
    ("ter_sahel", "Sahel jihadist attack", [], 10),
    # ─── Africa four — government / corridor (kept for tier 3 / tier 4)
    ("gov_ng", "Nigeria government approves", [], 12),
    ("gov_gh", "Ghana government policy", [], 12),
    ("gov_ne_ml", "Niger Mali junta", [], 12),
    ("cor_pipeline", "Nigeria Morocco gas pipeline", [], 12),
    ("cor_rail_road", "railway project Nigeria", [], 12),
    # ─── China periphery — 8 countries × China focus (per the reader's spec,
    #   "涉及中国人、中国企业、中国项目的新闻" only; no terror or US lens).
    # Short, keyword-style queries with NO domain filter: that is what actually
    # returns hits. The long AGENCY-restricted versions returned 0 on every run.
    ("cn_kr", "Chinese company South Korea investment", [], 12),
    ("cn_ph", "Chinese company Philippines project", [], 12),
    ("cn_mm", "Chinese company Myanmar investment project", [], 12),
    ("cn_ru", "China Russia trade energy cooperation", [], 12),
    ("cn_in", "Chinese company India investment border", [], 12),
    ("cn_np", "Chinese company Nepal investment hydropower", [], 12),
    ("cn_vn", "Chinese company Vietnam factory investment", [], 12),
    ("cn_la", "Chinese company Laos railway investment", [], 12),
    # Breadth for the four neighbours that were thin. Measured 2026-10-21 on the
    # live page: kr 57 / in 36 / ru 20 / ph 20, but vn 3 / la 2 / mm 2 / np 1.
    # Not a coverage gap in the world — those four simply had one narrow angle
    # each. Free RSS cannot fill it (only Korea/India/Vietnam/Mekong feeds
    # exist), so one broad angle per country, short form.
    ("vn_wide", "Vietnam China trade", [], 10),
    ("la_wide", "Laos China railway", [], 10),
    ("np_wide", "Nepal China project", [], 10),
    ("mm_wide", "Myanmar China project", [], 10),
    # ─── Breadth sweeps: a plain "latest news" recovers in-window items that
    #    keyword-specific angles miss. Short form measured at 9–10 results;
    #    the previous domain-restricted versions returned 0–3.
    ("ng_extra", "Nigeria latest news", [], 10),
    ("gh_extra", "Ghana latest news", [], 10),
    ("ne_extra", "Niger latest news", [], 10),
    ("ml_extra", "Mali latest news", [], 10),
]

client = TavilyClient(api_key=KEY) if TavilyClient else None
_key_idx = 0


def rotate_key():
    """Switch to the next Tavily key. Returns True when one is left to try."""
    global client, _key_idx
    while _key_idx + 1 < len(KEYS):
        _key_idx += 1
        client = TavilyClient(api_key=KEYS[_key_idx]) if TavilyClient else None
        print("   .. rotating to tavily key #%d (%s...)"
              % (_key_idx + 1, KEYS[_key_idx][:10]), flush=True)
        return True
    return False
# ── GDELT fallback (free, no key) ────────────────────────────────────────────
# The Tavily key is a dev-quota key; when it reports "exceeds your plan's set
# usage limit" the whole angle would come back empty and the site goes blank.
# GDELT's DOC 2.0 artlist API needs no key and indexes the same West African
# roster (plus the francophone outlets Tavily misses). Rate limit ~1 req/5s.
GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
QUOTA_RX = re.compile(r"usage limit|exceeds your plan|upgrade your plan", re.I)


def _gdelt_fetch(url, tries=3):
    """Fetch one GDELT URL with backoff.

    Measured behaviour of this endpoint: it answers 429 aggressively from a
    shared egress IP, and a query longer than roughly 500 characters gives a
    non-JSON reply instead of results. So: direct connection only (the env
    proxy adds another shared IP for nothing), 429 backs off 15/30s, and a
    non-JSON body is raised as ValueError so the caller can shrink the query.
    """
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 africa-eyes"})
            with direct.open(req, timeout=90) as resp:
                body = resp.read().decode("utf-8", "replace")
            if not body.lstrip().startswith("{"):
                raise ValueError("non-JSON reply: " + body.strip()[:80])
            return json.loads(body)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 and attempt < tries - 1:
                wait = 25 * (attempt + 1)
                print("      429 — waiting %ds" % wait, flush=True)
                time.sleep(wait)
                continue
            raise
        except ValueError as e:
            last = e
            if attempt < tries - 1:
                time.sleep(8)
                continue
            raise
    raise last


def gdelt_query(tag, kw, domains, n):
    """One GDELT artlist query, mapped to the Tavily-result shape.

    GDELT accepts a long boolean query but starts truncating past ~500 chars,
    so the domain OR-list is capped at 6 outlets and `kw` is already written as
    a compact `(a OR b OR c)` group by the caller.
    """
    parts = ["(%s)" % kw]
    if domains:
        parts.append("(%s)" % " OR ".join("domain:%s" % d for d in domains[:6]))
    query = " AND ".join(parts)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=LOOKBACK_DAYS)
    params = {"query": query, "mode": "artlist",
              "maxrecords": str(min(250, max(50, n))), "format": "json",
              "startdatetime": start.strftime("%Y%m%d%H%M%S"),
              "enddatetime": end.strftime("%Y%m%d%H%M%S")}
    url = GDELT_API + "?" + urllib.parse.urlencode(params)
    doc = _gdelt_fetch(url)
    results = []
    for a in doc.get("articles", []):
        pub = ""
        m = re.match(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})", a.get("seendate", ""))
        if m:
            pub = "%s-%s-%sT%s:%s:%sZ" % m.groups()
        results.append({"title": a.get("title") or "", "url": a.get("url") or "",
                        "content": "", "published_date": pub, "score": 0.5})
    return results


def _tavily_search(q, n, domains):
    """One Tavily query, with or without the vendor package.

    The cloud sandbox has no `tavily` package, which used to mean the whole
    Tavily pass was skipped server-side. sources.tavily_search speaks the same
    HTTPS API directly, so the deployed service now gets the same coverage as
    the laptop.
    """
    if client is not None:
        res = client.search(query=q, search_depth="advanced", topic="news",
                            max_results=n, include_answer=False,
                            include_domains=domains or None, days=LOOKBACK_DAYS)
        return res.get("results", [])
    if _sources is not None and _sources.tavily_keys():
        items = _sources.tavily_search(q, days=LOOKBACK_DAYS, max_results=n)
        if domains:
            items = [x for x in items
                     if any(d.replace("www.", "") in (x.get("url") or "") for d in domains)]
        return items
    raise RuntimeError("no tavily package and no key configured")


records = []
quota_dead = False
qmark = os.path.join(OUT_DIR, "_tavily_quota_dead")

# ── Tavily pass gating ───────────────────────────────────────────────────────
# Tavily + xAI are the primary channels now that several keys are configured:
# they are the only ones that can actually surface "China in country X" and
# "terror group activity" angles. RSS is a free supplement, GDELT a fallback.
# TAVILY_MIN_INTERVAL=0 therefore means "run every round"; set it higher only if
# the key pool starts running dry.
TAVILY_MIN_INTERVAL = int(os.environ.get("TAVILY_MIN_INTERVAL", "0"))
# NEWS_FAST=1 is the catch-up path: when a visit to the page wakes a sleeping
# container, the reader is waiting, so take only RSS (fast, free).
_FAST = os.environ.get("NEWS_FAST") == "1"
if _FAST:
    print("NEWS_FAST=1 — RSS only (catch-up run)")

# The cooldown timestamp is read from the FILE CONTENT, never from mtime: a fresh
# `git checkout` in Actions resets mtime to "now", which made every cloud run
# look like Tavily had just run and silently disabled it for good.
_tav_mark = os.path.join(OUT_DIR, "_tavily_last.txt")
_skip_tavily = False
try:
    if TAVILY_MIN_INTERVAL > 0 and os.path.exists(_tav_mark):
        with io.open(_tav_mark, encoding="utf-8") as fh:
            _last = float((fh.read() or "0").strip() or 0)
        _age_min = (time.time() - _last) / 60.0
        if 0 <= _age_min < TAVILY_MIN_INTERVAL:
            print("tavily: last pass %.0f min ago (< %d) — RSS/GDELT only this round"
                  % (_age_min, TAVILY_MIN_INTERVAL))
            _skip_tavily = True
except Exception:
    pass

_has_tavily = (client is not None) or (_sources is not None and bool(_sources.tavily_keys()))
_run_tavily = (not _skip_tavily) and (not _FAST) and _has_tavily

if _run_tavily:
    # Clear previous snapshots only when a fresh pass is about to run. Query sets
    # change between versions and a leftover file from an older plan would inject
    # out-of-window items. Deleting them on a *skipped* round — which the old code
    # did — threw away the last good Tavily data and left the page with RSS only.
    for stale in glob.glob(os.path.join(OUT_DIR, "tavily_news_*.json")):
        os.remove(stale)
    for stale in glob.glob(os.path.join(OUT_DIR, "gdelt_news_*.json")):
        os.remove(stale)
    if os.path.exists(qmark):
        os.remove(qmark)
    print("cleared previous news snapshots")
else:
    print("tavily: not run this round — keeping the previous snapshot")
    quota_dead = True
    if not _has_tavily:
        print("no tavily package/key — skipping the Tavily pass entirely")
        with io.open(qmark, "w", encoding="utf-8") as fh:
            fh.write("no tavily key available")

for tag, q, domains, n in PLAN:
    if quota_dead:
        break
    print(">>", tag, "(%dd)" % LOOKBACK_DAYS, "|", q[:52], flush=True)
    r = None
    while True:
        try:
            r = {"success": True, "results": _tavily_search(q, n, domains)}
            break
        except Exception as e:
            if QUOTA_RX.search(str(e)):
                mark_dead(KEYS[_key_idx])
                if rotate_key():
                    continue          # same angle, next key
                quota_dead = True
                print("   !! all Tavily keys exhausted — stopping the Tavily pass, "
                      "switching to the RSS + GDELT plan", flush=True)
                with io.open(qmark, "w", encoding="utf-8") as fh:
                    fh.write(str(e)[:300])
                r = {"success": False, "results": [], "error": "quota exhausted"}
            else:
                r = {"success": False, "results": [], "error": str(e)}
            break
    if quota_dead and not r.get("success"):
        break
    r["_tag"] = tag
    r["_query"] = q
    r["_domains"] = domains
    r["_days"] = LOOKBACK_DAYS
    records.append(r)
    with io.open(os.path.join(OUT_DIR, "tavily_news_%s.json" % tag), "w", encoding="utf-8") as fh:
        json.dump(r, fh, ensure_ascii=False, indent=1)
    print("   ->", len(r.get("results", [])), "results",
          "" if r.get("success") else str(r.get("error"))[:90], flush=True)
    time.sleep(0.7)

with io.open(os.path.join(OUT_DIR, "tavily_news_all.json"), "w", encoding="utf-8") as fh:
    json.dump(records, fh, ensure_ascii=False, indent=1)

# Mark this pass so the cooldown above can skip the next few rounds.
if records and not quota_dead:
    try:
        with io.open(_tav_mark, "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time())))
    except Exception:
        pass

# ── GDELT pass ───────────────────────────────────────────────────────────────
# Run when Tavily is quota-dead (and optionally always when GD_FORCE=1).
# Design constraints learned from the endpoint itself:
#   * a boolean query longer than ~500 chars is rejected outright → the keyword
#     group is capped at 4 terms and the domain group at 6 outlets;
#   * 429s come from the shared egress IP, so queries are few and wide (10
#     angles × up to 120 records) rather than many and narrow; 8s pacing.
# The wide angles also pick up the francophone outlets (actuniger, lesahel,
# malijet, maliweb) that the Tavily index does not cover at all.
WA_NG = ["punchng.com", "premiumtimesng.com", "vanguardng.com",
         "businessday.ng", "guardian.ng", "thenationonlineng.net"]
WA_GH = ["myjoyonline.com", "graphic.com.gh", "citinewsroom.com",
         "ghanaweb.com", "3news.com", "adomonline.com"]
FR_WA = ["actuniger.com", "lesahel.org", "airinfo.org", "malijet.com",
         "maliweb.net"]
AGENCY = ["reuters.com", "africanews.com", "allafrica.com", "bbc.com"]

# GDELT is now a narrow supplement, not a replacement: the RSS pass already
# covers the Nigerian and Ghanaian newsrooms comprehensively, so GDELT is only
# asked for what RSS cannot reach — the francophone Sahel outlets and the
# regional wire services. Fewer queries also means far fewer 429s from this
# endpoint's shared-IP rate limit.
GDELT_PLAN = [
    ("ne",       "Niger OR Niamey OR uranium OR junta", FR_WA[:5]),
    ("ml",       "Mali OR Bamako OR jihadist OR orpaillage", FR_WA[3:5] + AGENCY[:3]),
    ("regional", "Sahel OR ECOWAS OR JNIM OR ISWAP", AGENCY),
    # Two unrestricted angles for the assets the reader asked about by name on
    # 2026-10-26. The domain-restricted queries above cannot reach them: the
    # uranium beat is covered by Orano, RFI and the mining wires, none of which
    # are in FR_WA, and the JNIM beat by security monitors. GDELT is free, so
    # the only cost is 30 s of pacing — worth it for the two named beats.
    ("ne_uranium", "uranium AND (Niger OR Arlit OR Imouraren OR Orano)", []),
    ("ml_jnim",    "JNIM AND (Mali OR Bamako OR Gao OR Kidal OR Mopti)", []),
]

gdelt_total = 0
# NOTE: the GDELT pass itself runs after the RSS pass (see below) — RSS is fast
# and reliable, GDELT stalls on 429s, so RSS must not wait behind it.
# ── RSS pass (free, no key, no quota) ────────────────────────────────────────
# The most reliable free source of West African news: every outlet here publishes
# a WordPress RSS feed whose <pubDate> is exact to the second, unlike GDELT's
# index lag and Tavily's dev quota. Feeds carry title + link + lede only, so the
# article body still comes from fetch_articles.py (which falls back to a direct
# scrape when both paid extractors are out).
# Emitted under the tavily_news_* name so build_data.py and fetch_articles.py
# consume them through the existing glob without a second code path.
RSS_FEEDS = [
    ("punchng",     "https://punchng.com/feed/",                      "punchng.com"),
    ("premiumtimes", "https://www.premiumtimesng.com/feed",           "premiumtimesng.com"),
    ("vanguard",    "https://www.vanguardngr.com/feed/",              "vanguardng.com"),
    ("businessday", "https://businessday.ng/feed/",                   "businessday.ng"),
    ("thisday",     "https://www.thisdaylive.com/index.php/feed/",    "thisdaylive.com"),
    ("leadership",  "https://leadership.ng/feed/",                    "leadership.ng"),
    ("dailytrust",  "https://dailytrust.com/feed/",                   "dailytrust.com"),
    ("thenation",   "https://thenationonlineng.net/feed/",            "thenationonlineng.net"),
    ("sahara",      "https://saharareporters.com/feeds/latest/feed",  "saharareporters.com"),
    ("myjoyonline", "https://www.myjoyonline.com/feed/",              "myjoyonline.com"),
    ("citinews",    "https://citinewsroom.com/feed/",                 "citinewsroom.com"),
    ("ghanaweb",    "https://www.ghanaweb.com/GhanaHomePage/rss/news.xml", "ghanaweb.com"),
    # China periphery RSS (added 2026-10-20): English / cheap-channels that
    # cover China activity in the eight neighbouring countries. Multiple
    # candidates per country because some outlets (Irrawaddy, The Hindu, etc.)
    # block the cloud sandbox's egress IP — having a fallback means the
    # country still produces content if the primary feed 404s or 000s.
    ("irrawaddy",   "https://www.irrawaddy.com/rss.xml",              "irrawaddy.com"),
    ("frontier_mm", "https://www.frontiermyanmar.net/feed/",          "frontiermyanmar.net"),
    ("mizzima",     "https://www.mizzima.com/rss.xml",                "mizzima.com"),
    ("rappler",     "https://www.rappler.com/feed/",                  "rappler.com"),
    ("inquirer",    "https://www.inquirer.net/feed/",                 "inquirer.net"),
    ("gmanews",     "https://www.gmanetwork.com/news/rss/",           "gmanetwork.com"),
    ("moscowtimes", "https://www.themoscowtimes.com/rss/news",        "themoscowtimes.com"),
    ("koreaherald", "https://www.koreaherald.com/rss",                "koreaherald.com"),
    ("koreatimes",  "https://www.koreatimes.co.kr/www/rss/rss.xml",   "koreatimes.co.kr"),
    ("thehindu",    "https://www.thehindu.com/feeder/default.rss",    "thehindu.com"),
    ("htrss",       "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml", "hindustantimes.com"),
    ("vnexpress",   "https://vnexpress.net/rss/tin-moi-nhat.rss",     "vnexpress.net"),
    ("tuoitre",     "https://tuoitre.vn/rss/tin-moi-nhat.rss",        "tuoitre.vn"),
    ("kathmandupost", "https://kathmandupost.com/rss",                "kathmandupost.com"),
    # Laos had no feed at all, which is why 老挝 never got more than a couple of
    # items while Korea (three feeds) had 57. Free, so no credit cost.
    ("laotiantimes", "https://laotiantimes.com/feed/",                "laotiantimes.com"),
    ("rfa_chinese", "https://www.rfa.org/mandarin/rssfeed.xml",       "rfa.org"),
    # ── Beat feeds (added 2026-10-26) ───────────────────────────────────────
    # The reader asked for Niger uranium and Mali JNIM coverage and the page had
    # zero of each. Tavily was asked for both (ne_arlit / ne_orano / cn_uranium /
    # ter_jnim_ml2) and answered with Facebook posts, Instagram reels and YouTube
    # explainers — none of which carries a publish date, so every one of them was
    # discarded by the window gate. Measured that day: 9 of 9 "Arlit uranium"
    # hits were undated social posts. The topic does not live in the news index
    # right now; it lives in the trade press and the francophone wire. RSS is
    # free and always dated, so the fix belongs here.
    #   wnn          — World Nuclear News, the uranium/nuclear trade wire
    #   rfi_afrique  — francophone Africa; Tavily's index has ZERO coverage of it
    #                  (measured 2026-10-22), and it is where Niger/Mali news is
    #   africanews   — pan-African wire, English, dated
    # These are global feeds, so they are NOT in RSS_FEED_COUNTRY: build_data's
    # country gate rejects everything that does not anchor to the twelve.
    ("wnn",         "https://www.world-nuclear-news.org/rss",         "world-nuclear-news.org"),
    ("rfi_afrique", "https://www.rfi.fr/fr/afrique/rss",              "rfi.fr"),
    ("africanews",  "https://www.africanews.com/feed/",               "africanews.com"),
    # ── Francophone Sahel newsrooms (added 2026-10-26) ──────────────────────
    # These four are already in build_data's OUTLETS registry with country codes
    # ne/ml, but they had no RSS entry — so the only way their copy could reach
    # the page was GDELT, which returns ~20 items per angle and rate-limits
    # hard. Tavily's index has zero coverage of them (measured 2026-10-22), which
    # is why Niger/Mali coverage was so much thinner than Nigeria/Ghana.
    # They are single-country outlets, so they are deliberately NOT added to
    # RSS_FEED_COUNTRY: their headlines routinely name Diffa or Zinder without
    # ever saying "Niger", and anchoring would throw those away.
    #   lesahel  — Le Sahel, the Niger state daily
    #   airinfo  — Air Info, Niger
    #   malijet  — Malijet, Bamako (feed lives at /rss, not /feed)
    # actuniger is Joomla and answers ?feed=rss2 with the HTML homepage, so it has
    # no feed to subscribe to — it stays reachable through GDELT only.
    ("lesahel",     "https://www.lesahel.org/feed/",                  "lesahel.org"),
    ("airinfo",     "https://airinfo.org/feed/",                      "airinfo.org"),
    ("malijet",     "https://malijet.com/rss",                        "malijet.com"),
    # ── Local languages (added 2026-10-26) ──────────────────────────────────
    # "新闻、动态不仅英文；还要搜索下法语，非洲四国、周边国家当地语言、比如尼日尔豪萨语".
    # Hausa is the lingua franca of southern Niger and northern Nigeria, and it is
    # where the Nigerien street-level story lands first — the uranium and junta
    # copy that never reaches the English wire. BBC Hausa and RFI Hausa both
    # carry Niger by name (Nijar). Yoruba / Igbo / Pidgin cover the Nigerian
    # south that the Lagos papers report in English only.
    # Every one of these is translated before it reaches the page — the Chinese
    # gate in build_data defers anything whose translation is not ready, so a
    # Hausa or Korean headline can never surface untranslated.
    ("bbc_hausa",   "https://feeds.bbci.co.uk/hausa/rss.xml",         "bbc.com"),
    ("rfi_hausa",   "https://www.rfi.fr/ha/rss",                      "rfi.fr"),
    ("bbc_pidgin",  "https://feeds.bbci.co.uk/pidgin/rss.xml",        "bbc.com"),
    ("bbc_yoruba",  "https://feeds.bbci.co.uk/yoruba/rss.xml",        "bbc.com"),
    ("bbc_igbo",    "https://feeds.bbci.co.uk/igbo/rss.xml",          "bbc.com"),
    # Periphery in its own language: Korean, Burmese, Hindi, Nepali, Russian.
    # The English feeds above cover these countries through an English lens;
    # domestic-language copy carries the defence/procurement/border detail that
    # never gets translated for a foreign audience.
    ("bbc_korean",  "https://feeds.bbci.co.uk/korean/rss.xml",        "bbc.com"),
    ("bbc_burmese", "https://feeds.bbci.co.uk/burmese/rss.xml",       "bbc.com"),
    ("bbc_hindi",   "https://feeds.bbci.co.uk/hindi/rss.xml",         "bbc.com"),
    ("bbc_nepali",  "https://feeds.bbci.co.uk/nepali/rss.xml",        "bbc.com"),
    ("bbc_russian", "https://feeds.bbci.co.uk/russian/rss.xml",       "bbc.com"),
    ("yonhap_kr",   "https://www.yna.co.kr/rss/news.xml",             "yna.co.kr"),
]
_RSS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# Per-feed item cap, newest first (see the slice in fetch_rss).
RSS_MAX_ITEMS = int(os.environ.get("RSS_MAX_ITEMS", "30"))

# ── RSS country anchoring (added 2026-10-25) ────────────────────────────────
# The eight China-neighbourhood feeds are GENERAL newsrooms: koreatimes
# publishes its whole front page (K-pop, domestic politics, Wall Street),
# thehindu publishes all Indian news, vnexpress publishes all Vietnamese
# news. A feed that exists to cover "Chinese activity in Korea" therefore
# delivered ~70 off-beat items per round, and those items then filled the
# carry-over pool for three days — which is why the source-pollution rate sat
# at 30-37% for twenty-two consecutive rounds and the 涉华 tier opened with
# Tibet earthquakes and LAC border friction.
#
# The fix is at the ENTRY, not in the ranking: an item is kept only when its
# own copy anchors it to the country that feed exists to cover. Measured
# offline against the real snapshots: 403 kept / 150 dropped (27.1%).
#
# The West African feeds are NOT anchored — they are the core of the monitor,
# and a Ghanaian story about Nigeria is legitimately filed under Nigeria.
RSS_FEED_COUNTRY = {
    "koreatimes": ("kr", "Korea"), "koreaherald": ("kr", "Korea"),
    "rappler": ("ph", "Philippines"), "inquirer": ("ph", "Philippines"),
    "gmanews": ("ph", "Philippines"), "moscowtimes": ("ru", "Russia"),
    "vnexpress": ("vn", "Vietnam"), "tuoitre": ("vn", "Vietnam"),
    "kathmandupost": ("np", "Nepal"), "laotiantimes": ("la", "Laos"),
    "irrawaddy": ("mm", "Myanmar"), "frontier_mm": ("mm", "Myanmar"),
    "mizzima": ("mm", "Myanmar"), "thehindu": ("in", "India"),
    "htrss": ("in", "India"),
}
_RSS_CTY_MARKERS = {
    "kr": r"\bkorea(n|korean|n's)?\b|\bnorth korea\b|\bseoul\b|\bpyongyang\b|朝鲜|韩国|首尔|平壤",
    "ph": r"\bphilippine(s)?\b|\bmanila\b|\bfilipino(s)?\b|菲律宾|马尼拉",
    "ru": r"\brussia(n|s)?\b|\bmoscow\b|\bkremlin\b|俄罗斯|莫斯科|克里姆林",
    "vn": r"\bvietnam(ese)?\b|\bhanoi\b|\bhue\b|越南|河内",
    "np": r"\bnepal(ese|i)?\b|\bkathmandu\b|尼泊尔|加德满都",
    "la": r"\blaos\b|\blaotian\b|\bvientiane\b|老挝|万象",
    "mm": r"\bmyanmar(ese)?\b|\bburma(n|ese)?\b|\bnaypyidaw\b|\byangon\b|缅甸|内比都|仰光",
    "in": r"\bindia(n|ns)?\b|\bnew delhi\b|\bdelhi\b|\bmumbai\b|\bbangalore\b|印度|新德里|孟买",
}
_RSS_CTY_RX = {k: re.compile(v, re.I) for k, v in _RSS_CTY_MARKERS.items()}


def _rss_text(x):
    """Strip CDATA and decode entities from one RSS field value."""
    import html as _h
    x = re.sub(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", r"\1", x or "", flags=re.S)
    x = re.sub(r"<[^>]+>", " ", x)
    return " ".join(_h.unescape(x).split())


def fetch_rss():
    """Pull every configured feed, one request each, 1s apart."""
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    total = 0
    for name, feed_url, dom in RSS_FEEDS:
        print(">> rss", name, flush=True)
        try:
            req = urllib.request.Request(feed_url, headers={"User-Agent": _RSS_UA})
            with direct.open(req, timeout=30) as resp:
                xml = resp.read(1_500_000).decode("utf-8", "replace")
        except Exception as e:
            print("   !! %s: %s" % (name, str(e)[:70]), flush=True)
            time.sleep(1)
            continue
        results = []
        for block in re.findall(r"<item[ >].*?</item>", xml, re.S):
            def field(tag):
                m = re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), block, re.S)
                return _rss_text(m.group(1)) if m else ""
            title = field("title")
            link = field("link") or ""
            # strip tracking params WordPress appends to feed links
            link = re.sub(r"[?&](utm_source|utm_medium|utm_campaign)=[^&]*", "", link)
            link = link.rstrip("?&")
            if not title or not link:
                continue
            results.append({
                "title": title,
                "url": link,
                "content": field("description")[:900],
                "published_date": field("pubDate"),
                "score": 0.6,
            })
        # Country anchoring: a perimeter feed keeps only the items whose own
        # copy names the country that feed exists to cover. Everything else is
        # that newsroom's general output and has no business in this monitor.
        # See the RSS_FEED_COUNTRY block above for the measurements.
        _anchored = RSS_FEED_COUNTRY.get(name)
        if _anchored:
            _code, _label = _anchored
            _rx = _RSS_CTY_RX[_code]
            _before = len(results)
            results = [it for it in results
                       if _rx.search((it.get("title") or "") + " " +
                                     (it.get("content") or ""))]
            _dropped = _before - len(results)
            if _dropped:
                print("   anchor %s: dropped %d off-country item(s)"
                      % (_label, _dropped), flush=True)
        # Cap per feed. Adding the local-language wires (Hausa, Yoruba, Igbo,
        # Pidgin, Korean, Burmese, Hindi, Nepali, Russian) pushed the RSS leg from
        # ~350 to ~900 candidates a round, and every survivor is paid for twice —
        # once in translation, once in the reading time it takes from the page.
        # Feeds are newest-first, so the first 30 are the 30 that matter.
        results = results[:RSS_MAX_ITEMS]
        rec = {"success": True, "results": results, "engine": "rss",
               "_tag": "rss_" + name, "_query": feed_url, "_domains": [dom],
               "_days": LOOKBACK_DAYS}
        with io.open(os.path.join(OUT_DIR, "tavily_news_rss_%s.json" % name),
                     "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=1)
        total += len(results)
        print("   ->", len(results), "items", flush=True)
        time.sleep(1)
    return total


# TAVILY_ONLY=1 keeps the run to the paid index alone. Used for fast iteration:
# the RSS leg is 30 sequential feed fetches and is the slowest part of the pass.
_TAVILY_ONLY = os.environ.get("TAVILY_ONLY") == "1"
rss_total = 0 if _TAVILY_ONLY else fetch_rss()
print("rss items:", rss_total)

# ── GDELT pass (after RSS: it is the slowest and most rate-limited leg) ──────
if os.environ.get("GD_SKIP") != "1" and not _FAST:
    print("\n--- GDELT pass: %d angles ---" % len(GDELT_PLAN))
    for tag, kw, domains in GDELT_PLAN:
        if os.path.exists(os.path.join(OUT_DIR, "gdelt_news_%s.json" % tag)):
            continue
        print(">> gdelt", tag, "|", kw[:48], flush=True)
        try:
            g = gdelt_query(tag, kw, domains, 120)
        except Exception as e:
            print("   gdelt FAILED:", str(e)[:100], flush=True)
            time.sleep(15)
            continue
        gr = {"success": True, "results": g, "engine": "gdelt",
              "_tag": tag, "_query": kw, "_domains": domains, "_days": LOOKBACK_DAYS}
        with io.open(os.path.join(OUT_DIR, "gdelt_news_%s.json" % tag),
                     "w", encoding="utf-8") as fh:
            json.dump(gr, fh, ensure_ascii=False, indent=1)
        gdelt_total += len(g)
        print("   ->", len(g), "results", flush=True)
        time.sleep(15)



# Recount from disk so a partially-skipped pass still reports the true total.
gdelt_total = 0
for fn in glob.glob(os.path.join(OUT_DIR, "gdelt_news_*.json")):
    try:
        gdelt_total += len(json.load(io.open(fn, encoding="utf-8")).get("results", []))
    except Exception:
        pass
print("total queries:", len(records),
      "| total items:", sum(len(x.get("results", [])) for x in records),
      "| gdelt items:", gdelt_total,
      "| quota_dead:", quota_dead)
