# -*- coding: utf-8 -*-
"""需求覆盖核验：把用户 2026-10-26 明确的五类口径逐条对账。

用法： python _audit_coverage.py
读 data.js，输出每一条需求命中的条数、样例行，以及「不该收录」的残留。

判定依据直接取 build_data.py 里的真实函数与正则，不另写一套，
否则核验与生产会漂移。
"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "build_data.py"), encoding="utf-8").read()

ns = {"re": re}
ns["TOPIC_MARKERS"] = {}
exec(src[src.index('CN_RX = re.compile(r"""('):src.index("US_ABBR = re.compile")], ns)
exec(src[src.index("CTY_MARKERS = {"):src.index("def classify_cty")], ns)
_k = src.index("def classify_cty")
exec(src[_k:src.index("\n\n\n", _k)], ns)
exec(src[src.index("TERROR_RX = re.compile("):src.index("def terror_hit")], ns)
exec(src[src.index("def terror_hit"):src.index("\n\n\n", src.index("def terror_hit"))], ns)
exec(src[src.index("SPORT_RX = re.compile"):src.index("def is_off_scope")], ns)
_k2 = src.index("def is_off_scope")
exec(src[_k2:src.index("\n\n\n", _k2)], ns)

CN = ns["CN_RX"]; ASSET = ns["CN_ASSET_RX"]; TERROR = ns["terror_hit"]
SOFT = ns["is_soft"]; OFF = ns["is_off_scope"]; ZH = ns["zh_cty_hits"]
US_ABBR = ns["US_ABBR"] if "US_ABBR" in ns else None
if US_ABBR is None:
    _u = src.index("US_ABBR = re.compile")
    exec(src[_u:src.index("\n", _u)], ns)
    US_ABBR = ns["US_ABBR"]
US_RX = re.compile(r"\bamericans?\b|\bamerican\b|\bpentagon\b|\btrump\b|\bbiden\b|"
                   r"\busaid\b|\bafricom\b|\bexxon\b|\bchevron\b|\boccidental\b|"
                   r"\bunited\s+states\b|\bwashington\b|\bstate\s+department\b|"
                   r"\bwhite\s+house\b|\bwall\s+street\b|\bsilicon\s+valley\b|"
                   r"\bmillennium\s+challenge\b", re.I)
US_ZH = re.compile(r"美国|美方|美军|华盛顿|白宫|五角大楼|特朗普|拜登|美国国务院|非洲司令部")
AFRICA4 = {"ng", "ne", "ml", "gh"}
PERI = {"kr", "ph", "mm", "ru", "in", "np", "vn", "la"}
EU = {"eu"}
# 用户第二条的七个领域
DOMAIN_RX = re.compile(
    r"\b(politic\w*|government|parliament|election|minister|diplomat\w*|"
    r"econom\w*|trade|invest\w*|finance\w*|budget|inflation|gdp|"
    r"defence|defense|military|army|navy|troops|weapons?|arms|missile\w*|"
    r"security|insurgen\w*|militant\w*|attack\w*|airstrike\w*|bomb\w*|"
    r"energy|gas|lng|oil|petrol\w*|pipeline\w*|refinery|power|electric\w*|"
    r"mining|mine|minerals?|rare\s+earth\w*|lithium|cobalt|nickel|copper|"
    r"uranium|gold|port\w*|rail\w*|road\w*|corridor|shipping|strait|"
    r"supply\s+chain|sanction\w*|tariff\w*|export\s+control)\b|"
    r"政治|政府|议会|选举|部长|外交|经济|贸易|投资|财政|预算|通胀|"
    r"国防|防务|军事|军队|海军|空军|武器|军售|导弹|安全|恐袭|袭击|爆炸|"
    r"能源|天然气|石油|汽油|管道|炼油|电力|电网|"
    r"矿产|矿山|稀土|锂|钴|镍|铜|铀|黄金|港口|铁路|公路|走廊|航运|海峡|"
    r"供应链|制裁|关税|出口管制", re.I)

# ── 读 data.js ──────────────────────────────────────────────────────────────
raw = io.open(os.path.join(HERE, "data.js"), encoding="utf-8").read()
body = raw.split("window.SITE_DATA = ", 1)[1].rstrip().rstrip(";")
D = json.loads(body)
news = D.get("news") or []
social = D.get("social") or []
allit = news + social


def txt(it):
    return " ".join([it.get("title") or "", it.get("title_zh") or "",
                     it.get("summary") or "", it.get("summary_zh") or "",
                     it.get("snippet") or "", it.get("snippet_zh") or "",
                     it.get("body") or "", it.get("body_zh") or ""])


def ttl(it):
    return " ".join([it.get("title") or "", it.get("title_zh") or ""])


def is_cn(it):
    if it.get("cnHit") is True:
        return True
    t = txt(it)
    return bool(CN.search(t) or ASSET.search(t))


def is_us(it):
    if it.get("usHit") is True:
        return True
    t = txt(it)
    return bool(US_RX.search(t) or US_ZH.search(t) or US_ABBR.search(t))


def cty(it):
    return set(it.get("country") or [])


def show(label, items, n=3):
    print("  %-34s %d" % (label, len(items)))
    for it in items[:n]:
        print("      · %s" % (it.get("title_zh") or it.get("title") or "")[:76])


print("=" * 78)
print("data.js 共 %d 条（新闻 %d + 动态 %d）" % (len(allit), len(news), len(social)))
print("=" * 78)

# ── ① 涉华：非洲四国 + 周边国家 ────────────────────────────────────────────
c1 = [x for x in allit if is_cn(x) and (cty(x) & (AFRICA4 | PERI))]
print("\n【需求一】非洲四国 + 周边国家 涉中国人/中企/中国项目")
show("命中条数", c1)

# ── ② 七大领域 + 涉美互动 ─────────────────────────────────────────────────
c2a = [x for x in allit if (cty(x) & (AFRICA4 | PERI)) and DOMAIN_RX.search(txt(x))]
c2b = [x for x in allit if is_us(x) and (cty(x) & (AFRICA4 | PERI))]
print("\n【需求二】非洲四国 + 周边国家的政治/经济/国防/安全/能源/矿产/通道")
show("命中条数", c2a)
print("        其中涉美互动")
show("命中条数", c2b)

# ── ③ 恐组织 + 中资能源资产 ───────────────────────────────────────────────
GRP = [("JNIM", r"jnim|支持伊斯兰与穆斯林"), ("MPLJ", r"mplj|马西纳"),
       ("EIGS", r"eigs|大撒哈拉"), ("ISSP", r"issp|萨赫勒省"),
       ("ISIS", r"\bisis\b|伊斯兰国"), ("ISGS", r"isgs"),
       ("AQIM", r"aqim|马格里布基地"), ("Boko Haram", r"boko\s*haram|博科圣地"),
       ("ISWAP", r"iswap|西非省"), ("FLA", r"\bfla\b|阿扎瓦德解放"),
       ("MNLA", r"mnla|阿扎瓦德民族"), ("MAA", r"\bmaa\b|阿扎瓦德阿拉伯"),
       ("Lakurawa", r"lakurawa|拉库拉瓦")]
print("\n【需求三】非洲四国 · 恐怖组织")
c3 = [x for x in allit if (cty(x) & AFRICA4) and TERROR(ttl(x), x.get("summary_zh") or "")]
show("命中条数", c3)
for name, rx in GRP:
    r = re.compile(rx, re.I)
    hit = [x for x in allit if (cty(x) & AFRICA4) and r.search(txt(x))]
    print("      %-12s %d" % (name, len(hit)))
ASSETS = [("中石油尼日尔/CNPC", r"cnpc|中石油|中国石油"),
          ("津德尔炼油厂/SORAZ", r"soraz|津德尔|zinder"),
          ("西非原油管道/WAPCo", r"wapco|西非原油管道|西非天然气管道|west african pipeline"),
          ("尼日尔-贝宁管道", r"niger[-\s]?benin|benin[-\s]?niger|尼日尔[-—]?贝宁"),
          ("Agadem/Bilma", r"agadem|阿加德姆|bilma|比尔马")]
print("\n【需求三】非洲四国 · 中资能源资产")
for name, rx in ASSETS:
    r = re.compile(rx, re.I)
    hit = [x for x in allit if (cty(x) & AFRICA4) and r.search(txt(x))]
    print("      %-22s %d" % (name, len(hit)))

# ── ④ 欧洲模块 ────────────────────────────────────────────────────────────
c4 = [x for x in allit if cty(x) & EU]
c4a = [x for x in c4 if is_cn(x)]
c4b = [x for x in c4 if is_us(x)]
c4c = [x for x in c4 if cty(x) & PERI]
print("\n【需求四】欧洲模块")
show("命中条数", c4)
print("      ① 涉华（含欧盟对中企政策）")
show("命中条数", c4a, 2)
print("      ③ 涉美互动")
show("命中条数", c4b, 2)
print("      ④ 与中国周边国家互动")
show("命中条数", c4c, 2)

# ── ⑤ 不该收录的残留 ──────────────────────────────────────────────────────
bad_soft = [x for x in allit if SOFT(x.get("title"), x.get("title_zh"))]
bad_off = [x for x in allit if OFF(x.get("title"), x.get("title_zh"), x.get("summary"))]
print("\n【排除项】用户明确不要的（应为 0）")
show("娱乐/文体 残留", bad_soft)
show("本国人犯罪/伤亡/民事/疾病 残留", bad_off)

# ── 中文完整性 ────────────────────────────────────────────────────────────
def zh_ok(en, zh):
    if not (zh or "").strip():
        return False
    if re.search(r"[\u4e00-\u9fff]", zh):
        return True
    return not re.search(r"[A-Za-z]", en or "")


no_zh_t = [x for x in allit if not zh_ok(x.get("title"), x.get("title_zh"))]
no_zh_s = [x for x in allit if (x.get("summary") or x.get("snippet")) and
          not zh_ok(x.get("summary") or x.get("snippet"),
                    x.get("summary_zh") or x.get("snippet_zh"))]
url_t = [x for x in allit if re.search(r"https?://|\.com|\.ng|\.fr|\.de", x.get("title_zh") or "")]
print("\n【质量】")
show("标题非中文", no_zh_t, 2)
show("摘要非中文", no_zh_s, 2)
show("标题含网址/域名", url_t, 2)

# ── 3-gram 近似重复 ───────────────────────────────────────────────────────
def grams(s):
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", s or "")
    return {s[i:i + 3] for i in range(len(s) - 2)}


tg = [(x, grams(x.get("title_zh") or x.get("title"))) for x in allit]
dups = []
for i in range(len(tg)):
    for j in range(i + 1, len(tg)):
        a, b = tg[i][1], tg[j][1]
        if not a or not b:
            continue
        inter = len(a & b)
        if inter >= 8 and inter / len(a | b) >= 0.68:
            dups.append((tg[i][0], tg[j][0]))
show("3-gram 近似重复对", [{"title_zh": a.get("title_zh") or a.get("title")} for a, b in dups], 4)

print("\n" + "=" * 78)
print("结论：① %d 条　② %d 条（涉美 %d）　③ 恐 %d 条　④ 欧洲 %d 条"
      % (len(c1), len(c2a), len(c2b), len(c3), len(c4)))
print("      不该收录残留：文体 %d + 犯罪民事疾病 %d = %d"
      % (len(bad_soft), len(bad_off), len(bad_soft) + len(bad_off)))
print("      非中文标题 %d　非中文摘要 %d　标题含网址 %d　重复对 %d"
      % (len(no_zh_t), len(no_zh_s), len(url_t), len(dups)))
print("=" * 78)
