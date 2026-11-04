# -*- coding: utf-8 -*-
"""年龄闸门的单元测试：直接 exec build_data.py 里那段代码，不重写逻辑。

为什么要 exec 而不是复制一份判断：闸门是内联在 build_data.py 主流程里的，
复制一份就等于测了一份假的实现。这里把源文件里
「final age gate」到「persist the carry-over pool」之间的代码原样取出来执行，
所以测的就是线上跑的那段。

用法： python _test_age.py     期望：全部 PASS，退出码 0。
"""
import io
import sys
from datetime import datetime, timedelta, timezone

src = io.open("build_data.py", encoding="utf-8").read()
_a = src.index("# ─────────────── final age gate")
_b = src.index("# ─────────────── persist the carry-over pool")
GATE = src[_a:_b]

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)
D = 86400.0


def ts(days_ago):
    return NOW.timestamp() - days_ago * D


fail = 0
total = 0


def check(label, got, want):
    global fail, total
    total += 1
    ok = got == want
    if not ok:
        fail += 1
    print("%s  %-34s want=%-6s got=%-6s"
          % ("PASS" if ok else "FAIL", label, want, got))


def run(news, social, days=3):
    ns = {
        "now_utc": NOW, "WINDOW_DAYS": days, "timedelta": timedelta,
        "news": news, "social": social,
        "print": lambda *a, **k: None,
    }
    exec(compile(GATE, "<gate>", "exec"), ns)
    return ns["news"], ns["social"]


# ── ① 新闻：边界 ────────────────────────────────────────────────────────
news = [
    {"url": "fresh",    "_dt": ts(0.5)},
    {"url": "just-in",  "_dt": ts(2.99)},
    {"url": "edge",     "_dt": ts(3.01)},
    {"url": "stale",    "_dt": ts(15.7)},   # 尼日尔燃油短缺那种
    {"url": "no-dt"},
    {"url": "bad-dt",   "_dt": "not-a-number"},
]
kept, _ = run(list(news), [])
names = [n["url"] for n in kept]
print("── ① 新闻年龄 ──")
check("半天前 → 保留", "fresh" in names, True)
check("2.99 天 → 保留", "just-in" in names, True)
check("3.01 天 → 剔除", "edge" in names, False)
check("15.7 天 → 剔除", "stale" in names, False)
check("无 _dt → 剔除", "no-dt" in names, False)
check("_dt 非数字 → 剔除", "bad-dt" in names, False)
check("保留条数", len(kept), 2)

# ── ② ageDays 被重算，不是沿用旧值 ──────────────────────────────────────
print("\n── ② ageDays 重算 ──")
news2 = [{"url": "a", "_dt": ts(1.0), "ageDays": 99.9}]
kept2, _ = run(news2, [])
check("旧 ageDays 被覆盖", kept2[0]["ageDays"], 1.0)

# ── ③ 社交走 _ts，不走 _dt ──────────────────────────────────────────────
print("\n── ③ 社交用 _ts ──")
social = [
    {"url": "s-fresh", "_ts": ts(0.8)},
    {"url": "s-stale", "_ts": ts(3.5)},
    {"url": "s-wrong-key", "_dt": ts(0.1)},   # 只有 _dt 没有 _ts → 剔除
    {"url": "s-none"},
]
_, kept_s = run([], list(social))
sn = [s["url"] for s in kept_s]
check("0.8 天 → 保留", "s-fresh" in sn, True)
check("3.5 天 → 剔除", "s-stale" in sn, False)
check("只有 _dt 的社交条 → 剔除", "s-wrong-key" in sn, False)
check("无时间戳 → 剔除", "s-none" in sn, False)
check("保留条数", len(kept_s), 1)

# ── ④ 新闻和社交互不干扰 ────────────────────────────────────────────────
print("\n── ④ 两条链路独立 ──")
k1, k2 = run([{"url": "n", "_dt": ts(1)}], [{"url": "s", "_ts": ts(1)}])
check("新闻保留 1", len(k1), 1)
check("社交保留 1", len(k2), 1)
check("新闻不串到社交", k2[0]["url"], "s")

# ── ⑤ 窗口参数确实被读取，不是写死 3 ────────────────────────────────────
print("\n── ⑤ WINDOW_DAYS 生效 ──")
k3, _ = run([{"url": "x", "_dt": ts(2.5)}], [], days=1)
check("窗口 1 天时 2.5 天条目剔除", len(k3), 0)

print("\n%d 项，%d 失败" % (total, fail))
sys.exit(1 if fail else 0)
