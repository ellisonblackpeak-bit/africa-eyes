"""检查同一事件被拆成多张卡的情况，并验证 collapse_same_event 的合并效果。

用法：
    python _audit_dups.py _live_data.js

不做任何写操作，只读 data.js 快照、抽取 build_data.py 里的真实去重函数来跑。
"""
import ast
import io
import json
import re
import sys
from datetime import datetime, timezone

SNAP = sys.argv[1] if len(sys.argv) > 1 else "_live_data.js"
SRC = "build_data.py"

WANT = ["collapse_same_event", "same_event_strong", "_substance_hit", "_dup_hit",
        "_worth_keeping", "_title_key", "_shingles", "_event_marks", "_ents",
        "_rare_nums", "_marks_ok", "_recency", "_abbr_rx", "_fold",
        "_ENT_RX", "_NUM_RX", "_YEAR_RX", "_SUB_WORDS", "_ADMIN_ALIAS",
        "_CJK_RX", "_STOP", "_PUNCT_RX", "_WS_RX"]

src = io.open(SRC, encoding="utf-8").read()
tree = ast.parse(src)
ALL = {}
for node in tree.body:
    name = None
    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
        name = node.targets[0].id
    elif isinstance(node, ast.FunctionDef):
        name = node.name
    if name:
        ALL[name] = node

keep, have = [], set()
for w in WANT:
    if w in ALL:
        keep.append(ALL[w])
        have.add(w)
missing = [w for w in WANT if w not in ALL]
if missing:
    print("(源码里没有: %s)" % ", ".join(missing))

# 按 ast.Name 的 Load 引用自动补依赖，最多 60 轮
names = {id(x) for x in keep}
for _ in range(60):
    used = {s.id for n in keep for s in ast.walk(n)
            if isinstance(s, ast.Name) and isinstance(s.ctx, ast.Load)}
    add = [k for k in used if k in ALL and id(ALL[k]) not in names]
    if not add:
        break
    for k in add:
        keep.append(ALL[k])
        names.add(id(ALL[k]))

ns = {"re": re, "datetime": datetime, "timezone": timezone,
      "print": lambda *a, **k: None}
# 只按 ast.Name 补依赖是不够的：模块级常量的右值会立即求值（_EVENT_WORDS
# 依赖 CTY_MARKERS），exec 阶段就报 NameError，而不是等到调用时。所以补完
# 静态引用后再循环重试，按报错名字继续捞，最多 60 轮。
# ⚠ 每次 exec 前必须按行号排序：抽取出来的节点是乱序的，而
# `_EVENT_WORDS = sorted(CTY_MARKERS...)` 在 4006 行、`CTY_MARKERS` 在 804 行，
# 顺序反了就永远报 NameError，补依赖也补不好。
for _ in range(60):
    keep.sort(key=lambda n: getattr(n, "lineno", 0))
    try:
        exec(compile(ast.Module(body=keep, type_ignores=[]), "<extract>", "exec"), ns)
        break
    except NameError as e:
        missing_name = str(e).split("'")[1]
        if missing_name not in ALL:
            sys.exit("抽取失败，源码里找不到 %s" % missing_name)
        keep.append(ALL[missing_name])
        names.add(id(ALL[missing_name]))
else:
    sys.exit("依赖补齐超过 60 轮，放弃")

collapse = ns["collapse_same_event"]

raw = io.open(SNAP, encoding="utf-8").read()
data = json.loads(raw.split("window.SITE_DATA = ", 1)[1].rstrip().rstrip(";"))

AFR = {"ng", "ne", "ml", "gh"}


def groups(items):
    """把条目按 country 粗分组——去重是按模块跑的，跨模块不去重。"""
    out = {}
    for it in items:
        c = it.get("country") or []
        if isinstance(c, str):
            c = [c]
        key = tuple(sorted(set(c) & (AFR | {"cn", "us"}))) or ("other",)
        out.setdefault(key, []).append(it)
    return out


total_before = total_after = 0
detail = []
for k in ("news", "social"):
    arr = data.get(k) or []
    total_before += len(arr)
    for gk, grp in groups(arr).items():
        merged, dropped = collapse(grp)
        total_after += len(merged)
        if dropped:
            detail.append((k, gk, len(grp), dropped))

print("按模块分组跑 collapse_same_event：")
for k, gk, n, d in detail:
    print("  %-5s %-22s %3d → %3d  （合并 %d）" % (k, ",".join(gk), n, n - d, d))
print("\n合计 %d → %d，合并 %d 条" % (total_before, total_after,
                                    total_before - total_after))

# ── 用中文标题的 3-gram 反查还剩多少同事件多卡 ──
def grams(s):
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s or "")
    return {s[i:i + 3] for i in range(len(s) - 2)}


print("\n合并后仍高度相似的标题对（>=0.60）：")
allt = []
for k in ("news", "social"):
    arr = data.get(k) or []
    for gk, grp in groups(arr).items():
        merged, _ = collapse(grp)
        allt.extend((it.get("title_zh") or it.get("title") or "") for it in merged)
n = 0
for i in range(len(allt)):
    gi = grams(allt[i])
    if len(gi) < 6:
        continue
    for j in range(i):
        gj = grams(allt[j])
        if len(gj) < 6:
            continue
        inter = len(gi & gj)
        if inter >= 8 and inter / float(len(gi | gj)) >= 0.60:
            n += 1
            print("  %.2f\n    A %s\n    B %s" % (inter / float(len(gi | gj)),
                                                 allt[j][:64], allt[i][:64]))
print("剩余相似对：%d" % n)
