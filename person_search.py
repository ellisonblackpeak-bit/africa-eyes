# -*- coding: utf-8 -*-
"""Interactive X person lookup for Africa Eyes.

What the reader asked for
-------------------------
The earlier 人物画像 tab was a *fixed watchlist*: a hand-written topic (MPLJ／FPL)
was profiled once a day and that was all the page could show. The reader's
correction was precise — MPLJ/FPL were only an example. What is wanted is a
search box: type a name whenever you like, and get back

    姓名 · 所属领域 · 发帖概括 · 画像分析
    + 每一条相关帖文：中文摘要 / 展开全文译文 / 打开原文

So this module takes an arbitrary query and does two xAI Responses calls:

  1. identify — who is this? up to 4 candidate X accounts, with role and domain
  2. profile  — restricted to the chosen handle: post summary, portrait
                analysis, and up to 8 relevant posts with Chinese translation

Both calls use the server-side `x_search` tool, so Grok does the searching and
reading of X itself; nothing here scrapes X directly and no credentials for X
are involved. Results are cached in data/people_cache.json keyed by the
normalised query, so repeating a search costs nothing.

CLI:
    python person_search.py "Bakary Sambe"
    python person_search.py "MPLJ 安全 记者" --fresh
"""
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# Only re-wrap when run as a script: importing this module from the
# service must not replace (and later close) the host's stdout.
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "data", "people_cache.json")

API_BASE = "https://api.x.ai/v1"
API = API_BASE + "/responses"
MODEL = os.environ.get("XAI_SEARCH_MODEL", "grok-4.7")
TRANSLATE_MODEL = "grok-4.20-0309-non-reasoning"

# How far back a portrait may look. The news feed is strict 3-day; a person
# needs a wider sample or most accounts would look empty. 45 days is enough to
# capture a posting pattern without dragging in last year's positions.
LOOKBACK_DAYS = int(os.environ.get("PERSON_LOOKBACK_DAYS", "45"))
MAX_POSTS = 8
CACHE_HOURS = 12

STATUS_RX = re.compile(r"https?://(?:twitter\.com|x\.com)/[A-Za-z0-9_]+/(?:status|statuses)/\d+")

SYSTEM = (
    "你是开源情报（OSINT）分析员，专门分析 X（Twitter）上的公开账号。"
    "只使用 x_search 工具真实检索到的公开帖文，不得编造账号、帖文、链接或事实。"
    "如果检索不到，就如实说明检索不到。所有输出必须是简体中文（帖文原文除外）。"
)


# ─────────────────────────── key / http ───────────────────────────
def api_key():
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
    p = os.path.join(os.path.expanduser("~"), ".workbuddy-ai", "mcp.json")
    try:
        with io.open(p, encoding="utf-8") as fh:
            m = re.search(r'"XAI_API_KEY":\s*"([^"]+)"', fh.read())
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def _post(path, payload, key, timeout=240, tries=2):
    url = API_BASE + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last = ""
    for attempt in range(tries):
        req = urllib.request.Request(
            url, data=data,
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = "HTTP %d %s" % (e.code, e.read().decode("utf-8", "replace")[:300])
            if e.code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            if e.code in (400, 401, 403, 404):
                raise RuntimeError(last)
            time.sleep(5)
        except Exception as e:
            last = "%s %s" % (type(e).__name__, str(e)[:200])
            time.sleep(5)
    raise RuntimeError(last or "unknown error")


def x_search(prompt, handles=None, days=LOOKBACK_DAYS, max_turns=2, key=None):
    """One Responses call driving the server-side x_search tool."""
    now = datetime.now(timezone.utc)
    tool = {"type": "x_search",
            "from_date": (now - timedelta(days=days)).strftime("%Y-%m-%d"),
            "to_date": now.strftime("%Y-%m-%d")}
    if handles:
        tool["allowed_x_handles"] = [h.lstrip("@") for h in handles][:20]
    payload = {
        "model": MODEL,
        "input": [{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": prompt}],
        "tools": [tool],
        "max_turns": max(1, min(int(max_turns), 5)),
    }
    return _post("/responses", payload, key or api_key())


def out_text(resp):
    chunks = []
    for item in resp.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in (None, "message"):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                t = (part.get("text") or "").strip()
                if t:
                    chunks.append(t)
    if not chunks and isinstance(resp.get("output_text"), str):
        chunks.append(resp["output_text"].strip())
    return "\n\n".join(chunks).strip()


def out_citations(resp):
    urls, seen = [], set()
    for item in resp.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            for ann in part.get("annotations") or []:
                u = (ann.get("url") or "").strip() if isinstance(ann, dict) else ""
                if u and u not in seen:
                    seen.add(u)
                    urls.append(u)
    for u in resp.get("citations") or []:
        if isinstance(u, str) and u.strip() and u not in seen:
            seen.add(u)
            urls.append(u.strip())
    return urls


def parse_json(text):
    """Pull the first JSON object out of a model reply."""
    if not text:
        return None
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except Exception:
        return None


def x_dt(url):
    m = re.search(r"/(?:status|statuses)/(\d{15,25})", url or "")
    if not m:
        return ""
    ms = (int(m.group(1)) >> 22) + 1288834974657
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, timezone.utc)
    except Exception:
        return ""
    return dt.strftime("%Y-%m-%d") if 2020 <= dt.year <= 2030 else ""


# ─────────────────────────── phase 1: identify ───────────────────────────
def identify(query, key):
    prompt = (
        "请用 x_search 在 X 上检索，找出与「%s」最匹配的公开账号，最多 4 个。\n"
        "匹配可以是：账号本人的姓名／常用名；或该查询描述的主题下最活跃、最有信息量的账号。\n"
        "只输出一个 JSON 对象，不要任何解释文字，格式：\n"
        '{"candidates":[{"handle":"@xxx","name":"显示名","role":"身份或职业",'
        '"org":"所属机构（不明则空字符串）","domain":"所属领域，2-4 个词",'
        '"why":"为什么是这个人/这个账号，30-60 字中文","recent":"最近发帖的一句话概括，40-80 字中文"}]}\n'
        "要求：handle 必须真实存在且能在 X 上打开；找不到就返回 {\"candidates\":[]}。"
        % query)
    resp = x_search(prompt, max_turns=3, key=key)
    doc = parse_json(out_text(resp)) or {}
    cands = []
    for c in (doc.get("candidates") or [])[:4]:
        if not isinstance(c, dict):
            continue
        h = str(c.get("handle") or "").strip()
        if not h.startswith("@"):
            h = "@" + h.lstrip("@")
        if len(h) < 3:
            continue
        cands.append({
            "handle": h,
            "name": str(c.get("name") or "").strip(),
            "role": str(c.get("role") or "").strip(),
            "org": str(c.get("org") or "").strip(),
            "domain": str(c.get("domain") or "").strip(),
            "why": str(c.get("why") or "").strip(),
            "recent": str(c.get("recent") or "").strip(),
        })
    return cands


# ─────────────────────────── phase 2: profile ───────────────────────────
def profile(handle, query, key, citations=None):
    prompt = (
        "请用 x_search 只检索 @%s 这个账号最近 %d 天的帖文（不要检索别人的帖文），"
        "然后输出一份人物画像。查询意图：「%s」。\n"
        "只输出一个 JSON 对象，不要任何解释文字，格式：\n"
        '{"name":"显示名","handle":"@%s","org":"所属机构","role":"身份/职业",'
        '"domain":"所属领域，2-4 个词","topics":["议题1","议题2"],'
        '"postSummary":"发帖概括：这个账号最近在发什么、发帖频率与形式，150-250 字中文",'
        '"analysis":"画像分析：身份与角色依据、关注领域、立场与叙事框架、信息价值与可信度、'
        '与查询主题的关联，300-500 字中文，分 3-4 个自然段",'
        '"posts":[{"url":"完整帖文链接","date":"YYYY-MM-DD","text":"该帖完整原文",'
        '"textZh":"该帖完整中文译文","summaryZh":"该帖一句话中文摘要"}]}\n'
        "要求：\n"
        "1. posts 只列**有实质内容**的帖文，最多 %d 条，按时间倒序；"
        "排除纯致谢、寒暄、表情、单词回复这类无信息量的帖子。"
        "优先选与查询意图「%s」相关的帖文；相关帖文不足时再补该账号其他有代表性的原发帖。\n"
        "2. url 必须是该账号自己的帖文链接，text 必须是帖文原文（逐字照录，不要改写或概括），"
        "textZh 必须是 text 的完整逐句译文，summaryZh 是 30-60 字中文摘要。\n"
        "3. 检索不到帖文时 posts 返回空数组，并在 postSummary 中说明。\n"
        "4. 不要编造任何链接或帖文内容。"
        % (handle.lstrip("@"), LOOKBACK_DAYS, query, handle, MAX_POSTS, query))
    resp = x_search(prompt, handles=[handle], max_turns=3, key=key)
    doc = parse_json(out_text(resp)) or {}

    posts, seen = [], set()
    for p in (doc.get("posts") or []):
        if not isinstance(p, dict):
            continue
        u = str(p.get("url") or "").strip()
        if not STATUS_RX.match(u) or u in seen:
            continue
        seen.add(u)
        posts.append({
            "url": u,
            "date": str(p.get("date") or "").strip() or x_dt(u),
            "text": str(p.get("text") or "").strip(),
            "textZh": str(p.get("textZh") or p.get("text_zh") or "").strip(),
            "summaryZh": str(p.get("summaryZh") or p.get("summary_zh") or "").strip(),
        })

    # Any citation the model quoted but forgot to list still belongs on the page.
    for u in (citations or []) + out_citations(resp):
        if STATUS_RX.match(u) and u not in seen and "/" + handle.lstrip("@").lower() + "/" in u.lower():
            seen.add(u)
            posts.append({"url": u, "date": x_dt(u), "text": "", "textZh": "", "summaryZh": ""})

    posts.sort(key=lambda x: x.get("date") or "", reverse=True)
    return {
        "name": str(doc.get("name") or "").strip(),
        "handle": handle,
        "org": str(doc.get("org") or "").strip(),
        "role": str(doc.get("role") or "").strip(),
        "domain": str(doc.get("domain") or "").strip(),
        "topics": [str(t).strip() for t in (doc.get("topics") or []) if str(t).strip()][:6],
        "postSummary": str(doc.get("postSummary") or "").strip(),
        "analysis": str(doc.get("analysis") or "").strip(),
        "posts": posts[:MAX_POSTS],
    }


# ─────────────────────────── posts fallback ───────────────────────────
def posts_fallback(handle, key):
    """Second attempt when the profile call came back with no posts.

    A single request that asks for a portrait *and* a post list sometimes
    returns an empty list (the model spends its turns on the portrait). This
    narrower ask — "just list this account's most substantive recent posts" —
    reliably comes back with URLs, and a wider window is used because the
    point of the fallback is coverage, not recency.
    """
    prompt = (
        "请用 x_search 检索 @%s 最近 60 天发过的帖文，挑出 %d 条最有实质内容的"
        "（排除纯致谢、寒暄、表情、单词回复）。只输出一个 JSON 对象：\n"
        '{"posts":[{"url":"完整帖文链接","date":"YYYY-MM-DD","text":"原文",'
        '"textZh":"完整中文译文","summaryZh":"30-60字中文摘要"}]}\n'
        "url 必须是该账号自己的帖文；text 逐字照录。检索不到就返回 {\"posts\":[]}，不要编造。"
        % (handle.lstrip("@"), MAX_POSTS))
    try:
        resp = x_search(prompt, handles=[handle], days=60, max_turns=3, key=key)
    except Exception as e:
        print("posts fallback failed:", str(e)[:160], flush=True)
        return []
    doc = parse_json(out_text(resp)) or {}
    out, seen = [], set()
    for p in (doc.get("posts") or []):
        if not isinstance(p, dict):
            continue
        u = str(p.get("url") or "").strip()
        if not STATUS_RX.match(u) or u in seen:
            continue
        seen.add(u)
        out.append({"url": u,
                    "date": str(p.get("date") or "").strip() or x_dt(u),
                    "text": str(p.get("text") or "").strip(),
                    "textZh": str(p.get("textZh") or "").strip(),
                    "summaryZh": str(p.get("summaryZh") or "").strip()})
    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    return out[:MAX_POSTS]


# ─────────────────────────── translation top-up ───────────────────────────
def translate_missing(profile_doc, key):
    """Fill textZh / summaryZh for posts the model returned without Chinese."""
    need = []
    for p in profile_doc.get("posts") or []:
        if p.get("text") and not p.get("textZh"):
            need.append((p, "textZh", p["text"]))
        if not p.get("summaryZh"):
            src = p.get("textZh") or p.get("text")
            if src:
                need.append((p, "summaryZh", src[:600]))
    if not need:
        return
    lines = "\n\n".join("### %d\n%s" % (i, s) for i, (_, _, s) in enumerate(need))
    prompt = ("把下面每条英文帖文翻译成简体中文。只输出 JSON 对象，键为编号字符串，值为译文。"
              "summaryZh 那几条请译成 1 句中文摘要（30-60 字）。\n\n" + lines)
    payload = {"model": TRANSLATE_MODEL,
               "messages": [{"role": "system", "content": "你是国际新闻编译，逐条完整翻译，不概括不删减。"},
                            {"role": "user", "content": prompt}],
               "temperature": 0, "response_format": {"type": "json_object"}}
    try:
        doc = _post("/chat/completions", payload, key, timeout=180, tries=2)
        txt = (doc.get("choices") or [{}])[0].get("message", {}).get("content", "")
        got = parse_json(txt) or {}
        for i, (p, field, _) in enumerate(need):
            v = got.get(str(i)) or got.get(i)
            if isinstance(v, str) and v.strip():
                p[field] = v.strip()
    except Exception as e:
        print("translate top-up failed:", str(e)[:160], flush=True)


# ─────────────────────────── cache ───────────────────────────
def _cache():
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with io.open(CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def _save_cache(c):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with io.open(CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(c, fh, ensure_ascii=False, indent=1)


def cache_key(q):
    import hashlib
    return hashlib.sha1(re.sub(r"\s+", " ", q.strip().lower()).encode("utf-8")).hexdigest()[:16]


def search(query, fresh=False, handle=None, space=None):
    """Run (or reuse) a person search. Returns the result dict.

    `space` is a per-visitor dict supplied by cloud_app (see _load_space).
    When given, both the result cache and the "recent searches" list live in
    that visitor's own file, so two readers searching the same name never see
    each other's history and never read each other's cached result.
    """
    query = (query or "").strip()
    if len(query) < 2:
        raise ValueError("请输入至少 2 个字符的姓名或关键词")

    ck = cache_key(query + ("|" + handle if handle else ""))
    cache = space.setdefault("cache", {}) if space is not None else _cache()
    hit = cache.get(ck)
    if hit and not fresh:
        try:
            age_h = (time.time() - float(hit.get("_ts") or 0)) / 3600.0
            if age_h < CACHE_HOURS:
                return hit["result"]
        except Exception:
            pass

    key = api_key()
    if not key:
        raise RuntimeError("未配置 xAI 密钥，无法执行人物检索")

    cands = identify(query, key)
    if not cands:
        result = {"query": query, "candidates": [], "profile": None,
                  "note": "在 X 上未检索到与该姓名／关键词匹配的公开账号。"
                          "可以换用账号全名、英文名或加上机构名再试。"}
    else:
        chosen = None
        if handle:
            chosen = next((c for c in cands if c["handle"].lower() == handle.lower()), None)
        chosen = chosen or cands[0]
        prof = profile(chosen["handle"], query, key)
        if not prof.get("posts"):
            prof["posts"] = posts_fallback(chosen["handle"], key)
        translate_missing(prof, key)
        for f in ("name", "org", "role", "domain"):
            if not prof.get(f):
                prof[f] = chosen.get(f) or ""
        prof["why"] = chosen.get("why") or ""
        prof["recent"] = chosen.get("recent") or ""
        result = {"query": query, "candidates": cands, "profile": prof,
                  "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "lookbackDays": LOOKBACK_DAYS}

    cache[ck] = {"_ts": time.time(), "query": query,
                 "generatedAt": result.get("generatedAt") or
                 datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "result": result}
    # A per-visitor space stores whole profiles, so keep it bounded: 20 newest.
    if space is not None and len(cache) > 20:
        for k in sorted(cache, key=lambda k: cache[k].get("_ts") or 0)[:-20]:
            cache.pop(k, None)
    if space is None:
        _save_cache(cache)          # per-visitor spaces are persisted by the caller
    return result


def recent(limit=8, space=None):
    cache = space.get("cache", {}) if space is not None else _cache()
    rows = sorted(cache.values(), key=lambda r: -(r.get("_ts") or 0))[:limit]
    return [{"query": r.get("query", ""), "at": r.get("generatedAt", "")} for r in rows]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    fresh = "--fresh" in sys.argv
    if not args:
        print("usage: python person_search.py \"<name or keyword>\" [--fresh]")
        return
    t0 = time.time()
    res = search(args[0], fresh=fresh)
    print(json.dumps(res, ensure_ascii=False, indent=1)[:4000])
    print("\n(%.0fs)" % (time.time() - t0))


if __name__ == "__main__":
    main()
