# -*- coding: utf-8 -*-
"""假日期与 URL-ID 标题的回归测试。

起因（2026-11-04 线上实测）：非洲四国模块里有一条

    title    galamsey chinese
    title_zh 非法采金：中国
    url      https://www.citinewsroom.com/2020/01/amansie-central-three-chinese-
             nationals-arrested-for-engaging-in-illegal-mining/galamsey-chinese
    published Tue, 03 Nov 2026 10:00:00 GMT

正文是 2020 年 1 月的旧闻，日期却是构建当天上午十点整——来源没给时间，上游
补了一个占位时间戳，于是它堂堂正正进了「近 3 日」。这类假日期最难自查：`_dt`
明明落在窗口内，3 天闸门根本拦不住。

另一条：

    title    922573 us midterms democrats take control of senate lead in house seats
    title_zh 922573.us中期选举：民主党掌控参议院……

标题是从 URL slug 生成的，slug 开头的文章号被当成了正文，翻译后变成一个
域名形状的数字串挂在标题最前面。

用法： python _test_fakedate.py     期望：全部 PASS，退出码 0。
"""
import ast
import io
import re
import sys
from datetime import datetime, timezone

src = io.open("build_data.py", encoding="utf-8").read()
tree = ast.parse(src)

WANT = {"_URL_YEAR_RX", "url_year_conflict", "polish_title",
        "strip_links", "clean", "_title_from_url", "_is_compatible_prefix",
        "_looks_cut_midword", "_complete_title", "SEP_RX", "MASTHEAD_RX",
        "_PLACEHOLDER_RX", "HOMEPAGE_RX",
        # clean() 的依赖，不带上会 NameError
        "_INVIS", "_MD_IMG", "_MD_LINK", "_MD_BOLD",
        # strip_links() 的依赖
        "_LINK_IN_TITLE_RX", "_BARE_URL_RX"}
keep = []
_kept_names = set()
for node in tree.body:
    name = None
    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
        name = node.targets[0].id
    elif isinstance(node, ast.FunctionDef):
        name = node.name
    if name in WANT:
        keep.append(node)
        _kept_names.add(name)

ns = {"re": re, "datetime": datetime, "timezone": timezone}
# 静态补齐依赖。
#
# 不能靠 exec 时的 NameError 来补：函数体只有被调用时才会解析全局名，exec 阶段
# 永远不会报错，一调用就崩（clean→_INVIS、strip_links→_LINK_IN_TITLE_RX→
# _BARE_LINK_RX，一个个补了三轮还没完）。所以改成扫 AST：把已保留节点里所有
# 被「读」的名字收集起来，凡是在模块顶层定义过、又还没保留的，就加进来，循环
# 到不再新增为止。这样一次就能收敛。
_ALL = {}
for node in tree.body:
    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
        _ALL[node.targets[0].id] = node
    elif isinstance(node, ast.FunctionDef):
        _ALL[node.name] = node

for _round in range(30):
    used = set()
    for node in keep:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                used.add(sub.id)
    add = [n for n in used if n in _ALL and n not in _kept_names]
    if not add:
        break
    for n in add:
        keep.append(_ALL[n])
        _kept_names.add(n)
    print("  (补入依赖 %s)" % ", ".join(sorted(add)))

exec(compile(ast.Module(body=keep, type_ignores=[]), "<extract>", "exec"), ns)

CONFLICT = ns["url_year_conflict"]
POLISH = ns["polish_title"]

fail = 0
total = 0


def chk(label, got, want):
    global fail, total
    total += 1
    ok = got == want
    if not ok:
        fail += 1
    print("%s  %-34s want=%-5s got=%s"
          % ("PASS" if ok else "FAIL", label, want, got))


def dt(y, m=11, d=4):
    return datetime(y, m, d, tzinfo=timezone.utc)


print("── A. URL 年份早于声称年份 → 判为假日期 ──")
chk("2020 路径 / 2026 日期",
    CONFLICT("https://www.citinewsroom.com/2020/01/amansie-central-three-chinese"
             "-nationals-arrested/galamsey-chinese", dt(2026)), True)
chk("2024 路径 / 2026 日期",
    CONFLICT("https://example.com/2024/06/some-old-story", dt(2026)), True)
chk("/news/2020/ 形式",
    CONFLICT("https://example.com/news/2020/story", dt(2026)), True)
chk("2026-11-04 短横线形式 / 2026",
    CONFLICT("https://example.com/2026-11-04/story", dt(2026)), False)

print("\n── B. 年份一致或无法判断 → 不误杀 ──")
chk("同年路径 / 同年日期",
    CONFLICT("https://example.com/2026/11/04/fresh-story", dt(2026)), False)
chk("URL 里没有年份",
    CONFLICT("https://punchng.com/tinubu-seeks-senate-nod/", dt(2026)), False)
chk("URL 年份更新（页面改挂新路径）",
    CONFLICT("https://example.com/2027/01/story", dt(2026)), False)
chk("文章号 922573 不是年份",
    CONFLICT("https://www.premiumtimesng.com/news/top-news/922573-us-midterms-"
             "democrats-take-control-of-senate-lead-in-house-seats.html", dt(2026)),
    False)
chk("空 URL",
    CONFLICT("", dt(2026)), False)
chk("无日期对象",
    CONFLICT("https://example.com/2020/01/x", None), False)

print("\n── C. 标题开头的文章 ID 要摘掉 ──")
chk("Premium Times 文章号",
    POLISH("922573 us midterms democrats take control of senate lead in house seats"),
    "us midterms democrats take control of senate lead in house seats")
chk("短横线分隔的文章号",
    POLISH("922573-us-midterms-democrats-take-control"),
    "us-midterms-democrats-take-control")
chk("点号分隔的文章号（由 strip_links 当裸域名整段摘掉）",
    POLISH("123456.us midterms democrats"), "midterms democrats")

print("\n── D. 正经标题不能被误伤 ──")
chk("年份开头的预算标题",
    POLISH("2026 Budget: Tinubu signs record spending plan"),
    "2026 Budget: Tinubu signs record spending plan")
chk("年份加短横线的日期标题",
    POLISH("2026-11-04 US midterms: Democrats win"), "2026-11-04 US midterms: Democrats win")
chk("普通标题原样",
    POLISH("Nigeria opens new refinery in Port Harcourt"),
    "Nigeria opens new refinery in Port Harcourt")

print("\n── E. 站名后缀要剥干净（中英混排也算站名）──")
chk("中英双段站名",
    POLISH("尼日利亚与中国签署3000万美元基层发展项目协议 | Daily Champion | 国家之声，真相守护者"),
    "尼日利亚与中国签署3000万美元基层发展项目协议")
chk("英文站名",
    POLISH("Nigeria signs $30m grassroots deal | Daily Champion"),
    "Nigeria signs $30m grassroots deal")
chk("路透后缀",
    POLISH("US midterms: Democrats win control of Congress | Reuters"),
    "US midterms: Democrats win control of Congress")
chk("破折号后的真从句要留住",
    POLISH("Nigeria - Ghana trade hits record high"),
    "Nigeria - Ghana trade hits record high")

print("\n%d 项，%d 失败" % (total, fail))
sys.exit(1 if fail else 0)
