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
           "尼日利亚", "尼联邦", "阿布贾", "拉各斯", "卡诺", "卡杜纳", "高原州",
           "迈杜古里", "博尔诺", "扎姆法拉", "蒂努布"],
    "ne": ["nigerien", "niamey", "tiani", "somair", "orano", "agadez", "tillaberi",
           "imouraren", "cnsp", "bazoum", "niger republic", "republic of niger",
           "niger's", "niger junta", "niger government", "niger army",
           "尼日尔", "尼亚美", "阿加德兹", "阿尔利特", "奇亚尼"],
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
           "Korean nuclear", "朝鲜", "韩国", "平壤", "首尔", "韩半岛", "朝鲜半岛", "韩方"],
    "ph": ["philippines", "philippine", "filipino", "manila", "marcos", "duterte",
           "philippine navy",
           "south china sea", "west philippine sea", "scarborough", "spratly",
           "palawan", "philippine coast guard", "bfar", "菲律宾", "马尼拉",
           "马科斯", "杜特尔特", "南海", "菲方"],
    "mm": ["myanmar", "burma", "burmese", "naypyidaw", "yangon", "mandalay", "rangoon",
           "junta", "tatmadaw", "min aung hlaing", "nug", "pdf myanmar",
           "rohingya", "wa state army", "kokang", "缅甸", "仰光", "内比都",
           "敏昂莱", "若开军", "果敢"],
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


def _rx(words):
    words = sorted(set(words), key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


TOPIC_RX = {k: _rx(v) for k, v in TOPIC_MARKERS.items()}
CTY_RX = {k: _rx(v) for k, v in CTY_MARKERS.items()}

# Every country code the page actually has a module or a filter chip for.
ALL_CTYS = {"ng", "ne", "ml", "gh", "kr", "ph", "mm", "ru", "in", "np", "vn", "la"}

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


def classify_cty(title, body, dom=""):
    out = [c for c, rx in CTY_RX.items()
           if 3 * len(rx.findall(title)) + len(rx.findall(body))]
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
    # signal that always survives: a story on vnexpress.net is about Vietnam.
    # Applied only as a last resort, so it never overrides a real keyword hit.
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
# corridors only, sport and entertainment are dropped by headline before
# classification runs. Kept deliberately narrow: only unambiguous markers, so a
# genuine security story mentioning "players" or "coach" is not lost.
SPORT_RX = re.compile(
    r"\b(football|soccer|NFF|Super Eagles|Falcons|Eagles|coach|stadium|"
    r"matchday|tennis|basketball|boxing|athletics|olympic|FIFA|CAF|NPFL|"
    r"referee|goalkeeper|striker|midfielder|hat-trick|derby|fixture|"
    r"club|cricket|rugby|golf|chess|scrabble|wrestling|transfer window|"
    r"league table|qualifier|semi-final|quarter-final|world cup|afcon)\b", re.I)
ENT_RX = re.compile(
    r"\b(nollywood|movie|film|album|singer|musician|actor|actress|celebrity|"
    r"comedy|concert|beauty pageant|big brother|reality show|fashion week)\b", re.I)

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
    t = clean(title)
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
        cty = classify_cty(title, body[:1500], dom)
        # Relevance gate. A wire service will happily return a global story that
        # merely shares a token ("oil", "security", "Trump"), and a story page
        # often carries related-article links naming Africa in the body. So the
        # anchor must appear in the TITLE: local outlets name the country or region
        # in the headline, and this is what keeps the priority tiers on topic.
        cty_title = classify_cty(title, "", dom)
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
            body_cty = classify_cty(title, body[:1200], dom)
            in_scope_source = dom in WA_DOMAINS or dom in REGIONAL_DOMAINS
            if in_scope_source and (CN_RX.search(title) or has_us(title)
                                    or body_cty != ["af"]):
                cty_title = body_cty if body_cty != ["af"] else ["wa"]
            else:
                _tag_cty0 = TAG_CTY.get(doc.get("_tag") or "")
                if _tag_cty0:
                    # The angle that found it was country-specific, so the tag is
                    # a better anchor than "unknown" — and dropping the item here
                    # is what made modules look thin.
                    cty_title = (list(_tag_cty0)
                                 if isinstance(_tag_cty0, (list, tuple)) else [_tag_cty0])
                else:
                    dropped_offtopic += 1
                    continue
        # Full classification uses the body too; the relaxed gate result is merged
        # in so a China story recovered above stays tagged to the region.
        cty = classify_cty(title, body[:1500], dom)
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
        _tag_cty = TAG_CTY.get(doc.get("_tag") or "")
        _has_real = bool(set(cty) & ALL_CTYS)
        if _tag_cty and not _has_real:
            cty = list(_tag_cty) if isinstance(_tag_cty, (list, tuple)) else [_tag_cty]
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
            if len(fb) < len(summary):
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
            "score": float(it.get("score") or 0),
            "tag": doc.get("_tag", ""),
        })

# ─────────────── carry-over pool ───────────────
# Items collected on an earlier run are re-admitted here, so the page shows a
# 5-day archive while each fetch only reaches 3 days back. The pool stores the
# fully processed record (including its Chinese fields), so re-admitting an item
# costs nothing — its translations are already attached and the translation
# queue skips anything that carries a _zh slot.
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
    if _dt < CUTOFF:
        expired_from_pool += 1
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
_PLACEHOLDER_RX = re.compile(
    r"仅返回导航摘要|未返回可读摘要|该来源未返回|原文页面仅返回", re.I)
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
TIER4_CAP = 30
# Designated-group items bypass the tier-4 cap entirely.
#
# JNIM / EIGS / MPLJ / ISWAP / Boko Haram / Lakurawa stories have no China, US or
# government actor, so they are tier 4 — and the 30-item cap threw them away
# first, which is why the page showed essentially no terror coverage even though
# the fetch had it. They are the reader's explicit requirement, so they are never
# capped; the cap now only trims routine regional copy.
_t4_terror = [n for n in news if n["tier"] == 4 and n.get("terrHit")]
_t4_other = [n for n in news if n["tier"] == 4 and not n.get("terrHit")]
_keep_t1_3 = [n for n in news if n["tier"] != 4]
_keep_t4 = _t4_terror + _t4_other[:TIER4_CAP]
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
            title = clean(it.get("title")).replace(" - LinkedIn", "").replace(" / X", "")
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
    out_counts[n["domain"]] = out_counts.get(n["domain"], 0) + 1
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
