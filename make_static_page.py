# -*- coding: utf-8 -*-
"""Render data.js into a fully pre-rendered, JS-free HTML page.

Why: the WorkBuddy "sites" preview link mints a new public domain on every
deploy and the old one starts answering 400/12803 within the hour, so it cannot
be handed out as a daily address. A library page (workbuddy.cc/p/<id>) keeps one
stable link, but it re-renders the HTML from a parsed node tree — scripts are not
executed there. So this builder renders every card server-side: no JS is needed
to read the page, and <details> gives the fold natively.

Output: dist/africa-eyes-static.html
"""
import html
import io
import json
import os
import re
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data.js")
OUTDIR = os.path.join(HERE, "dist")
OUT = os.path.join(OUTDIR, "africa-eyes-static.html")

BODY_CAP = 2600
CN = {"ng": "尼日利亚", "ne": "尼日尔", "ml": "马里", "gh": "加纳", "wa": "西非区域", "af": "非洲"}
TOPIC = {"pol": "政治", "eco": "经济", "sec": "安全", "ene": "能源", "min": "矿产", "cor": "战略通道"}
PLAT = {"x": "X", "linkedin": "LinkedIn", "facebook": "Facebook", "instagram": "Instagram"}
TIER_ORDER = [1, 2, 3, 4]
TIER_NAME = {1: "涉华动态", 2: "美国与西方", 3: "四国政府", 4: "其他值得关注"}

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#e6edf3;font:16px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:16px 14px 60px}
header{padding:18px 16px;background:linear-gradient(135deg,#0F3D5C,#123c5a 55%,#0b2b42);border-radius:14px;margin-bottom:14px}
h1{margin:0 0 6px;font-size:21px;letter-spacing:.5px}
.sub{font-size:13px;color:#a9c2d4;line-height:1.8}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 6px}
.stat{background:#161b22;border:1px solid #24303c;border-radius:10px;padding:8px 12px;font-size:13px;color:#c9d6e2}
.stat b{color:#f0b429;font-size:16px;margin-right:4px}
h2{font-size:17px;margin:26px 0 10px;padding-left:10px;border-left:4px solid #f0b429;color:#f0f6fc}
h2 small{font-weight:400;font-size:12px;color:#8b98a5;margin-left:8px}
.card{background:#161b22;border:1px solid #24303c;border-radius:12px;padding:13px 14px;margin:0 0 11px}
.card h3{margin:0 0 7px;font-size:16px;line-height:1.55;color:#f0f6fc;font-weight:600}
.meta{font-size:12px;color:#8b98a5;margin:0 0 8px;line-height:1.9}
.meta .tag{display:inline-block;background:#1f2937;color:#9fb3c8;border-radius:5px;padding:1px 7px;margin-right:6px}
.meta .cn{color:#f0b429}
.sum{margin:0 0 9px;font-size:14.5px;color:#c9d6e2}
details{margin-top:4px}
summary{cursor:pointer;font-size:13px;color:#58a6ff;list-style:none;padding:5px 0}
summary::-webkit-details-marker{display:none}
summary::before{content:"▸ ";}
details[open] summary::before{content:"▾ ";}
.body{font-size:14px;color:#b9c7d4;background:#0f151c;border-radius:8px;padding:11px 12px;margin:6px 0 8px;white-space:pre-wrap;word-break:break-word}
.orig{font-size:13px}
.orig a{color:#58a6ff;text-decoration:none}
.en{font-size:12px;color:#6e7c8a;margin-top:7px;line-height:1.6;word-break:break-word}
footer{margin-top:34px;padding-top:14px;border-top:1px solid #24303c;font-size:12px;color:#6e7c8b;line-height:1.9}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
td,th{border-bottom:1px solid #24303c;padding:6px 4px;text-align:left;vertical-align:top}
th{color:#8b98a5;font-weight:500}
@media(max-width:520px){.wrap{padding:10px 10px 50px}h1{font-size:19px}.card{padding:11px 11px}}
"""


def esc(s):
    return html.escape(str(s or ""), quote=True)


def cut(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def load():
    raw = io.open(DATA, encoding="utf-8").read()
    m = re.search(r"window\.SITE_DATA\s*=\s*", raw)
    body = raw[m.end():].strip() if m else raw.strip()
    return json.loads(body.rstrip(";"))


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def meta_line(it):
    bits = []
    if it.get("source"):
        bits.append('<span class="tag">%s</span>' % esc(it["source"]))
    nm = "／".join(CN.get(c, c) for c in _as_list(it.get("country")))
    if nm:
        bits.append('<span class="tag cn">%s</span>' % esc(nm))
    tp = "／".join(TOPIC.get(t, t) for t in _as_list(it.get("topic")))
    if tp:
        bits.append('<span class="tag">%s</span>' % esc(tp))
    stamp = it.get("postedAt") or it.get("published")
    if stamp:
        bits.append(esc(str(stamp)[:22]))
    return "".join(bits)


def news_card(it):
    title = it.get("title_zh") or it.get("title") or ""
    summ = it.get("summary_zh") or it.get("summary") or ""
    body = it.get("body_zh") or it.get("body") or ""
    o = ['<div class="card">']
    o.append("<h3>%s</h3>" % esc(title))
    o.append('<div class="meta">%s</div>' % meta_line(it))
    if summ:
        o.append('<p class="sum">%s</p>' % esc(cut(summ, 560)))
    if body and len(body) > len(summ) + 40:
        o.append("<details><summary>展开全文译文</summary>")
        o.append('<div class="body">%s</div>' % esc(cut(body, BODY_CAP)))
        if it.get("bodyLevel") == "excerpt":
            o.append('<div class="en">该来源未开放正文抓取（付费墙或反爬限制），以上为可获得的完整文本。</div>')
        o.append("</details>")
    if it.get("url"):
        o.append('<div class="orig"><a href="%s" target="_blank" rel="noopener">打开原文链接 →</a></div>'
                 % esc(it["url"]))
    if it.get("title"):
        o.append('<div class="en">英文原标题：%s</div>' % esc(cut(it["title"], 200)))
    o.append("</div>")
    return "".join(o)


def social_card(it):
    title = it.get("title_zh") or it.get("title") or ""
    snip = it.get("snippet_zh") or it.get("snippet") or ""
    body = it.get("body_zh") or it.get("body") or snip
    o = ['<div class="card">']
    o.append("<h3>%s</h3>" % esc(title))
    plat = PLAT.get(it.get("platform"), it.get("platform") or "")
    bits = ['<span class="tag">%s</span>' % esc(plat)]
    if it.get("handle") or it.get("author"):
        bits.append('<span class="tag">%s</span>' % esc(it.get("handle") or it.get("author")))
    if it.get("postedAt"):
        bits.append(esc(str(it["postedAt"])[:22]))
    o.append('<div class="meta">%s</div>' % "".join(bits))
    if snip:
        o.append('<p class="sum">%s</p>' % esc(cut(snip, 460)))
    if body and len(body) > len(snip) + 40:
        o.append("<details><summary>展开全文译文</summary>")
        o.append('<div class="body">%s</div>' % esc(cut(body, 2000)))
        o.append("</details>")
    if it.get("url"):
        o.append('<div class="orig"><a href="%s" target="_blank" rel="noopener">打开原文链接 →</a></div>'
                 % esc(it["url"]))
    o.append("</div>")
    return "".join(o)


def people_block(people):
    if not people:
        return ""
    rows = []
    for p in people[:160]:
        nm = p.get("name") or ""
        org = p.get("org") or ""
        role = p.get("role") or ""
        duty = p.get("duty") or p.get("roleHint") or ""
        links = []
        for pr in (p.get("profiles") or [])[:3]:
            if pr.get("url"):
                links.append('<a href="%s" target="_blank" rel="noopener">%s</a>'
                             % (esc(pr["url"]), esc(PLAT.get(pr.get("platform"), pr.get("platform") or "主页"))))
        rows.append("<tr><td><b>%s</b>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                    % (esc(nm), ("<br>" + esc(org)) if org else "", esc(role),
                       esc(cut(duty, 220)), " ".join(links) or "—"))
    return ('<h2>人员索引<small>公开职务身份与公开帖文，仅供参考</small></h2>'
            '<div class="card"><table><tr><th>姓名 / 机构</th><th>身份</th><th>做什么</th><th>主页</th></tr>'
            + "".join(rows) + "</table></div>")


def main():
    d = load()
    news = d.get("news") or []
    social = d.get("social") or []
    people = d.get("people") or []

    parts = []
    parts.append('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
                 '<meta name="viewport" content="width=device-width,initial-scale=1">'
                 "<title>Africa Eyes · 西非四国动态监测</title>"
                 "<style>%s</style></head><body><div class=\"wrap\">" % CSS)

    gen = d.get("generatedAt") or ""
    parts.append("<header><h1>Africa Eyes · 西非四国动态监测</h1>"
                 '<div class="sub">尼日利亚 · 尼日尔 · 马里 · 加纳　｜　时间窗口：%s<br>'
                 "数据生成时间：%s（北京时间）　·　本页为长期托管的静态版，链接不会失效</div>"
                 "</header>" % (esc(d.get("window") or "近 5 日"), esc(gen)))

    tc = d.get("tierCounts") or {}
    parts.append('<div class="stats">'
                 '<div class="stat"><b>%d</b>条新闻</div>'
                 '<div class="stat"><b>%d</b>条社交动态</div>'
                 '<div class="stat"><b>%d</b>位人物</div>'
                 '<div class="stat">涉华 <b>%s</b></div>'
                 '<div class="stat">美国 <b>%s</b></div>'
                 '<div class="stat">四国政府 <b>%s</b></div>'
                 "</div>"
                 % (len(news), len(social), len(people),
                    tc.get("1", tc.get(1, 0)), tc.get("2", tc.get(2, 0)), tc.get("3", tc.get(3, 0))))

    # ── news grouped by tier ───────────────────────────────────────────────
    for t in TIER_ORDER:
        group = [n for n in news if int(n.get("tier") or 4) == t]
        if not group:
            continue
        parts.append("<h2>%s<small>%d 条</small></h2>" % (TIER_NAME[t], len(group)))
        for it in group:
            parts.append(news_card(it))

    # ── social ────────────────────────────────────────────────────────────
    if social:
        parts.append("<h2>社交动态<small>%d 条 · X / LinkedIn / Facebook / Instagram 公开页面</small></h2>" % len(social))
        for it in social[:90]:
            parts.append(social_card(it))

    parts.append(people_block(people))

    parts.append("<footer>"
                 "数据来源：四国主流媒体、国际通讯社、全球矿业媒体，以及 LinkedIn / X / Facebook / Instagram 公开页面。"
                 "默认按层级分组（涉华 → 美国 → 四国政府 → 其他），层内按发布时间倒序；窗口近 5 日。<br>"
                 "每条均提供中文标题、中文摘要与展开全文，并附原文链接便于核对；"
                 "部分来源未开放正文抓取（付费墙或反爬限制），已在卡片内注明。<br>"
                 "仅供研判参考，所有条目以原始链接为准。生成时间：%s"
                 "</footer></div></body></html>" % esc(gen))

    out = "".join(parts)
    if not os.path.isdir(OUTDIR):
        os.makedirs(OUTDIR)
    io.open(OUT, "w", encoding="utf-8").write(out)
    print("wrote %s (%.1f KB) news=%d social=%d people=%d gen=%s"
          % (OUT, len(out.encode("utf-8")) / 1024.0, len(news), len(social), len(people), gen))


if __name__ == "__main__":
    main()
