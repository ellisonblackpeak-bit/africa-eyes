# -*- coding: utf-8 -*-
"""过滤器误杀审计：把所有抓到的原始条目跑一遍过滤规则，逐条列出被丢的。"""
import json, glob, io, os, re, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "build_data.py"), encoding="utf-8").read()

# 只取正则与三个判定函数，不执行 build_data 主体
a = src.index("SPORT_RX = re.compile")
b = src.index("def is_off_scope")
ns = {"re": re}
c0 = src.index("CN_RX = re.compile")
c1 = src.index("# NOTE: the country abbreviation")
exec(src[c0:c1], ns)
exec(src[a:b], ns)
k = src.index("def is_off_scope"); k2 = src.index("\n\n\n", k); exec(src[k:k2], ns)
soft, fluff, off = ns["is_soft"], ns["is_fluff"], ns["is_off_scope"]

# ── 收集所有原始抓取条目 ──
seen, items = set(), []
for f in glob.glob(os.path.join(HERE, "data", "raw", "*.json")):
    base = os.path.basename(f)
    if base.startswith("article_") or base.startswith("_prev_round") or base.startswith("_"):
        continue
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    rs = d.get("results") if isinstance(d, dict) else d
    for r in rs or []:
        if not isinstance(r, dict):
            continue
        u = (r.get("url") or "").strip()
        t = (r.get("title") or "").strip()
        if not u or not t or u in seen:
            continue
        seen.add(u)
        items.append({"title": t, "url": u, "summary": r.get("content") or "",
                      "src": base})
print("原始条目（去重后）: %d" % len(items))

# ── 逐条判定 ──
hits = {"soft": [], "fluff": [], "off": []}
for it in items:
    t, s = it["title"], it["summary"]
    if soft(t):
        hits["soft"].append(it)
    elif fluff(t):
        hits["fluff"].append(it)
    elif off(t, "", s):
        it["why"] = _why = None
        for name in ("LOCAL_CRIME_RX", "FR_CRIME_RX", "CIVIL_RX", "FR_CIVIL_RX",
                     "HEALTH_RX", "FR_HEALTH_RX"):
            m = ns[name].search(t)
            if m:
                it["why"] = "%s:%s" % (name.replace("_RX", ""), m.group(0))
                break
        hits["off"].append(it)

print("\n文体娱乐 丢弃 %d 条" % len(hits["soft"]))
print("人文软闻 丢弃 %d 条" % len(hits["fluff"]))
print("治安/事故/疫情/民事 丢弃 %d 条" % len(hits["off"]))
print("合计 %d / %d = %.1f%%"
      % (len(hits["soft"]) + len(hits["fluff"]) + len(hits["off"]), len(items),
         100.0 * (len(hits["soft"]) + len(hits["fluff"]) + len(hits["off"])) / max(1, len(items))))

print("\n" + "=" * 70)
print("【治安/事故/疫情/民事】逐条 —— 重点核对有没有误杀")
for it in hits["off"]:
    print("[%-16s] %s" % (it.get("why") or "?", it["title"][:78]))
    print("                   %s" % it["src"])

print("\n" + "=" * 70)
print("【文体娱乐】逐条")
for it in hits["soft"]:
    print("  ·", it["title"][:80])

print("\n" + "=" * 70)
print("【人文软闻】逐条")
for it in hits["fluff"]:
    print("  ·", it["title"][:80])
