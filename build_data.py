# -*- coding: utf-8 -*-
"""Aggregate raw collection into data.js for the China Eyes monitor.

Inputs  : data/raw/tavily_news_*.json  (media reporting)
          data/raw/firecrawl_*.json    (LinkedIn / X / Facebook / Instagram)
          data/zh_cache.json           (English -> Chinese, agent-maintained)
Output  : data.js    ->  window.SITE_DATA = {...}
          data/queue.json            (strings still needing Chinese, for the agent step)

Design notes
------------
* Window is WINDOW_DAYS (5) shown, FETCH_DAYS (3) fetched, enforced HERE and not only at fetch time, so a stale
  search index cannot widen it.
* Every item gets a priority tier: 1 = China (Chinese people / companies /
  projects), 2 = United States activity, 3 = other external actors. Items are
  ordered by tier then recency — the reading order the reader asked for.
* Translation: content is English. Chinese comes from data/zh_cache.json, which the
  daily agent step fills. Anything not yet cached is emitted to data/queue.json and
  shipped with the English text plus a "待译" marker, so a partial translation pass
  degrades visibly rather than silently.
* Topic/country tagging uses word-boundary regex, not substring tests, so "mine"
  cannot fire inside "determined". "Niger State"/"Niger Delta" are Nigerian
  toponyms and are excluded from Niger (country) tagging.
* Only publicly published professional identity and contact channels are kept.
  No private phone numbers, private emails, or inferred personal data.
"""
import glob
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(ROOT, "data", "raw")
CACHE_PATH = os.path.join(ROOT, "data", "zh_cache.json")
QUEUE_PATH = os.path.join(ROOT, "data", "queue.json")
POOL_PATH = os.path.join(ROOT, "data", "pool.json")
CST = timezone(timedelta(hours=8))

# Two different clocks, deliberately:
#   FETCH_DAYS  — what the collectors reach for on each run (fetch_news.py and
#                 fetch_social.py use their own 3-day setting). Only fresh copy
#                 enters the pipeline.
#   WINDOW_DAYS — how long a *processed* item stays on the page. The reader
#                 (2026-10-20) asked for ≤ 3 days between publication and
#                 collection, so the display window is the same: anything past
#                 WINDOW_DAYS is dropped before it reaches the page. The pool
#                     carries items across runs but ages them at this same bound,
#                     so a stale piece presented as fresh is impossible.
# The bridge between them is data/pool.json — items are carried across runs and
# aged out at WINDOW_DAYS. Without the pool the page would empty out every run.
FETCH_DAYS = 3
WINDOW_DAYS = 3
# Two narrow exceptions to WINDOW_DAYS, both requested by name (2026-10-22).
# The Niger oil complex and China-interest risk events are RARE — the whole
# Niger oil beat produced two usable items in the 3-day window, and the fuel
# shortage cluster (export ban, queues in Niamey) was 10-18, four days back.
# A strict 3-day window therefore showed an empty beat most days, which is what
# the reader was seeing. These assets and events keep a longer history; every
# card still carries its real date, so nothing is passed off as current.
OIL_DAYS = 21        # Niger oil complex: CNPC / SORAZ / Agadem / pipeline / fuel

# Some tier-4 items bypass the cap entirely.
#
# JNIM / EIGS / MPLJ / ISWAP / Boko Haram / Lakurawa stories have no China, US or
# government actor, so they are tier 4 — and the cap threw them away first, which
# is why the page showed essentially no terror coverage even though the fetch had
# it.
#
# The Niger oil complex is in the same position and the reader asked for it by
# name. "Oil well blowout in Niger still not under control" names no government
# and no Chinese firm in a way the tier test recognises, so it was capped out —
# 428 tier-4 items were being cut to 30, and the well blowout, the 45-day
# repatriation order and the SORAZ story were all inside that cut. Anything
# touching the Chinese oil assets (CNPC / SORAZ / Agadem / Zinder / the
# Niger–Benin pipeline) is therefore never capped, and neither is any
# China-interest risk event.
NIGER_OIL_RX = re.compile(
    r"\bcnpc\b|\bsoraz\b|\bagadem\b|\bzinder\b|"
    r"\bniger[-\s]benin\b|\bpetrolin\b|\bsonidep\b|"
    r"oil\s+well|\bblowout\b|\bwell\s+(?:eruption|blow-?out)\b|"
    # The fuel-shortage cluster. Every Niger oil story in the 2026-10-18 window
    # was a FUEL story, not an asset story: the export ban, the queues in
    # Niamey, the refinery running short. None of those headlines contain
    # "CNPC" or "SORAZ", so they read as generic regional copy and were capped.
    r"\bfuel\s+(?:shortage|queues?|crisis|scarcity|supply)\b|"
    r"\bpetrol\b|\bdiesel\b|\bgasoline\b|\bgasoil\b|"
    r"\b(?:export|exports)\s+ban\b|\bbans?\s+(?:petrol|diesel|fuel)\b|"
    r"\brefiner(?:y|ies)\b|\bcrude\b|"
    r"中石油|中石化|津德尔|索拉兹|阿加德姆|尼贝管道|尼日尔-贝宁|"
    r"炼油厂|输油管道|油井|井喷|燃油|汽油|柴油|燃料短缺|石油部|油气", re.I)
RISK_DAYS = 14       # Chinese nationals kidnapped, arrested, killed, fined

# Ceiling for the unfoldable article body, in characters. 4000 cut long-form
# stories roughly in half, so the 展开全文 panel ended mid-article; 12000 holds a
# full wire story or feature. Only the truly enormous pieces are still trimmed,
# and that trim is labelled in the panel.
BODY_LIMIT = int(os.environ.get("BODY_LIMIT", "12000"))
# Summary length. The reader wants the summary readable in full on the card
# without unfolding, so it stays at three sentences / ~520 chars rather than
# becoming a second article — but it must never end mid-sentence (see
# _cut_at_sentence).
SUMMARY_CHARS = int(os.environ.get("SUMMARY_CHARS", "520"))

# Placeholder summaries/bodies written by older builds. They must be recognised
# everywhere — when carrying items over from the pool, when rebuilding a body, and
# in the final sanitation pass — so the regex is defined up here rather than next
# to the pass that first needed it.
_PLACEHOLDER_RX = re.compile(
    r"仅返回导航摘要|未返回可读摘要|该来源未返回|原文页面仅返回", re.I)


def _fallback_summary(item):
    """A readable Chinese summary for an item whose text we could not obtain.

    The card must still tell the reader what the story is about. Using the
    headline is honest (it is the publisher's own summary of the story), keeps the
    page fully Chinese, and avoids both failure modes seen before: a fixed
    "仅返回导航摘要" string that says nothing, and dropping the item altogether,
    which cost 34 China-related cards.
    """
    t = (item.get("title_zh") or item.get("title") or "").strip()
    src = (item.get("source") or item.get("domain") or "").strip()
    who = ("据 %s 报道：" % src) if src else ""
    if t:
        return who + t.rstrip(".") + "。该来源未开放正文与摘要，点击下方原文可查看完整内容。"
    return "该来源未开放正文与摘要，点击下方原文可查看完整内容。"

# ─────────────── outlet registry (contact = publicly published page) ───────────
OUTLETS = {
    "reuters.com": ("路透社", "af", "https://www.reuters.com/world/africa/"),
    "punchng.com": ("Punch（尼日利亚）", "ng", "https://punchng.com/"),
    "premiumtimesng.com": ("Premium Times（尼日利亚）", "ng", "https://www.premiumtimesng.com/contact-us"),
    "vanguardng.com": ("Vanguard（尼日利亚）", "ng", "https://www.vanguardng.com/contact-us/"),
    "saharareporters.com": ("Sahara Reporters（尼日利亚）", "ng", "https://saharareporters.com/contact"),
    "businessday.ng": ("BusinessDay（尼日利亚）", "ng", "https://www.businessday.ng/contact-us/"),
    "thecable.ng": ("TheCable（尼日利亚）", "ng", "https://www.thecable.ng/"),
    "myjoyonline.com": ("MyJoyOnline（加纳）", "gh", "https://www.myjoyonline.com/contact-us/"),
    "citinewsroom.com": ("Citi Newsroom（加纳）", "gh", "https://citinewsroom.com/"),
    "graphic.com.gh": ("Graphic Online（加纳）", "gh", "https://www.graphic.com.gh/"),
    "mining.com": ("Mining.com（全球矿业）", "af", "https://www.mining.com/"),
    "mining-technology.com": ("Mining Technology", "af", "https://www.mining-technology.com/"),
    "mining-journal.com": ("Mining Journal", "af", "https://www.mining-journal.com/"),
    "im-mining.com": ("International Mining", "af", "https://im-mining.com/"),
    "africanews.com": ("Africanews", "af", "https://www.africanews.com/"),
    "bbc.com": ("BBC News Africa", "af", "https://www.bbc.com/news/africa"),
    "allafrica.com": ("AllAfrica", "af", "https://allafrica.com/"),
    "theafricareport.com": ("The Africa Report", "af", "https://www.theafricareport.com/"),
    # expanded roster: the tight list of nine West African outlets starved the
    # in-window result set, and francophone Niger/Mali need French-language sources
    "thenationonlineng.net": ("The Nation（尼日利亚）", "ng", "https://thenationonlineng.net/"),
    "dailytrust.com": ("Daily Trust（尼日利亚）", "ng", "https://dailytrust.com/"),
    "guardian.ng": ("The Guardian Nigeria（尼日利亚）", "ng", "https://guardian.ng/"),
    "thisdaylive.com": ("ThisDay（尼日利亚）", "ng", "https://www.thisdaylive.com/"),
    "leadership.ng": ("Leadership（尼日利亚）", "ng", "https://leadership.ng/"),
    "tribuneonlineng.com": ("Nigerian Tribune（尼日利亚）", "ng", "https://tribuneonlineng.com/"),
    "sunnewsonline.com": ("The Sun（尼日利亚）", "ng", "https://www.sunnewsonline.com/"),
    "premiumtimesng.com": ("Premium Times（尼日利亚）", "ng", "https://www.premiumtimesng.com/contact-us"),
    "ghanaweb.com": ("GhanaWeb（加纳）", "gh", "https://www.ghanaweb.com/"),
    "3news.com": ("3News（加纳）", "gh", "https://3news.com/"),
    "adomonline.com": ("Adom Online（加纳）", "gh", "https://www.adomonline.com/"),
    "peacefmonline.com": ("Peace FM Online（加纳）", "gh", "https://www.peacefmonline.com/"),
    "actuniger.com": ("ActuNiger（尼日尔·法语）", "ne", "https://www.actuniger.com/"),
    "lesahel.org": ("Le Sahel（尼日尔官方报·法语）", "ne", "https://www.lesahel.org/"),
    "airinfo.org": ("Air Info（尼日尔·法语）", "ne", "https://www.airinfo.org/"),
    "malijet.com": ("Malijet（马里·法语）", "ml", "https://malijet.com/"),
    "maliweb.net": ("Maliweb（马里·法语）", "ml", "https://www.maliweb.net/"),
    "aouaga.com": ("Aouaga（布基纳法索·法语）", "wa", "https://www.aouaga.com/"),
    "ecofinagency.com": ("Ecofin Agency（非洲法语区财经）", "af", "https://www.ecofinagency.com/"),
}

# West African outlets, used as the geographic anchor of last resort: a
# China-related story in one of these is West-Africa-relevant by construction.
WA_DOMAINS = {"premiumtimesng.com", "punchng.com", "vanguardng.com",
              "saharareporters.com", "businessday.ng", "thecable.ng",
              "myjoyonline.com", "graphic.com.gh", "citinewsroom.com",
              "thenationonlineng.net", "dailytrust.com", "guardian.ng",
              "thisdaylive.com", "leadership.ng", "tribuneonlineng.com",
              "sunnewsonline.com", "ghanaweb.com", "3news.com", "adomonline.com",
              "peacefmonline.com", "actuniger.com", "lesahel.org", "airinfo.org",
              "malijet.com", "maliweb.net", "aouaga.com"}

VERIFIED_ACCOUNTS = {
    "NigeriaGov": ("尼日利亚联邦政府", "ng", "联邦政府官方账号"),
    "FMINONigeria": ("尼日利亚联邦信息与国家导向部", "ng", "联邦部委官方账号"),
    "NGRPresident": ("尼日利亚总统府", "ng", "总统府官方账号"),
    "HQNigerianArmy": ("尼日利亚陆军总部", "ng", "军方官方账号"),
    "PoliceNG": ("尼日利亚警察部队", "ng", "执法机构官方账号"),
    "ecowas_cedeao": ("西非国家经济共同体（ECOWAS）", "wa", "区域组织官方账号"),
    "MoFAGA_Ghana": ("加纳地方政府与农村发展部", "gh", "政府部委官方账号"),
    "GhanaMFA": ("加纳外交部", "gh", "政府部委官方账号"),
}

# ─────────────── priority tiers: China / US / other ───────────────
# NOTE: compiled with re.X, where literal spaces in the pattern are IGNORED.
# Every multi-word alternative therefore uses \s+ rather than a literal space —
# writing "\bunited states\b" silently becomes "\bunitedstates\b" and never matches.
CN_RX = re.compile(r"""(
 \bchina\b|\bchinese\b|\bbeijing\b|\bprc\b|\bsino[\s-]|\bcnpc\b|\bcnooc\b|\bsinopec\b|
 \bsinochem\b|\bchinalco\b|\bcmoc\b|\bzijin\b|\bhuawei\b|\bzte\b|\bchec\b|\bccecc\b|
 \bcrcc\b|\bgezhouba\b|\bwanbao\b|\bhuayou\b|\bcatl\b|\bbyd\b|\bshandong\b|\bcnmc\b|
 \bchina\s+(?:harbour|railway|national|state|nonferrous|gezhouba|expo)\b|
 \bchinese[-\s](?:owned|company|companies|firm|firms|miner|miners|investment|national)|
 \bchinese\s+(?:worker|workers|embassy|ambassador|government|authorit\w*)\b|
 \bchinese-?(?:built|funded|backed)\b
)""", re.I | re.X)

# NOTE: the country abbreviation must be matched CASE-SENSITIVELY and separately
# from the word list. `\bu\.?s\.?\b` under re.I also matches the English pronoun
# "us", so a line like "it lets us refine our own products" made a purely local
# Nigerian story look US-related and pushed it into the US tier — which is how
# Dangote-refinery and Ghana-GoldBod items ended up ranked as 涉美.
# The `(?!\s*\$)` guard keeps "US$1 billion" (a currency amount) out of it: that
# is how "GoldBod terminates US$1 billion Goldstream deal in Ghana" was read as a
# US story when it is purely a Ghanaian transaction.
US_ABBR = re.compile(r"\bU\.S\.(?:A\.)?|\bUSA\b|\bUS\b(?!\s*\$)|(?<![A-Z])U\.S\b")
US_RX = re.compile(r"""(
 \bamericans?\b|\bamerican\b|\bpentagon\b|\btrump\b|\bbiden\b|
 \busaid\b|\bafricom\b|\bexxon\b|\bchevron\b|\boccidental\b|
 \bunited\s+states\b|\bwashington\b|\bstate\s+department\b|\bwhite\s+house\b|
 \bwall\s+street\b|\bsilicon\s+valley\b|\bmillennium\s+challenge\b|
 \bu\.?s\.?\s+(?:aid|troops|military|sanctions|embassy|dollar|firms|companies)\b
)""", re.I | re.X)


def us_hits(text):
    """US mentions in a string, counting the case-sensitive abbreviation too."""
    text = text or ""
    return len(US_RX.findall(text)) + len(US_ABBR.findall(text))


def has_us(text):
    text = text or ""
    return bool(US_RX.search(text) or US_ABBR.search(text))

THIRD = [
    ("法国", r"\bfrance\b|\bfrench\b|\bparis\b|\belysee\b"),
    ("俄罗斯", r"\brussia\b|\brussian\b|\bmoscow\b|\bkremlin\b|\bwagner\b|\bafrica corps\b"),
    ("土耳其", r"\bturkey\b|\bturkish\b|\bturkiye\b|\bankara\b"),
    ("阿联酋", r"\buae\b|\bemirates?\b|\babu dhabi\b|\bdubai\b"),
    ("沙特", r"\bsaudi\b|\briyadh\b"),
    ("印度", r"\bindia\b|\bindian\b|\bdelhi\b"),
    ("日本", r"\bjapan\b|\bjapanese\b|\btokyo\b"),
    ("韩国", r"\bsouth korea\b|\bkorean\b|\bseoul\b"),
    ("英国", r"\bbritain\b|\bbritish\b|\buk\b|\blondon\b"),
    ("德国", r"\bgermany\b|\bgerman\b|\bberlin\b"),
    ("以色列", r"\bisrael\b|\bisraeli\b"),
    ("卡塔尔", r"\bqatar\b|\bdoha\b"),
    ("摩洛哥", r"\bmorocco\b|\bmoroccan\b|\brabat\b"),
    ("阿尔及利亚", r"\balgeria\b|\balgerian\b|\balgiers\b"),
    ("伊朗", r"\biran\b|\biranian\b|\btehran\b"),
    ("巴西", r"\bbrazil\b|\bbrazilian\b"),
    ("加拿大", r"\bcanada\b|\bcanadian\b|\bottawa\b"),
    ("澳大利亚", r"\baustralia\b|\baustralian\b"),
    ("欧盟", r"\beuropean union\b|\beu\b|\bbrussels\b|\beuropean commission\b"),
    ("意大利", r"\bitaly\b|\bitalian\b|\brome\b"),
    ("西班牙", r"\bspain\b|\bspanish\b|\bmadrid\b"),
    ("挪威", r"\bnorway\b|\bnorwegian\b"),
    ("荷兰", r"\bnetherlands\b|\bdutch\b"),
    ("南非", r"\bsouth africa\b|\bsouth african\b"),
    ("埃及", r"\begypt\b|\begyptian\b|\bcairo\b"),
    ("马来西亚", r"\bmalaysia\b|\bmalaysian\b"),
    ("印尼", r"\bindonesia\b|\bindonesian\b"),
]
THIRD_RX = [(nm, re.compile(rx, re.I)) for nm, rx in THIRD]

TIER_LABEL = {1: "涉华动态", 2: "涉美动态", 3: "当地政府动态", 4: "其他报道"}
TIER_DESC = {
    1: "报道涉及中国人员、中国企业或中国项目（覆盖非洲四国 + 中国周边共 12 个监测国）",
    2: "报道涉及美国在监测区域的活动（政府、军方、企业或援助机构）",
    3: "尼日利亚、尼日尔、马里、加纳等当事国政府自身发布的动态，涵盖政治、经济、能源、矿产与战略通道（管道、铁路、公路、港口）等议题",
    4: "上述三类以外的公开报道（非中美亦非当事国政府），含 MPLJ/FPL/JNIM/EIGS/ISIS/Lakurawa 等恐怖组织的活动报道",
}

# Actors that ARE a government of one of the four countries. Used to decide
# tier 3 ("政府发布的动态"): either the publisher is an official government
# body, or the item reports a government decision/statement.
GOV_ACTOR_RX = re.compile(r"""(
 \bfederal\s+government\b|\bfg\b|\bstate\s+government\b|\bministry\s+of\b|
 \bminister\s+of\b|\bthe\s+government\s+(?:said|announced|approved|has|will|plans|has\s+approved)\b|
 \bgovernment\s+spokesperson\b|\bpresidency\b|\bstate\s+house\b|\basa\s+rock\b|
 \bnational\s+assembly\b|\bsenate\b|\bhouse\s+of\s+representatives\b|
 \bcentral\s+bank\s+of\s+nigeria\b|\bcbn\b|\bnnpc\b|\bncdmb\b|\bonsa\b|
 \bnigeria\s+at\s+\d{2}\b|\bfederal\s+executive\s+council\b|\bfec\b|
 \bniger\s+government\b|\bcouncil\s+of\s+ministers\b|\bcnsp\b|\bjunta\s+(?:said|has|approved)\b|
 \bgovernment\s+of\s+ghana\b|\bministry\s+of\s+lands\b|\bminerals\s+commission\b|
 \bghana\s+gold\s+board\b|\bgoldbod\b|\bministry\s+of\s+energy\b|
 \bgovernment\s+of\s+mali\b|\btransitional\s+government\b|
 \btransmits?\s+bills?\b|\bsigned\s+into\s+law\b|\bexecutive\s+order\b|
 \bcabinet\s+(?:approved|meeting)\b|\bbudget\s+(?:presented|approved|assented)\b|
 \bapproved\s+the\s+sum\s+of\b|\bhas\s+approved\b|\bpresident\s+\w+\s+said\b|
 \bthe\s+minister\s+said\b|\bminister\s+said\b
)""", re.I | re.X)

# Official government publishers, by domain and by social handle.
GOV_DOMAINS = {
    "nigeria.gov.ng", "statehouse.gov.ng", "fmino.gov.ng", "mines.gov.ng",
    "nnpcgroup.com", "onsa.gov.ng", "nass.gov.ng", "cenbank.org",
    "ghana.gov.gh", "mesti.gov.gh", "lands.gov.gh", "energymin.gov.gh",
    "gouvernement.ne", "gouvernement.ml", "presidence.ne", "primature.gov.ml",
}
GOV_HANDLES = {
    "nigeriaGov", "ngrpresident", "fminonigeria", "solid_min_dev", "official_onsa",
    "nnpclimited", "hqnigerianarmy", "nigeriaarmyinfo", "policeng", "nassnigeria",
    "ghana_gov", "ghanagoldbod", "ghanagoldbodofficial", "mofaga_ghana", "ghanamfa",
    "cenbankng", "nitda", "ecowas_cedeao",
}

TOPIC_CN = {"pol": "政治", "eco": "经济", "sec": "安全", "ene": "能源", "min": "矿产",
            "cor": "战略通道"}
# topics that count as tier-3 subject matter
GOV_TOPICS = {"pol", "eco", "ene", "min", "cor"}


def is_gov_actor(title, summary):
    """True when the item reports a government decision or statement."""
    return bool(GOV_ACTOR_RX.search(title or "") or GOV_ACTOR_RX.search(summary or ""))


def actor_tier(title, summary, min_hits=3, is_gov_publisher=False, topics=()):
    """Priority tier, decided on headline-level evidence.

    A single passing mention of "China" or "the United States" inside a summary
    (often a related-articles snippet picked up with the body) is not a
    China-related or US-related story. So a marker qualifies only when it appears
    in the TITLE, or at least `min_hits` times in the summary — which indicates
    the summary genuinely discusses that actor rather than name-dropping it.
    Social snippets are short, so callers pass a lower threshold there.
    """
    t = title or ""
    s = summary or ""
    if CN_RX.search(t) or len(CN_RX.findall(s)) >= min_hits:
        return 1, "中国", bool(has_us(t) or us_hits(s) >= min_hits)
    if has_us(t) or us_hits(s) >= min_hits:
        return 2, "美国", False
    # Tier 3: the four governments' own dynamics.
    if is_gov_publisher or (is_gov_actor(t, s) and (set(topics) & GOV_TOPICS)):
        return 3, "当事国政府", False
    return 4, "第三方", False


def subject_hits(title, summary, min_hits=3):
    """(china, us) — non-exclusive.

    `actor_tier` is single-valued by design (one priority band per item), and the
    page used to decide module membership from it too. That dropped stories from
    the modules they belong to: "Trump says he may meet Xi on North Korea" is
    tier 1, so it never appeared in 涉美 even though it is squarely a US move;
    "US sanctions Chinese firms over Niger" likewise. The two axes are genuinely
    independent, so they are now reported separately and `inModule()` uses these
    flags while `tier` keeps its ranking/filter role.
    """
    t = title or ""
    s = summary or ""
    cn = bool(CN_RX.search(t)) or len(CN_RX.findall(s)) >= min_hits
    us = has_us(t) or us_hits(s) >= min_hits
    return cn, us


# ─────────────── designated terrorist organisations ───────────────
# The reader asked specifically for MPLJ / FPL / JNIM / EIGS / ISIS / Lakurawa
# activity. Those items were almost entirely MISSING from the page, for three
# compounding reasons once fixed here:
#   1. VOLUME CAP — such a story has no China/US/government actor, so it is tier 4
#      and the tier-4 cap (30) discarded it ("volume cap: dropped 62 oldest
#      tier-4 items"). Terror coverage was the first thing thrown away.
#   2. GEOGRAPHY — "Rival Sahel jihadists clash" names no single country, so it
#      was tagged `wa` and appeared in NO module (非洲四国 needs ng/ne/ml/gh).
#   3. QUERY — the old angle "MPLJ FPL jihadist Sahel attack" matched Premier
#      League FANTASY FOOTBALL for "FPL". Replaced with per-group short queries.
# `FPL` in particular must be disambiguated: in a search index it is the fantasy
# game far more often than the Front Patriotique de Libération.
TERROR_RX = re.compile(
    r"\bjnim\b|\bjama'?at\s+nusrat|\bmplj\b|\beigs\b|\bisgs\b|\bisis\b|\bisil\b|"
    r"\biswap\b|\blakurawa\b|\bboko\s+haram\b|\bansaru\b|\biscap\b|\bisc-sp\b|"
    r"\bdan\s+na\s+ambassagou\b|\bmacina\b|\bkatiba\b|\bserma\b|"
    r"\bfront\s+patriotique|\bmouvement\s+patriotique|"
    r"支持伊斯兰与穆斯林组织|伊斯兰国西非省|伊斯兰国|博科圣地|恐怖组织|圣战", re.I)
# Weak signal: an explicit jihad/terror word, accepted only when the copy also
# names a Sahel theatre. Without the region guard a US political story calling
# someone a "jihadist" would qualify.
JIHAD_RX = re.compile(r"\bjihad\w*|\bdjihad\w*|\bterror(?:ist|ism)?\b|\binsurgen\w+", re.I)
SAHEL_RX = re.compile(
    r"\bsahel\b|\bniger\b|\bnigeria\b|\bmali\b|\bburkina\b|\bchad\b|\bcameroon\b|"
    r"\btillaberi\b|\bagadez\b|\bmopti\b|\bsokoto\b|\bborno\b|\byobe\b|\bzamfara\b|"
    r"\bkatsina\b|\blake\s+chad\b|\bliptako\b|\bmacina\b|\btimbuktu\b|\bgao\b",
    re.I)
# "FPL" is only the Nigerien Front Patriotique de Libération when the copy also
# talks about Niger / armed activity. Otherwise it is Fantasy Premier League.
FPL_GUARD_RX = re.compile(
    r"\bfpl\b(?!\s*(?:player|price|gameweek|deadline|chip|wildcard|transfer\s+news|"
    r"lessons|review|tips|team|league|fixtures|scout|draft))", re.I)
FPL_CONTEXT_RX = re.compile(
    r"\bniger\b|\bsahel\b|\barmed\b|\bmilitant|\bjihad|\bfront\b|\blib(?:é|e)ration\b|"
    r"\brebels?\b|\battack", re.I)
FOOTBALL_RX = re.compile(
    r"\bpremier\s+league\b|\bfootball\b|\bsoccer\b|\bgameweek\b|\bfpl\s+(?:player|price|"
    r"lessons|review|tips)|\bfantasy\b", re.I)


def terror_hit(title, summary=""):
    """True when the item is about one of the designated groups, or about
    jihadist/insurgent activity inside a Sahel theatre.

    The group has to be named in the HEADLINE — a passing mention deep in an
    article about something else is not a terror story. "FPL" and the spelled-out
    French names get extra guards because "FPL" is usually the Fantasy Premier
    League and "front" alone appears in ordinary political copy.
    """
    t = title or ""
    if not t:
        return False
    if FOOTBALL_RX.search(t) and not re.search(r"\bmplj\b|\bjnim\b|\beigs\b", t, re.I):
        return False
    if TERROR_RX.search(t):
        return True
    if FPL_GUARD_RX.search(t) and FPL_CONTEXT_RX.search(t + " " + (summary or "")):
        return True
    # Generic jihad/insurgency prose: only inside the Sahel theatre.
    return bool(JIHAD_RX.search(t) and SAHEL_RX.search(t))


# ─────────────── China-enterprise risk events ───────────────
# The reader asked (2026-10-21) for five specific classes of story about Chinese
# interests in the four countries, and for them to be findable in one tap:
#   中国人被绑架 / 被杀 · 中资企业被罚款 · 被当地政府约谈 · 中国项目被袭击
# A generic "涉华" tag is not enough — those stories are a small minority of the
# ~90 China items on the page, and on a phone they scroll away.
#
# The subject test is not just the word "China": "Gunmen abduct two Chinese
# miners" names China only as an adjective, and "中资矿企被罚" does not say 中国
# at all. So the subject is China/Chinese OR a named Chinese operator/project,
# and the predicate is one of the five risk verbs.
CN_SUBJECT_RX = re.compile(
    r"(?<![A-Za-z])(?:chinese|china|prc|cnpc|sinopec|sinomach|crcc|crec|cgg|"
    r"chinatex|huawei|ztec|sinohydro|cnoc|citcc|china\s+national|"
    r"chinese[- ]owned|chinese[- ]run|soraz|petrolin|agadem)(?![A-Za-z])|"
    r"中国|中方|中资|中企|华商|华人|华侨|中石油|中石化|中铁|中交|中电建|"
    r"中国石油|中国石化|葛洲坝|华为|中兴|津德尔|索拉兹|阿加德姆|尼贝管道", re.I)

# Harm to a PERSON, not to a state. "人身安全" is about Chinese nationals being
# kidnapped, killed or arrested; a headline that says 中国军队 or 中国企业 is
# talking about an institution, and pairing it with 死亡 produced a false
# positive on "金正恩死亡传闻…9000名中国军队".
CN_PERSON_RX = re.compile(
    r"(?<![A-Za-z])chinese(?:\s+|[-])(?:nationals?|citizens?|workers?|miners?|"
    r"traders?|businessm[ae]n|engineers?|expatriates?|crew|sailors?|students?|"
    r"tourists?|doctors?|nurses?|drivers?|technicians?|residents?|national)\b|"
    r"\bchinese\s+(?:man|woman|men|women|boy|girl|people)\b|"
    # "18 including 8 Chinese nabbed" — a count plus the bare demonym is the
    # commonest way a wire desk writes it, and it names people, not a state.
    r"\b\d+\s+(?:more\s+)?chinese\b(?!\s+(?:army|troops|military|firms?|companies))|"
    # …but "9000名中国军队" is an institution, not nine thousand people in harm's
    # way. A following institutional noun disqualifies the match.
    r"\b\d+\s*名\s*中国(?!军队|军方|企业|公司|政府|大使馆|海军|空军|舰|船队)|"
    r"中国(?:公民|工人|矿工|商人|船员|渔民|留学生|工程师|医生|护士|司机|"
    r"游客|侨民|籍|人)|中方(?:人员|人员)|华侨|华人|华商|华工", re.I)
RISK_KIDNAP_RX = re.compile(
    r"\bkidnap|\babduct|\bseized\b|\bmissing\b|\bkilled?\b|\bkilling|\bslain\b|"
    r"\bmurder|\bshot\s+dead|\bgunmen\b|\bexecuted\b|\binjured\b|\bwounded\b|"
    r"\bdeaths?\b|\bdied\b|\bdead\b|\bfatal|\bstoned\b|\blynched\b|"
    # Arrest and detention are risks to the reader's people in the same way a
    # kidnapping is. "18 including 8 Chinese nabbed for illegal mining" was on
    # the wire for three days and never reached the page, because the old
    # predicate only knew about violence.
    r"\barrest(?:ed|s)?\b|\bnabbed\b|\bdetain(?:ed|s)?\b|\bjailed\b|"
    r"\bheld\s+(?:by|in)\b|\bcustody\b|\bdeported\b|\bextradit\w*|"
    r"绑架|劫持|被绑|遇害|被杀|杀害|失踪|身亡|遇难|枪杀|击毙|死亡|丧生|"
    r"受伤|伤员|伤亡|被捕|逮捕|拘留|拘押|被扣|驱逐|遣返|获刑", re.I)
# Commercial friction that puts Chinese traders or firms at risk. Kept separate
# from the violence predicate: "Nigerian traders' dispute with Chinese vendors"
# is a real risk story for anyone with capital on the ground, and it never
# touches a violence word.
RISK_DISPUTE_RX = re.compile(
    r"\bdispute[sd]?\b|\bprotest(?:s|ed|ing)?\b|\bbacklash\b|\brow\b|"
    r"\btension|\bclash(?:es|ed)?\b|\bboycott|\bshut\s+down\b|"
    r"\bracket(?:s|eering)?\b|\bblackmail|\bextortion|"
    r"纠纷|争议|抗议|示威|抵制|冲突|矛盾|摩擦|抵制|勒索|索贿", re.I)
RISK_FINE_RX = re.compile(
    r"\bfined?\b|\bpenalt(?:y|ies)\b|\bsanction|\bprosecut|\bconvicted\b|"
    r"\bseized\s+(?:assets|goods|cargo)|\bforfeit|\bdefault(?:ed)?\s+on\b|"
    r"\btax\s+(?:bill|demand)|\bcustoms\s+(?:duty|seizure)|"
    r"\bjail(?:s|ed)?\b|\bsentenc(?:ed|ing)\b|\bcourt\s+(?:orders|rules)\b|"
    r"罚款|罚金|处罚|被罚|查封|没收|追缴|补税|起诉|判刑|定罪|入狱|获刑", re.I)
RISK_SUMMON_RX = re.compile(
    r"\bsummon|\bquizzed\b|\bquestioned\s+by\b|\binvited\s+by\s+(?:the\s+)?"
    r"(?:minister|ministry|government|committee|assembly|senate|regulator|"
    r"commission|agency|parliament)|\border(?:ed)?\s+to\s+(?:appear|explain)|"
    r"\bappeared\s+before\s+(?:the\s+)?(?:committee|assembly|panel|house)|"
    r"\bunder\s+investigation|\bprobe[ds]?\b|\bregulator\b|\breview\b|"
    r"\brenegotiat|\bterminat(?:ed|ion|es)\b|\bcancel(?:led|s)?\b|"
    r"\brevok(?:ed|e)\b|\bsuspend(?:ed|s)?\b|\baudit(?:ed|s)?\b|\bseiz(?:ed|ure)\b|"
    r"\bshut\s*(?:down|s)?\b|\bhalt(?:ed|s)?\b|\bsuspend\w*\b|\bstopp?ed\b|"
    r"\bblocked\b|\bbanned\b|\bexpel(?:led|s)?\b|\bdeport\w*\b|"
    r"约谈|问责|传唤|调查|质询|被要求说明|立案|审查|重新谈判|重谈|终止|取消|"
    r"吊销|撤销|暂停|查封|接管|停产|停工|关停|叫停|驱逐|遣返", re.I)
RISK_ATTACK_RX = re.compile(
    r"\battack(?:ed|s)?\b|\braid(?:ed|s)?\b|\bbomb(?:ed|ing)?\b|\bambush|"
    r"\bsabotag|\bvandal|\bloot(?:ed|ing)?\b|\bburn(?:ed|t|ing)?\b|"
    r"\bstormed\b|\boverrun\b|\bhostage|"
    r"袭击|遇袭|被袭|轰炸|爆炸|伏击|破坏|纵火|焚毁|劫掠|抢劫|冲击|攻击(?!型|无人机|机)", re.I)
# A project / asset owned by Chinese interests.
CN_PROJECT_RX = re.compile(
    r"\b(?:refinery|pipeline|mine|mining\s+site|smelter|plant|dam|rail(?:way)?|"
    r"port|road|bridge|power\s+(?:plant|station)|substation|construction\s+site|"
    r"oilfield|oil\s+field|rig|quarry|factory|project|site|camps?)\b|"
    r"炼油厂|管道|输油|矿山|矿区|冶炼|工厂|电站|铁路|公路|港口|桥梁|"
    r"工地|施工现场|项目|营地", re.I)


def _harm_near_cn(t, window=40):
    """Does a harm word apply to a Chinese PERSON in this headline?

    Three conditions, each closing a real false positive:
      · the subject must be a person, not an institution — "9000名中国军队" is
        not a casualty and pairing it with 死亡 produced "金正恩死亡传闻" as a
        Chinese death story;
      · a harm word must be present;
      · and the two must sit within `window` characters of each other, because
        a headline about one thing often mentions the other in passing.
    """
    t = t or ""
    m = CN_PERSON_RX.search(t)
    if not m:
        return False
    lo, hi = max(0, m.start() - window), min(len(t), m.end() + window)
    return bool(RISK_KIDNAP_RX.search(t[lo:hi]))


def cn_risk(title, summary="", country=None):
    """(is_risk, kind) for a Chinese-interest risk event.

    Rewritten 2026-10-22. The reader was right that the page was too clean:
    a court jailing a Chinese national, eight Chinese arrested in a mining
    raid, traders organising against Chinese vendors — all on the wire, none
    on the page. Four things were wrong and all four are fixed here.

    1. SUBJECT. China/Chinese, or a named Chinese operator (CNPC, SORAZ), in
       the HEADLINE. A summary may also supply it, but only its first 200
       characters, so a related-links rail cannot turn a local crime story
       into a China story.
    2. PREDICATE. Violence AND arrest AND detention AND commercial friction.
       The old set only knew about violence, so "18 including 8 Chinese nabbed
       for illegal mining" and "traders dispute with Chinese vendors" both
       scored zero.
    3. THEATRE, GRADED. Personal harm (kidnap, death, arrest, attack on a
       project) counts in the four countries AND the eight periphery countries
       — Chinese nationals are at risk in Myanmar and the Philippines too, and
       the reader asked about both. Official-friction categories (fined,
       summoned) stay limited to the four, because those fire on diplomatic
       copy ("US sanctions five Chinese firms", "Manila summons China's
       ambassador") that has nothing to do with operations on the ground.
    4. KINDS are reported so the card can say what happened.
    """
    t = title or ""
    _subj = CN_SUBJECT_RX.search(t)
    _subj_weak = (not _subj) and bool(CN_SUBJECT_RX.search((summary or "")[:200]))
    if not t or (not _subj and not _subj_weak):
        return False, ""

    AFRICA4 = {"ng", "ne", "ml", "gh"}
    PERIPH = {"kr", "ph", "mm", "ru", "in", "np", "vn", "la"}
    if country is not None:
        cs = set(country if isinstance(country, (list, tuple, set)) else [country])
        in4, in_per = bool(cs & AFRICA4), bool(cs & PERIPH)
    else:
        in4 = bool(re.search(r"nigeria|niger\b|mali|ghana|尼日利亚|尼日尔|马里|加纳",
                             t, re.I))
        in_per = bool(re.search(r"myanmar|philippines|thailand|vietnam|nepal|laos|"
                                r"india|russia|korea|缅甸|菲律宾|越南|尼泊尔|老挝|"
                                r"印度|俄罗斯|朝鲜|韩国", t, re.I))
    if not (in4 or in_per):
        return False, ""

    blob = t + " " + (summary or "")[:400]
    kinds = []
    # ── personal harm: allowed in the four AND the periphery ──
    # The harm must be ABOUT the Chinese subject, not merely on the same line.
    # "金正恩死亡传闻加剧…9000名中国军队" matched on 死亡 alone and was filed as
    # a Chinese casualty event. Requiring the two within 40 characters keeps
    # "两名中国矿工在夸利遭绑架" and drops that.
    if RISK_KIDNAP_RX.search(t) and _harm_near_cn(t):
        kinds.append("人身安全")
    if RISK_ATTACK_RX.search(t) and (CN_PROJECT_RX.search(blob) or RISK_KIDNAP_RX.search(t)):
        kinds.append("项目遇袭")
    # ── commercial friction: four countries only ──
    if in4 and RISK_DISPUTE_RX.search(t):
        kinds.append("经营纠纷")
    # ── official action: four countries only ──
    if in4:
        if RISK_FINE_RX.search(t):
            kinds.append("处罚")
        if RISK_SUMMON_RX.search(t):
            kinds.append("约谈调查")
    if not kinds:
        return False, ""
    return True, "、".join(kinds)


# ─────────────── topic / country vocabulary ───────────────
TOPIC_MARKERS = {
    "sec": ["security", "insecurity", "attack", "attacks", "killed", "killings", "kidnap",
            "kidnapping", "bandit", "bandits", "banditry", "jihadist", "jihadis", "jihadi",
            "insurgent", "insurgents", "terror", "terrorist", "terrorism", "military",
            "army", "soldier", "soldiers", "ambush", "massacre", "iswap", "jnim",
            "boko haram", "militant", "militants", "gunmen", "clash", "clashes", "conflict",
            "ceasefire", "defence", "defense", "troops", "violent", "violence", "extremist",
            "hostage", "coup", "coups", "security forces", "raid", "raids",
            "insurgency", "counterterrorism", "counter-terrorism",
            # Sahel / Nigeria militant groups (added 2026-10-20):
            # MPLJ = Mouvement Patriotique de Libération du Djado (Niger Tuareg)
            # FPL  = Front Patriotique de Libération (Niger armed faction)
            # JNIM = Jama'at Nusrat al-Islam wal-Muslimin (Sahel jihadist coalition)
            # EIGS = État Islamique dans le Grand Sahara (IS Sahel province)
            # ISIS = Islamic State (used here as the umbrella; sub-affiliates handled
            #        through ISWAP, EIGS, Lakurawa, etc.)
            # Lakurawa = Nigerian IS-aligned group in NW Nigeria / Sokoto / Kebbi
            "mplj", "fpl", "eigs", "isis", "isgs", "lakurawa"],
    "pol": ["election", "elections", "electoral", "president", "presidential", "parliament",
            "parliamentary", "minister", "ministry", "senate", "senator", "governor",
            "governance", "policy", "reform", "reforms", "diplomacy", "diplomatic",
            "sanction", "sanctions", "democracy", "democratic", "constitution",
            "constitutional", "supreme court", "lawmakers", "opposition", "ruling party",
            "cabinet", "junta", "transition", "transitional", "sovereignty", "embassy",
            "bill", "bills", "corruption", "protest", "protests", "impeachment", "tribunal",
            "legislation", "law", "laws", "parties", "electorate", "referendum"],
    "eco": ["economy", "economic", "economics", "inflation", "gdp", "budget", "tax", "taxes",
            "taxation", "currency", "naira", "cedi", "imf", "world bank", "trade",
            "exports", "export", "imports", "import", "investment", "investors", "bank",
            "banking", "fiscal", "debt", "deficit", "revenue", "tariff", "subsidy",
            "subsidies", "growth", "reserves", "stock exchange", "manufacturing",
            "unemployment", "poverty", "monetary", "central bank", "remittances", "cbn"],
    "ene": ["oil", "gas", "petrol", "petroleum", "refinery", "refineries", "nnpc", "pipeline",
            "power sector", "power supply", "electricity", "energy", "fuel", "diesel",
            "solar", "renewable", "electricity grid", "national grid", "dangote refinery",
            "lng", "upstream", "downstream", "crude", "opec", "tariff hike", "metering",
            "megawatt", "power plant", "hydropower", "nuclear", "uranium mine"],
    "min": ["mining", "mine", "mines", "miners", "mineral", "minerals", "gold", "lithium",
            "uranium", "cobalt", "coltan", "bauxite", "manganese", "iron ore", "concession",
            "concessions", "smelter", "exploration", "ore", "royalty", "royalties",
            "artisanal", "galamsey", "quarry", "tailings", "solid minerals", "barrick",
            "orano", "somair", "birimian", "drilling", "processing plant", "refined gold"],
    # strategic corridors: pipelines, rail, roads, ports — the "战略通道" axis
    "cor": ["pipeline", "pipelines", "gas pipeline", "railway", "rail line", "railway line",
            "standard gauge", "corridor", "corridors", "port", "seaport", "deep sea port",
            "expressway", "highway", "motorway", "bridge", "transmission line",
            "trans-sahara", "trans-sahel", "abidjan-lagos", "lagos-abidjan", "lome",
            "cotonou", "dakar", "rail link", "free trade zone", "logistics hub",
            "refinery pipeline", "gas corridor", "energy corridor", "trade route",
            "transport corridor", "seaport terminal", "train", "freight"],
}
CTY_MARKERS = {
    # Chinese names are included because the Chinese-language feeds (rfa_chinese)
    # and every translated headline carry them, and because a headline such as
    # "外国直接投资：高原州与中国公司签署 5 亿美元协议" names a Nigerian state
    # that no English keyword would catch once translated.
    "ng": ["nigeria", "nigerian", "nigerians", "abuja", "lagos", "kano", "kaduna",
           "maiduguri", "tinubu", "nnpc", "naira", "borno", "zamfara", "niger delta",
           "niger state", "port harcourt", "katsina", "sokoto", "efcc", "nass", "cbn",
           "dangote", "ondo", "anambra", "imo state", "plateau state", "benue",
           "ogun", "kwara", "kebbi", "bauchi", "gombe", "yobe", "adamawe",
           "adamawa", "taraba", "nasarawa", "kogi", "oyo", "ekiti", "edo",
           "delta state", "bayelsa", "rivers state", "akwa ibom", "cross river",
           "abia", "enugu", "ebonyi", "jigawa",
           "尼日利亚", "尼联邦", "阿布贾", "拉各斯", "卡诺", "卡杜纳", "高原州",
           "迈杜古里", "博尔诺", "扎姆法拉", "蒂努布",
           # Nigerian states in Chinese. Needed by the duplicate check as much as
           # by country tagging: without them "奥贡州议会通过…" and "高原州议会
           # 通过…" looked like the same headline and one law was deleted.
           "奥贡", "翁多", "贝努埃", "阿南布拉", "卡齐纳", "索科托", "凯比",
           "包奇", "贡贝", "约贝", "阿达马瓦", "塔拉巴", "纳萨拉瓦", "科吉",
           "夸拉", "奥约", "埃基蒂", "埃多州", "三角洲州", "巴耶尔萨",
           "河流州", "阿夸伊博姆", "十字河", "阿比亚", "埃努古", "埃博尼",
           "伊莫州", "吉加瓦", "尼日尔州", "联邦首都特区"],
    "ne": ["nigerien", "niamey", "tiani", "somair", "orano", "agadez", "tillaberi",
           "imouraren", "cnsp", "bazoum", "niger republic", "republic of niger",
           "niger's", "niger junta", "niger government", "niger army",
           # ── the Chinese oil complex, which the reader asked to track by name ──
           # CNPC's Agadem block, the Zinder refinery (SORAZ) and the Niger–Benin
           # export pipeline are the three assets a China-risk story in Niger is
           # almost always about. Without these tokens the stories named only
           # "Zinder" or "SORAZ" and were filed as 泛非洲.
           "soraz", "zinder", "agadem", "cnpc niger", "petrolin", "sonidep",
           "niger-benin pipeline", "niger benin pipeline", "niamey-dosso",
           "尼日尔", "尼亚美", "阿加德兹", "阿尔利特", "奇亚尼",
           "津德尔", "阿加德姆", "索拉兹", "尼日尔-贝宁", "尼贝管道",
           "中石油尼日尔", "尼日尔炼油厂"],
    "ml": ["mali", "malian", "bamako", "goita", "jnim", "timbuktu", "gao", "kidal", "mopti",
           "barrick", "loulo", "gounkoto", "sikasso", "kayes", "azawad", "malien",
           "马里", "巴马科", "通布图", "加奥", "基达尔", "戈伊塔"],
    "gh": ["ghana", "ghanaian", "ghanaians", "accra", "kumasi", "cedi", "mahama", "galamsey",
           "akufo", "tema", "obuasi", "ashanti", "volta", "ecg", "newmont", "goldfields",
           "tamale", "takoradi", "ghanian", "asantehene", "otumfuo", "goldbod",
           "bagbin", "ndc ghana", "nhc ghana", "trades union congress ghana",
           "加纳", "阿克拉", "库马西", "马哈马", "加纳塞地", "非法采金", "加纳黄金局"],
    "wa": ["ecowas", "sahel", "west africa", "west african", "regional bloc",
           "alliance of sahel", "aes states", "gulf of guinea", "burkina faso", "benin",
           "togo", "ivory coast", "cote d'ivoire", "sierra leone", "liberia",
           "guinea-bissau", "senegal", "coastal states", "sahelian", "economic community"],
    # China periphery (added 2026-10-20): the reader asked the page to cover not
    # only the four African countries but also the eight neighbours — Chinese
    # activities there are the second axis. Country codes match index.html's
    # AFRICA_CTYS/PERIPHERY_CTYS lists so the 周边国家 module filter lines up.
    # Adjective forms matter: "South Korean IPO", "Philippine coast guard",
    # "Laotian railway" name the country without using its exact name, and the
    # noun-only lists missed them — which is how "AI data center stock Elice
    # plunges in South Korean IPO" ended up in 泛非洲 and out of 周边国家.
    "kr": ["north korea", "south korea", "north korean", "south korean", "korean",
           "pyongyang", "seoul", "korean peninsula",
           "dprk", "rok", "kim jong", "kpa", "Korean war", "korean dmz",
           "Korean nuclear", "朝鲜", "韩国", "平壤", "首尔", "韩半岛", "朝鲜半岛", "韩方",
           # Jeju / Busan / Incheon appear without "Korea" in the headline, and
           # an item naming only 济州岛 would otherwise be swept out as foreign.
           "jeju", "busan", "incheon", "daegu", "gwangju", "ulsan", "daejeon",
           "济州", "釜山", "仁川", "大邱", "光州", "蔚山", "大田"],
    "ph": ["philippines", "philippine", "filipino", "manila", "marcos", "duterte",
           "philippine navy",
           "south china sea", "west philippine sea", "scarborough", "spratly",
           "palawan", "philippine coast guard", "bfar", "菲律宾", "马尼拉",
           "马科斯", "杜特尔特", "南海", "菲方",
           # Provinces and cities. "8 Chinese among 18 arrested in Surigao
           # illegal mining raid" names no country, so it was dropped as
           # off-topic even though it is a Chinese-national arrest in the
           # neighbourhood — exactly the class of story the reader asked for.
           "surigao", "davao", "cebu", "zamboanga", "mindanao", "bataan",
           "pampanga", "batangas", "cagayan", "leyte", "samar", "bohol",
           "iloilo", "baguio", "clark", "subic", "tawi-tawi", "sulu",
           "棉兰老", "宿务", "达沃", "苏里高", "三宝颜", "巴拉望"],
    "mm": ["myanmar", "burma", "burmese", "naypyidaw", "yangon", "mandalay", "rangoon",
           # NOT bare "junta": Niger, Mali and Burkina all have juntas, and the
           # word was tagging "Niger junta names new cabinet" as Myanmar — the
           # same class of mistake as filing French news under Korea. Only
           # Myanmar-specific phrasings are listed.
           "myanmar junta", "military council", "state administration council",
           "tatmadaw", "min aung hlaing", "nug", "pdf myanmar",
           "rohingya", "wa state army", "kokang", "缅甸", "仰光", "内比都",
           "敏昂莱", "若开军", "果敢", "缅军", "缅北"],
    "ru": ["russia", "russian", "moscow", "kremlin", "putin", "lavrov", "fsb", "gru",
           "wagner", "russian army", "donbas", "crimea", "russian oil", "gazprom",
           "rosatom", "俄罗斯", "莫斯科", "克里姆林宫", "普京", "瓦格纳"],
    "in": ["india", "indian", "delhi", "new delhi", "modi", "bjp", "congress india",
           "indian army", "lac", "line of actual control", "arunachal pradesh",
           "indian navy", "balakot", "印度", "新德里", "莫迪", "印度人民党",
           "实控线", "拉达克"],
    "np": ["nepal", "nepali", "nepalese", "kathmandu", "kp oli", "kp sharma",
           "prachanda", "nepal congress", "bipin ledra", "everest", "himalayan",
           "尼泊尔", "加德满都", "奥利", "普拉昌达", "尼方"],
    "vn": ["vietnam", "vietnamese", "hanoi", "ho chi minh city", "hcmc", "saigon",
           "to lam", "vietnam communist party", "vietnam navy", "south china sea",
           "paracel", "spratly", "brics vietnam", "vietnam rail",
           "越南", "河内", "胡志明市", "苏林", "越共"],
    "la": ["laos", "lao", "laotian", "vientiane", "lprp", "sonexay", "lao army",
           "lao pdr", "lao chinese railway", "boten", "laos china railway",
           "laos vietnam", "mekong laos", "老挝", "万象", "中老铁路", "磨憨", "老方"],
}
NE_ONLY = re.compile(r"\bniger\b(?!\s*(?:state|delta|river|basin|company|insurance))", re.I)
NG_RE = re.compile(r"\bnigeri", re.I)

# Domain-based country attribution (added 2026-10-20). The eight-neighbour RSS
# feeds come back in the local language (Vietnamese / Korean / Hindi / Burmese
# etc.) — the language-specific keyword list cannot match the title. Mapping
# the *domain* to a country is the only signal that survives: an item from
# vnexpress.net is always about Vietnam, from thehindu.com always about India.
# Used only as a fallback after the keyword classifier failed to find any
# country, so it never overrides an explicit match.
DOMAIN_CTY = {
    # ── KR / Korea ────────────────────────────────────────────────────────
    "koreaherald.com": "kr", "koreatimes.co.kr": "kr", "yna.co.kr": "kr",
    "koreajoongangdaily.joins.com": "kr", "joongangdaily.joins.com": "kr",
    "hani.co.kr": "kr", "chosun.com": "kr", "donga.com": "kr",
    "mk.co.kr": "kr", "biz.chosun.com": "kr", "sedaily.com": "kr",
    "korea.net": "kr", "yonhapnews.co.kr": "kr",
    # ── PH / Philippines ──────────────────────────────────────────────────
    "rappler.com": "ph", "inquirer.net": "ph", "gmanetwork.com": "ph",
    "philstar.com": "ph", "mb.com.ph": "ph", "manilatimes.net": "ph",
    "businessmirror.com.ph": "ph", "pna.gov.ph": "ph", "abs-cbn.com": "ph",
    "news.abs-cbn.com": "ph", "sunstar.com.ph": "ph", "bworldonline.com": "ph",
    # ── MM / Myanmar ──────────────────────────────────────────────────────
    "irrawaddy.com": "mm", "frontiermyanmar.net": "mm", "mizzima.com": "mm",
    "elevenmyanmar.com": "mm", "myanmarnow.com": "mm", "dvb.no": "mm",
    "burmanet.org": "mm", "mmtimes.com": "mm", "myanmar-now.org": "mm",
    # ── RU / Russia ───────────────────────────────────────────────────────
    "themoscowtimes.com": "ru", "meduza.io": "ru", "rferl.org": "ru",
    "tass.com": "ru", "rt.com": "ru", "sputniknews.com": "ru",
    "kommersant.ru": "ru", "rbc.ru": "ru", "kyivindependent.com": "ru",
    "novayagazeta.eu": "ru",
    # ── IN / India ────────────────────────────────────────────────────────
    "thehindu.com": "in", "hindustantimes.com": "in", "indianexpress.com": "in",
    "timesofindia.indiatimes.com": "in", "economictimes.indiatimes.com": "in",
    "theprint.in": "in", "thewire.in": "in", "scroll.in": "in",
    "ndtv.com": "in", "livemint.com": "in", "business-standard.com": "in",
    "deccanherald.com": "in", "telegraphindia.com": "in", "firstpost.com": "in",
    "thebetterindia.com": "in", "moneycontrol.com": "in",
    # ── NP / Nepal ────────────────────────────────────────────────────────
    "kathmandupost.com": "np", "thehimalayantimes.com": "np",
    "myrepublica.nagariknetwork.com": "np", "onlinekhabar.com": "np",
    "setopati.com": "np", "nepalnews.com": "np", "recordnepal.com": "np",
    # ── VN / Vietnam ──────────────────────────────────────────────────────
    "vnexpress.net": "vn", "tuoitre.vn": "vn", "thanhnien.vn": "vn",
    "vietnamnews.vn": "vn", "vir.com.vn": "vn", "vietnamnet.vn": "vn",
    "baodautu.vn": "vn", "nhandan.vn": "vn", "dtinews.vn": "vn",
    "saigontimes.vn": "vn",
    # ── LA / Laos ─────────────────────────────────────────────────────────
    "vientianetimes.org.la": "la", "kpl.gov.la": "la", "laotiantimes.com": "la",
}

# Multi-country desks: they cover the whole neighbourhood, so a fixed country
# would be wrong. They are tracked separately and only used to relax the
# relevance gate (a regional outlet writing about China + a neighbour without
# naming the country is still in scope); they never set `country`.
REGIONAL_DOMAINS = {
    "scmp.com", "thediplomat.com", "channelnewsasia.com", "straitstimes.com",
    "bangkokpost.com", "japantimes.co.jp", "nikkei.com", "asia.nikkei.com",
    "benarnews.org", "rfa.org", "voanews.com", "asiasociety.org",
    "lowyinstitute.org", "cfr.org", "brookings.edu", "csis.org",
}


def outlet_info(dom):
    """(label, country, page) for a domain, with a sane fallback.

    OUTLETS is a hand-curated registry of West African outlets. The eight
    China-neighbourhood queries return publishers that are not in it
    (elevenmyanmar.com, scmp.com, thediplomat.com …). The old `if dom not in
    OUTLETS: continue` therefore threw away every single one of those items,
    which is why the 周边国家 module stayed empty no matter how well the fetch
    worked. Unknown domains now get a neutral label instead of being dropped;
    the relevance gate further down still rejects anything without a country
    anchor, so this does not let junk in.
    """
    hit = OUTLETS.get(dom)
    if hit:
        return hit
    return (dom, "af", "https://" + dom)


_CJK_RX = re.compile(r"[\u4e00-\u9fa5]")


def _rx(words):
    """A boundary-anchored regex over the ASCII markers only.

    Chinese cannot use `\\b`: every Han character is a word character, so
    "尼日利亚军方" has no boundary between 亚 and 军 and the pattern never
    matched. That single mistake meant almost no Chinese headline was ever
    classified by its own words — everything fell through to the domain
    fallback, which is how a story about France on koreatimes.co.kr was filed
    under 朝韩. Chinese markers are handled by `zh_cty_hits()` instead.
    """
    words = [w for w in sorted(set(words), key=len, reverse=True)
             if not _CJK_RX.search(w)]
    if not words:
        return re.compile(r"(?!)")         # matches nothing
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


# ── Chinese country names: one pass, longest match wins ──
# Per-country regexes cannot work here. "尼日尔" is a prefix of "尼日利亚", and
# "印度" is a prefix of "印度尼西亚", so matching each country independently
# tagged a Nigerian story as Niger *and* Nigeria. A single alternation sorted by
# length, scanned left to right without overlap, resolves both: at each position
# the longest name is tried first and the match consumes it.
_ZH_CTY_MAP = {}
for _code, _words in CTY_MARKERS.items():
    for _w in _words:
        if _CJK_RX.search(_w):
            _ZH_CTY_MAP.setdefault(_w, set()).add(_code)
_ZH_CTY_KEYS = sorted(_ZH_CTY_MAP, key=len, reverse=True)
_ZH_CTY_RX = re.compile("|".join(re.escape(w) for w in _ZH_CTY_KEYS))


def zh_cty_hits(text):
    """Country codes named in Chinese anywhere in `text`."""
    if not text:
        return set()
    out = set()
    for m in _ZH_CTY_RX.finditer(text):
        out |= _ZH_CTY_MAP.get(m.group(0), set())
    return out


TOPIC_RX = {k: _rx(v) for k, v in TOPIC_MARKERS.items()}
CTY_RX = {k: _rx(v) for k, v in CTY_MARKERS.items()}

# Every country code the page actually has a module or a filter chip for.
ALL_CTYS = {"ng", "ne", "ml", "gh", "kr", "ph", "mm", "ru", "in", "np", "vn", "la"}

# ── third countries, used only to VETO the domain fallback ──
# A national daily reports the whole world. "France to deploy troops, admits
# failure in weekend riots" ran on koreatimes.co.kr with no mention of Korea, so
# the domain fallback filed it under 朝韩 and it showed up in 周边国家. The
# reader caught it. The rule now: a domain may only decide the country when the
# copy itself points at no other place.
FOREIGN_MARKERS = [
    "france", "french", "paris", "macron", "法国", "巴黎", "马克龙",
    "germany", "german", "berlin", "scholz", "merz", "德国", "柏林",
    "britain", "british", "uk ", "london", "starmer", "英国", "伦敦",
    "japan", "japanese", "tokyo", "日本", "东京",
    "brazil", "brazilian", "巴西",
    "ukraine", "ukrainian", "kyiv", "zelensk", "乌克兰", "基辅", "泽连斯基",
    "israel", "israeli", "以色列",
    "iran", "iranian", "tehran", "伊朗", "德黑兰",
    "canada", "canadian", "ottawa", "加拿大",
    "australia", "australian", "澳大利亚", "澳洲",
    "spain", "spanish", "madrid", "西班牙",
    "italy", "italian", "rome", "意大利",
    "turkey", "turkish", "ankara", "erdogan", "土耳其",
    "mexico", "mexican", "墨西哥",
    "south africa", "南非",
    "argentina", "阿根廷", "poland", "polish", "波兰",
    "netherlands", "dutch", "荷兰", "belgium", "比利时",
    "saudi", "riyadh", "沙特", "egypt", "egyptian", "cairo", "埃及",
    "ethiopia", "addis ababa", "埃塞俄比亚",
    "kenya", "nairobi", "肯尼亚", "pakistan", "pakistani", "巴基斯坦",
    "bangladesh", "孟加拉", "indonesia", "indonesian", "jakarta",
    "印度尼西亚", "印尼", "thailand", "thai ", "bangkok", "泰国", "曼谷",
    "singapore", "新加坡", "cambodia", "phnom penh", "柬埔寨",
    "sri lanka", "斯里兰卡", "afghanistan", "kabul", "阿富汗",
    "syria", "syrian", "叙利亚", "iraq", "iraqi", "伊拉克",
    "venezuela", "caracas", "委内瑞拉", "cuba", "havana", "古巴",
    "colombia", "哥伦比亚", "chile", "智利", "peru", "秘鲁",
    "taiwan", "台湾", "hong kong", "香港",
    "sweden", "stockholm", "瑞典", "norway", "挪威", "denmark", "丹麦",
    "switzerland", "geneva", "瑞士", "austria", "奥地利",
    "greece", "athens", "希腊", "portugal", "葡萄牙",
    "ireland", "irish", "爱尔兰", "finland", "芬兰",
    "czech", "hungary", "hungarian", "匈牙利",
    "romania", "bulgaria", "serbia", "croatia", "克罗地亚",
    "belarus", "minsk", "白俄罗斯", "georgia", "tbilisi", "格鲁吉亚",
    "armenia", "yerevan", "亚美尼亚", "azerbaijan", "baku", "阿塞拜疆",
    "kazakhstan", "uzbekistan", "哈萨克斯坦", "乌兹别克斯坦",
    "mongolia", "蒙古", "qatar", "doha", "卡塔尔", "uae", "dubai", "阿联酋",
    "kuwait", "科威特", "jordan", "约旦", "lebanon", "beirut", "黎巴嫩",
    "morocco", "rabat", "摩洛哥", "algeria", "algiers", "阿尔及利亚",
    "tunisia", "突尼斯", "libya", "tripoli", "利比亚", "sudan", "khartoum", "苏丹",
    "somalia", "mogadishu", "索马里", "cameroon", "yaounde", "喀麦隆",
    "chad", "n'djamena", "乍得", "senegal", "dakar", "塞内加尔",
    "ivory coast", "cote d'ivoire", "abidjan", "科特迪瓦",
    "burkina faso", "ouagadougou", "布基纳法索", "guinea", "conakry", "几内亚",
    "togo", "lome", "多哥", "benin", "cotonou", "贝宁",
    "sierra leone", "freetown", "塞拉利昂", "liberia", "monrovia", "利比里亚",
    "gambia", "banjul", "冈比亚", "mauritania", "努瓦克肖特", "毛里塔尼亚",
    "eritrea", "djibouti", "吉布提", "rwanda", "kigali", "卢旺达",
    "uganda", "kampala", "乌干达", "tanzania", "dar es salaam", "坦桑尼亚",
    "zambia", "lusaka", "赞比亚", "zimbabwe", "harare", "津巴布韦",
    "mozambique", "maputo", "莫桑比克", "angola", "luanda", "安哥拉",
    "dr congo", "democratic republic", "kinshasa", "刚果",
    "new zealand", "新西兰", "fiji", "斐济", "papua", "巴布亚",
]
_FOREIGN_RX = re.compile(
    "|".join(re.escape(w) for w in sorted(FOREIGN_MARKERS, key=len, reverse=True)),
    re.I)

# International desks of a national daily. A story under /world/ is by
# definition not about the home country, whatever the masthead is.
_WORLD_PATH_RX = re.compile(
    r"/(?:world|international|global|foreign|asia|europe|americas|"
    r"middle-?east|africa|oceania|world-news)(?:/|$)", re.I)

# Query tag → the country that query was aimed at. Used as a LAST-RESORT anchor
# when the headline names nothing recognisable: "FDI: Plateau, Chinese firm ink
# $500m deal" is Nigerian, but says "Plateau" rather than "Nigeria", and with
# only a headline for body text no marker could fire — so the story landed in
# 泛非洲 and never appeared in the 非洲四国 module. Only country-specific angles
# are listed; regional angles (cn_wa, cn_af, ter_wide …) are deliberately absent
# because they do not pin a country.
TAG_CTY = {
    "cn_ng": "ng", "cn_ng2": "ng", "cn_ng_emb": "ng", "cn_ng_gov": "ng",
    "us_ng": "ng", "gov_ng": "ng", "ng_extra": "ng",
    "cn_gh": "gh", "cn_gh2": "gh", "us_gh": "gh", "gov_gh": "gh", "gh_extra": "gh",
    "cn_ne": "ne", "ne_extra": "ne",
    # The Niger oil complex: every result from these angles is Nigerien, and the
    # copy often names only the asset ("SORAZ", "Agadem"), so the tag is the
    # reliable anchor. The pipeline angle also runs through Benin, hence the list.
    "cn_cnpc": "ne", "cn_soraz": "ne", "cn_agadem": "ne",
    "cn_cnpc2": "ne", "ne_oil": "ne", "ne_fuel": "ne",
    "ne_export": "ne", "ne_ministry": "ne",
    "cn_pipe": ["ne", "wa"],
    # ── China-risk angles ──────────────────────────────────────────────────
    # Deliberately NOT mapped to a country. "foreign workers abducted Nigeria"
    # and "Chinese mine site attack" return items from the Philippines, Congo and
    # Pakistan as well, and pinning them to ng/gh would file those under a West
    # African module. Left unmapped, the relevance gate decides from the copy.
    "cn_kidnap": None, "cn_abduct": None, "cn_arrest": None,
    "cn_charged": None, "cn_mineatk": None, "cn_trader": None,
    "cn_protest": None, "cn_fined": None, "cn_illegal": None,
    "cn_casualty": None,
    "cn_ml": "ml", "ml_extra": "ml",
    "cn_kr": "kr", "cn_ph": "ph", "cn_mm": "mm", "cn_ru": "ru",
    "cn_in": "in", "cn_np": "np", "cn_vn": "vn", "cn_la": "la",
    # ── designated terror groups ───────────────────────────────────────────
    # A JNIM / EIGS / MPLJ story frequently names the group and the region
    # ("Sahel", "Tillaberi") but no state, so it landed in no module at all.
    # Values are LISTS where the angle spans two countries.
    "ter_jnim_ml": ["ml"], "ter_jnim_ne": ["ne"],
    "ter_eigs": ["ne", "ml"], "ter_mplj": ["ne"],
    "ter_lakurawa": ["ng"], "ter_boko": ["ng"], "ter_iswap": ["ng"],
    "ter_sahel": ["ne", "ml"], "ter_wide": ["ne", "ml"], "ter_ne_ml": ["ne", "ml"],
    "ter_ng": ["ng"],
}
# The social collector uses its own soc_* tags, written into the filename
# (firecrawl_xai_<tag>_<stamp>.json) rather than the payload. Same purpose:
# last-resort country anchor for a post that names no place.
SOC_TAG_CTY = {
    "soc_cn_ng": "ng", "soc_us_ng": "ng", "soc_ter_ng": "ng",
    "soc_cn_gh": "gh", "soc_cn_ne": "ne", "soc_cn_ml": "ml",
    "soc_ter_ne": "ne", "soc_ter_ml": "ml",
    "soc_cn_kr": "kr", "soc_cn_ph": "ph", "soc_cn_mm": "mm",
    "soc_cn_ru": "ru", "soc_cn_in": "in", "soc_cn_np": "np",
    "soc_cn_vn": "vn", "soc_cn_la": "la",
    # Earlier single-country sweeps still present in the raw cache.
    "soc_ng": "ng", "soc_gh": "gh",
    "soc_ng_gov": "ng", "soc_ng_energy": "ng", "soc_ng_mining": "ng",
    "soc_ng_security": "ng",
}


def classify_topic(title, body):
    """Title matches weigh 3x: body text carried nav chrome and unrelated
    headline rails that would otherwise decide the tag."""
    hits = {}
    for k, rx in TOPIC_RX.items():
        n = 3 * len(rx.findall(title)) + len(rx.findall(body))
        if n:
            hits[k] = n
    if not hits:
        return ["pol"]
    return [k for k, _ in sorted(hits.items(), key=lambda kv: -kv[1])[:2]]


def _tag_anchor_ok(c, dom, text):
    """Is the query tag allowed to decide the country for this record?

    The tag is a last resort, and on its own it was too generous: "China's
    licensing deals soar as US biotechs enjoy a bumper M&A year" (biospace.com)
    and "Sihasak asks Chinese Embassy to keep travel warnings proportionate"
    (nationthailand.com) both came from a `cn_mm` angle and were filed under
    缅甸 — a US biotech story and a Thai foreign-ministry story, in the Myanmar
    module. The angle names what the QUERY was about, not where the story is.

    So the tag needs corroboration:
      · the outlet itself belongs to that country (catalogue match), or
      · it is a West African tag and the outlet is West African / regional press
        (a Nigerian paper reporting a Chinese investment is still Nigerian news
        even when the headline names no state), or
      · the copy names the country.
    Anything else falls back to "unknown", which the relevance gate then judges.
    """
    if not c or c in ("af", "wa"):
        return True
    for suf, code in DOMAIN_CTY.items():
        if dom == suf or dom.endswith("." + suf):
            return code == c
    # Copy that names the country is the strongest evidence there is.
    rx = CTY_RX.get(c)
    if rx and rx.search(text or ""):
        return True
    if c in zh_cty_hits(text or ""):
        return True
    if c in ("ng", "ne", "ml", "gh") and (dom in WA_DOMAINS or dom in REGIONAL_DOMAINS):
        # A West African outlet for a West African tag. Allowed only when the
        # copy does not point at a DIFFERENT West African country — without this,
        # a pure-Nigeria story found by a `cn_ne` angle would be filed as Niger,
        # which is the exact confusion this whole change is meant to end.
        others = [o for o in ("ng", "ne", "ml", "gh") if o != c]
        if not any((CTY_RX.get(o) and CTY_RX[o].search(text or ""))
                   or (o in zh_cty_hits(text or "")) for o in others):
            return True
    return False


def classify_cty(title, body, dom="", url=""):
    out = [c for c, rx in CTY_RX.items()
           if 3 * len(rx.findall(title)) + len(rx.findall(body))]
    # Chinese names, matched once over the whole string so "尼日利亚" cannot also
    # fire "尼日尔" (see zh_cty_hits).
    for c in zh_cty_hits(title) | zh_cty_hits(body):
        if c not in out:
            out.append(c)
    blob = title + " " + body
    if NG_RE.search(blob) and "ng" not in out:
        out.append("ng")
    if NE_ONLY.search(blob) and "ne" not in out:
        out.append("ne")
    if "wa" in out and any(c in out for c in ("ng", "ne", "ml", "gh")):
        out.remove("wa")
    if out:
        return out
    # Keyword classifier found nothing. For the eight China-neighbourhood feeds
    # the copy is in the local language (Vietnamese / Korean / Burmese), so the
    # English keyword list can never fire. The publishing domain is the one
    # signal that always survives — but only when the item is plausibly a local
    # story. A national daily runs the whole world, so two vetoes apply first:
    #   1. an international-desk URL (/world/, /international/) is not local;
    #   2. copy naming a third country ("France", "Germany", "Japan") is not
    #      local either, even when it names no monitored country.
    # Without these, "France to deploy troops, admits failure in weekend riots"
    # was filed under 朝韩 purely because koreatimes.co.kr published it.
    if _WORLD_PATH_RX.search(url or ""):
        return ["af"]
    if _FOREIGN_RX.search(title):
        return ["af"]
    for suffix, code in DOMAIN_CTY.items():
        if dom == suffix or dom.endswith("." + suffix):
            return [code]
    return ["af"]


# Self-promo / get-rich advert blocks that publishers inject into the body
# column. Punch in particular prepends a "NEWS UPDATE … earn $15,000-$25,000 with
# premium domains … CLICK HERE TO START" panel and appends a "Stay in the know …
# Download the Punch News App" footer, so on 2026-10-20 six articles came back
# with that advert as their ENTIRE body and several more had it glued to the end
# of the real text. Matched per sentence so a glued footer is removed too.
PROMO_RX = re.compile(
    r"click here|earn as much as|premium domains|stay in the know|"
    r"instant alerts|never miss a beat|^\s*news update:|attention nigerians|"
    r"download (?:the|our) \w+ app|get (?:instant|unlimited) access|"
    r"subscribe now|buy now|order now|advertise with us", re.I)


def purge_promo(text):
    """Drop promo sentences from a body, keeping the article prose."""
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", str(text))
    keep = [p for p in parts if p.strip() and not PROMO_RX.search(p)]
    return " ".join(keep).strip()
NAV_RX = re.compile(
    r"(advertise with us|subscribe|e-?paper|^###|^##|more news|related news|read more|"
    r"discover more from|sign up|newsletter|follow us|share this|tags?:|jump to|"
    r"trending|cookie|privacy policy|all rights reserved|^menu\b|^home\b|^search\b|"
    r"^main menu|^skip to|all content is copyright|copyrighted|©|"
    r"part of your everyday|preferred sources|add us to your|"
    r"reps panel backs|rescuers setting out|^\s*\[|^\s*\]|#\s{3,}|"
    # BusinessDay live-masthead chrome and paywall/timestamp furniture
    r"log in or create|--:--|stay informed and ahead|"
    r"^\s*\.\.\.|\[\.\.\.\]|^#\s|^\s*Monday|^\s*Tuesday|^\s*Wednesday|^\s*Thursday|"
    r"^\s*Friday|^\s*Saturday|^\s*Sunday|"
    # Guardian Nigeria's category nav: ~50 consecutive lines of this, each one
    # short enough to slip past a length-based test
    r"explore the latest updates in|"
    # "trending" rails, which are timestamped rather than punctuated
    r"\b\d+\s+(?:minute|hour|day|week)s?\s+ago\b|"
    # house adverts that sit inside the body column
    r"connect with healthcare professionals|now just a click away|"
    r"^\s*advertisement\s*$|^\s*sponsored\b|read also:|also read:|"
    r"^\s*recommended\s*$|^\s*most read\b|"
    # ⎯⎯ self-promo blocks that the extractor mistakes for the article ⎯⎯
    # Punch's page wrapper injects a "NEWS UPDATE … earn $15,000-$25,000 with
    # premium domains … CLICK HERE TO START … Download the Punch News App"
    # panel as the first text node. On 2026-10-20 six articles came back with
    # that 652-character advert as their ENTIRE body, so the summary read like a
    # get-rich advert and the translation was wasted on it. Any line carrying
    # these markers is dropped outright; if nothing survives, the caller falls
    # back to the RSS description instead of presenting the advert as the story.
    r"click here|earn as much as|premium domains|stay in the know|"
    r"instant alerts|never miss a beat|^\s*news update:|attention nigerians|"
    r"download (?:the|our) \w+ app|free \w+ alerts|"
    # generic e-paper / subscription funnels
    r"buy now|order now|subscribe now|get (?:instant|unlimited) access)", re.I)
CAPTION_RX = re.compile(
    r"(\(c?redit|credit:|file photo|photo:|image:|getty|afp photo|reuters/|ap photo|"
    r"pictured|in this file photo|^\[|^\]|"
    # standfirst-style photo captions the extractor emits as plain prose
    r"\b(?:group\s+)?photograph\s+of\b|\billustrative\b|\bleft\s+to\s+right\b|"
    r"\bposing\s+for\b|\bin\s+the\s+picture\b|\bseen\s+in\s+(?:this|the)\s+photo\b|"
    r"\bspeaking\s+at\s+the\s+unveiling\b)", re.I)
HOMEPAGE_RX = re.compile(
    r"(breaking news, latest stories|latest news|latest breaking|homepage|"
    r"news, ?sports|front page|top stories|all the latest)", re.I)

# Out-of-scope gate for the RSS firehose.
# A WordPress feed carries everything a newsroom publishes, and the topic
# classifier keys on words like "governor" / "state" / "budget" that sports and
# showbiz copy uses too ("Anambra Governor's Cup tennis", "Rangers defeat").
# Since this platform tracks politics / economy / security / energy / mining /
# corridors only, sport and entertainment are dropped by headline. The reader
# asked for this explicitly on 2026-10-21 after seeing IU's new single, an LPGA
# stop and a KBO batting story on the page.
#
# Narrow on purpose. Bare "club", "match", "Eagles", "single" and "drama" are
# NOT in the list: they fire on "investment club", "match funding", "single
# window" and "political drama", which are exactly the stories this page exists
# for. Only phrases that cannot be anything but sport or showbiz are listed.
SPORT_RX = re.compile(
    r"\b(football|soccer|NFF|Super Eagles|Falcons|coach|head coach|stadium|"
    r"matchday|tennis|basketball|NBA|NFL|MLB|NHL|LPGA|PGA|boxing|athletics|"
    r"olympic|olympics|paralympic|FIFA|CAF|AFCON|NPFL|referee|goalkeeper|"
    r"striker|midfielder|hat-trick|derby|fixture|fixtures|cricket|rugby|golf|"
    r"chess|scrabble|wrestling|transfer window|league table|qualifier|"
    r"quarter-final|semi-final|world cup|grand slam|medal table|podium|"
    r"batsman|wicket|innings|athlete|marathon|doping|sports minister|"
    r"KBO|K-league|batting|home run|pitcher|ballpark)\b", re.I)
ENT_RX = re.compile(
    r"\b(nollywood|bollywood|hollywood|movie|film|album|new single|"
    r"singer|musician|rapper|actor|actress|celebrity|comedy|concert|"
    r"beauty pageant|big brother|reality show|fashion week|grammy|oscar|"
    r"eurovision|billboard|netflix|box office|k-?pop|idol group|"
    r"drama series|tv series|webtoon|anime|manga|influencer|red carpet|"
    r"premiere|sequel|trailer|episode|music award|film festival)\b", re.I)
# Chinese equivalents. Chinese headlines are what the translated cards actually
# show, and the English list can never see them.
SPORT_ZH_RX = re.compile(
    r"(世界杯|英超|西甲|意甲|德甲|欧冠|亚冠|非洲杯|奥运|残奥|网球|篮球|足球|"
    r"球员|主教练|射手|进球|联赛|锦标赛|夺冠|季军|半决赛|四分之一决赛|"
    r"俱乐部|球队|球迷|转会|梅西|C罗|姆巴佩|内马尔|棒球|投手|本垒打|"
    r"高尔夫|拳击|田径|马拉松|奖牌榜|运动会)")
ENT_ZH_RX = re.compile(
    r"(演唱会|单曲|专辑|歌手|演员|明星|综艺|真人秀|选美|电影|影片|票房|"
    r"首映|电视剧|网红|八卦|恋情|绯闻|韩流|偶像团体|女团|男团|组合回归|"
    r"预告片|杀青|开机|红毯|颁奖礼|音乐奖|电影节|票房榜|热播|收视率)")
# A sports-ground bombing or an entertainer's shooting is a security story, not
# a soft one. Anything carrying these stays.
HARD_NEWS_RX = re.compile(
    r"\b(attack|attacked|killed|killing|kidnap|abduct|arrest|detain|"
    r"sanction|tariff|military|missile|airstrike|war|protest|election|"
    r"parliament|court|verdict|sentenced|explosion|bomb|gunmen)\b|"
    r"(袭击|遇袭|爆炸|绑架|劫持|被捕|拘留|判决|宣判|制裁|关税|军事|"
    r"选举|议会|法院|枪击|死亡|受伤)", re.I)


def is_soft(title, title_zh=""):
    """True for a sport / showbiz headline that carries no hard-news substance."""
    blob = (title or "") + " " + (title_zh or "")
    if not blob.strip():
        return False
    if HARD_NEWS_RX.search(blob):
        return False
    return bool(SPORT_RX.search(title or "") or ENT_RX.search(title or "")
                or SPORT_ZH_RX.search(blob) or ENT_ZH_RX.search(blob))


# ── human-interest fluff ──
# A neighbourhood desk covers the whole country, so pure human-interest copy
# arrives alongside the politics: a shark trapped in a Busan canal, a "food Silk
# Road" festival, a museum curator resigning over a misattributed piece, an
# Amelia Earhart search update. The reader asked for sport and showbiz to go;
# this is the same category, and it was 46 of the 262 items in 周边国家.
#
# Deliberately NARROW. Policy and economy stay: a housing-loan programme, a
# primary-care upgrade, a payroll settlement or a customs rating is how a
# government actually governs, and the 北部 reader wants it. Only copy that is
# purely an animal / festival / curiosity story is dropped.
FLUFF_RX = re.compile(
    r"(鲨鱼|海豚|鲸鱼|大象|老虎|狮子|熊猫|宠物|动物园|水族馆|"
    r"选美|美食节|美食丝绸|庆典|烟花|嘉年华|狂欢节|"
    r"露天排便|埃尔哈特|寻宝|吉尼斯|彩票|超长寿|最长寿|"
    r"藏品归属|私人收藏|文物归还仪式)")
FLUFF_EN_RX = re.compile(
    r"\b(shark|dolphin|whale|elephant|tiger cub|lion|pet dog|zoo|aquarium|"
    r"beauty pageant|food festival|carnival|fireworks|guinness record|"
    r"lottery|amelia earhart|treasure hunt|longest[- ]living)\b", re.I)


def is_fluff(title, title_zh=""):
    """True for a pure human-interest / festival headline with no news value."""
    blob = (title or "") + " " + (title_zh or "")
    if not blob.strip():
        return False
    if HARD_NEWS_RX.search(blob):        # a bombing at a festival is news
        return False
    return bool(FLUFF_RX.search(blob) or FLUFF_EN_RX.search(title or ""))

# Chrome that is GLUED to real prose on the same line, so it has to be repaired
# rather than dropped — dropping the line would throw the lead sentence away with
# it. "…Push In Africa Share on WhatsAppShare on FacebookShare on XTelegram The
# U.S. faces an uphill battle…" is one line, and the last part is the story.
_SHARE_RX = re.compile(
    r"(?:share\s+(?:on|via)\s+(?:whatsapp|facebook|x|twitter|telegram|linkedin|email)\s*)+"
    r"(?:telegram|whatsapp|facebook|twitter|linkedin|email)?", re.I)
_TICKER_RX = re.compile(
    r"\b(?:micro\s+|mini\s+)?(?:silver|gold|copper|crude|brent|platinum|palladium)\s+futures\b"
    r"[^.!?]{0,40}?[\d.,]+\s*/\s*ozt[^.!?]{0,20}", re.I)


def deshuck(s):
    """Remove share widgets and market tickers glued onto real prose."""
    if not s:
        return ""
    t = _SHARE_RX.sub(" ", s)
    t = _TICKER_RX.sub(" ", t)
    t = re.sub(r"^\s*(?:telegram|whatsapp|facebook|twitter|linkedin)\b[\s:]*", "", t, flags=re.I)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip(" |·-–—")
STOP = set(("the a an and or of to in on for with by at from as is are was were be been "
            "has have had it its this that these those their his her they we you not but "
            "if then than over under after before during into out up down new says said "
            "say also more most will would can could may might who whom which what when "
            "where how all any both each few other some such only own same so too very").split())


# Zero-width / bidi marks the index sometimes pastes in front of a headline
# ("\u200eNigeria's $11.1bn ..." rendered with a leading box glyph).
_INVIS = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"), None)
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]{1,140})\]\([^)]*\)")
_MD_BOLD = re.compile(r"\*\*([^*]{1,200})\*\*")


def clean(s):
    """Normalise text coming from two very different extractors.

    The news index returns plain prose with navigation chrome; the article
    backfill returns Markdown. Both have to survive the same downstream tests,
    so Markdown scaffolding is unwrapped here rather than in every caller.
    """
    if not s:
        return ""
    t = str(s).translate(_INVIS).replace("\u00a0", " ")
    t = _MD_IMG.sub(" ", t)                 # images carry no text
    t = _MD_LINK.sub(r"\1", t)              # keep the link label, drop the URL
    t = _MD_BOLD.sub(r"\1", t)
    # Line-anchored, so a hashtag mid-sentence in a social post is left alone.
    t = re.sub(r"(?m)^\s*[*\-+]\s+", "", t)          # bullet markers
    t = re.sub(r"(?m)^\s*#{1,6}\s+", "", t)          # heading markers
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


def _is_headline_rail(line):
    """Detect a 'related headlines' rail pasted into the body.

    Three signals, applied in order of reliability:

    1. **Punctuation density.** This is the one that actually works on the long
       cases. Real prose terminates a sentence roughly every 60-200 characters;
       a rail of ten concatenated headlines running to 900 characters may carry
       only a single full stop, and that one stop was enough to defeat the
       earlier "contains no punctuation" test, which is exactly how blocks like
       "Nigeria trails African peers in IMF's 2026 growth forecast / Higher
       output, exports drive Dangote Refinery's revenue up 144% to $22.5bn /
       BusinessDay sees September inflation rising to 15.5% ..." reached the
       page as a "summary". The test is a ratio, so it is length-independent.
    2. **No punctuation at all** on a line long enough to need some.
    3. **Capitalisation ratio** against function words, for short rails that do
       happen to contain a full stop.
    """
    n = len(line)
    if n < 30:
        return False
    # Only SENTENCE-FINAL punctuation counts. Counting every dot made the test
    # useless on financial copy: "…revenue up 144% to $22.5bn" and "N6.29trn"
    # supply enough decimal points to make a 1000-character headline rail look
    # like dense prose.
    punct = len(re.findall(r"[.?!](?=\s|$)", line))
    if n > 200 and punct * 350 < n:
        return True
    if n > 100 and punct == 0:
        return True
    toks = re.findall(r"[A-Za-z][A-Za-z'’\-]*", line)
    if len(toks) < 8:
        return False
    caps = sum(1 for t in toks if t[:1].isupper())
    fn = len(re.findall(r"\b(?:the|of|in|to|and|for|on|with|that|was|has|were|its|"
                        r"a|an|is|are|by|as|at|from|be|it|this|which|who|will|had)\b",
                        line, re.I))
    return (caps / len(toks)) > 0.55 and fn < max(3, len(toks) * 0.06)


def strip_body(content, title):
    if not content:
        return ""
    keep = []
    for raw in re.split(r"\n+", str(content)):
        r = raw.strip()
        if not r:
            continue
        # Related-article rails arrive from the extractor as Markdown bullet
        # links — "* [Headline](url)". This has to be tested on the RAW line:
        # clean() unwraps "[label](url)" into "label", after which the rail is an
        # indistinguishable run of headline text glued to the article's own
        # prose. Catching it here is what stops "ProjectLEAD mentors 100 Lagos
        # girls on choices, careers" appearing inside a Punch summary.
        if re.match(r"^[*\-+]\s*\[", r) or r.count("](") >= 2:
            continue
        ln = deshuck(clean(r))
        if len(ln) < 40 or NAV_RX.search(ln) or CAPTION_RX.search(ln):
            continue
        if ln.count("|") > 2 or ln.count("–") > 3 or ln.count(" # ") > 0:
            continue
        if _is_headline_rail(ln):
            continue
        keep.append(ln)
    # No usable prose at all: return nothing. Falling back to the raw content here
    # would re-admit exactly the navigation and headline rails just filtered out.
    if not keep:
        return ""
    body = " ".join(keep)
    body = purge_promo(body)
    t = clean(title)
    if t and body.lower().startswith(t.lower()[:45]):
        body = body[len(t):].strip(" |.–—-")
    if t and len(t) > 25:
        body = body.replace(t, " ", 1).strip(" |.–—-")
    return body


def _toks(s):
    return {w for w in re.findall(r"[a-z]{4,}", (s or "").lower()) if w not in STOP}


# ─────────────── headline clean-up ───────────────
# The publisher's masthead is appended to the headline by the CMS, not by the
# journalist: "FG moves to align economic policies under new framework –
# Businessday NG", "…in FCT - THISDAYLIVE", "Why Is Nigeria on the Red List? |
# The Guardian Nigeria News - Nigeria and World News". None of it belongs in a
# title, and it is what makes a translated headline read as noise.
MASTHEAD_RX = re.compile(r"""(
 \bthe\s+nation\s+(?:newspaper|nigeria)\b|\bthisday(?:live)?\b|\bbusinessday(?:\s+ng)?\b|
 \bthe\s+guardian\s+nigeria\b|\bguardian\s+ng\b|\bpunch\b|\bvanguard\b|\bpremium\s+times\b|
 \bthecable\b|\bsahara\s*reporters?\b|\bdaily\s+trust\b|\bleadership\b|
 \bnigerian\s+tribune\b|\btribune\s+online\b|\bthe\s+sun\s+(?:newspaper|nigeria)\b|
 \bmyjoyonline\b|\bciti\s+newsroom\b|\bgraphic\s+online\b|\bghanaweb\b|
 \b3news\b|\badom\s+online\b|\bpeace\s*fm\s+online\b|
 \bmining\.com\b|\bmining\s+journal\b|\bmining\s+technology\b|\binternational\s+mining\b|
 \bafricanews\b|\ballafrica\b|\bthe\s+africa\s+report\b|\breuters\b|\bbbc\b|
 \becofin\s+agency\b|\bactuniger\b|\ble\s+sahel\b|\bair\s+info\b|\bmalijet\b|\bmaliweb\b|
 \bnigeria\s+and\s+world\s+news\b|\bworld\s+news\b|\blatest\s+news\b|
 \bnews\s+agency\b|\bnews\s+online\b|\bnews\s+paper\b|\bnewspaper\b
)""", re.I | re.X)

# Separators a CMS puts between the headline and the masthead. Captured, so a
# separator that turns out NOT to precede a masthead can be put back verbatim
# instead of being flattened into a space.
SEP_RX = re.compile(r"(\s+[|–—]\s+|\s+-\s+|\s+»\s+|\s+·\s+)")


# ── links inside headlines ──
# A search result's title field is frequently the page title *plus its own URL*:
# "尼日利亚-美国卫生协议：… https://sciencenigeria.com/nigeria-us-health-deal".
# It arrives that way from the social index (which concatenates title + link) and
# the translation step then faithfully keeps the link in the Chinese title, so the
# card shows a raw URL in the headline. 19 live items had one. A headline is not
# a place for a link — the card already has 打开原文 — so every link is cut here.
_LINK_IN_TITLE_RX = re.compile(r"(?:https?://|ftp://|www\.)\S+", re.I)
_BARE_LINK_RX = re.compile(
    r"(?<![\w.@/-])(?:[a-z0-9][a-z0-9\-]{0,30}\.){1,4}"
    r"(?:com|org|net|info|news|press|media|tv|me|io|co|gov|edu|africa|"
    r"ng|gh|ke|za|cn|ru|in|fr|de|uk|us|ca|au|jp|kr|vn|np|mm|ph|la)"
    r"(?:\.[a-z]{2})?(?:/[^\s，。；、）)\]】]*)?", re.I)
# Reading lead-ins the summariser bolts onto a title ("… 请在 Wallflake 上阅读：").
_TITLE_TAIL_RX = re.compile(
    r"[\s，,；;:：\-–—|·]*(?:请在\s*\S{0,24}\s*上阅读|阅读全文|点击(?:此处|这里|查看)|详见|参见|来源[:：])\s*[:：]?\s*$",
    re.I)


def strip_links(t):
    """Remove URLs, bare domains and reading lead-ins from a headline."""
    if not t:
        return t
    s = _LINK_IN_TITLE_RX.sub(" ", t)
    s = _BARE_LINK_RX.sub(" ", s)
    for _ in range(3):                       # stacked tails are common
        s2 = _TITLE_TAIL_RX.sub("", s)
        if s2 == s:
            break
        s = s2
    s = re.sub(r"[\s\u3000]+", " ", s)
    s = re.sub(r"\s+([，。；：、！？）》])", r"\1", s)
    s = re.sub(r"([（《])\s+", r"\1", s)
    s = re.sub(r"[\s，,；;:：\-–—|·]+$", "", s).strip()
    # A title that was nothing but a link keeps its original form rather than
    # becoming an empty string the card cannot render.
    return s if len(s) >= 6 else t.strip()


# ── trailing "| Section | Outlet" tails ──
# MASTHEAD_RX only knows the outlets it was taught, so a headline from an
# unlisted site kept its furniture: "中国数据中心沉入水下引发担忧 | 新闻 |
# Eco-Business | 亚太", "… | The Star", "… | News.az". 12 live titles still had
# one. Rather than grow the list forever, drop trailing pipe segments that look
# like furniture and keep every segment that reads like content.
_PIPE_RX = re.compile(r"\s*[|｜]\s*")
_CLAUSE_RX = re.compile(r"[：:，,。；;！？!?]")


def _strip_pipe_tail(t):
    parts = _PIPE_RX.split(t or "")
    while len(parts) > 1:
        last = parts[-1].strip()
        if not last or len(last) > 18:
            break
        if _CLAUSE_RX.search(last):      # punctuation ⇒ a real clause, not a desk
            break
        cjk = bool(re.search(r"[\u4e00-\u9fa5]", last))
        if cjk and len(last) <= 6:       # 新闻 / 亚太 / 国际
            parts.pop()
            continue
        if not cjk and re.fullmatch(r"[A-Za-z0-9 .&'’\-–—/]+", last):
            parts.pop()                  # The Star / News.az / Eco-Business
            continue
        break
    return " | ".join(p.strip() for p in parts)


# ── masthead tails the list above cannot know ──
# Publisher names are effectively unbounded ("The Eastleigh Voice", "真相尼日利亚",
# "国防新闻", "商业内幕非洲"), so the rule is: the LAST segment of a headline is
# furniture when it carries no clause — no verb-shaped punctuation, short, and
# either Latin-only or a Chinese outlet name. 38 live headlines ended in one.
_TAIL_SEP_RX = re.compile(r"\s+(?:[|｜•·ᐉ]|[-–—])\s+")
# Chinese words that end an outlet name. A headline ending in 新闻 / 日报 / 时报
# is ending in a masthead; a headline ending in 报道 is not.
_ZH_OUTLET_TAIL_RX = re.compile(
    r"(新闻|日报|晚报|时报|周报|晨报|商报|邮报|先驱报|都市报|导报|观察者|"
    r"通讯社|电视台|广播|在线|网|网站|报|杂志|周刊|月刊|社)$")
_ZH_OUTLET_NAMES = {
    "南华早报", "真相尼日利亚", "商业内幕非洲", "中国-全球南方项目",
    "埃尔克福德区", "国防新闻", "伊拉克新闻", "南亚先驱报", "半岛电视台",
    "环球时报", "参考消息", "澎湃新闻", "财新", "界面新闻", "观察者网",
    "今日头条", "纽约时报", "华盛顿邮报", "华尔街日报", "金融时报",
    "路透社", "法新社", "美联社", "彭博社", "英国广播公司",
}
# A tail with no Chinese character is a foreign publisher name ("Demócrata",
# "نون بوست"), whatever the script. \x20-\x7e alone missed the accented and
# Arabic ones.
_LATIN_TAIL_RX = re.compile(r"^[^\u4e00-\u9fff]+$")


def _strip_masthead_tail(t):
    """Drop trailing publisher segments: "… - The Eastleigh Voice", "… | 国防新闻"."""
    if not t:
        return t
    s = t.strip()
    for _ in range(3):                   # "… - 世界 - Fakti.bg" has two
        parts = _TAIL_SEP_RX.split(s)
        if len(parts) < 2:
            break
        last = parts[-1].strip(" .·•")
        if not last or len(last) > 26:
            break
        if re.search(r"[。！？!?；;：:，,]$", last):
            break
        rest = " ".join(parts[:-1]).strip()
        if len(rest) < 14:               # never reduce a headline to a stub
            break
        cjk = bool(re.search(r"[\u4e00-\u9fa5]", last))
        drop = False
        if not cjk and _LATIN_TAIL_RX.match(last):
            # "Plans to Shift Exports" is ASCII too, so require the segment to
            # read like a name: no more than four words, no sentence verb tail.
            drop = len(last.split()) <= 4
        elif cjk and (last in _ZH_OUTLET_NAMES
                      or (len(last) <= 12 and _ZH_OUTLET_TAIL_RX.search(last))):
            drop = True
        if not drop:
            break
        s = rest
    return s


def _cut_dangling(t, limit=150):
    """Drop a truncated tail and any over-long tail, always at a clause edge.

    The limit is generous on purpose. At 110 the index's own 111-character
    titles were being cut, which left a visible "…" on about a hundred cards —
    the reader asked for headlines they can read to the end.
    """
    if not t:
        return t
    body = re.sub(r"\s*(?:…|\.{3,})+\s*$", "", t).strip()
    if len(body) < 12:
        return t
    if len(body) > limit:
        window = body[:limit]
        cut = max(window.rfind("。"), window.rfind("！"), window.rfind("？"),
                  window.rfind("；"), window.rfind("，"), window.rfind("、"))
        if cut >= 24:
            return window[:cut].rstrip("，、； ")
        return window.rstrip("，、； ") + "…"
    if body != t:                        # there WAS an ellipsis: cut back to a clause
        cut = max(body.rfind("。"), body.rfind("！"), body.rfind("？"),
                  body.rfind("；"), body.rfind("，"))
        if cut >= 12:
            return body[:cut].rstrip("，； ")
    return body


def tidy_headline(t):
    """Everything a headline needs after cleaning: no links, no furniture, no cut."""
    return _cut_dangling(_strip_masthead_tail(_strip_pipe_tail(strip_links(t))))


# A masthead the CMS cut in half ("… | South China Morning…", "… - Auto…").
# MASTHEAD_RX cannot see these because the name is incomplete, so the fragment
# survived as the tail of the headline.
_TRUNC_MASTHEAD_RX = re.compile(
    r"\s*[|–—\-]\s*(?:south\s+china\s+morning|scmp|auto|the\s+armenian|"
    r"business\s+insider|bloomberg|reuters|the\s+eastleigh|eleven\s+media|"
    r"news\.az|the\s+star|dem[oó]crata)\b[^|–—]*?(?:…|\.{2,})?\s*$", re.I)

# "…Plans to Shift Exports From China to U.…" — the index cut inside "U.S.".
_CUT_ABBR_RX = re.compile(r"(?<![A-Za-z])U\.\s*(?:…|\.{2,})\s*$")
# Chinese titles keep the ellipsis from the English source, and a machine
# translation often trails off on a conjunction.
_ZH_TAIL_RX = re.compile(r"(?:…|\.{3,})\s*$")
_ZH_DANGLING_RX = re.compile(r"[的地得和与及在对将为把从由、，,]\s*$")


def tidy_zh_title(t):
    """Final pass on a translated headline: no links, no ellipsis, no half clause."""
    if not t:
        return t
    s = strip_links(t)
    s = re.sub(r"(?<![A-Za-z])U\.\s*(?:…|\.{2,})", "U.S.", s)
    s = _TRUNC_MASTHEAD_RX.sub("", s)
    s = _strip_masthead_tail(_strip_pipe_tail(s))
    s = _ZH_TAIL_RX.sub("", s).strip(" -–—|·,，:：、")
    # Trailing on 的/和/将 leaves the reader mid-sentence; cut back to the last
    # full clause if there is one.
    if _ZH_DANGLING_RX.search(s):
        cut = max(s.rfind("，"), s.rfind("、"), s.rfind("；"), s.rfind("："))
        if cut >= 10:
            s = s[:cut].rstrip("，、；：")
    return s or t


def polish_title(title, body="", url=""):
    """Return the headline the way a reader would write it.

    Four fixes, all of them observed in the live data:

    * strip the trailing masthead ("… – Businessday NG", "… | The Guardian
      Nigeria News - Nigeria and World News");
    * drop a truncation ellipsis ("Nigeria's $11.1bn foreign education bill
      raises concerns ...") and, where the article body is available, restore the
      clause the CMS cut off;
    * when the last word is clearly cut mid-word ("...new schoo"), the URL slug
      usually contains the complete headline — re-build it from the slug;
    * remove the leading zero-width/bidi marks the index sometimes injects.
    """
    t = strip_links(clean(title))
    if not t:
        return t

    # 1. Masthead. Cut from the FIRST separator whose right-hand side is a
    #    masthead, so "… | The Guardian Nigeria News - Nigeria and World News"
    #    loses everything after the pipe. A separator whose tail is NOT a
    #    masthead is left alone — "Nigeria - Ghana trade" keeps its dash.
    toks = SEP_RX.split(t)
    if len(toks) > 1:
        out, i = toks[0], 1
        while i + 1 < len(toks):
            sep, nxt = toks[i], toks[i + 1]
            if MASTHEAD_RX.search(nxt):
                break                      # masthead starts here — drop the rest
            out += sep + nxt               # a real clause: keep it, dash and all
            i += 2
        t = out.strip()

    # 2. Truncation.
    truncated = bool(re.search(r"(\.\.\.|…)\s*$", t))
    t = re.sub(r"[\s.,;:]*[.…]{2,}\s*$", "", t).strip(" -–—|·")
    if truncated and body:
        t = _complete_title(t, body)

    # 3. Mid-word cut. If the title ends on a partial word ("...new schoo"),
    #    the URL slug is the most reliable source of the full headline. The
    #    body rarely re-states the headline verbatim, so step (2) above will
    #    have failed for these.
    if url and _looks_cut_midword(t):
        slug = _title_from_url(url)
        if slug and len(slug) > len(t) and _is_compatible_prefix(slug, t):
            t = slug[:160]

    t = re.sub(r"\s{2,}", " ", t).strip(" -–—|·,:;")
    # 4. Furniture the masthead list does not know, and a tail still cut short.
    #    Runs LAST so steps 2-3 still see the ellipsis they rely on.
    t = _cut_dangling(_strip_pipe_tail(t))
    return t


def _looks_cut_midword(t):
    """True if `t` ends on a partial word — i.e. a lowercase, short token
    with no terminal punctuation, after a longer word."""
    if not t or len(t) < 25:
        return False
    # The trailing whitespace-free token
    last = re.split(r"\s+", t.rstrip())[-1]
    if not last or not last.isalpha():
        return False
    # Heuristic: cut-midword tokens are 3–6 chars, all lowercase. Common
    # full words tend to be longer or to have an article/verb before them.
    return 3 <= len(last) <= 6 and last.islower()


def _title_from_url(u):
    """Build a probable title from the last path segment of a URL.

    e.g. "https://punchng.com/tinubu-seeks-senate-nod-to-replace-ascon-with-national-school-of-government"
        → "Tinubu seeks senate nod to replace ascon with national school of government"
    """
    if not u:
        return ""
    from urllib.parse import urlparse, unquote
    try:
        path = urlparse(u).path
    except Exception:
        return ""
    seg = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    # Drop extension and obvious ID-like segments (all digits, very long hex)
    seg = re.sub(r"\.[a-z]{2,5}$", "", seg, flags=re.I)
    if not seg or re.match(r"^[\d_-]{6,}$", seg):
        return ""
    words = [w for w in re.split(r"[-_]+", seg) if w]
    if len(words) < 4:
        return ""
    title = " ".join(words)
    # Capitalise: first word + any word that follows a punctuation break; keep
    # the rest lowercase to match newspaper-headline style.
    parts = title.split(" ")
    if parts:
        parts[0] = parts[0][:1].upper() + parts[0][1:]
    return " ".join(parts)


def _is_compatible_prefix(longer, shorter):
    """True if `shorter` is plausibly a prefix of `longer` (case-insensitive,
    ignoring trailing punctuation / partial words, and tolerant of word-order
    differences — the slug and the truncated title rarely word-for-word
    agree, e.g. "Senate approval" vs "Senate nod")."""
    def norm(s):
        s = s.lower().rstrip(".,;:!? ")
        # Drop a partial trailing word (≤ 6 chars) — that's the cut bit.
        toks = s.split()
        if len(toks) >= 2 and len(toks[-1]) <= 6:
            toks = toks[:-1]
        # Stop at the first non-alpha token (handles "approval" vs "nod to").
        return " ".join(toks)
    a, b = norm(longer), norm(shorter)
    if not a or not b:
        return False
    if a == b:
        return True
    # Word-set overlap: the slug and the title should share the bulk of
    # their content words. Stop words are excluded.
    stop = {"a", "an", "the", "of", "in", "on", "for", "to", "with", "and",
            "or", "but", "is", "at", "by", "as"}
    wa = {w for w in a.split() if w not in stop and len(w) > 2}
    wb = {w for w in b.split() if w not in stop and len(w) > 2}
    if len(wa) < 3 or len(wb) < 3:
        return False
    overlap = len(wa & wb) / max(1, min(len(wa), len(wb)))
    return overlap >= 0.7


def _complete_title(t, body):
    """Re-attach the clause a truncated headline was cut off at.

    Only fires when the body actually contains the continuation: the headline's
    last few words are located in the article text and the rest of that sentence
    is appended, up to a sane length. If the continuation cannot be found the
    headline is left as it is rather than guessed at.
    """
    if len(t) < 30:
        return t
    tail = " ".join(re.findall(r"[A-Za-z']+", t)[-4:]).lower()
    if len(tail.split()) < 3:
        return t
    for s in split_sentences(strip_body(body, t) or body):
        rest = ""
        if s.lower().startswith(tail[:24]):
            rest = s[len(tail):]
        elif tail in s.lower():
            i = s.lower().index(tail)
            rest = s[i + len(tail):]
        if not rest:
            continue
        # Only the remainder of the SAME sentence, and only when it stops
        # cleanly. Appending the whole lead sentence produced a 190-character
        # "title" that duplicated its own opening clause; truncating mid-clause
        # would be worse still.
        rest = _title_remainder(rest)
        if not rest:
            continue
        # _title_remainder strips the leading space, so the join has to put it
        # back — otherwise "…raises concerns" + "over local institutions" came
        # out as "…raises concernsover local institutions".
        joiner = "" if rest[:1] in ",.;:!?" else " "
        cand = (t.rstrip() + joiner + rest).strip()
        if len(cand) <= 140:
            return cand
    return t


_DANGLING = ("and", "or", "but", "of", "to", "in", "on", "for", "with", "as",
             "at", "by", "from", "the", "a", "an", "its", "his", "her", "their")


def _title_remainder(rest, limit=60):
    """The part of a sentence that can be appended to a headline cleanly.

    Returns "" unless the remainder ends at a full stop inside the limit, or is
    itself a short clause that does not trail off on a conjunction or article.
    """
    rest = (rest or "").strip(" ,;:")
    if not rest:
        return ""
    m = re.search(r"[.?!](?=\s|$)", rest)
    if m and m.end() <= limit:
        return rest[:m.end()].rstrip(".")
    m = re.search(r",(?=\s)", rest)
    if m and m.end() <= limit:
        return rest[:m.end()].rstrip(",")
    # A capital letter directly after a lower-case word, with no punctuation
    # between, is where the extractor butted one headline up against the next
    # ("…raises concerns over local institutions Nigeria's reported
    # expenditure…"). Only accepted once the phrase is long enough to read as a
    # clause, so "…in Lagos State" is not cut down to "…in".
    for m in re.finditer(r"(?<=[a-z]) (?=[A-Z])", rest):
        if m.start() >= 15:
            return rest[:m.start()]
    if len(rest) <= limit and rest.split()[-1].lower().strip(",;:") not in _DANGLING:
        return rest
    return ""


_ABBR = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Prof|Gen|Col|Sen|Rep|Gov|St|No|vs|etc|Jr|Sr|Inc|Ltd|Co|Corp|"
    r"U\.S|U\.K|U\.N|a\.m|p\.m|e\.g|i\.e|approx|Dept|Est)\.", re.I)


def split_sentences(text):
    """Split on sentence ends without breaking on 'U.S.' / 'Dr.' / 'p.m.'."""
    prot = _ABBR.sub(lambda m: m.group(0)[:-1] + "\u0001", text or "")
    parts = re.split(r"(?<=[.!?])\s+", prot)
    return [p.replace("\u0001", ".").strip() for p in parts if p.strip()]


def _cut_at_sentence(s, limit):
    """Trim near `limit`, always ending on a complete sentence where possible.

    The reader's requirement is that the title and summary read as finished text
    on the card, never as a dangling fragment. The old version gave up as soon as
    no sentence end fell inside the window and appended " …", so a summary could
    end mid-thought with an ellipsis. Now the window is allowed to run PAST the
    limit (up to 1.35x) to reach the next full stop — a slightly longer summary
    that reads completely beats a shorter one that does not.
    """
    if not s:
        return ""
    s = s.strip()
    if len(s) <= limit:
        return s
    # 1) last sentence end inside the window
    window = s[:limit]
    m = None
    for m in re.finditer(r"[.?!](?=\s|$)|[。！？]", window):
        pass
    if m and m.end() >= limit * 0.5:
        return window[:m.end()].strip()
    # 2) look a little further ahead for the next full stop
    hard = min(len(s), int(limit * 1.35))
    m2 = re.search(r"[.?!](?=\s|$)|[。！？]", s[limit:hard])
    if m2:
        return s[:limit + m2.end()].strip()
    # 3) genuinely no sentence end (a list, a table, a rail): cut at a word break
    sp = window.rfind(" ")
    return (window[:sp] if sp > limit * 0.6 else window).strip() + " …"


def summary_of(content, title, max_chars=520, max_sents=3):
    """Write a summary that is complete and on-topic, or nothing at all.

    Differences from the first version, all driven by what the reader saw:

    * up to three sentences and ~520 characters instead of two and 300, so the
      summary can actually carry the story rather than the first clause of it;
    * trimming happens at a sentence boundary, never mid-word — the old fixed
      slice produced "…Opposition dares Tinubu, says Wike, Soludo inclusi";
    * sentences are scored on title overlap *and* position, so the lead is
      preferred over a later aside that happens to repeat a keyword;
    * the same rail test runs on the assembled result, so a rail that survived
      per-line filtering still cannot reach the page.
    """
    body = strip_body(content, title)
    if not body:
        return ""
    sents = []
    for s in split_sentences(body[:8000]):
        s = deshuck(s)
        # Unbalanced quotation marks mean the other half of the quotation was
        # filtered out, so what remains reads as an orphan: either it opens on a
        # closing quote, or it is the second half of a quote whose opening half
        # sits in a sentence that was dropped ("I am the commander of field
        # operations as far as the FCT election is concerned."").
        if s[:1] in ("\u201d", "\u2019"):
            continue
        if s.count("\u201d") > s.count("\u201c") or s.count('"') % 2:
            continue
        if (len(s) >= 45 and not NAV_RX.search(s) and not CAPTION_RX.search(s)
                and not _is_headline_rail(s)):
            sents.append(s)
    if not sents:
        return ""
    tt = _toks(title)
    scored = []
    for i, s in enumerate(sents):
        ov = len(tt & _toks(s))
        pos = 1.0 if i == 0 else 1.0 / (1 + i * 0.35)
        scored.append((ov * 2.0 + pos, ov, i, s))
    scored.sort(key=lambda x: (-x[0], x[1]))
    # A sentence with ZERO vocabulary overlap with the headline is usually not
    # part of this story — on a page whose body is a sidebar rail it is the next
    # story along ("Remi Surutu celebrates Seyi Tinubu on his birthday…").
    # BUT requiring overlap from every sentence was too strict: a report whose
    # lead paraphrases the headline ("The committee approved the sum on Tuesday")
    # has no shared words, so the whole summary came back empty and the card
    # showed a placeholder instead of the opening of a perfectly good article.
    # Now: prefer overlapping sentences; if none qualify, use the LEAD sentences
    # (position 0, 1) because the first sentences of an article are its summary
    # by construction.
    qual = [x for x in scored if x[1] >= 1]
    if qual:
        chosen = sorted(qual[:max_sents], key=lambda x: x[2])
    else:
        chosen = [x for x in scored if x[2] < max_sents]
        chosen.sort(key=lambda x: x[2])

    out = ""
    for _, _, _, s in chosen:
        if out and len(out) + len(s) + 1 > max_chars:
            break
        out = (out + " " + s).strip()
    if not out:
        return ""
    # The extractor usually repeats the headline as the first body line, and a
    # share widget often sits between the two.
    out = _strip_leading_title(out, title)
    out = _cut_at_sentence(out, max_chars)
    # Final gate: prose with no sentence end at all is a rail that was split
    # across source lines, so each fragment looked short enough to pass.
    if len(out) > 120 and not re.search(r"[.?!]", out):
        return ""
    return out


def _strip_leading_title(text, title):
    """Remove a repeated headline at the head of the summary.

    Extraction tools emit the headline, then share chrome, then the article, so
    the assembled summary can open with "US Fights To Counter China's Telecom
    Push In Africa Share on WhatsAppShare on FacebookShare on XTelegram The
    U.S. faces an uphill battle…". The share chrome is already dropped by
    NAV_RX; this removes the duplicated headline in front of what remains.
    """
    if not title:
        return text
    head = _fold(clean(title))
    if len(head) < 20:
        return text
    low = _fold(text)
    # Walk the longest common prefix rather than testing fixed slice lengths.
    # A fixed length either stops short of the headline (leaving a stray tail
    # word in the summary) or overshoots into the next word — cutting the
    # headline at 70 characters turned "…over local institutions" into
    # "ns over local institutions".
    n = 0
    while n < len(head) and n < len(low) and head[n] == low[n]:
        n += 1
    if n < 20:
        return text
    while n < len(text) and text[n].isalnum():
        n += 1                      # finish the word the match stopped inside
    rest = text[n:].lstrip(" :–-—.|")
    # A remainder that opens lower-case continues the headline's own clause, so
    # removing the headline would leave a sentence fragment; keep the original.
    if not rest or rest[:1].islower():
        return text
    return rest


_QUOTES = {0x2018: "'", 0x2019: "'", 0x201c: '"', 0x201d: '"', 0x02bc: "'"}


def _fold(s):
    """Lower-case and normalise smart quotes, for prefix comparisons.

    The search index and the article extractor disagree on apostrophes
    ("Nigeria's" vs "Nigeria's"), so a raw comparison silently fails to match a
    headline against the same headline.
    """
    return (s or "").translate(_QUOTES).lower()


# ─────────────── article full text (from fetch_articles.py) ───────────────
_ARTICLE_CACHE = {}


def article_text(url):
    """Full article body cached by fetch_articles.py, or "" when unavailable."""
    key = norm_url(url)
    if key in _ARTICLE_CACHE:
        return _ARTICLE_CACHE[key]
    p = os.path.join(RAW, "article_%s.json" % hashlib.sha1(key.encode()).hexdigest()[:16])
    txt = ""
    if os.path.exists(p):
        try:
            with io.open(p, encoding="utf-8") as fh:
                rec = json.load(fh)
            if rec.get("ok"):
                txt = rec.get("text") or ""
        except Exception:
            txt = ""
    _ARTICLE_CACHE[key] = txt
    return txt


def norm_url(u):
    return re.sub(r"[?#].*$", "", str(u or "")).rstrip("/").lower()


def is_article_url(u):
    path = re.sub(r"^https?://[^/]+", "", u or "").strip("/")
    if not path:
        return False
    if re.match(r"^(news|politics|business|world|africa|section|category|tag|author)/?$", path, re.I):
        return False
    return True


def parse_pub(s):
    try:
        return parsedate_to_datetime(s)
    except Exception:
        return None


now_utc = datetime.now(timezone.utc)
CUTOFF = now_utc - timedelta(days=WINDOW_DAYS)

# ─────────────── collect news ───────────────
news, seen, dropped_old, dropped_offtopic = [], set(), 0, 0
dropped_scope = 0
news_files = sorted(glob.glob(os.path.join(RAW, "tavily_news_*.json")))
news_files += sorted(glob.glob(os.path.join(RAW, "gdelt_news_*.json")))
for fn in news_files:
    if fn.endswith("_all.json"):
        continue
    try:
        with io.open(fn, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as exc:
        # A half-written or merge-conflicted cache file must never take the whole
        # page down: skip it, say so, and let the next fetch overwrite it.
        print("skip unreadable raw file %s (%s)" % (os.path.basename(fn), type(exc).__name__))
        continue
    for it in doc.get("results", []):
        u = it.get("url")
        if not u or norm_url(u) in seen or not is_article_url(u):
            continue
        title = clean(it.get("title"))
        if not title or HOMEPAGE_RX.search(title):
            continue
        # Scope gate: sport / entertainment only. Applied to the headline, and
        # skipped for anything naming China or a security actor — those are the
        # two cases where a stray sports word ("coach", "club") would otherwise
        # throw away a story this platform exists to catch.
        if (SPORT_RX.search(title) or ENT_RX.search(title)) \
                and not CN_RX.search(title) \
                and not re.search(r"\b(attack|killed|kidnap|bandit|military|"
                                  r"jihadist|insurgent|Boko|ISWAP|JNIM)\b", title, re.I):
            dropped_scope += 1
            continue
        # Scope gate: a third country named in the headline, with none of the
        # twelve monitored countries. "中国要求公民在泰国注意防范诈骗和绑架" is a
        # real story, but Thailand is not on this monitor's beat — and because it
        # ran on vnexpress.net and khmertimeskh.com it was being filed under
        # 越南 and 缅甸, which is precisely the "周边国家把别国的事算进来" complaint.
        # China / the US are actors here, not monitored countries, so naming them
        # does not exempt an item.
        if _FOREIGN_RX.search(title) \
                and not (set(classify_cty(title, "")) & ALL_CTYS):
            dropped_scope += 1
            continue
        dom = re.sub(r"^www\.", "", re.sub(r"^https?://", "", u).split("/")[0])
        # Tavily's own field is `published_date`, but sources.tavily_search
        # normalises it to `published`. Reading only the first name silently
        # marked every Tavily item as undated, and the 3-day window then dropped
        # the entire Tavily contribution — which is why the page kept showing
        # little more than the RSS leftovers.
        pub = it.get("published_date") or it.get("published") or ""
        dt = parse_pub(pub)
        # Strict window. An item is kept ONLY when it carries a parseable date and
        # that date is inside WINDOW_DAYS at the moment this build runs — i.e. at
        # refresh time, not at some earlier fetch time. Undated items are dropped
        # too: without a date there is no way to show the reader that it is
        # current, and a stale piece presented as fresh is worse than a gap.
        if dt is None or dt < CUTOFF:
            dropped_old += 1
            continue
        seen.add(norm_url(u))
        # Prefer the fetched article body over the search index's `content`,
        # which is frequently only nav chrome plus a related-headlines rail.
        # `src_full` is what the title/summary are built from; the index text is
        # the fallback for URLs the backfill could not retrieve.
        src_full = article_text(u) or it.get("content") or ""
        body = strip_body(src_full, title)
        title = polish_title(title, src_full, u) or title
        summary = summary_of(src_full, title, max_chars=SUMMARY_CHARS)
        # Fallback when the page is paywalled / behind a thin nav-chrome stub
        # (e.g. Businessday NG landing pages return only the header to Tavily).
        # The OLD code computed `c0` — the search index's own description of the
        # story — and then threw it away, writing a fixed "仅返回导航摘要" sentence
        # instead. That is why 22 cards had a summary that told the reader nothing.
        # The index text is a real, on-topic description, so it becomes the
        # summary; the "could not fetch the full text" note is appended after it
        # rather than replacing it.
        summary_zh = ""
        if not summary and it.get("content"):
            c0 = " ".join((it.get("content") or "").split())
            c0 = _cut_at_sentence(c0, SUMMARY_CHARS) if c0 else ""
            if len(c0) >= 40:
                summary = c0
        if not summary:
            # Nothing usable at all: keep the card honest about that instead of
            # presenting the headline as if it were a digest.
            summary = "该来源未返回可读摘要（页面被付费墙或反爬拦截），请点击原文查看。"
            summary_zh = summary
        cty = classify_cty(title, body[:1500], dom, u)
        # Relevance gate. A wire service will happily return a global story that
        # merely shares a token ("oil", "security", "Trump"), and a story page
        # often carries related-article links naming Africa in the body. So the
        # anchor must appear in the TITLE: local outlets name the country or region
        # in the headline, and this is what keeps the priority tiers on topic.
        cty_title = classify_cty(title, "", dom, u)
        if cty_title == ["af"]:
            # Scoped exceptions, both requiring a publisher that actually covers
            # the monitored regions so the tier cannot fill with wire-service
            # world news:
            #  a) China- or US-related headline — in scope by construction even
            #     without a country name ("Ecobank, CIPS partner on China-Africa
            #     financial corridor", punchng.com). The US half is new: a story
            #     like "US and China square off over Pacific islands" names no
            #     single country but is squarely what this monitor is for.
            #  b) the country/region is named in the BODY while the source is a
            #     West African outlet — local outlets run such headlines routinely.
            body_cty = classify_cty(title, body[:1200], dom, u)
            in_scope_source = dom in WA_DOMAINS or dom in REGIONAL_DOMAINS
            if in_scope_source and (CN_RX.search(title) or has_us(title)
                                    or body_cty != ["af"]):
                cty_title = body_cty if body_cty != ["af"] else ["wa"]
            else:
                _tag_cty0 = TAG_CTY.get(doc.get("_tag") or "")
                _tag0_list = (list(_tag_cty0) if isinstance(_tag_cty0, (list, tuple))
                              else ([_tag_cty0] if _tag_cty0 else []))
                # The tag only counts when the outlet or the copy backs it up —
                # otherwise a `cn_mm` angle files a Thai story under Myanmar.
                _tag0_list = [c for c in _tag0_list
                              if _tag_anchor_ok(c, dom, title + " " + body[:400])]
                if _tag0_list:
                    # The angle that found it was country-specific, so the tag is
                    # a better anchor than "unknown" — and dropping the item here
                    # is what made modules look thin.
                    cty_title = _tag0_list
                else:
                    #  c) A headline that is explicitly about China or the US, from
                    #     an outlet whose country is known. "8 Chinese among 18
                    #     arrested in Surigao illegal mining raid" names no state in
                    #     the headline, and its angle (`cn_illegal`) is not
                    #     country-specific, so it was counted as off-topic and
                    #     thrown away — which is precisely the class of story the
                    #     reader asked for (Chinese nationals arrested / attacked /
                    #     fined abroad). The outlet's own country is the anchor.
                    _dom_cty = None
                    for _suf, _code in DOMAIN_CTY.items():
                        if dom == _suf or dom.endswith("." + _suf):
                            _dom_cty = _code
                            break
                    if (_dom_cty and (CN_RX.search(title) or has_us(title))
                            and not _FOREIGN_RX.search(title)):
                        # The FOREIGN check matters: "中国要求公民在泰国注意防范
                        # 诈骗和绑架" ran on khmertimeskh.com and was filed under
                        # 缅甸 purely because of the masthead. Thailand is not one
                        # of the twelve monitored countries, so the story is out of
                        # scope however interesting it is.
                        cty_title = [_dom_cty]
                    else:
                        dropped_offtopic += 1
                        continue
        # Full classification uses the body too; the relaxed gate result is merged
        # in so a China story recovered above stays tagged to the region.
        cty = classify_cty(title, body[:1500], dom, u)
        for c in cty_title:
            if c not in cty:
                cty.append(c)
        # Last-resort anchor from the query tag.
        #
        # "FDI: Plateau, Chinese firm ink $500m deal" is a Nigerian story — Plateau
        # is a Nigerian state — but the headline says "Plateau", not "Nigeria",
        # and the extractor only returned the headline, so no keyword could fire
        # and the item was filed under 泛非洲, which the 非洲四国 module does not
        # show. Since the angle that found it was country-specific ("China Nigeria
        # investment"), the tag is a reliable anchor in exactly this situation.
        # It is applied only when NOTHING more specific was found, so it can never
        # override a real keyword hit, and it does not loosen the relevance gate
        # above (the item must still have passed that).
        # Designated-group flag, computed here because the country anchor below
        # needs it. A JNIM / EIGS story has no China, US or government actor, so
        # it is tier 4 — the band the volume cap used to empty first — and its
        # headline usually names the region ("Sahel") rather than the state.
        ter_hit = terror_hit(title, summary)
        # China-enterprise risk flag, computed here so the 中企风险 chip and
        # the module ordering can both use it.
        cn_risk_hit, cn_risk_kind = cn_risk(title, summary, cty_title)
        _tag_cty = TAG_CTY.get(doc.get("_tag") or "")
        _has_real = bool(set(cty) & ALL_CTYS)
        if _tag_cty and not _has_real:
            _tl = (list(_tag_cty) if isinstance(_tag_cty, (list, tuple)) else [_tag_cty])
            _tl = [c for c in _tl
                   if _tag_anchor_ok(c, dom, title + " " + body[:400])]
            if _tl:
                cty = _tl
        tier, actor, also_us = actor_tier(
            title, summary, is_gov_publisher=(dom in GOV_DOMAINS),
            topics=classify_topic(title, body[:1500]))
        cn_hit, us_hit = subject_hits(title, summary)
        age_days = max(0.0, (now_utc - dt).total_seconds() / 86400.0)
        # Full body for the collapsible reader. Only emitted when the article
        # backfill actually retrieved the page (fulltext) — the search index's
        # `content` fragment is too short to be worth a second section, and
        # paywall stubs must not be presented as "the article". Capped at 4000
        # chars on a sentence boundary so the translation step stays tractable;
        # longer pieces continue at the original link.
        # EVERY card must be able to unfold something: the reader asked for a
        # 「展开翻译全文」button on every single item. When the article page could
        # not be retrieved (paywall / bot wall / RSS-only), fall back to the
        # longest clean text we do have — the search-index excerpt or the RSS
        # description — and label it as an excerpt instead of silently dropping
        # the button.
        # The reader asked for the 展开全文 panel to hold the COMPLETE article, so
        # the ceiling is now BODY_LIMIT (12000) instead of 4000: at 4000 every
        # long-form piece was cut roughly in half, and the panel ended on "…"
        # which is exactly what "点击全文阅读完整" is asking us to fix. 12000
        # characters covers a full wire story or feature; anything longer still
        # continues at the original link, and the cut is labelled.
        full_body, body_level = "", "fulltext"
        if article_text(u):
            fb = _cut_at_sentence(clean(body), BODY_LIMIT)
            if len(fb) >= 150:
                full_body = fb
        if not full_body:
            fb = _cut_at_sentence(clean(body), BODY_LIMIT)
            # Never fall back to a placeholder string: that put
            # "摘要：原文页面仅返回导航摘要…" inside `body`, and the fold then
            # presented it as the article text (124 cards shipped like that).
            if len(fb) < len(summary) and not _PLACEHOLDER_RX.search(summary or ""):
                fb = summary
            full_body = fb
            body_level = "excerpt"
        news.append({
            "title": title,
            "url": u,
            "domain": dom,
            "source": outlet_info(dom)[0],
            "published": pub,
            "_dt": dt.timestamp(),
            "ageDays": round(age_days, 2),
            "summary": summary,
            # Pre-set when upstream filled it (e.g. paywall fallback that is
            # already Chinese); the translation queue respects this and skips.
            "summary_zh": summary_zh,
            "summarySource": "fulltext" if article_text(u) else "index",
            "body": full_body,
            "bodyLevel": body_level,
            "country": cty,
            "topic": classify_topic(title, body[:1500]),
            "tier": tier,
            "actor": actor,
            "alsoUS": also_us,
            "cnHit": cn_hit,
            "usHit": us_hit,
            "terrHit": ter_hit,
            "riskHit": cn_risk_hit,
            "riskKind": cn_risk_kind,
            "score": float(it.get("score") or 0),
            "tag": doc.get("_tag", ""),
        })

# ─────────────── carry-over pool ───────────────
# Items collected on an earlier run are re-admitted here, so the page shows a
# 5-day archive while each fetch only reaches 3 days back. The pool stores the
# fully processed record (including its Chinese fields), so re-admitting an item
# costs nothing — its translations are already attached and the translation
# queue skips anything that carries a _zh slot.
# URL → the search index's own description, harvested from the raw snapshots.
#
# A pooled record keeps only what build_data computed; it does not keep the raw
# `content` field. So once a body extraction failed, the item had neither body nor
# index text available at carry-over time and its summary became a placeholder —
# 68 cards saying "该来源未返回可读摘要" even though the index description was sitting
# in data/raw the whole time. This map restores it at zero cost: no API call, just
# re-reading files that are already committed.
_INDEX_TEXT = {}
for _fn in glob.glob(os.path.join(RAW, "tavily_news_*.json")):
    try:
        with io.open(_fn, encoding="utf-8") as _fh:
            _doc = json.load(_fh)
    except Exception:
        continue
    if not isinstance(_doc, dict):
        continue
    for _it in (_doc.get("results") or []):
        if not isinstance(_it, dict):
            continue
        _u = _it.get("url")
        _c = (_it.get("content") or "").strip()
        if _u and _c:
            _INDEX_TEXT.setdefault(norm_url(_u), _c)
if _INDEX_TEXT:
    print("index text recovered for %d url(s)" % len(_INDEX_TEXT))

_pool_prev = {}
if os.path.exists(POOL_PATH):
    try:
        with io.open(POOL_PATH, encoding="utf-8") as fh:
            _pool_prev = json.load(fh) or {}
    except Exception as e:
        print("pool unreadable, starting empty:", e)

_pool_in = _pool_prev.get("news") or {}
added_from_pool = expired_from_pool = 0
for _k, _item in _pool_in.items():
    if _k in seen:
        continue
    try:
        _dt = datetime.fromtimestamp(_item["_dt"], timezone.utc)
    except Exception:
        continue
    # Extended window for the two narrow exceptions. Everything else still
    # ages out at WINDOW_DAYS.
    _keep_days = WINDOW_DAYS
    _probe = (_item.get("title") or "") + " " + (_item.get("title_zh") or "")
    if NIGER_OIL_RX.search(_probe) and "ne" in (_item.get("country") or []):
        _keep_days = OIL_DAYS
    elif _item.get("riskHit"):
        _keep_days = RISK_DAYS
    if _dt < now_utc - timedelta(days=_keep_days):
        continue
    # Re-age against THIS run's clock; the stored value came from an older one.
    _item["ageDays"] = round(max(0.0, (now_utc - _dt).total_seconds() / 86400.0), 2)
    _item["fromPool"] = True
    # Re-classify carried-over items whose geography is still unresolvable.
    #
    # A pooled record keeps the country list computed when it was first fetched,
    # so any classifier improvement never reaches the items already in the window
    # — they simply sit in 泛非洲 for their remaining days and appear in no
    # geographic module. That is the "some stories are missing" symptom. Only
    # items with NO concrete country are re-run, so a correct earlier result is
    # never disturbed.
    _pc = _item.get("country") or []
    if not (set(_pc) & ALL_CTYS):
        _pct = classify_cty(_item.get("title") or "",
                            (_item.get("body") or "")[:1500])
        _ptag = TAG_CTY.get(_item.get("tag") or "")
        if set(_pct) & ALL_CTYS:
            _item["country"] = _pct
        elif _ptag:
            _item["country"] = [_ptag]
        # cnHit / usHit were added after the pool was first written; recompute so
        # 涉华 / 涉美 membership is right for older items too.
    if "cnHit" not in _item or "usHit" not in _item:
        _c, _u = subject_hits(_item.get("title") or "",
                              _item.get("summary") or _item.get("snippet") or "")
        _item["cnHit"] = _c
        _item["usHit"] = _u
    if "terrHit" not in _item:
        _item["terrHit"] = terror_hit(_item.get("title") or "",
                                      _item.get("summary") or "")
    # Carry-overs were built before the China-risk flag existed, so recompute it
    # rather than leaving the 中企风险 chip empty for the whole first day.
    _rk_hit, _rk_kind = cn_risk(_item.get("title") or "",
                                _item.get("summary") or "",
                                _item.get("country"))
    _item["riskHit"] = _rk_hit
    _item["riskKind"] = _rk_kind
    # Rebuild a missing body from the article cache.
    #
    # fetch_articles caches the extracted text per URL, but a pooled record only
    # carries the `body` that was computed when it was first built. Once that body
    # was empty (or held an old placeholder), the item stayed body-less for the
    # rest of its 3-day window even though the full text was sitting in the cache
    # the whole time — so its 展开全文 panel showed a one-line summary. Re-derive
    # it here; article_text() is a local file read, so this costs nothing.
    _pbody = _item.get("body") or ""
    if (not _pbody.strip()) or _PLACEHOLDER_RX.search(_pbody):
        _raw = article_text(_item.get("url") or "")
        if _raw:
            _nb = _cut_at_sentence(clean(strip_body(_raw, _item.get("title") or "")),
                                   BODY_LIMIT)
            if len(_nb) >= 150:
                _item["body"] = _nb
                _item["bodyLevel"] = "fulltext"
                _item["body_zh"] = ""       # queue it for translation
                _item["summarySource"] = "fulltext"
    # Same for a placeholder summary: rebuild from whatever text is now available
    # — the body if it exists, otherwise the search index's description.
    if _PLACEHOLDER_RX.search(_item.get("summary") or ""):
        _src = _item.get("body") or _INDEX_TEXT.get(norm_url(_item.get("url") or ""), "")
        _ns = summary_of(_src, _item.get("title") or "", max_chars=SUMMARY_CHARS)
        if len(_ns) >= 60:
            _item["summary"] = _ns
            _item["summary_zh"] = ""
            if not (_item.get("body") or "").strip():
                # No article body, but there is a real index description: show it
                # as the foldable text and mark it honestly as an excerpt.
                _item["body"] = _cut_at_sentence(clean(_src), BODY_LIMIT)
                _item["bodyLevel"] = "excerpt"
                _item["body_zh"] = ""
    news.append(_item)
    seen.add(_k)
    added_from_pool += 1
if added_from_pool or expired_from_pool:
    print("pool: +%d carried over, %d aged out" % (added_from_pool, expired_from_pool))

# Strict reverse-chronological order, newest first. The reader asked for a feed
# ordered by time; the tier survives as a badge on every card and as a filter
# chip, and the page offers a 按层级 toggle for anyone who wants the old
# grouping. Time is the primary key and score only breaks exact ties.
news.sort(key=lambda n: (-n["_dt"], -n["score"]))

# Final sanitation pass over BOTH freshly built and pool-carried items.
# The pool keeps the previous round's `body` verbatim, so an article that carried
# a publisher advert in its body kept that advert for the whole 3-day window even
# after strip_body learned to drop it. Re-running the promo purge here fixes
# carried-over entries too; if nothing but promo is left, fall back to the
# summary so the 展开全文 panel still has real Chinese text to show.
_promo_fixed = 0
for _n in news:
    _b = _n.get("body") or ""
    if _b and PROMO_RX.search(_b):
        _cl = purge_promo(_b)
        if len(_cl) >= 160:
            _n["body"] = _cl
        else:
            _n["body"] = _n.get("summary") or ""
            _n["bodyLevel"] = "excerpt"
        _promo_fixed += 1
if _promo_fixed:
    print("promo blocks removed from %d article body/bodies" % _promo_fixed)

# Repair placeholder summaries on pool-carried items.
#
# Older builds wrote a fixed sentence ("摘要：原文页面仅返回导航摘要…") whenever body
# extraction had failed. Those strings are frozen in the pool and would otherwise
# stay on the page for the rest of the 3-day window, telling the reader nothing
# even when a real body had since become available. Rebuild the summary from
# whatever text the item actually has.
# The same placeholder also ended up inside `body` (the old code fell back to
# `body = summary` when extraction failed), so 64 cards showed "摘要：原文页面仅
# 返回导航摘要…" as if it were the article text. Clear those: the fold then shows
# the Chinese summary and says the full text is still being fetched, and the
# retry queue in fetch_articles fills the real body on the next round.
_body_cleared = 0
for _n in news:
    if _PLACEHOLDER_RX.search(_n.get("body") or ""):
        _n["body"] = ""
        _n["body_zh"] = ""
        _n["bodyLevel"] = "excerpt"
        _body_cleared += 1
if _body_cleared:
    print("placeholder bodies cleared: %d (awaiting re-fetch)" % _body_cleared)

_sum_fixed = 0
for _n in news:
    if not _PLACEHOLDER_RX.search(_n.get("summary") or ""):
        continue
    _cand = ""
    for _src in (_n.get("body") or "", _n.get("summary") or ""):
        _src = _src.strip()
        if len(_src) >= 80 and not _PLACEHOLDER_RX.search(_src):
            _cand = summary_of(_src, _n.get("title") or "") or ""
            if len(_cand) >= 60:
                break
            _cand = ""
    if _cand:
        _n["summary"] = _cand
        _n["summary_zh"] = ""      # force re-translation of the better text
        _sum_fixed += 1
if _sum_fixed:
    print("placeholder summaries rebuilt from body: %d" % _sum_fixed)

# ─────────────── volume cap ───────────────
# A WordPress RSS feed publishes everything its newsroom writes, so a free-source
# pass can return 200+ in-window items — mostly routine copy that buries the
# stories this monitor exists for. Tiers 1-3 are kept in full (there are only a
# few dozen at most); tier 4 is trimmed to its newest TIER4_CAP items. This keeps
# the page readable and keeps the hourly translation bill bounded, without ever
# dropping a China / US / government item.
TIER4_CAP = 60


def _oil_asset(it):
    return bool(NIGER_OIL_RX.search((it.get("title") or "") + " " +
                                    (it.get("title_zh") or "")))


_t4_protected = [n for n in news if n["tier"] == 4
                 and (n.get("terrHit") or n.get("riskHit") or _oil_asset(n))]
_t4_other = [n for n in news if n["tier"] == 4
             and not (n.get("terrHit") or n.get("riskHit") or _oil_asset(n))]
# A card with no readable summary is NOT dropped.
#
# Dropping looked tidy but cost too much: 121 cards went, including 34 of the 51
# 涉华 items, because a China story is exactly the kind that lives on a page the
# extractor cannot open. Losing coverage to improve neatness is the wrong trade
# for this monitor. Such items instead get a summary built from the headline, so
# the card still says what the story is about (see _fallback_summary).
for _n in news:
    if _PLACEHOLDER_RX.search(_n.get("summary") or ""):
        _n["summary"] = _fallback_summary(_n)
        _n["summary_zh"] = ""

_keep_t1_3 = [n for n in news if n["tier"] != 4]
_keep_t4 = _t4_protected + _t4_other[:TIER4_CAP]
capped_off = len(news) - len(_keep_t1_3) - len(_keep_t4)
news = _keep_t1_3 + _keep_t4
news.sort(key=lambda n: (-n["_dt"], -n["score"]))

# ─────────────── collect social ───────────────
# ─────────────── social post timestamps ───────────────
# A platform search index does NOT honour a time filter: decoding the 28 X posts
# returned by a "past week" query showed dates spanning 2024-12 to today, i.e.
# most were genuinely old. So the window cannot be trusted to the query — each
# post's own identifier is decoded to recover its exact publish time:
#
#   X          status id       -> (id >> 22) + 1288834974657        [ms]
#   Instagram  shortcode       -> base64url -> media id,
#                                (id >> 23) + 1314220021721         [ms]
#   LinkedIn   activity id     -> (id >> 22) + 0                    [ms]
#   Facebook   no decodable id -> only the "N days ago" caption, when present
#
# Verified: the decoded Instagram times line up exactly with the relative captions
# ("2 days ago" -> 2026-10-13, "19 hours ago" -> 2026-10-14).
IG_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
IG_MAP = {c: i for i, c in enumerate(IG_ALPHABET)}
EP_X = 1288834974657
EP_IG = 1314220021721
EP_LI = 0


def _ms_to_dt(ms):
    if not ms or ms <= 0:
        return None
    dt = datetime.fromtimestamp(ms / 1000.0, timezone.utc)
    # sanity band: reject obviously wrong decodes rather than trusting them
    return dt if 2020 <= dt.year <= 2030 else None


def x_post_time(url):
    m = re.search(r"/(?:status|statuses)/(\d{15,25})", url or "")
    if not m:
        return None
    return _ms_to_dt((int(m.group(1)) >> 22) + EP_X)


def li_post_time(url):
    m = re.search(r"activity[-:]?(\d{15,25})", url or "")
    if not m:
        m = re.search(r"/posts/[^_/?#]+[_-]activity[-:]?(\d{15,25})", url or "")
    if not m:
        return None
    return _ms_to_dt((int(m.group(1)) >> 22) + EP_LI)


def ig_post_time(url):
    m = re.search(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_\-]{6,})", url or "")
    if not m:
        return None
    n = 0
    for ch in m.group(1):
        if ch not in IG_MAP:
            return None
        n = n * 64 + IG_MAP[ch]
    return _ms_to_dt((n >> 23) + EP_IG)


def post_time(platform, url, desc):
    """Exact publish time when derivable, else None."""
    if platform == "x":
        return x_post_time(url)
    if platform == "linkedin":
        return li_post_time(url)
    if platform == "instagram":
        return ig_post_time(url)
    # Facebook: no decodable identifier — fall back to the relative caption only.
    d = social_age_days(desc)
    if d is None:
        return None
    return now_utc - timedelta(days=d)


PLAT_LABEL = {"x": "X", "linkedin": "LinkedIn", "facebook": "Facebook", "instagram": "Instagram"}
social, see_s, dropped_old_s, dropped_undated_s = [], set(), 0, 0
AGE_RX = re.compile(r"(\d+)\s*(minute|hour|day|week|month|year)s?\s+ago", re.I)


def social_age_days(text):
    t = text or ""
    if re.search(r"\b(just now|\d+\s*(?:mins?|minutes?)\s+ago)\b", t, re.I):
        return 0
    if re.search(r"\b(yesterday|\d+\s*hrs?\s+ago|\d+\s*hours?\s+ago)\b", t, re.I):
        return 1
    m = AGE_RX.search(t)
    if not m:
        return None
    n = int(m.group(1))
    return {"minute": 0, "hour": 0, "day": n, "week": n * 7,
            "month": n * 30, "year": n * 365}[m.group(2).lower()]


def x_handle(u):
    m = re.match(r"https?://(?:www\.)?(?:x|twitter)\.com/([^/?#]+)", u)
    h = m.group(1) if m else ""
    return "" if h.lower() in ("i", "home", "search", "hashtag", "intent", "explore") else h


def li_author(u):
    m = re.search(r"/posts/([^_/]+)_", u)
    if m:
        toks = m.group(1).split("-")
        while toks and re.match(r"^[0-9a-f]{6,}$", toks[-1]):
            toks.pop()
        if toks:
            nm = " ".join(t.capitalize() for t in toks if t)
            return re.sub(r"\bPh ?d\b", "PhD", nm), m.group(1)
    m = re.search(r"linkedin\.com/(?:in|company)/([^/?#]+)", u)
    if m:
        return " ".join(t.capitalize() for t in m.group(1).split("-")), m.group(1)
    m = re.search(r"/pulse/([^/?#]+)", u)
    if m:
        toks = [t for t in m.group(1).split("-") if t]
        while toks and re.match(r"^[0-9a-f]{6,}$", toks[-1]):
            toks.pop()
        return " ".join(t.capitalize() for t in (toks[-4:] if len(toks) >= 4 else toks)), m.group(1)
    return "", ""


def fb_page(u):
    m = re.match(r"https?://(?:www\.|m\.|web\.)?facebook\.com/([^/?#]+)", u)
    h = m.group(1) if m else ""
    return "" if h.lower() in ("permalink.php", "story.php", "photo.php", "watch", "share",
                               "sharer", "pages", "groups", "profile.php", "reel") else h


for fn in sorted(glob.glob(os.path.join(RAW, "firecrawl_*.json"))):
    try:
        with io.open(fn, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as exc:
        print("skip unreadable raw file %s (%s)" % (os.path.basename(fn), type(exc).__name__))
        continue
    # The collector's tag lives in the FILENAME (firecrawl_xai_<tag>_<stamp>.json),
    # not in the payload, so parse it here — it is the last-resort country anchor
    # for posts that name no place.
    _query_tag = ""
    _m_tag = re.search(r"firecrawl_xai_([a-z0-9_]+?)_\d{8}_\d{4}\.json$",
                       os.path.basename(fn))
    if _m_tag:
        _query_tag = _m_tag.group(1)
    for plat in doc.get("platforms", []):
        pname = plat.get("name")
        for it in plat.get("results") or []:
            u = it.get("url")
            if not u or norm_url(u) in see_s:
                continue
            title = strip_links(clean(it.get("title")).replace(" - LinkedIn", "").replace(" / X", ""))
            snip = clean(it.get("description"))
            # Social platforms often return the post's first ~80 chars as
            # the "title" and a fuller ~140 chars as the "description". When
            # the title is truncated, the description almost always contains
            # the full opening sentence — promote that to the title so the
            # card doesn't show "…" all the way to the right edge.
            if re.search(r"[.…]\s*$", title):
                # Description may start with a relative-time caption
                # ("3 hours ago · ..." or "16 hours ago · ..."). Strip it
                # before extracting the first sentence.
                snip_clean = re.sub(
                    r"^\s*\d+\s*(?:minute|hour|day|week|month|year)s?\s*ago\s*[·•\-:|]\s*",
                    "", snip, flags=re.I)
                m = re.match(r"^([^.。!?？！\n]{20,160}?[.!?？！\n])", snip_clean)
                if m:
                    cand = m.group(1).strip().rstrip(",;")
                    if 25 <= len(cand) <= 180:
                        title = cand
            # STRICT: a post is kept only if its own publish time is known and
            # inside the window. Items whose time cannot be established are
            # dropped rather than shown undated, because an undated item cannot
            # be distinguished from a stale one — which is exactly the failure
            # the reader reported.
            ts = post_time(pname, u, snip)
            if ts is None:
                dropped_undated_s += 1
                continue
            age_days = (now_utc - ts).total_seconds() / 86400.0
            if age_days > FETCH_DAYS or age_days < -0.5:
                dropped_old_s += 1
                continue
            see_s.add(norm_url(u))
            handle, author = "", ""
            if pname == "x":
                handle = x_handle(u)
                title = re.sub(r"\s*\(?@[\w.]+\)?\s*/?\s*X$", "", title).strip()
                m = re.match(r"^(.*?)\s+on X:\s*[\"“]", title)
                if m:
                    author = m.group(1).strip()
                    rest = title[m.end():].strip().rstrip('"”“')
                    if rest:
                        title = rest
            elif pname == "linkedin":
                author, handle = li_author(u)
            elif pname == "facebook":
                handle = fb_page(u)
                author = handle
            if not title:
                title = snip[:90]
            tier, actor, also_us = actor_tier(
                title, snip, min_hits=2,
                is_gov_publisher=((handle or "").lower() in GOV_HANDLES),
                topics=classify_topic(title + " " + author, snip))
            cn_hit, us_hit = subject_hits(title + " " + author, snip, min_hits=2)
            ter_hit_s = terror_hit(title + " " + author, snip)
            # Country attribution: author text matters here (an account name often
            # names its country where the post copy does not), and the query tag
            # is the last resort — a post surfaced by "China Ghana investment"
            # that mentions no place name at all was previously filed as
            # 泛非洲 and therefore appeared in no geographic module.
            _scty = classify_cty(title + " " + author, snip)
            if not (set(_scty) & ALL_CTYS):
                _stag = SOC_TAG_CTY.get(_query_tag) or TAG_CTY.get(_query_tag)
                if _stag:
                    _scty = list(_stag) if isinstance(_stag, (list, tuple)) else [_stag]
            _rk_s = cn_risk(title, snip, _scty)
            social.append({
                "platform": pname,
                "title": title,
                "url": u,
                "snippet": snip,
                # The unfoldable text. All four platforms return the post copy
                # in `description` (already cleaned into `snip`); this is the
                # fullest text upstream gives us, so the fold shows it and the
                # card keeps a 打开原文 link for the rest.
                "body": snip or title,
                "bodyLevel": "post" if pname == "x" else "excerpt",
                "author": author,
                "handle": ("@" + handle) if handle and pname in ("x", "facebook") else "",
                "country": _scty,
                "topic": classify_topic(title + " " + author, snip),
                "tier": tier,
                "actor": actor,
                "alsoUS": also_us,
                "cnHit": cn_hit,
                "usHit": us_hit,
                "terrHit": ter_hit_s,
                "riskHit": _rk_s[0],
                "riskKind": _rk_s[1],
                "ageDays": round(age_days, 2),
                "_ts": ts.timestamp(),
                "postedAt": ts.astimezone(CST).strftime("%Y-%m-%d %H:%M"),
                "hasBody": (pname == "x" and not it.get("grok_summary")),
                "grokSummary": bool(it.get("grok_summary")),
                "engine": (it.get("source") or plat.get("engine") or
                           ("firecrawl" if pname == "x" else "")),
                "note": ("本卡片为 Grok 综合摘要，非原帖全文；点击查看原帖。" if it.get("grok_summary")
                         else ("" if pname == "x" else
                               "该平台上游仅返回标题与摘要，无法取得正文，请点击原帖查看完整内容。")),
                "tag": doc.get("_tag") or doc.get("query", ""),
            })

# Same rule as the news feed: newest first, straight down the page.
social.sort(key=lambda s: (-s["_ts"], -len(s["snippet"])))

# Carry-over pool for social, on the same two-clock logic as news: a post is
# fetched only while it is inside FETCH_DAYS, but it stays on the page for
# WINDOW_DAYS. Posts are keyed by normalised URL, which is also what the
# per-platform de-duplication uses.
_pool_in_s = _pool_prev.get("social") or {}
added_s = expired_s = 0
for _k, _item in _pool_in_s.items():
    if _k in see_s:
        continue
    try:
        _ts = float(_item["_ts"])
    except Exception:
        continue
    _age = (now_utc - datetime.fromtimestamp(_ts, timezone.utc)).total_seconds() / 86400.0
    if _age > WINDOW_DAYS or _age < -0.5:
        expired_s += 1
        continue
    _item["ageDays"] = round(max(0.0, _age), 2)
    _item["fromPool"] = True
    # Same re-classification as the news pool: carry-overs keep the country list
    # from the run that first fetched them, so classifier fixes never reach them.
    _pc = _item.get("country") or []
    if not (set(_pc) & ALL_CTYS):
        _pct = classify_cty(_item.get("title") or "",
                            _item.get("snippet") or _item.get("body") or "")
        if set(_pct) & ALL_CTYS:
            _item["country"] = _pct
        else:
            _ptag = (SOC_TAG_CTY.get(_item.get("tag") or "")
                     or TAG_CTY.get(_item.get("tag") or ""))
            if _ptag:
                _item["country"] = [_ptag]
    if "cnHit" not in _item or "usHit" not in _item:
        _c, _u = subject_hits(_item.get("title") or "",
                              _item.get("snippet") or "", min_hits=2)
        _item["cnHit"] = _c
        _item["usHit"] = _u
    if "terrHit" not in _item:
        _item["terrHit"] = terror_hit(_item.get("title") or "",
                                      _item.get("snippet") or "")
    _rk_hit, _rk_kind = cn_risk(_item.get("title") or "",
                                _item.get("snippet") or "",
                                _item.get("country"))
    _item["riskHit"] = _rk_hit
    _item["riskKind"] = _rk_kind
    social.append(_item)
    see_s.add(_k)
    added_s += 1
if added_s or expired_s:
    print("pool(social): +%d carried over, %d aged out" % (added_s, expired_s))
social.sort(key=lambda s: (-s["_ts"], -len(s["snippet"])))

# ─────────────── X people profiles (daily xai snapshot) ───────────────
# fetch_people_x.py writes data/raw/xprofiles_YYYYMMDD.json at most once a day.
# build_data only ever reads the LATEST snapshot, so hourly rebuilds reuse it.
profiles, profiles_days = [], 0
prof_files = sorted(glob.glob(os.path.join(RAW, "xprofiles_*.json")))
if prof_files:
    try:
        with io.open(prof_files[-1], encoding="utf-8") as fh:
            prec = json.load(fh)
        profiles = prec.get("profiles") or []
        profiles_days = int(prec.get("profileDays") or 0)
    except Exception as e:
        print("profiles snapshot unreadable:", e)

# ─────────────── translation (agent-filled cache + queue) ───────────────
cache = {}
if os.path.exists(CACHE_PATH):
    try:
        with io.open(CACHE_PATH, encoding="utf-8") as fh:
            cache = json.load(fh)
    except Exception as e:
        print("zh_cache unreadable, starting empty:", e)


def tkey(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


# A cache entry is only a *translation* if it actually contains Chinese. The
# 14:12 build exposed the opposite: 72 entries in zh_cache held the English
# source verbatim (the model echoed the input, or a retry stored the original),
# so every downstream check that asked "is title_zh non-empty?" said 116/116
# while the page showed 52 English headlines. Validation now looks at the
# characters, not at emptiness.
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def has_cn(s):
    return bool(_CJK.search(s or ""))


def zh_ok(en, zh):
    """True when `zh` can stand in for `en` on the page."""
    if not zh or not str(zh).strip():
        return False
    if has_cn(zh):
        return True
    if has_cn(en):                      # source already Chinese — identity is fine
        return True
    # Source with no Latin letters (pure digits, symbols, URLs) needs no work.
    return not re.search(r"[A-Za-z]", en or "")


# ─────────────── link-free headlines, then collapse near-duplicates ───────────────
# Two defects the reader sees immediately, both handled here so that everything
# downstream — the translation queue, the carry-over pool, the coverage stats —
# already works on the cleaned, de-duplicated lists.
#
# 1. A headline carrying its own URL. The social index concatenates the page
#    title and the link, and the translation step keeps the link, so 19 cards
#    showed a raw URL as part of the headline. Stripped from both the original
#    and the translated field.
# 2. The same story arriving twice: two outlets, a news article plus a post that
#    quotes it, or the same post returned by two queries. Compared on character
#    3-grams, which is the only measure that works for Chinese (no word breaks).
#    Threshold 0.68 with at least 8 shared trigrams: measured on the live corpus,
#    every pair above 0.66 was a genuine duplicate and nothing below 0.60 was.
for _it in news + social:
    if _it.get("title"):
        # tidy_headline also drops a masthead the CMS cut in half and repairs a
        # headline truncated inside "U.S." — both survived strip_links alone.
        _it["title"] = tidy_headline(_it["title"])
    if _it.get("title_zh"):
        # The Chinese title is what the card actually shows, so it needs its own
        # pass: the translation keeps the source ellipsis, and a machine
        # translation can trail off on a conjunction.
        _it["title_zh"] = tidy_zh_title(_it["title_zh"])


def _shingles(s, n=3):
    s = re.sub(r"https?://\S+", "", (s or "").lower())
    s = re.sub(r"[^0-9a-z\u4e00-\u9fa5]+", "", s)
    if not s:
        return set()
    if len(s) <= n:
        return {s}
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _title_key(it):
    return (it.get("title_zh") or it.get("title") or "").strip()


# ── event fingerprints, used to veto a false merge ──
# Character trigrams cannot tell "Ogun State Assembly passes state police bill"
# from "Plateau State Assembly passes state police bill": same template, one word
# of substance swapped, 0.67 similar. Merging them would delete a real story.
# The difference is always an entity — a place, a figure, an amount — so two
# headlines may only be treated as the same event when their entities agree.
_NUM_RX = re.compile(r"\d[\d,.]*")
_EVENT_WORDS = sorted({w for _ws in CTY_MARKERS.values() for w in _ws},
                      key=len, reverse=True)

# Country and region names are NOT event fingerprints. Every Nigerian story says
# "Nigeria", so including it would make "Ogun Assembly passes state police bill"
# and "Plateau Assembly passes state police bill" agree on a shared place and be
# merged — deleting one of two real laws. Only sub-national names distinguish
# events, so only those are collected.
_NATION_WORDS = {
    "nigeria", "nigerian", "nigerians", "尼日利亚", "尼联邦",
    "ghana", "ghanaian", "ghanaians", "ghanian", "加纳",
    "mali", "malian", "malien", "马里",
    "niger", "nigerien", "niger's", "niger republic", "republic of niger",
    "niger junta", "niger government", "niger army", "尼日尔",
    "ecowas", "sahel", "sahelian", "west africa", "west african",
    "regional bloc", "alliance of sahel", "aes states", "gulf of guinea",
    "economic community", "coastal states",
    "korea", "korean", "朝鲜", "韩国", "韩方", "韩半岛", "朝鲜半岛",
    "philippines", "philippine", "filipino", "菲律宾", "菲方",
    "myanmar", "burma", "burmese", "缅甸",
    "russia", "russian", "俄罗斯",
    "india", "indian", "印度",
    "nepal", "nepali", "nepalese", "尼泊尔", "尼方",
    "vietnam", "vietnamese", "越南",
    "laos", "lao", "laotian", "老挝", "老方",
    "south china sea", "west philippine sea", "南海",
    "line of actual control", "lac", "实控线",
}

# One place, several spellings. A story moves between outlets and the paper
# switches between the city and the district ("Abuja confirms Ebola case" vs
# "Federal Capital Territory confirms Ebola case") — without this they read as
# two places and the duplicate survives. Keys are folded to lower case.
_ADMIN_ALIAS = {
    # ── Nigeria: 36 states + FCT ──
    "fct": "abuja", "federal capital territory": "abuja", "联邦首都特区": "abuja",
    "阿布贾": "abuja", "abuja": "abuja",
    "拉各斯": "lagos", "港口哈科特": "port harcourt", "哈科特港": "port harcourt",
    "卡诺": "kano", "卡杜纳": "kaduna", "迈杜古里": "maiduguri",
    "博尔诺": "borno", "扎姆法拉": "zamfara", "卡齐纳": "katsina",
    "索科托": "sokoto", "奥贡": "ogun", "翁多": "ondo", "阿南布拉": "anambra",
    "伊莫州": "imo state", "高原州": "plateau state", "贝努埃": "benue",
    "凯比": "kebbi", "包奇": "bauchi", "贡贝": "gombe", "约贝": "yobe",
    "阿达马瓦": "adamawa", "亚当瓦": "adamawa", "塔拉巴": "taraba",
    "纳萨拉瓦": "nasarawa", "科吉": "kogi", "夸拉": "kwara", "奥约": "oyo",
    "埃基蒂": "ekiti", "埃多州": "edo", "三角洲州": "delta state",
    "巴耶尔萨": "bayelsa", "河流州": "rivers state", "阿夸伊博姆": "akwa ibom",
    "十字河": "cross river", "阿比亚": "abia", "埃努古": "enugu",
    "埃博尼": "ebonyi", "吉加瓦": "jigawa", "尼日尔州": "niger state",
    "尼日尔三角洲": "niger delta", "拉各斯州": "lagos",
    # ── Niger / Mali / Ghana ──
    "尼亚美": "niamey", "阿加德兹": "agadez", "阿尔利特": "arlit",
    "塔瓦": "tahoua", "蒂拉贝里": "tillaberi", "津德尔": "zinder",
    "迪法": "diffa", "多索": "dosso",
    "巴马科": "bamako", "通布图": "timbuktu", "加奥": "gao", "基达尔": "kidal",
    "莫普提": "mopti", "塞古": "segou", "锡卡索": "sikasso", "卡耶斯": "kayes",
    "梅纳卡": "menaka", "阿扎瓦德": "azawad",
    "阿克拉": "accra", "库马西": "kumasi", "特马": "tema", "塔马利": "tamale",
    "塔科拉迪": "takoradi", "奥布阿西": "obuasi", "阿散蒂": "ashanti",
    "沃尔特": "volta", "东部地区": "eastern region",
}

_SUB_WORDS = sorted(
    {w for w in _EVENT_WORDS
     if w.lower() not in _NATION_WORDS and w not in _NATION_WORDS},
    key=len, reverse=True)
for _k, _v in _ADMIN_ALIAS.items():
    if _k not in _SUB_WORDS:
        _SUB_WORDS.append(_k)
_SUB_WORDS.sort(key=len, reverse=True)


def _event_marks(t):
    """(places, numbers) named in a string — what makes an event that event.

    PLACES are sub-national only, folded through _ADMIN_ALIAS so "Abuja" and
    "Federal Capital Territory" are the same place. Country names are excluded:
    every Nigerian story says Nigeria, so including them would make two
    different state laws look like one event.
    NUMBERS reject only when both sides carry them and they differ — one write-up
    usually has the death toll and the other does not, but 40 dead and 48 dead are
    two different massacres.
    """
    s = t or ""
    low = s.lower()
    nums = set(_NUM_RX.findall(s))
    places = set()
    for w in _SUB_WORDS:
        if (w in s) if _CJK_RX.search(w) else (w in low):
            places.add(_ADMIN_ALIAS.get(w.lower(), w.lower()))
    return (frozenset(places), frozenset(nums))


# ── event entities ──
# Text similarity cannot see this class of duplicate. Measured on the live page,
# "尼日利亚军方在一系列空袭中击毙103名ISWAP武装分子" and "尼日利亚军方报告称
# 103名伊斯兰国西非省武装分子被击毙" score 0.36 on English summaries and 0.31 on
# Chinese ones — every outlet words the same raid differently, so no threshold
# that survives false positives would catch them. What they DO share is the
# substance: the same toll (103) and the same organisation (ISWAP). That pair is
# the fingerprint.
# NOTE on boundaries: `\b` cannot be used around these abbreviations.
# "ISWAP武装分子" has no word boundary between P and 武 (both are \w), so
# `\biswap\b` missed it — and the miss split one airstrike into three cards.
# Lookarounds limited to ASCII letters do the job in both scripts.
def _abbr_rx(*forms):
    alt = "|".join(forms)
    return re.compile(r"(?<![A-Za-z])(?:" + alt + r")(?![A-Za-z])", re.I)


_ENT_RX = [
    ("iswap", _abbr_rx("iswap", r"islamic state west africa",
                       r"islamic state in west africa",
                       "伊斯兰国西非省", "西非省")),
    ("jnim", _abbr_rx("jnim", r"nusrat al-islam", "伊斯兰支持阵线",
                      "支持伊斯兰与穆斯林")),
    ("eigs", _abbr_rx("eigs", r"islamic state in the greater sahara",
                      "大撒哈拉伊斯兰国")),
    ("mplj", _abbr_rx("mplj", "macina", "马西纳解放阵线", "马西纳")),
    ("fpl", _abbr_rx("fpl", r"front patriotique", "爱国解放阵线")),
    ("boko", _abbr_rx(r"boko\s*haram", "博科圣地")),
    ("lakurawa", _abbr_rx("lakurawa", "拉库拉瓦")),
    ("ebola", _abbr_rx("ebola", "埃博拉")),
    ("mpox", _abbr_rx("mpox", "monkeypox", "猴痘")),
    ("cholera", _abbr_rx("cholera", "霍乱")),
    ("diphtheria", _abbr_rx("diphtheria", "白喉")),
    ("lassa", _abbr_rx("lassa", "拉沙热")),
    ("bandits", _abbr_rx("bandit", "bandits", "匪患", "土匪")),
]
# A number only fingerprints an event when it is specific: a toll, a ransom, an
# amount. Years, small ordinals and round figures are excluded because they
# recur across unrelated stories.
_YEAR_RX = re.compile(r"^(?:19|20)\d\d$")


def _rare_nums(t):
    """Specific figures in a HEADLINE — a toll, a count, an amount.

    Only the title is read. Summaries are 300 characters of prose and almost any
    two of them share some number ("10月20日", "2026"), which made the first
    version of this test merge a Seoul arson arrest with an Ebola alert. A
    headline that repeats a figure is repeating it for a reason.

    Years, day-of-month and anything under 10 are dropped for the same reason.
    """
    out = set()
    s = t or ""
    for m in _NUM_RX.finditer(s):
        n = m.group(0)
        raw = n.replace(",", "").rstrip(".")
        if not raw or _YEAR_RX.match(raw):
            continue
        after = s[m.end():m.end() + 2]
        before = s[max(0, m.start() - 1):m.start()]
        if after[:1] in ("月", "日", "年", "时", "点") or before == "第":
            continue                      # a date or an ordinal, not a count
        try:
            v = float(raw)
        except ValueError:
            continue
        if len(raw) < 2 or v < 10:        # "5 家", "2 名" say nothing
            continue
        out.add(raw)
    return out


def _ents(t):
    return {k for k, rx in _ENT_RX if rx.search(t or "")}


def same_event_strong(a_title, a_body, b_title, b_body):
    """True when two records report the same incident, judged on substance.

    Requires a shared EVENT ENTITY (ISWAP, JNIM, Ebola, bandits …) plus one of:
      * the same specific figure in both headlines — "103 killed" in three
        outlets is one raid;
      * the same sub-national place — two Abujas reporting an Ebola case is one
        announcement.

    An entity on only one side, or two conflicting places, means different
    events: "103 ISWAP killed" and "103 Boko Haram killed" are two raids.
    """
    at, bt = a_title or "", b_title or ""
    ea, eb = _ents(at), _ents(bt)
    # An entity must be shared. Without this the test degenerated into "do these
    # two long summaries share a number", which merged unrelated stories.
    if not (ea & eb):
        return False
    pa = _event_marks(at)[0]
    pb = _event_marks(bt)[0]
    if pa and pb and not (pa & pb):
        return False
    if _rare_nums(at) & _rare_nums(bt):
        return True
    return bool(pa & pb)


def _marks_ok(a, b):
    """May two records with these fingerprints describe the same event?

    A place named on only ONE side is not a conflict — that is the normal shape
    of a follow-up ("…kills 103 ISWAP fighters" vs "…kills 103 ISWAP fighters in
    Borno and Adamawa"). Two DIFFERENT places named on both sides is a conflict,
    and merging there deletes a real story.
    """
    pa, na = a
    pb, nb = b
    if pa and pb and not (pa & pb):
        return False
    if na and nb and na != nb:
        return False
    return True


def _dup_hit(key, sh, kept, th=0.68, floor=8, marks=None):
    """Index in `kept` of the first record that duplicates this headline."""
    if not key and not sh:
        return None
    for j, entry in enumerate(kept):
        pk, ps = entry[0], entry[1]
        if key and pk == key:
            return j
        if not ps or not sh:
            continue
        inter = len(sh & ps)
        if inter < floor or inter / float(len(sh | ps)) < th:
            continue
        # Same template, different place or figure ⇒ different story.
        if marks is not None and len(entry) > 2 and not _marks_ok(entry[2], marks):
            continue
        return j
    return None


def _recency(it):
    """Sort key for "which of these two is the later report" (higher = newer).

    `_dt` is the parsed publication time on news; `_ts` is the decoded post time
    on social. Both are floats, and both are missing on a few records, so the
    fallback is 0 rather than an exception.
    """
    v = it.get("_dt")
    if v is None:
        v = it.get("_ts")
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ─────────────── element-level comparison ───────────────
# Word overlap cannot decide whether two records are the same event: "Gunmen
# abduct two Chinese miners in Kwali" and "两名中国矿工在夸利遭绑架" share no
# tokens at all, and two unrelated shootings share plenty ("police", "killed",
# "Nigeria"). So compare ELEMENTS instead — the atomic facts a story carries:
# who acted, on whom, where, how many, what happened.
#
# The reader's threshold (2026-10-22): about 50% shared elements means the same
# event. Below that, two different things are being described.
_EVT_RX = [
    ("kill",     re.compile(r"\bkill(?:ed|s|ing)?\b|\bdead\b|\bdeath|"
                            r"\bslain\b|\bmurder|击毙|杀死|杀害|死亡|丧生", re.I)),
    ("abduct",   re.compile(r"\bkidnap|\babduct|\bseized\b|\bhostage|绑架|劫持|被绑", re.I)),
    ("arrest",   re.compile(r"\barrest|\bnabbed\b|\bdetain|\bcustody\b|\bjail|"
                            r"逮捕|拘留|被捕|拘押|判刑", re.I)),
    ("attack",   re.compile(r"\battack|\braid\b|\bbomb|\bambush|\bgunmen\b|"
                            r"袭击|遇袭|空袭|伏击|爆炸", re.I)),
    ("protest",  re.compile(r"\bprotest|\bdemonstrat|\briot|\bboycott|抗议|示威|游行", re.I)),
    ("fine",     re.compile(r"\bfined?\b|\bpenalt|\bsanction|罚款|处罚|制裁", re.I)),
    ("summon",   re.compile(r"\bsummon|\bquestioned\b|\bprobe\b|\baudit|约谈|传唤|调查|审查", re.I)),
    ("ban",      re.compile(r"\bban(?:s|ned)?\b|\bhalts?\b|\bsuspend|禁止|暂停|叫停", re.I)),
    ("shortage", re.compile(r"\bshortage|\bqueue|\bscarcity|\bscarcity|短缺|排队|缺油", re.I)),
    ("sign",     re.compile(r"\bsign(?:s|ed)?\b|\bdeal\b|\bagreement|签署|协议|合同", re.I)),
    ("visit",    re.compile(r"\bvisit|\bmeet(?:s|ing)?\b|\btalks\b|访问|会见|会谈", re.I)),
    ("invest",   re.compile(r"\binvest|\bfunding\b|\bplant\b|\bfactory|投资|兴建|建厂", re.I)),
]
_ACTOR_RX = [
    ("cn",     re.compile(r"(?<![A-Za-z])(?:chinese|china)(?![A-Za-z])|中国|中方|中资|中企|华人|华商", re.I)),
    ("us",     re.compile(r"(?<![A-Za-z])(?:united states|american|u\.s\.|us)(?![A-Za-z])|美国|美方|美军", re.I)),
    ("mil",    re.compile(r"\bmilitary\b|\barmy\b|\btroops\b|\bsoldiers?\b|\bair\s?force\b|"
                          r"军方|军队|士兵|空军", re.I)),
    ("police", re.compile(r"\bpolice\b|\bcourt\b|\bjudge\b|警方|法院|法官", re.I)),
    ("gov",    re.compile(r"\bgovern(?:ment|or)\b|\bminist(?:er|ry)\b|\bpresident\b|"
                          r"政府|州长|部长|总统", re.I)),
    ("co",     re.compile(r"\bcompan(?:y|ies)\b|\bfirm\b|\bcorp|公司|企业|集团", re.I)),
]


def _facts(it):
    """(count, set) of the atomic facts a record carries.

    Read from the headline plus the first 400 characters of the summary, in BOTH
    languages, because the Chinese fields are a translation of the same facts and
    a translator sometimes drops a figure the English kept.
    """
    title = it.get("title") or ""
    tz = it.get("title_zh") or ""
    body = (it.get("summary") or it.get("snippet") or "")[:400]
    bz = (it.get("summary_zh") or it.get("snippet_zh") or "")[:400]
    blob = " ".join([title, tz, body, bz])

    out = set()
    # numbers: tolls, amounts, counts — the strongest identity signal there is
    for n in _NUM_RX.findall(blob):
        raw_n = n.replace(",", "").rstrip(".")
        if not raw_n or _YEAR_RX.match(raw_n):
            continue
        try:
            v = float(raw_n)
        except ValueError:
            continue
        if len(raw_n) >= 2 and v >= 10:
            out.add("n:" + raw_n)
    # event verbs
    for name, rx in _EVT_RX:
        if rx.search(blob):
            out.add("e:" + name)
    # actors
    for name, rx in _ACTOR_RX:
        if rx.search(blob):
            out.add("a:" + name)
    # named groups / diseases (reuses the entity table)
    for k in _ents(blob):
        out.add("g:" + k)
    # sub-national places
    for pl in _event_marks(blob)[0]:
        out.add("p:" + pl)
    return len(out), out


def _element_sim(a, b, min_facts=4):
    """Shared-element ratio between two fact sets, 0..1, or 0 when not the event.

    Three conditions, all required. The gates were tuned against 822 live records
    and each one closes a failure I could name:

    1. A PLACE OR A NAMED GROUP must be shared (`p:` / `g:`). Pure figures are
       not enough — "国防部与BEL签署770亿卢比雷达合同" and "特朗普称企业应向社区
       支付红利" scored 0.83 on numbers alone.
    2. At least TWO specific elements shared (place, group or figure). One place
       in common is not identity: two separate Zamfara bandit briefs share
       "zamfara" and "bandits" and nothing else.
    3. Ratio >= th over the UNION. Dividing by the smaller set made any thin
       record score 1.00 against anything that happened to contain it.

    Set sizes under `min_facts` are skipped: four generic elements ("a government
    signed something with China") describe half the page.
    """
    if len(a) < min_facts or len(b) < min_facts:
        return 0.0
    inter = a & b
    if not inter:
        return 0.0
    pg = sum(1 for x in inter if x[0] in ("p", "g"))
    if pg < 1:
        return 0.0
    hard = sum(1 for x in inter if x[0] in ("p", "g", "n"))
    if hard < 2:
        return 0.0
    return len(inter) / float(len(a | b))


def collapse_same_facts(items, th=0.5):
    """Third pass: one event, several write-ups, different words.

    Runs after the shingle passes. Those catch rewrites that keep sentence
    shape; this catches the ones that do not —

      "Gunmen abduct two Chinese miners in Kwali"  ┐
      "两名中国矿工在夸利遭武装分子绑架"            ├ same four elements
      "Police: two Chinese nationals kidnapped"    ┘  (who, what, where, how many)

    At the reader's 0.5 threshold. Of the two records, `_worth_keeping` keeps the
    more complete one, so the survivor is the version that names the town, the
    operator and the toll rather than the one that only confirms a death.
    """
    kept, out, dropped = [], [], 0
    for it in items:
        n, fs = _facts(it)
        hit = None
        if fs:
            for j, prev in enumerate(out):
                if _element_sim(fs, kept[j]) >= th:
                    hit = j
                    break
        if hit is None:
            kept.append(fs)
            out.append(it)
            continue
        dropped += 1
        if _worth_keeping(it) > _worth_keeping(out[hit]):
            out[hit] = it
            kept[hit] = fs
    return out, dropped


def _worth_keeping(it):
    """Which of two records about the SAME event to keep.

    The reader's rule (2026-10-22): "选取一条要素最完善、时间更新最早的" —
    when two records carry the same facts, show the one that carries MORE of
    them, because that is the one that actually explains what happened.

    This replaces the 2026-10-21 rule ("keep the latest"), which fixed one
    problem and created another: the newest wire copy is often the thinnest —
    a one-line "Police confirm three dead" replacing a piece that named the
    town, the operator, the toll and the response. Completeness now leads.

    Ordering, and why each key is there:
      1. `_fact_count` — how many distinct facts the record carries (numbers,
         named groups, places, actors, event verbs). This is "要素最完善".
      2. article over post — a post points at a report, it is not the report.
      3. body length, then summary length — a proxy for how much is readable.
      4. recency — the tie-break, so among equally complete records the newer
         one wins, which keeps the "最新进展" intent of the earlier rule.
    """
    fact_n, _ = _facts(it)
    return (fact_n,
            1 if not it.get("platform") else 0,
            len(it.get("body") or ""),
            len(it.get("summary") or it.get("snippet") or ""),
            _recency(it))


def collapse_near_dups(items, th=0.62, floor=8):
    kept, out, dropped = [], [], 0
    for it in items:
        k, sh = _title_key(it), _shingles(_title_key(it))
        mk = _event_marks(k)
        j = _dup_hit(k, sh, kept, th, floor, mk)
        if j is None:
            kept.append((k, sh, mk))
            out.append(it)
            continue
        dropped += 1
        if _worth_keeping(it) > _worth_keeping(out[j]):
            out[j] = it
            kept[j] = (k, sh, mk)
    return out, dropped


def collapse_same_event(items, th=0.52, floor=14):
    """Second pass: two outlets writing up the SAME event with different wording.

    Two independent tests, either of which is enough:

    1. SUMMARY SIMILARITY — catches rewrites that happen to share sentence
       structure (the Ebola pair scored 0.88 on Chinese summaries).
    2. SUBSTANCE FINGERPRINT (`same_event_strong`) — a shared specific figure
       plus the same organisation or disease. This is what catches the rest:
       five outlets reporting one airstrike word it five different ways, so the
       similarity test sees 0.31 and stops, while all five say "103" and "ISWAP".

    Similarity alone was not enough. Measured on the live page before this pass
    existed, the page carried five cards for one raid on 103 ISWAP fighters and
    four for one Ebola announcement.

    When records merge, `_worth_keeping` keeps the LATEST one: the reader asked
    for "相似的内容只要有最新的进展情况就行了" — the freshest report is the one
    that carries the development.
    """
    kept, out, dropped = [], [], 0
    for it in items:
        body = (it.get("summary") or it.get("snippet") or it.get("body") or "")[:300]
        sh = _shingles(body)
        # The place fingerprint comes from the HEADLINE, not the summary.
        # Summaries of two different state bills both say "尼日利亚 … 州议会 …
        # 法案", so a summary-derived fingerprint merged Ogun's bill into
        # Plateau's. The headline names the place.
        mk = _event_marks(_title_key(it))
        j = _dup_hit("", sh, kept, th, floor, mk)
        if j is None:
            j = _substance_hit(it, out)
        if j is None:
            kept.append(("", sh, mk))
            out.append(it)
            continue
        dropped += 1
        if _worth_keeping(it) > _worth_keeping(out[j]):
            out[j] = it
    return out, dropped


def _substance_hit(it, out):
    """Index in `out` of the record reporting the same incident, by substance."""
    for j, prev in enumerate(out):
        if same_event_strong(
                _title_key(it), it.get("summary") or it.get("snippet") or "",
                _title_key(prev), prev.get("summary") or prev.get("snippet") or ""):
            return j
    return None


# ── country evidence check ──
# A country tag has to be backed by something in the copy: a name, a city, a
# marker the classifier knows — or the query tag that found it. Everything else
# is residue from an earlier, buggier classifier or from a masthead-based guess
# ("koreatimes.co.kr published it, so it must be Korea"). Re-checking every
# record before it is written is the only way corrections reach carry-overs as
# well as fresh items.
def _evidence_for(c, title, body, summary, tag, strict=False):
    """Does the copy support country `c`? `strict` requires the TITLE to say it.

    Strict is used when the record also carries "af" — that means the classifier
    found nothing and the other code came from a query tag. Body text is then
    unusable: a Thai article about travel advisories lists Myanmar among the
    countries being warned about, and that mention alone kept the story in the
    缅甸 module.
    """
    if c in ("af", "wa"):
        return True
    rx = CTY_RX.get(c)
    if rx and (rx.search(title) or (not strict and rx.search(body))):
        return True
    if c in zh_cty_hits(title) or (not strict and (c in zh_cty_hits(body) or c in zh_cty_hits(summary))):
        return True
    if c == "ng" and NG_RE.search(title + ("" if strict else " " + body)):
        return True
    if c == "ne" and NE_ONLY.search(title + ("" if strict else " " + body)):
        return True
    for tbl in (TAG_CTY, SOC_TAG_CTY):
        v = tbl.get(tag or "")
        v = v if isinstance(v, (list, tuple)) else ([v] if v else [])
        if c in v and _tag_anchor_ok(c, "", title if strict else (title + " " + body + " " + summary)):
            return True
    return False


def revalidate_ctry(items, kind="news"):
    """Drop country tags the copy does not support; fall back to the query tag."""
    fixed = 0
    for it in items:
        cur = it.get("country") or []
        # Defensive: an earlier build could store a tag list as one element
        # (`country: [["ne","wa"]]`), which made every lookup below raise
        # TypeError: unhashable type: 'list'. Flatten to plain codes.
        flat = []
        for _c in cur:
            if isinstance(_c, (list, tuple, set)):
                flat.extend([x for x in _c if isinstance(x, str)])
            elif isinstance(_c, str):
                flat.append(_c)
        if flat != cur:
            it["country"] = flat
        cur = flat
        if not cur:
            continue
        title = it.get("title") or ""
        body = (it.get("body") or "")[:1500]
        summary = (it.get("summary") or it.get("snippet") or "")[:600]
        tag = it.get("tag") or it.get("_tag")
        _strict = "af" in cur and len(cur) > 1
        keep = [c for c in cur
                if _evidence_for(c, title, body, summary, tag, _strict)]
        if keep == cur:
            continue
        fixed += 1
        if not keep:
            anc = TAG_CTY.get(tag or "") or SOC_TAG_CTY.get(tag or "")
            anc = anc if isinstance(anc, (list, tuple)) else ([anc] if anc else [])
            # The fallback must obey the same corroboration rule, or it puts
            # back exactly what was just removed: the two items that reached
            # 缅甸 this way were a Thai foreign-ministry story and a US biotech
            # story, both from a `cn_mm` angle with no Myanmar in the copy.
            anc = [c for c in anc
                   if _tag_anchor_ok(c, "", title + " " + body + " " + summary)]
            keep = list(anc) or ["af"]
        it["country"] = keep
    return fixed


queue = {}
for coll, fields, group in ((news, ("title", "summary", "body"), "news"),
                            (social, ("title", "snippet", "body"), "social")):
    for it in coll:
        for field in fields:
            en = it.get(field) or ""
            if not en:
                continue
            # If upstream already filled the zh slot (e.g. paywall fallback that
            # is itself Chinese), keep it and skip the queue lookup. An English
            # string sitting in a _zh slot is *not* a translation — fall through
            # so it gets re-queued instead of sticking forever.
            if zh_ok(en, it.get(field + "_zh")):
                continue
            ck = tkey(en)
            zh = cache.get(ck)
            it[field + "_zh"] = zh if zh_ok(en, zh) else ""
            if not it[field + "_zh"]:
                queue.setdefault(ck, {"key": ck, "group": group, "field": field, "en": en})

news, _dup_news = collapse_near_dups(news)
social, _dup_soc = collapse_near_dups(social)
# Second pass on the summaries: same event, different wording, different outlets.
news, _dup_evt_n = collapse_same_event(news)
social, _dup_evt_s = collapse_same_event(social)
# Third pass on ELEMENTS: same event, different wording, different language.
# The shingle passes compare surface text; this compares who/what/where/how-many,
# at the reader's "要素相似度 50% 左右" (2026-10-22). Operating point 0.55 rather
# than 0.50: on 822 live records 0.50 still merged two pairs that were different
# events (a soldiers-killed-by-IED brief and a bandit clash brief, both tagged
# Zamfara with no figure in common). 0.55 caught every true duplicate pair and
# lost none of them.
news, _dup_el_n = collapse_same_facts(news, th=0.55)
social, _dup_el_s = collapse_same_facts(social, th=0.55)
# Cross-channel: a post that merely restates a headline already on the page reads
# as a repeat, not as a second source. But "latest development wins" applies here
# too — an embassy post late in the evening can carry a development the morning's
# article does not, so the post replaces the article when it is clearly newer.
# "Clearly" = 6 hours, so a same-cycle repost does not displace a real report.
_NEWER_POST_H = 6.0
_news_kept = [(_title_key(n), _shingles(_title_key(n)), _event_marks(_title_key(n)))
              for n in news]
_soc_out, _dup_cross, _cross_repl = [], 0, 0
for _s in social:
    _k, _sh = _title_key(_s), _shingles(_title_key(_s))
    _j = _dup_hit(_k, _sh, _news_kept, 0.68, 8, _event_marks(_k))
    if _j is None:
        _soc_out.append(_s)
        continue
    _dup_cross += 1
    if _recency(_s) - _recency(news[_j]) > _NEWER_POST_H * 3600:
        # The post carries the newer development. Drop the stale ARTICLE and keep
        # the post in its own list: a post has no `domain`, and putting one into
        # `news` crashed the outlet statistics with KeyError: 'domain'. Two lists
        # mean two record shapes, so the swap happens across lists, not inside one.
        news[_j] = None
        _soc_out.append(_s)
        _cross_repl += 1
social = _soc_out
news = [n for n in news if n is not None]
print("near-duplicates collapsed: news %d+%d+%d, social %d+%d+%d, social-vs-news %d (stale article replaced by post %d)"
      % (_dup_news, _dup_evt_n, _dup_el_n, _dup_soc, _dup_evt_s, _dup_el_s,
         _dup_cross, _cross_repl))

# Country tags get one last check against the copy, so a classifier fix reaches
# the carry-overs too — they keep whatever list the run that first fetched them
# produced, and that is how "France to deploy troops" stayed under 朝韩.
_fx_n = revalidate_ctry(news, "news")
_fx_s = revalidate_ctry(social, "social")
print("country tags revalidated: news %d, social %d" % (_fx_n, _fx_s))

# Sport / showbiz, applied at the END so it also reaches posts and carry-overs —
# the entry-point filter only saw fresh news headlines, which is why IU's single
# and an LPGA round were still on the page after the first filter was added.
_soft_n = [n for n in news if is_soft(n.get("title"), n.get("title_zh"))]
_soft_s = [s for s in social if is_soft(s.get("title"), s.get("title_zh"))]
news = [n for n in news if not is_soft(n.get("title"), n.get("title_zh"))]
social = [s for s in social if not is_soft(s.get("title"), s.get("title_zh"))]
print("sport/showbiz dropped: news %d, social %d" % (len(_soft_n), len(_soft_s)))

# Human-interest fluff, same placement and same reason as above: it has to run
# on the final lists, not at the fetch entry point, or carry-overs slip through.
_fluff = [x for x in news + social if is_fluff(x.get("title"), x.get("title_zh"))]
if _fluff:
    _fd = {id(x) for x in _fluff}
    news = [n for n in news if id(n) not in _fd]
    social = [s for s in social if id(s) not in _fd]
    print("human-interest fluff dropped: %d" % len(_fluff))
    for _x in _fluff[:6]:
        print("   ·", (_x.get("title_zh") or _x.get("title") or "")[:56])

# Third-country sweep, applied at the END for the same reason: the entry-point
# gate only sees freshly fetched items, so carry-overs kept their wrong country.
# "中国要求公民在泰国注意防范诈骗和绑架" survived four rebuilds this way.
# Classified WITHOUT the domain on purpose — otherwise the outlet's own country
# (vnexpress → vn) re-admits the story.
_fgn = []
for _n in news + social:
    _t = _n.get("title") or ""
    if _FOREIGN_RX.search(_t) and not (set(classify_cty(_t, "")) & ALL_CTYS):
        _fgn.append(_n)
if _fgn:
    _drop = {id(x) for x in _fgn}
    news = [n for n in news if id(n) not in _drop]
    social = [s for s in social if id(s) not in _drop]
    print("third-country items dropped: %d" % len(_fgn))

# The queue was filled before the collapse, so it still holds strings belonging
# to the records just dropped. Prune it — otherwise every dropped duplicate is
# paid for in translation credits on this run and the next.
_live_en = set()
for _it in news + social:
    for _f in ("title", "summary", "body", "snippet"):
        _v = _it.get(_f)
        if _v:
            _live_en.add(tkey(_v))
queue = {_k: _v for _k, _v in queue.items() if _k in _live_en}

need = len(queue)
# Coverage is measured in Chinese characters, not in "field is non-empty".
have_news = sum(1 for n in news if zh_ok(n.get("title"), n.get("title_zh")))
have_soc = sum(1 for s in social if zh_ok(s.get("title"), s.get("title_zh")))
have_sum = sum(1 for n in news if zh_ok(n.get("summary"), n.get("summary_zh")))
have_snip = sum(1 for s in social if zh_ok(s.get("snippet"), s.get("snippet_zh")))
have_body = sum(1 for n in news if n.get("body"))
have_body_zh = sum(1 for n in news if zh_ok(n.get("body"), n.get("body_zh")))
have_sbody_zh = sum(1 for s in social if zh_ok(s.get("body"), s.get("body_zh")))
print("zh coverage: news titles %d/%d | summaries %d/%d | bodies %d/%d"
      " || social titles %d/%d | snippets %d/%d | bodies %d/%d | queued %d"
      % (have_news, len(news), have_sum, len(news), have_body_zh, have_body,
         have_soc, len(social), have_snip, len(social), have_sbody_zh, len(social),
         need))
with io.open(QUEUE_PATH, "w", encoding="utf-8") as fh:
    json.dump(sorted(queue.values(), key=lambda x: (x["group"], x["field"], x["key"])),
              fh, ensure_ascii=False, indent=1)

# ─────────────── persist the carry-over pool ───────────────
# Written here, after the translation step, so every stored record already
# carries its Chinese fields — re-admitting it next run costs no translation.
# Nothing older than WINDOW_DAYS is kept: the pool is the 5-day archive, not an
# ever-growing store.
_pool_new = {"news": {}, "social": {}}
for _n in news:
    _n.pop("fromPool", None)
    _pool_new["news"][norm_url(_n["url"])] = _n
for _s in social:
    _s.pop("fromPool", None)
    _pool_new["social"][norm_url(_s["url"])] = _s
with io.open(POOL_PATH, "w", encoding="utf-8") as fh:
    json.dump(_pool_new, fh, ensure_ascii=False)
print("pool saved: %d news, %d social (display window %d days)"
      % (len(_pool_new["news"]), len(_pool_new["social"]), WINDOW_DAYS))

# ─────────────── publisher classification ───────────────
ORG_SUB = ["news", "insider", "media", "times", "report", "press", "daily", "post",
           "watch", "agency", "bureau", "group", "bank", "energy", "mining", "mineral",
           "resource", "capital", "global", "world", "africa", "intel", "analytics",
           "consult", "advisory", "technology", "digital", "network", "online", "info",
           "magazine", "journal", "channel", "service", "authority", "commission",
           "foundation", "institute", "association", "union", "ministry", "government",
           "official", "stream", "alert", "monitor", "review", "digest", "herald",
           "tribune", "guardian", "vanguard", "punch", "wire", "desk", "hub", "platform",
           "system", "payment", "materials", "holding", "ventures", "partners", "labs",
           "works", "ltd", "inc", "plc", "fund", "equity", "asset", "properties",
           "logistics", "shipping", "aviation", "petroleum", "refinery", "cement",
           "steel", "pharma", "academy", "clinic", "hospital", "chamber", "federation",
           "cooperative", "society", "trust", "initiative", "project", "programme",
           "program", "council", "assembly", "parliament", "senate", "congress"]
ORG_HINTS = re.compile(r"\b(" + "|".join(ORG_SUB) + r")\b", re.I)
ORG_ACCOUNTS = {"sputnik", "oilprice.com", "middleeast_24", "solid_min_dev",
                "official_onsa", "firstpostin", "dw.africa"}
ROLE_RX = re.compile(
    r"\b(deputy minister|minister|vice president|president|governor|senator|professor|"
    r"prof\.|director[- ]general|managing director|executive director|country director|"
    r"director|chief executive|ceo|cfo|chairman|chairperson|secretary[- ]general|"
    r"ambassador|commissioner|spokesperson|spokesman|coordinator|analyst|researcher|"
    r"economist|editor|correspondent|journalist|consultant|head of [a-z ]{3,30}|"
    r"senior [a-z ]{3,25}|chief [a-z ]{3,25})\b", re.I)


def looks_like_org(name, handle):
    h = re.sub(r"[^a-z0-9]", "", (handle or "").lower())
    if handle and handle.lower() in ORG_ACCOUNTS:
        return True
    if ORG_HINTS.search(name or "") or ORG_HINTS.search(handle or ""):
        return True
    if h and any(s in h for s in ORG_SUB):
        return True
    core = re.sub(r"[^A-Za-z0-9 ]", " ", name or "")
    toks = [t for t in core.split() if t]
    if not toks:
        return None
    person = re.compile(r"^[A-Z][a-z'’\-]+\.?$")
    if len(toks) == 1:
        return False if person.match(toks[0]) else True
    if 2 <= len(toks) <= 4 and all(person.match(t) or re.match(r"^[A-Z]\.?$", t) for t in toks):
        return False
    return True


def role_hint(text):
    """Extract a role ONLY when it is anchored to an organisation.

    A bare role word is far too noisy: scanning raw post text produced matches
    like "Chief is appointed" and "Minister" taken from unrelated sentences.
    Requiring "<role> of/at/for <Org>" keeps just the instances that actually
    state someone's position.
    """
    if not text:
        return ""
    m = re.search(
        r"\b(deputy minister|minister|vice president|president|governor|senator|"
        r"professor|director[- ]general|managing director|executive director|"
        r"country director|chief executive|chairman|chairperson|secretary[- ]general|"
        r"ambassador|commissioner|spokesperson|country manager|country director)\s+"
        r"(?:of|at|for)\s+([A-Z][\w&.\-' ]{2,44})", text)
    if not m:
        return ""
    return "%s（%s）" % (m.group(1).title(), m.group(2).strip().rstrip(".,"))


# ─────────────── publisher-name hygiene ───────────────
JUNK_SUFFIX = re.compile(r"\s+[0-9a-z]{4,6}$")


def tidy_name(name, handle):
    """Clean search-index artefacts out of a page name.

    Facebook page URLs and mangled LinkedIn slugs yield things like
    "Gas Pipeline Advances 0gibe", "Trade Yash Singh F7icc" or a bare page id
    "100093193603041". The trailing token is a hash fragment, not part of a name,
    so it is removed where a real name remains.
    """
    nm = clean(name or "")
    nm = re.sub(r"\s*[-|]\s*(Facebook|LinkedIn|X|Instagram)\s*$", "", nm, flags=re.I)
    toks = nm.split()
    if len(toks) >= 2:
        last = toks[-1]
        # a hash fragment carries a digit and no vowel pattern; a real surname
        # ("Buah", "Makama", "Simons") does not
        if re.match(r"^[0-9a-z]{4,6}$", last) and any(c.isdigit() for c in last):
            nm = " ".join(toks[:-1])
    nm = nm.strip(" -|·")
    return nm


def is_junk_subject(name, handle):
    """Reject entries that are not identifiable as a person or organisation."""
    nm = clean(name or "")
    hnd = (handle or "").strip("@")
    if not nm:
        return True
    if re.fullmatch(r"\d{6,}", hnd) or re.fullmatch(r"[\d\s]+", nm):
        return True
    if re.fullmatch(r"\d{6,}", nm):
        return True
    # a sentence-like fragment rather than a name
    if len(nm.split()) >= 4 and len(re.findall(r"\b(?:is|was|are|were|has|have|will|to|of|the|and)\b",
                                               nm, re.I)) >= 2:
        return True
    return False
    if tail:
        return s.title() + " " + tail.group(1) + " " + tail.group(2).strip(" .,")
    return s[0].upper() + s[1:]


people, plat_counts, dropped_people = {}, {}, 0
for it in social:
    h = it["handle"].lstrip("@") or it["author"]
    if not h:
        continue
    plat_counts.setdefault((it["platform"], h), []).append(it)

# ── duties.json: authored job descriptions, keyed by normalised name ──
DUTIES = {}
_dp = os.path.join(ROOT, "data", "duties.json")
if os.path.exists(_dp):
    try:
        with io.open(_dp, encoding="utf-8") as fh:
            DUTIES = {re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", k.lower()): v
                      for k, v in json.load(fh).items() if not k.startswith("_")}
    except Exception as e:
        print("duties.json unreadable:", e)

TOPIC_CN = {"pol": "政治", "eco": "经济", "sec": "安全", "ene": "能源", "min": "矿产"}
CTY_CN = {"ng": "尼日利亚", "ne": "尼日尔", "ml": "马里", "gh": "加纳",
          "wa": "西非／萨赫勒区域", "af": "非洲整体"}
ROLE_LEAD = {
    "媒体机构": "该媒体的编辑部",
    "机构": "机构账号",
    "个人": "个人账号",
    "未确定": "公开账号",
}


def duty_for(kind, name, ctys, tops, samples, role_hint_text):
    """A one-line "what this subject does" description.

    Prefers an authored description from duties.json. Otherwise it is composed
    from what the account actually published (country + topic distribution) and
    from the subject matter of its own posts — never from an assumption about the
    person's employer or politics.
    """
    key = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", (name or "").lower())
    if key in DUTIES:
        return DUTIES[key]

    cty_txt = "、".join(CTY_CN[c] for c in ctys if c in CTY_CN) or "西非地区"
    top_txt = "、".join(TOPIC_CN[t] for t in tops if t in TOPIC_CN) or "综合"
    if kind == "媒体机构":
        return ("该媒体的编辑部，负责%s等国家与地区政治、经济、安全、能源、矿产领域新闻的"
                "采访、编辑与发布；本次采集到其公开报道。" % cty_txt)
    if kind == "机构":
        subj = _subject_phrase(samples)
        lead = "关注%s的%s议题" % (cty_txt, top_txt)
        return ("机构账号，%s%s。其发布内容可作为该机构对外表态的参考。"
                % (lead, ("，主要涉及" + subj) if subj else ""))
    subj = _subject_phrase(samples)
    base = "公开账号（主体性质待核验）" if kind == "未确定" else "个人账号"
    rh = ("公开资料显示其职务为%s；" % role_hint_text) if role_hint_text else ""
    return ("%s。%s发布内容聚焦%s的%s议题%s。其职务与所属机构以本人公开档案页为准。"
            % (base, rh, cty_txt, top_txt, ("，主要涉及" + subj) if subj else ""))


def _subject_phrase(items, limit=40):
    """A short Chinese subject phrase taken from the account's own post titles.

    Prefers the translated title so the sentence stays in one language; a raw
    English fragment truncated mid-word reads as an error, so it is only used if
    no translation exists.
    """
    if not items:
        return ""
    cands = []
    for it in items:
        for field in ("title_zh", "title"):
            t = re.sub(r"^[#\d\s.·\-]+", "", (it.get(field) or "")).strip()
            t = re.sub(r"\s*[-–|]\s*(Facebook|LinkedIn|X|Instagram)\s*$", "", t, flags=re.I)
            t = re.sub(r"[\.…\s]+$", "", t)
            # Skip titles that only identify the account itself ("…在 X 上",
            # "… on X"); they carry no subject information.
            if re.search(r"(在\s*(X|Facebook|LinkedIn|Instagram)\s*上|on\s+X$|的帖文|的帖子)", t):
                break
            if 10 <= len(t) <= 60 and not re.fullmatch(r"[A-Za-z0-9\s|·.\-]+", t):
                cands.append(t)
                break
    if not cands:
        return ""
    return max(cands, key=len)[:limit]

for (plat, h), items in plat_counts.items():
    ctys, tops = set(), set()
    for i in items:
        ctys |= set(i["country"])
        tops |= set(i["topic"])
    raw_name = items[0]["author"] or h
    name = tidy_name(raw_name, h) or raw_name
    # Drop search-index artefacts: bare page ids, hashed fragments, and
    # sentence-shaped strings are not identifiable publishers.
    if is_junk_subject(name, h):
        dropped_people += 1
        continue
    org_flag = looks_like_org(name, h)
    v = VERIFIED_ACCOUNTS.get(h)
    if v:
        kind, role = "机构", v[2]
    elif org_flag is True:
        kind, role = "机构", "机构账号 / 信息发布方"
    elif org_flag is False:
        kind, role = "个人", "个人账号 / 公开帖文发布者"
    else:
        kind, role = "未确定", "公开账号（主体性质待核验）"
    prof = ("https://x.com/" + h) if plat == "x" else \
           ("https://www.facebook.com/" + h) if plat == "facebook" else items[0]["url"]
    key = re.sub(r"[^a-z0-9]", "", (v[0] if v else name).lower()) or h.lower()
    rec = people.setdefault(key, {
        "name": v[0] if v else name, "type": kind, "role": role,
        "org": (v[0] if v else (name if kind == "机构" else "")),
        "country": set(), "profiles": [], "items": 0, "topics": set(),
        "roleHint": "", "verified": bool(v),
        "verifiedNote": ("机构官方账号，名称与机构公开称谓一致" if v else
                         "账号身份来自搜索引擎索引，未回原页二次确认"),
        "_samples": [],
    })
    rec["country"] |= ctys
    rec["topics"] |= tops
    rec["items"] += len(items)
    if len(rec["_samples"]) < 6:
        rec["_samples"].extend(items[:2])
    if not any(p["url"] == prof for p in rec["profiles"]):
        rec["profiles"].append({"platform": plat, "url": prof,
                                "handle": ("@" + h) if plat in ("x", "facebook") else ""})
    if not rec["roleHint"]:
        rec["roleHint"] = role_hint(" ".join(i["title"] + " " + i["snippet"] for i in items[:3]))

out_counts = {}
for n in news:
    # A social post can now replace a news article (the newer-development rule in
    # the cross-channel pass), and a post carries `platform`/`handle` rather than
    # `domain`. Reading the key directly crashed the whole build with
    # KeyError: 'domain'; such a record simply has no outlet to register.
    _dom = n.get("domain")
    if not _dom:
        continue
    out_counts[_dom] = out_counts.get(_dom, 0) + 1
for dom, cnt in sorted(out_counts.items(), key=lambda kv: -kv[1]):
    nm, cy, page = outlet_info(dom)
    is_contact = bool(re.search(r"contact", page, re.I))
    people["outlet::" + dom] = {
        "name": nm + " 编辑部", "type": "媒体机构", "role": "报道发布方（编辑部）",
        "org": nm, "country": {"af"} if cy == "af" else {cy},
        "profiles": [{"platform": "news", "url": page, "handle": ""}],
        "items": cnt, "topics": set(), "roleHint": "",
        "verified": True,
        "verifiedNote": "机构公开页面，本次已实测可访问" if is_contact else "机构主页，本次已实测可访问",
        "basis": "本次采集自该媒体公开报道 %d 条" % cnt,
    }

ORDER_T = {"news": 0, "x": 1, "linkedin": 2, "facebook": 3, "instagram": 4}
CTY_ORDER = {"ng": 0, "ne": 1, "ml": 2, "gh": 3, "wa": 4, "af": 5}
people_out = []
for rec in people.values():
    rec["country"] = sorted(rec["country"], key=lambda c: CTY_ORDER.get(c, 9))
    rec["topics"] = sorted(rec["topics"])
    rec["profiles"].sort(key=lambda p: ORDER_T.get(p["platform"], 9))
    if "basis" not in rec:
        rec["basis"] = "本次采集自该账号公开帖文 %d 条（%s）" % (
            rec["items"], "、".join(PLAT_LABEL[p["platform"]] for p in rec["profiles"]))
    if rec["type"] == "媒体机构":
        rec["contact"] = rec["profiles"][0]["url"] + (
            "（机构对外联络页）" if "contact" in rec["profiles"][0]["url"] else "（机构主页）")
    else:
        plats = "、".join(PLAT_LABEL[p["platform"]] for p in rec["profiles"])
        rec["contact"] = "经 %s 公开主页联络" % plats if plats else "—"
    rec["duty"] = duty_for(rec["type"], rec["name"], rec["country"], rec["topics"],
                           rec.pop("_samples", []), rec.get("roleHint", ""))
    people_out.append(rec)

people_out.sort(key=lambda r: ({"媒体机构": 3, "机构": 2, "未确定": 1, "个人": 0}[r["type"]],
                               -r["items"]))

# ─────────────── highlights (tier-ordered) ───────────────
def usable(n):
    return 38 <= len(n["title"]) <= 150 and not re.search(r"[—–|]\s*$", n["title"])


tc = {1: 0, 2: 0, 3: 0, 4: 0}
for n in news:
    tc[n["tier"]] += 1

hl = []
for tno in (1, 2, 3):
    pick = next((n for n in news if n["tier"] == tno and usable(n)), None)
    if pick:
        hl.append("%s｜%s（%s，%s）" % (TIER_LABEL[tno], pick["title"][:62], pick["source"],
                                        (pick["published"] or "")[:16]))
hl.append("本期归集：媒体条目 %d 条（涉华 %d、美国 %d、当事国政府 %d、其他报道 %d；来自 %d 家媒体）、"
          "社交平台动态 %d 条、可追溯发布主体 %d 个。时间窗口：近 %d 日。"
          % (len(news), tc[1], tc[2], tc[3], tc[4], len(out_counts), len(social),
             len(people_out), WINDOW_DAYS))

data = {
    "generatedAt": datetime.now(CST).isoformat(timespec="seconds"),
    "appName": "China Eyes",
    "window": "近 %d 日" % WINDOW_DAYS,
    "windowDays": WINDOW_DAYS,
    "translation": {"newsTitles": have_news, "newsTotal": len(news),
                    "socialTitles": have_soc, "socialTotal": len(social),
                    "newsBodies": have_body, "newsBodiesZh": have_body_zh,
                    "newsSummariesZh": have_sum,
                    "socialBodies": sum(1 for s in social if s.get("body")),
                    "socialBodiesZh": have_sbody_zh,
                    "socialSnippetsZh": have_snip,
                    "pending": need},
    "tierCounts": tc,
    "tierLabel": TIER_LABEL,
    "tierDesc": TIER_DESC,
    "windowStrict": True,
    "orderMode": "tier_first",
    "highlights": hl,
    "news": news,
    "social": social,
    "people": people_out,
    "profiles": profiles,
    "profileDays": profiles_days,
}

with io.open(os.path.join(ROOT, "data.js"), "w", encoding="utf-8") as fh:
    fh.write("window.SITE_DATA = ")
    json.dump(data, fh, ensure_ascii=False, indent=1)
    fh.write(";\n")

print("news=%d (cn=%d us=%d gov=%d other=%d) | dropped: older-than-window-or-undated=%d off-topic=%d out-of-scope=%d"
      % (len(news), tc[1], tc[2], tc[3], tc[4], dropped_old, dropped_offtopic, dropped_scope))
print("volume cap: dropped %d oldest tier-4 item(s) (cap %d)" % (capped_off, TIER4_CAP))
print("social=%d | dropped: older-than-window=%d undated=%d | outlets=%d | people=%d"
      % (len(social), dropped_old_s, dropped_undated_s, len(out_counts), len(people_out)))
print("translation: news titles %d/%d | social titles %d/%d | pending strings=%d"
      % (have_news, len(news), have_soc, len(social), need))
print("bodies: news %d/%d (zh %d) | social %d/%d (zh %d)"
      % (sum(1 for n in news if n.get("body")), len(news),
         sum(1 for n in news if n.get("body_zh")),
         sum(1 for s in social if s.get("body")), len(social),
         sum(1 for s in social if s.get("body_zh"))))
_full = sum(1 for n in news if n.get("summarySource") == "fulltext")
_idx = sum(1 for n in news if n.get("summarySource") == "index")
_none = sum(1 for n in news if not (n.get("summary") or "").strip())
print("summaries: fulltext=%d index=%d none=%d of %d | avg len=%.0f"
      % (_full, _idx, _none, len(news),
         (sum(len(n.get("summary") or "") for n in news) / len(news)) if news else 0))
