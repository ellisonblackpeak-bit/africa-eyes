# -*- coding: utf-8 -*-
"""One place that talks to every external data provider.

Why this file exists
--------------------
Until now each collector had its own way in: fetch_news.py used the `tavily`
package (absent in the cloud sandbox, so it silently fell back to RSS),
fetch_social.py shelled out to a local MCP server, fetch_people_x.py did the
same, fetch_linkedin_bd.py needed a Bright Data zone. The result was that the
deployed service could only refresh *some* of the sources — the rest depended
on the reader's laptop being on.

The reader's instruction was to consolidate all of it into the link itself, so
that the deployed page can call xAI, Tavily, GDELT, RSS and Bright Data on its
own. This module is that consolidation: every provider is a plain HTTPS call
with a key read from the project, no local daemon, no MCP server, no vendor SDK.

Provider status
---------------
Each provider reports `ok` plus a human-readable `detail`. Status is derived
from configuration and from the last observed failure (cached in
data/source_status.json), not from a paid probe on every page load.

    python sources.py --status
    python sources.py --search "Nigeria lithium mining"
"""
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# Only re-wrap when run as a script: importing this module from the
# service must not replace (and later close) the host's stdout.
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_PATH = os.path.join(HERE, "data", "source_status.json")

UA = "AfricaEyes/1.0 (+monitoring)"

PROVIDERS = [
    ("xai", "xAI / Grok（X 实时检索）"),
    ("tavily", "Tavily（新闻检索）"),
    ("gdelt", "GDELT（全球事件库，免密钥）"),
    ("rss", "RSS（西非 12 家媒体，免密钥）"),
    ("brightdata", "Bright Data（LinkedIn 检索）"),
    ("firecrawl", "Firecrawl（网页抓取）"),
]


# ───────────────────────────── keys ─────────────────────────────
def _mcp_key(name):
    p = os.path.join(os.path.expanduser("~"), ".workbuddy-ai", "mcp.json")
    try:
        with io.open(p, encoding="utf-8") as fh:
            m = re.search(r'"%s":\s*"([^"]+)"' % name, fh.read())
        return m.group(1) if m else ""
    except Exception:
        return ""


def xai_key():
    k = (os.environ.get("XAI_API_KEY") or "").strip()
    if k:
        return k
    local = os.path.join(HERE, "keys.json")
    if os.path.exists(local):
        try:
            with io.open(local, encoding="utf-8") as fh:
                v = (json.load(fh).get("xai") or "").strip()
            if v:
                return v
        except Exception:
            pass
    return _mcp_key("XAI_API_KEY")


def tavily_keys():
    """All Tavily keys, best first: env → tavily_keys.txt → mcp.json."""
    out = []
    k = (os.environ.get("TAVILY_API_KEY") or "").strip()
    if k:
        out.append(k)
    p = os.path.join(HERE, "tavily_keys.txt")
    if os.path.exists(p):
        try:
            with io.open(p, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("tvly-") and line not in out:
                        out.append(line)
        except Exception:
            pass
    k = _mcp_key("TAVILY_API_KEY")
    if k and k not in out:
        out.append(k)
    return out


def brightdata_cfg():
    p = os.path.join(HERE, "brightdata.json")
    try:
        with io.open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        return (d.get("apiKey") or "").strip(), (d.get("zone") or "").strip()
    except Exception:
        return "", ""


def firecrawl_key():
    return (os.environ.get("FIRECRAWL_API_KEY") or "").strip() or _mcp_key("FIRECRAWL_API_KEY")


# ───────────────────────────── status cache ─────────────────────────────
_lock = threading.Lock()


def _status_load():
    try:
        with io.open(STATUS_PATH, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def _status_set(pid, ok, detail):
    with _lock:
        st = _status_load()
        st[pid] = {"ok": bool(ok), "detail": detail,
                   "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
        try:
            with io.open(STATUS_PATH, "w", encoding="utf-8") as fh:
                json.dump(st, fh, ensure_ascii=False, indent=1)
        except Exception:
            pass


def capabilities():
    """Configuration-level status for every provider, plus the last failure."""
    st = _status_load()
    rows = []

    def add(pid, ok, detail, note=""):
        last = st.get(pid) or {}
        if last and not last.get("ok") and ok:
            detail = "%s（上次调用失败：%s）" % (detail, (last.get("detail") or "")[:80])
        rows.append({"id": pid, "name": dict(PROVIDERS)[pid],
                     "ok": bool(ok), "detail": detail, "note": note})

    add("xai", bool(xai_key()),
        "已配置密钥，可实时检索 X" if xai_key() else "未配置 XAI_API_KEY")
    tk = tavily_keys()
    add("tavily", bool(tk),
        ("已配置 %d 个密钥" % len(tk)) if tk else "未配置密钥",
        "额度耗尽时会自动降级到 RSS/GDELT")
    add("gdelt", True, "免密钥，随时可用")
    add("rss", True, "免密钥，随时可用")
    bkey, bzone = brightdata_cfg()
    if not bkey:
        add("brightdata", False, "未配置 API Token")
    elif not bzone:
        add("brightdata", False, "已配置 Token，但账号下没有 zone",
            "需在 brightdata.com/cp/zones 建一个 SERP API zone，把名字填入 brightdata.json")
    else:
        add("brightdata", True, "zone: " + bzone)
    add("firecrawl", bool(firecrawl_key()),
        "已配置密钥" if firecrawl_key() else "未配置密钥（本机走连接器代理，云端不可用）")
    return rows


# ───────────────────────────── http helper ─────────────────────────────
def _get(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _post_json(url, payload, timeout=60, headers=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers=dict({"Content-Type": "application/json",
                                               "User-Agent": UA}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ───────────────────────────── Tavily ─────────────────────────────
def tavily_search(query, days=3, max_results=10):
    """Direct HTTPS search. Replaces the `tavily` package, which the cloud
    sandbox does not have — this is why Tavily used to be skipped server-side."""
    keys = tavily_keys()
    if not keys:
        raise RuntimeError("no Tavily key configured")
    err = ""
    for k in keys:
        try:
            doc = _post_json("https://api.tavily.com/search", {
                "api_key": k, "query": query, "topic": "news",
                "search_depth": "basic", "days": max(1, int(days)),
                "max_results": max(1, min(int(max_results), 20)),
                "include_answer": False,
            }, timeout=45)
            out = []
            for r in doc.get("results") or []:
                out.append({
                    "title": (r.get("title") or "").strip(),
                    "url": r.get("url") or "",
                    "snippet": (r.get("content") or "").strip()[:400],
                    "published": r.get("published_date") or "",
                    "source": "tavily",
                })
            _status_set("tavily", True, "ok")
            return out
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            err = "HTTP %d %s" % (e.code, body)
            if e.code in (401, 403, 432, 433):
                continue          # key dead or over quota — try the next one
            break
        except Exception as e:
            err = "%s %s" % (type(e).__name__, str(e)[:120])
            break
    _status_set("tavily", False, err)
    raise RuntimeError("Tavily failed: " + err)


# ───────────────────────────── GDELT ─────────────────────────────
def gdelt_search(query, days=3, max_results=12):
    q = urllib.parse.quote('(%s) sourcelang:english' % query)
    url = ("https://api.gdeltproject.org/api/v2/doc/doc?query=%s"
           "&mode=artlist&format=json&maxrecords=%d&timespan=%dd&sort=datedesc"
           % (q, max(1, min(int(max_results), 50)), max(1, int(days))))
    try:
        raw = _get(url, timeout=30)
        doc = json.loads(raw)
    except Exception as e:
        _status_set("gdelt", False, str(e)[:120])
        raise
    out = []
    for a in doc.get("articles") or []:
        out.append({
            "title": (a.get("title") or "").strip(),
            "url": a.get("url") or "",
            "snippet": "",
            "published": a.get("seendate") or "",
            "source": (a.get("domain") or "gdelt"),
        })
    _status_set("gdelt", True, "ok")
    return out


# ───────────────────────────── xAI / X ─────────────────────────────
def xai_posts(query, days=3, max_results=12, translate=True):
    """Search X through Grok's server-side x_search tool and return the posts
    as structured rows (url / date / original text / Chinese translation).

    This is the cloud-safe replacement for the local xsearch.py CLI: same API,
    plain HTTPS, no local daemon."""
    key = xai_key()
    if not key:
        raise RuntimeError("no xAI key configured")
    now = datetime.now(timezone.utc)
    tool = {"type": "x_search",
            "from_date": (now - timedelta(days=days)).strftime("%Y-%m-%d"),
            "to_date": now.strftime("%Y-%m-%d")}
    prompt = (
        "用 x_search 检索最近 %d 天关于「%s」的 X 帖文，挑出最有信息量的 %d 条。"
        "只输出一个 JSON 对象：\n"
        '{"posts":[{"url":"帖文链接","date":"YYYY-MM-DD","author":"发布者显示名",'
        '"handle":"@账号","text":"帖文原文","textZh":"完整中文译文","summaryZh":"30-60字中文摘要"}]}\n'
        "url 必须是 /status/ 链接；text 逐字照录，不要改写；排除纯致谢、表情、无信息量的回复。"
        "检索不到就返回 {\"posts\":[]}，不要编造。" % (days, query, max_results))
    payload = {"model": os.environ.get("XAI_SEARCH_MODEL", "grok-4.7"),
               "input": [{"role": "system",
                          "content": "你是开源情报分析员，只使用检索到的真实公开帖文，不得编造。"},
                         {"role": "user", "content": prompt}],
               "tools": [tool], "max_turns": 3}
    try:
        resp = _post_json("https://api.x.ai/v1/responses", payload,
                          timeout=240, headers={"Authorization": "Bearer " + key})
    except Exception as e:
        _status_set("xai", False, str(e)[:160])
        raise
    txt = ""
    for item in resp.get("output") or []:
        if not isinstance(item, dict) or item.get("type") not in (None, "message"):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                txt += (part.get("text") or "")
    i, j = txt.find("{"), txt.rfind("}")
    doc = {}
    if i >= 0 and j > i:
        try:
            doc = json.loads(txt[i:j + 1])
        except Exception:
            doc = {}
    posts = []
    for p in (doc.get("posts") or [])[:max_results]:
        u = (p.get("url") or "").strip()
        if "/status/" not in u:
            continue
        posts.append({
            "url": u,
            "date": (p.get("date") or "").strip(),
            "author": (p.get("author") or "").strip(),
            "handle": (p.get("handle") or "").strip(),
            "text": (p.get("text") or "").strip(),
            "textZh": (p.get("textZh") or "").strip(),
            "summaryZh": (p.get("summaryZh") or "").strip(),
            "source": "xai",
        })
    _status_set("xai", True, "ok")
    return posts


# ───────────────────────────── Bright Data ─────────────────────────────
def brightdata_linkedin(query, max_results=10):
    """LinkedIn search through Bright Data's SERP zone.

    Bright Data refuses every request until the account has a zone, so this
    raises a clear, actionable error rather than a stack trace."""
    key, zone = brightdata_cfg()
    if not key:
        raise RuntimeError("Bright Data 未配置 API Token")
    if not zone:
        raise RuntimeError("Bright Data 账号下没有 zone：请先在 brightdata.com/cp/zones "
                           "建一个 SERP API zone，把名字填入 brightdata.json")
    payload = {"zone": zone, "url": "https://www.google.com/search?q="
                                    + urllib.parse.quote("site:linkedin.com/posts " + query),
               "format": "json"}
    try:
        doc = _post_json("https://api.brightdata.com/request", payload,
                         timeout=90, headers={"Authorization": "Bearer " + key})
    except Exception as e:
        _status_set("brightdata", False, str(e)[:160])
        raise
    _status_set("brightdata", True, "ok")
    return doc.get("results") or []


# ───────────────────────────── unified search ─────────────────────────────
def unified(query, days=3, want=("news", "x", "linkedin")):
    """Ask every available provider at once; never fail as a whole."""
    query = (query or "").strip()
    groups, errors = [], []

    def news_group():
        items, srcs = [], []
        for name, fn in (("Tavily", lambda: tavily_search(query, days, 10)),
                         ("GDELT", lambda: gdelt_search(query, days, 12))):
            try:
                got = fn()
                if got:
                    srcs.append(name)
                    items.extend(got)
            except Exception as e:
                errors.append({"provider": name, "error": str(e)[:200]})
        if items:
            seen, uniq = set(), []
            for it in items:
                u = it.get("url") or ""
                if u and u not in seen:
                    seen.add(u)
                    uniq.append(it)
            groups.append({"id": "news", "label": "新闻与事件库",
                           "providers": srcs, "items": uniq[:24]})

    def x_group():
        try:
            got = xai_posts(query, days, 12)
        except Exception as e:
            errors.append({"provider": "xAI", "error": str(e)[:200]})
            return
        if got:
            groups.append({"id": "x", "label": "X 实时讨论",
                           "providers": ["xAI / Grok"], "items": got})

    def li_group():
        try:
            got = brightdata_linkedin(query)
        except Exception as e:
            errors.append({"provider": "Bright Data", "error": str(e)[:200]})
            return
        if got:
            groups.append({"id": "linkedin", "label": "LinkedIn",
                           "providers": ["Bright Data"], "items": got})

    jobs = {"news": news_group, "x": x_group, "linkedin": li_group}
    threads = [threading.Thread(target=jobs[w]) for w in want if w in jobs]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=260)
    order = {"news": 0, "x": 1, "linkedin": 2}
    groups.sort(key=lambda g: order.get(g["id"], 9))
    return {"query": query, "days": days, "groups": groups, "errors": errors,
            "elapsed": round(time.time() - t0, 1),
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def main():
    args = sys.argv[1:]
    if "--status" in args or not args:
        for c in capabilities():
            print("%-12s %-6s %s %s" % (c["id"], "OK" if c["ok"] else "MISSING",
                                        c["detail"], ("| " + c["note"]) if c["note"] else ""))
        return
    if "--search" in args:
        i = args.index("--search")
        q = args[i + 1] if i + 1 < len(args) else ""
        res = unified(q, days=3)
        print(json.dumps(res, ensure_ascii=False, indent=1)[:6000])


if __name__ == "__main__":
    main()
