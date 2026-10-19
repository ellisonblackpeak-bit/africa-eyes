# -*- coding: utf-8 -*-
"""X people discovery + profiling for the Africa Eyes monitor.

Given a watchlist of topics (people_x_watch.json), find the X accounts that
post about each topic most substantively, then build a per-person profile from
their recent posts: which fields their posting concentrates in, what role they
appear to hold (journalist / analyst / official / org account), and a few
representative posts with links and dates.

Everything goes through the xAI x_search server-side tool via the local
xsearch.py CLI, i.e. Grok does the search and the synthesis; this script only
orchestrates, parses, decodes post timestamps and writes the raw snapshot.

Cost: one discovery call per topic (max_turns=3) plus one deepen call per
discovered handle (max_turns=2). With one topic and ~5 handles that is about
6 calls ≈ $0.6-0.9 per day. Runs at most once per day — daily_update.py skips
this script when today's snapshot already exists.

Output: data/raw/xprofiles_YYYYMMDD.json
"""
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "raw")
os.makedirs(OUT, exist_ok=True)

XSEARCH = r"C:\Users\A\.workbuddy-ai\mcp-servers\xai-xsearch-mcp\xsearch.py"
XSEARCH_PY = r"C:\Users\A\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe"

WATCH_PATH = os.path.join(HERE, "people_x_watch.json")
DEFAULT_WATCH = {
    "topics": [
        {"id": "mplj_fpl_security",
         "label": "MPLJ／FPL 相关安全风险",
         "query": ("MPLJ FPL Niger Agadez security risk — find the journalists, "
                   "analysts and researchers who post about this most often")},
    ]
}

# Profile material window. The main feeds enforce a strict 3-day window; a
# person profile needs a wider sample or most accounts would show nothing.
PROFILE_DAYS = 14
MAX_HANDLES_PER_TOPIC = 5

EP_X = 1288834974657


def x_post_dt(url):
    m = re.search(r"/(?:status|statuses)/(\d{15,25})", url or "")
    if not m:
        return None
    ms = (int(m.group(1)) >> 22) + EP_X
    dt = datetime.fromtimestamp(ms / 1000.0, timezone.utc)
    return dt if 2020 <= dt.year <= 2030 else None


def xai_call(query, handles=None, days=PROFILE_DAYS, max_turns=2):
    """One xsearch.py call. Returns the parsed JSON dict or None."""
    now = datetime.now(timezone.utc)
    cmd = [XSEARCH_PY, XSEARCH, query,
           "--from", (now - timedelta(days=days)).strftime("%Y-%m-%d"),
           "--to", now.strftime("%Y-%m-%d"),
           "--max-turns", str(max_turns), "--json"]
    if handles:
        cmd += ["--handles", ",".join(handles)]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=300)
        raw = p.stdout.decode("utf-8", "replace").strip()
        if not raw:
            print("   !! empty stdout:", p.stderr.decode("utf-8", "replace")[:160])
            return None
        return json.loads(raw)
    except Exception as e:
        print("   !! xai call failed:", str(e)[:140])
        return None


HANDLE_RX = re.compile(r"@([A-Za-z0-9_]{2,20})")
STATUS_RX = re.compile(r"https?://(?:twitter\.com|x\.com)/[A-Za-z0-9_]+/(?:status|statuses)/\d+")


def discover(topic):
    """Phase 1: ask Grok to find the accounts for this topic and return them in
    a strict pipe-separated format that survives parsing."""
    q = (
        "主题：%s。\n"
        "请找出在 X 上最常发布该主题内容的账号（记者、研究人员、分析师、机构账号优先），"
        "至多 %d 个。对每个账号，请严格按以下单行格式输出（竖线分隔，不要输出其他任何内容）：\n"
        "@handle | 显示名 | 角色判断 | 集中领域1,集中领域2 | 一句话画像（中文） | 代表帖文URL1,代表帖文URL2\n"
        "如果某字段无法确定就写「不明」。代表帖文必须是该账号自己发的帖子的完整链接。"
        % (topic["query"], MAX_HANDLES_PER_TOPIC))
    print(">> discover:", topic["id"], flush=True)
    d = xai_call(q, max_turns=3)
    if not d:
        return [], ""
    answer = (d.get("answer") or "").strip()
    cost = float(d.get("estimated_tool_cost_usd") or 0)
    print("   discovery cost ~$%.2f" % cost, flush=True)

    people = []
    for line in answer.splitlines():
        line = line.strip().strip("|").strip()
        if not line.startswith("@"):
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 5:
            continue
        handle = cells[0].lstrip("@")
        urls = []
        if len(cells) >= 6:
            urls = STATUS_RX.findall(cells[5])
        people.append({
            "handle": "@" + handle,
            "name": cells[1] if cells[1] != "不明" else "",
            "roleGuess": cells[2] if cells[2] != "不明" else "",
            "topics": [t.strip() for t in re.split(r"[,，、]", cells[3]) if t.strip() and t.strip() != "不明"],
            "discoveryNote": cells[4],
            "postUrls": urls[:4],
        })
    print("   parsed %d account rows" % len(people), flush=True)
    return people[:MAX_HANDLES_PER_TOPIC], answer


def deepen(person):
    """Phase 2: one focused call on the handle's own recent posts."""
    h = person["handle"].lstrip("@")
    q = ("分析 @%s 这个 X 账号最近两周的帖文。请用中文回答："
         "1) 帖文集中在哪些领域/议题（列出 2-4 个）；"
         "2) 这个账号的角色与立场（记者/分析师/官员/机构/活动人士等，依据其自述与发帖内容推断）；"
         "3) 一段 80—150 字的人物画像；"
         "4) 列出 2-3 条代表性帖子的完整链接。" % h)
    print(">> deepen @%s" % h, flush=True)
    d = xai_call(q, handles=[h], max_turns=2)
    if not d:
        return
    answer = (d.get("answer") or "").strip()
    cost = float(d.get("estimated_tool_cost_usd") or 0)
    print("   deepen cost ~$%.2f" % cost, flush=True)
    if answer:
        person["summaryZh"] = answer[:1200] + ("…" if len(answer) > 1200 else "")
    # Supplement representative posts from the deepen call's own citations.
    for c in d.get("citations") or []:
        u = c.get("url") or ""
        if "/status/" in u and u not in person["postUrls"]:
            person["postUrls"].append(u)


def main():
    watch = DEFAULT_WATCH
    if os.path.exists(WATCH_PATH):
        try:
            with io.open(WATCH_PATH, encoding="utf-8") as fh:
                watch = json.load(fh)
        except Exception as e:
            print("watch file unreadable, using defaults:", e)

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path = os.path.join(OUT, "xprofiles_%s.json" % today)

    all_profiles = []
    total_cost = 0.0
    for topic in watch.get("topics", []):
        people, raw_answer = discover(topic)
        for p in people:
            p["topicId"] = topic["id"]
            p["topicLabel"] = topic["label"]
            deepen(p)
            # Decode dates for representative posts; keep only dated ones so the
            # page can show a real date next to every link.
            posts = []
            for u in p["postUrls"]:
                dt = x_post_dt(u)
                posts.append({"url": u,
                              "date": dt.strftime("%Y-%m-%d") if dt else ""})
            p["posts"] = posts[:5]
            p.pop("postUrls", None)
            p["engine"] = "xai-xsearch"
            all_profiles.append(p)
            time.sleep(3)
        time.sleep(3)

    rec = {"generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "profileDays": PROFILE_DAYS,
           "profiles": all_profiles}
    with io.open(out_path, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=1)
    print("profiles written: %d -> %s" % (len(all_profiles), out_path), flush=True)


if __name__ == "__main__":
    main()
