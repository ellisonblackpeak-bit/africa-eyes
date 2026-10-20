# -*- coding: utf-8 -*-
"""Render a daily Markdown digest that can be dropped straight into IMA.

Why a file instead of an API push
---------------------------------
ima (ima.qq.com) has no public write API that WorkBuddy can reach today: the
connector marketplace has no IMA entry and the skill registry returns 404, so
there is no supported way for this pipeline to write into a knowledge base
directly. What IMA *does* support is importing a Markdown file (and a web link),
and it indexes the import itself. So the pipeline produces one file per day in
digest/ — import it and the day's material is in the knowledge base, with its
headlines, summaries, dates and source links intact.

Layout of the digest
--------------------
* Front matter as plain Markdown headings, no tables: IMA's parser handles
  headings and lists more reliably than wide tables on a phone screen.
* One section per priority tier, so a reader who only cares about China-related
  or government items can jump straight there.
* Every item carries its source and a link, because a knowledge base entry
  without provenance is not much use for later verification.
"""
import io
import json
import os
import re
import sys
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, "digest")
os.makedirs(OUTDIR, exist_ok=True)

TIER_TITLE = {1: "一、涉华动态", 2: "二、美国在该地区的活动",
              3: "三、四国政府动态", 4: "四、其他相关报道"}


def load_data():
    p = os.path.join(HERE, "data.js")
    with io.open(p, encoding="utf-8") as fh:
        src = fh.read()
    body = src[src.index("{"):].rstrip().rstrip(";")
    return json.loads(body)


def clean(s, limit=None):
    s = " ".join((s or "").split())
    if limit and len(s) > limit:
        s = s[:limit].rstrip() + "……"
    return s


def item_md(it, social=False):
    title = it.get("title_zh") or it.get("title") or ""
    summ = it.get("summary_zh") or it.get("summary") or it.get("snippet_zh") or it.get("snippet") or ""
    src = it.get("source") or it.get("author") or it.get("handle") or ""
    dom = it.get("domain") or ""
    when = it.get("published") or it.get("postedAt") or ""
    age = it.get("ageDays")
    line = "### %s\n" % clean(title)
    if summ:
        line += "%s\n\n" % clean(summ, 400)
    meta = []
    if src or dom:
        meta.append("来源：%s" % (src or dom))
    if when:
        meta.append("时间：%s" % when)
    if isinstance(age, (int, float)):
        meta.append("距今 %.1f 天" % age)
    if it.get("url"):
        meta.append("[原文](%s)" % it["url"])
    if meta:
        line += "> " + "　·　".join(meta) + "\n"
    return line + "\n"


def main():
    d = load_data()
    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    news = d.get("news") or []
    social = d.get("social") or []
    profiles = d.get("profiles") or []

    out = []
    out.append("# China Eyes 每日动态简报 · %s\n" % day)
    out.append("> 监测范围：尼日利亚、尼日尔、马里、加纳（含西非／萨赫勒区域）。"
               "领域：政治、经济、安全、能源、矿产、战略通道。\n")
    out.append("> 抓取窗口 %s，页面显示窗口 %s。本期共 %d 条媒体条目、%d 条社交动态、%d 个 X 人物画像。\n"
               % (d.get("window") or "", d.get("window") or "", len(news), len(social), len(profiles)))

    # 本期要点
    hl = d.get("highlights") or []
    if hl:
        out.append("\n## 本期要点\n")
        for h in hl:
            out.append("- %s" % clean(h))
        out.append("")

    # 按层级
    by_tier = {}
    for n in news:
        by_tier.setdefault(n.get("tier") or 4, []).append(n)
    for t in (1, 2, 3, 4):
        items = by_tier.get(t) or []
        if not items:
            continue
        out.append("\n## %s（%d 条）\n" % (TIER_TITLE[t], len(items)))
        for it in items:
            out.append(item_md(it))

    # 社交
    if social:
        out.append("\n## 五、社交平台动态（%d 条）\n" % len(social))
        for it in social:
            out.append(item_md(it, social=True))

    # 人物画像
    if profiles:
        out.append("\n## 六、X 人物画像\n")
        for p in profiles:
            name = p.get("name") or p.get("handle") or ""
            out.append("### %s %s\n" % (name, p.get("handle") or ""))
            extra = []
            if p.get("roleGuess"):
                extra.append("角色判断：%s" % p["roleGuess"])
            if p.get("topics"):
                extra.append("集中领域：%s" % "、".join(p["topics"]))
            if extra:
                out.append("> " + "　·　".join(extra) + "\n")
            if p.get("discoveryNote"):
                out.append("%s\n\n" % clean(p["discoveryNote"], 300))
            if p.get("summaryZh"):
                out.append("%s\n\n" % clean(p["summaryZh"], 900))
            for post in (p.get("posts") or [])[:3]:
                if post.get("url"):
                    out.append("- %s[帖文](%s)"
                               % ((post.get("date") + " ") if post.get("date") else "",
                                  post["url"]))
            out.append("")

    out.append("\n---\n\n*本文件由 China Eyes 自动生成，可直接导入 IMA 知识库。"
               "生成时间：%s*\n" % d.get("generatedAt", day))

    text = "\n".join(out)
    dated = os.path.join(OUTDIR, "Africa-Eyes-%s.md" % day)
    with io.open(dated, "w", encoding="utf-8") as fh:
        fh.write(text)
    # A stable name too, so a manual import does not have to track dates.
    with io.open(os.path.join(OUTDIR, "latest.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    print("digest written: %s (%d bytes, %d news, %d social, %d profiles)"
          % (dated, len(text.encode("utf-8")), len(news), len(social), len(profiles)))


if __name__ == "__main__":
    main()
