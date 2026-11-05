# -*- coding: utf-8 -*-
"""线上验收：尼日利亚 / 尼日尔 / 尼日利亚的尼日尔州，到底分清了没有。

用法：
    python _audit_niger.py            # 拉线上 data.js 审计
    python _audit_niger.py 文件.js     # 审指定快照

判据（任何一条不满足即视为未通过）：
  1. 任何 *_zh 字段里不得出现「尼日利亚州」（不存在的行政区划）；
  2. 中文标题出现「尼日尔州」的卡片，country 必须含 ng，且 place == '尼日尔州'；
  3. 中文标题出现「尼日尔河三角洲」的卡片，country 必须含 ng；
  4. 同时挂 ng 与 ne 的卡片逐条打印，人工确认是不是真的两国并列；
  5. 标题含 "Niger State" 的英文条目，country 必须含 ng。
"""
import io
import json
import re
import sys
import urllib.request
from datetime import datetime

LIVE = "https://ellisonblackpeak-bit.github.io/africa-eyes/data.js"

if len(sys.argv) > 1:
    raw = io.open(sys.argv[1], encoding="utf-8").read()
    src = sys.argv[1]
else:
    req = urllib.request.Request(LIVE + "?t=%d" % int(datetime.now().timestamp()),
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read().decode("utf-8", "replace")
    io.open("_live_data.js", "w", encoding="utf-8").write(raw)
    src = LIVE

data = json.loads(raw.split("window.SITE_DATA = ", 1)[1].rstrip().rstrip(";"))
items = (data.get("news") or []) + (data.get("social") or [])
print("文件: %s" % src)
print("生成: %s  新闻 %d / 社交 %d"
      % (data.get("generatedAt"), len(data.get("news") or []), len(data.get("social") or [])))
print()

bad = 0

# 1. 「尼日利亚州」不该存在
hits = []
for it in items:
    for f in ("title_zh", "summary_zh", "body_zh", "snippet_zh"):
        if "尼日利亚州" in (it.get(f) or ""):
            hits.append((f, it.get("title") or ""))
if hits:
    bad += 1
    print("✗ 「尼日利亚州」仍有 %d 处：" % len(hits))
    for f, t in hits[:10]:
        print("    [%s] %s" % (f, t[:80]))
else:
    print("✓ 全文没有「尼日利亚州」")

# 2/3. 中文提到尼日尔州 / 三角洲的卡片
for key, want_place in (("尼日尔州", "尼日尔州"), ("尼日尔河三角洲", "尼日尔河三角洲")):
    rows = [it for it in items if key in (it.get("title_zh") or "")]
    bad_rows = [it for it in rows if "ng" not in (it.get("country") or [])
                or (want_place == "尼日尔州" and it.get("place") != "尼日尔州")]
    print("%s 标题含「%s」的卡片 %d 条，其中 %d 条国别/地区不对"
          % ("✓" if not bad_rows else "✗", key, len(rows), len(bad_rows)))
    for it in bad_rows[:8]:
        print("    country=%s place=%s | %s"
              % (it.get("country"), it.get("place"), (it.get("title_zh") or "")[:60]))
    for it in rows[:6]:
        print("    · %s ｜ %s ｜ %s"
              % (",".join(it.get("country") or []), it.get("place") or "-",
                 (it.get("title_zh") or "")[:58]))
    if bad_rows:
        bad += 1

# 4. 同时挂 ng 与 ne 的卡片
dual = [it for it in items
        if {"ng", "ne"} <= set(it.get("country") or [])]
print("同时挂「尼日利亚」「尼日尔」的卡片 %d 条（应只剩真正的两国并列）：" % len(dual))
for it in dual[:12]:
    print("    · %s" % (it.get("title") or it.get("title_zh") or "")[:88])
    print("      zh: %s" % (it.get("title_zh") or "")[:70])

# 5. 英文标题写了 Niger State 的条目
ns = re.compile(r"\bniger\s+state\b(?!\s*[-–—]\s*owned)", re.I)
wrong = [it for it in items if ns.search(it.get("title") or "")
         and "ng" not in (it.get("country") or [])]
print("%s 英文标题含 \"Niger State\" 的条目 %d 条，判错 %d 条"
      % ("✓" if not wrong else "✗",
         len([it for it in items if ns.search(it.get("title") or "")]), len(wrong)))
for it in wrong[:8]:
    print("    country=%s | %s" % (it.get("country"), (it.get("title") or "")[:80]))
if wrong:
    bad += 1

print()
print("结论: %s" % ("全部通过" if not bad else "有 %d 项未通过" % bad))
sys.exit(1 if bad else 0)
