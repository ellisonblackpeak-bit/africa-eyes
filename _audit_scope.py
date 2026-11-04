# -*- coding: utf-8 -*-
"""白名单口径审计：拿真实 data.js 跑一遍 is_africa_off_topic，逐条列出会被删掉的内容。

为什么要单独一个脚本：测试文件里那 72 条是我自己编的句子，只能证明「我想到的
情况判对了」，证明不了「线上的稿子会怎么判」。真正的风险永远来自没想到的写法，
所以必须拿全量真实数据过一遍，人眼扫一遍删掉的东西。

用法：
    python _audit_scope.py                # 用本地 data.js
    python _audit_scope.py _live_data.js  # 用指定快照
    python _audit_scope.py --live         # 先拉线上快照再跑
"""
import ast
import io
import json
import re
import sys
import urllib.request
from datetime import datetime

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

# 只抽取真正要用的那几个定义，避免 import build_data 触发整条流水线
src = io.open("build_data.py", encoding="utf-8").read()
tree = ast.parse(src)
WANT = {
    "CN_RX", "CN_RX_INTL", "CN_ASSET_RX", "TERROR_RX", "NIGER_OIL_RX",
    "POLICY_ANCHOR_RX", "ECON_RX", "ILLEGAL_MINING_RX", "DEFENSE_POLICY_RX",
    "AFR4_CTYS", "US_RX", "US_ABBR", "has_us", "is_africa_off_topic",
}
keep = []
for node in tree.body:
    name = None
    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
        name = node.targets[0].id
    elif isinstance(node, ast.FunctionDef):
        name = node.name
    if name in WANT:
        keep.append(node)
ns = {"re": re}
exec(compile(ast.Module(body=keep, type_ignores=[]), "<extract>", "exec"), ns)
OFF = ns["is_africa_off_topic"]
AFR4 = set(ns["AFR4_CTYS"])

raw = io.open(SRC, encoding="utf-8").read()
data = json.loads(raw.split("window.SITE_DATA = ", 1)[1].rstrip().rstrip(";"))

print("文件: %s" % SRC)
print("生成: %s" % data.get("generatedAt"))
print("四国代码: %s" % ",".join(sorted(AFR4)))

total_afr = 0
dropped = []
kept_afr = []
for key in ("news", "social"):
    for it in data.get(key) or []:
        cty = it.get("country") or []
        if isinstance(cty, str):
            cty = [cty]
        if not (set(cty) & AFR4):
            continue
        total_afr += 1
        if OFF(it.get("title"), it.get("title_zh"), it.get("summary"), cty):
            dropped.append((key, it))
        else:
            kept_afr.append((key, it))

print("\n四国条目 %d 条 → 保留 %d，删除 %d（%.1f%%）"
      % (total_afr, len(kept_afr), len(dropped),
         100.0 * len(dropped) / max(1, total_afr)))

print("\n=== 会被删掉的（请人眼确认没有误杀）===")
for key, it in dropped:
    t = (it.get("title_zh") or it.get("title") or "")[:78]
    print("  [%s/%s] %s" % (key[:4], ",".join(it.get("country") or []), t))

print("\n=== 保留样本（随机 15 条，确认口径没放宽）===")
import random
random.seed(7)
for key, it in random.sample(kept_afr, min(15, len(kept_afr))):
    t = (it.get("title_zh") or it.get("title") or "")[:78]
    print("  [%s/%s] %s" % (key[:4], ",".join(it.get("country") or []), t))
