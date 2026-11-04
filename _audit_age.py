# -*- coding: utf-8 -*-
"""时效性验收：data.js 里每一条都必须落在 windowDays 之内。

背景（用户 2026-11-04 的两条意见，其实是同一个 bug 的两面）：
  「新闻实效性没有在三天以内，有的内容已经超过了3天有效期」
  「抓取到的新闻、动态在这个平台上展示三天后不会自动删除」

判定标准只有一条：以构建时刻为基准，每条的发布时间距现在不得超过
windowDays。超过就是不合格，不管它是本轮新抓的、还是从 pool 结转的。

用法：
    python _audit_age.py            # 查本地 data.js
    python _audit_age.py _live.js   # 查指定文件
    python _audit_age.py --live     # 现拉线上 data.js 再查

退出码 1 表示存在超期条目。
"""
import io
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

LIVE = "https://ellisonblackpeak-bit.github.io/africa-eyes/data.js"
SRC = "data.js"

if "--live" in sys.argv:
    req = urllib.request.Request(LIVE + "?t=%d" % int(datetime.now().timestamp()),
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        io.open("_live_data.js", "w", encoding="utf-8").write(
            r.read().decode("utf-8", "replace"))
    SRC = "_live_data.js"
elif len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
    SRC = sys.argv[1]

if not os.path.exists(SRC):
    print("找不到 %s" % SRC)
    sys.exit(2)

raw = io.open(SRC, encoding="utf-8").read()
data = json.loads(raw.split("window.SITE_DATA = ", 1)[1].rstrip().rstrip(";"))
days = data.get("windowDays") or 3

# 用文件里的生成时刻做基准，而不是本机当前时间：这份 data.js 是那一轮构建的
# 产物，判它合不合格要按那一轮的时钟。否则拿一份两小时前的快照来查，会把
# 本来合格、只是「现在看又老了两小时」的条目算成超期。
gen = data.get("generatedAt") or ""
try:
    ref = datetime.fromisoformat(gen)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
except Exception:
    ref = datetime.now(timezone.utc)

print("文件: %s" % SRC)
print("生成: %s" % gen)
print("窗口: %d 天" % days)

bad_total = 0
for key in ("news", "social"):
    arr = data.get(key) or []
    rows, no_ts = [], []
    for it in arr:
        ts = it.get("_dt") if key == "news" else it.get("_ts")
        if not isinstance(ts, (int, float)):
            no_ts.append(it)
            continue
        age = (ref.timestamp() - float(ts)) / 86400.0
        rows.append((age, it))
    rows.sort(key=lambda t: -t[0])
    over = [r for r in rows if r[0] > days]
    bad_total += len(over) + len(no_ts)
    print("\n=== %s: %d 条 ===" % (key, len(arr)))
    print("  最旧 %.2f 天 / 最新 %.2f 天" % (rows[0][0], rows[-1][0]) if rows
          else "  (无带时间戳条目)")
    print("  超 %d 天: %d 条 | 无时间戳: %d 条" % (days, len(over), len(no_ts)))
    for age, it in over[:10]:
        print("   ✗ %5.1f 天 | %s" % (age, (it.get("title_zh") or it.get("title") or "")[:60]))
    for it in no_ts[:5]:
        print("   ✗ 无时间 | %s" % (it.get("title_zh") or it.get("title") or "")[:60])

print("\n结论: %s" % ("全部在窗口内" if not bad_total else "有 %d 条超期/无时间" % bad_total))
sys.exit(1 if bad_total else 0)
