# -*- coding: utf-8 -*-
"""Social platform scan (LinkedIn / X / Facebook / Instagram).

Wraps the firecrawl-social-search skill script one platform at a time. Firecrawl
enforces a per-minute request budget (~13 req/min observed); issue the four
platforms back to back and the later ones come back HTTP 429. So requests are
paced, and any platform that still fails is retried once after a longer wait.

Discovery only (no --content) — full-text extraction on X costs roughly
31 credits per item, which would burn the free tier in a few days.

X source toggle (env var USE_XAI_X):
  unset / "0"  → Firecrawl only (cheap, ~1 credit / call, but ~70% of
                 returned X posts come back undated and get dropped)
  "1"          → xAI X Search (per-call ~$0.14, 20+ post URLs, Grok
                 summary as snippet, date filter is enforced server-side)
  "both"       → run both, dedupe by post_id; gives widest coverage but
                 costs the most

Usage:
  python fetch_social.py            # full run, 6 topic angles x 4 platforms
  python fetch_social.py --quick    # 1 angle, for pipeline smoke-testing
"""
import glob
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SKILL = r"C:\Users\A\.workbuddy-ai\skills\firecrawl-social-search\scripts\social_search.py"
PY = r"C:\Users\A\WorkBuddy AI\2026-10-14-10-42-35\firecrawl-search\.venv\Scripts\python.exe"
XSEARCH = r"C:\Users\A\.workbuddy-ai\mcp-servers\xai-xsearch-mcp\xsearch.py"
XSEARCH_PY = r"C:\Users\A\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "raw")
os.makedirs(OUT, exist_ok=True)

# Cost ledger across one fetch_social.py run.
xai_cost = {"calls": 0, "posts_fetched": 0, "users_fetched": 0,
            "input_tokens": 0, "output_tokens": 0, "estimated_usd": 0.0}

QUERIES = [
    # Kept to 8 angles x 4 platforms x ~2 credits = ~64 credits per run. Post dates
    # are decoded and filtered afterwards, so extra angles mainly buy more
    # candidates; eight covers the priority tier-3 subject matter (government,
    # energy, mining, corridors) without multiplying the per-run cost.
    ("soc_ng_gov", "Nigeria Federal Government ministry announcement policy"),
    ("soc_ng_security", "Nigeria security politics government"),
    ("soc_ng_energy", "Nigeria NNPC refinery oil gas pipeline electricity"),
    ("soc_ng_mining", "Nigeria mining lithium gold minerals policy"),
    ("soc_sahel", "Niger Mali Sahel junta uranium military"),
    ("soc_gh", "Ghana government GoldBod mining gold economy policy"),
    ("soc_corridor", "Nigeria Morocco gas pipeline West Africa corridor project"),
    ("soc_china", "Nigeria Ghana China Chinese company project investment"),
]
PLATFORMS = ["x", "linkedin", "facebook", "instagram"]
PLAT_DELAY = 16          # keeps us under the per-minute request budget
RETRY_WAIT = 70          # longer pause before a single retry of a failed platform
TBS = "qdr-w"            # candidate window (past week); the authoritative 3-day cut is
                         # applied per post in build_data.py by decoding each post id.
                         # A tighter qdr-d window starves the index and returns less.
LIMIT = 14               # candidates per platform; strict date filtering later discards most

USE_XAI_X = os.environ.get("USE_XAI_X", "0").strip().lower()
# 0 / unset / "false" → Firecrawl only
# "1" / "true"        → xai only (Firecrawl X skipped)
# "both"              → both, dedupe by post_id

# A 3-day window for the xai date filter. Use UTC dates so they line up with
# what xai treats as inclusive start / exclusive end (their `to` is exclusive).
XAI_LOOKBACK_DAYS = int(os.environ.get("XAI_LOOKBACK_DAYS", "3"))
_now_utc = datetime.now(timezone.utc)
_xai_to = _now_utc.strftime("%Y-%m-%d")
_xai_from = (_now_utc - timedelta(days=XAI_LOOKBACK_DAYS)).strftime("%Y-%m-%d")


def run_xai_x_search(query):
    """Call xai-xsearch once for X and translate the result into a Firecrawl-
    compatible platform slot.

    Returns (slot_dict, used_fallback_bool). The slot shape matches what
    build_data.py reads from firecrawl_*.json, so no downstream change needed.

    - The Grok-generated `answer` becomes the snippet for every cited post.
    - We don't have per-post author or post text — the citation list only
      gives url + post_id. So `fulltext` is False and the source is marked
      "xai" so the page can label the snippet as "Grok 综合摘要" instead of
      "全文".
    - The `from_date..to_date` filter is enforced server-side, so all
      citations are in window by construction. build_data.py still decodes
      the post id and applies the 3-day cut as defense in depth.
    """
    print(">> xai / x :", query[:60], flush=True)
    cmd = [XSEARCH_PY, XSEARCH, query,
           "--from", _xai_from, "--to", _xai_to,
           "--max-turns", "2", "--json"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=180)
        raw = p.stdout.decode("utf-8", "replace").strip()
        if not raw:
            err = p.stderr.decode("utf-8", "replace")[:160]
            raise RuntimeError("empty stdout: " + err)
        data = json.loads(raw)
    except Exception as e:
        print("   !! xai call failed:", str(e)[:120], flush=True)
        return None, False

    # Cost ledger
    usage = data.get("usage") or {}
    xai_cost["calls"] += 1
    xai_cost["posts_fetched"] += int(usage.get("x_posts_fetched", 0) or 0)
    xai_cost["users_fetched"] += int(usage.get("x_users_fetched", 0) or 0)
    xai_cost["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
    xai_cost["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
    xai_cost["estimated_usd"] += float(data.get("estimated_tool_cost_usd") or 0)

    answer = (data.get("answer") or "").strip()
    cites = data.get("citations") or []
    # Keep only post URLs (status/...), drop user profile URLs and any oddities.
    post_cites = [c for c in cites if isinstance(c, dict)
                  and "/status/" in (c.get("url") or "")]
    if not post_cites:
        print("   -> xai returned no post citations", flush=True)
        return None, False

    # Use the first ~480 chars of the Grok answer as a shared snippet
    # prefix; each post entry can include the full answer for completeness.
    results = []
    seen = set()
    # All citations from a single xai call share the same Grok prose, so the
    # title must be the *topic* (not a copy of the prose) or we'd render
    # several identical-looking cards. Keep the answer in the snippet and
    # tag the URL → which one of the cited posts this card points at.
    for idx, c in enumerate(post_cites, 1):
        url = c.get("url", "").strip()
        post_id = c.get("post_id") or ""
        if not url or post_id in seen:
            continue
        seen.add(post_id)
        results.append({
            "url": url,
            "post_id": post_id,
            # Per-card title: "X 讨论：{query}" plus a tiny ordinal so the 5
            # cards from the same call don't collide in the page list. The
            # rich Grok prose lives in the snippet below.
            "title": ("X 讨论：%s（#%d）" % (query, idx)),
            "description": answer[:900] + ("…" if len(answer) > 900 else ""),
            "snippet": answer[:900] + ("…" if len(answer) > 900 else ""),
            "source": "xai-xsearch",
            "grok_summary": True,
        })
    print("   ->", len(results), "post citations  (~$%.2f)" %
          float(data.get("estimated_tool_cost_usd") or 0), flush=True)
    return {
        "name": "x",
        "label": "X",
        "kind": "social",
        "fulltext": False,         # Grok summary, not the post text
        "engine": "xai-xsearch",
        "model": data.get("model", "grok-4.7"),
        "window": {"from": _xai_from, "to": _xai_to},
        "answer_excerpt": answer[:600] + ("…" if len(answer) > 600 else ""),
        "tool_calls": len(data.get("tool_calls") or []),
        "results": results,
    }, True


if "--quick" in sys.argv:
    QUERIES = QUERIES[:1]
    PLATFORMS = PLATFORMS[:2]

total = 0
for tag, q in QUERIES:
    path = os.path.join(OUT, "firecrawl_%s.json" % tag)
    doc = {"query": q, "platforms": []}
    if os.path.exists(path):
        try:
            with io.open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            pass
    by_name = {p["name"]: p for p in doc.get("platforms", [])}

    # X platform has two possible backends (Firecrawl / xai / both). We route
    # it before the inner loop so the rest of the pacing + retry logic is
    # untouched.
    if "x" in PLATFORMS:
        ran_xai = False
        if USE_XAI_X in ("1", "true", "yes", "both"):
            xai_slot, ok = run_xai_x_search(q)
            if ok and xai_slot:
                by_name["x"] = xai_slot
                ran_xai = True
        if USE_XAI_X in ("0", "", "false", "no") or (
            USE_XAI_X == "both" or (USE_XAI_X in ("1", "true", "yes") and not ran_xai)
        ):
            # Run the Firecrawl X path; merge results when USE_XAI_X=both.
            pl = "x"
            got = None
            for attempt in (1, 2):
                print(">>", tag, "/", pl, "(try %d)" % attempt, flush=True)
                cmd = [PY, SKILL, q, "--platforms", pl, "--limit", str(LIMIT),
                       "--tbs", TBS, "--json"]
                try:
                    p = subprocess.run(cmd, capture_output=True, timeout=240)
                    new = json.loads(p.stdout.decode("utf-8", "replace"))
                    got = next((e for e in new.get("platforms", []) if e["name"] == pl), None)
                except Exception as e:
                    print("   !! ", str(e)[:100], flush=True)
                    got = None
                if got and got.get("results"):
                    print("   ->", len(got["results"]), "results", flush=True)
                    break
                err = str((got or {}).get("error", ""))[:80]
                if "429" in err and attempt == 1:
                    print("   rate limited, waiting %ds" % RETRY_WAIT, flush=True)
                    time.sleep(RETRY_WAIT)
                    continue
                print("   -> empty:", err or "no results", flush=True)
                break

            if ran_xai and USE_XAI_X == "both" and got and got.get("results"):
                # Merge: keep xai slot, append firecrawl results that have a
                # post_id not already present.
                have = {r.get("post_id") for r in by_name["x"].get("results", [])}
                fc_added = 0
                for r in got.get("results", []):
                    pid = r.get("post_id") or r.get("url", "").rsplit("/", 1)[-1]
                    if pid and pid not in have:
                        r["source"] = "firecrawl"
                        by_name["x"]["results"].append(r)
                        have.add(pid)
                        fc_added += 1
                by_name["x"]["merged_with_firecrawl"] = fc_added
            elif got and got.get("results"):
                by_name["x"] = got
            elif "x" not in by_name:
                by_name["x"] = {"name": "x", "label": "X", "kind": "social",
                                "fulltext": False, "error": "no results",
                                "engine": "xai-xsearch" if ran_xai else "firecrawl",
                                "results": []}
        # Persist immediately so a mid-run crash doesn't lose what we have.
        doc["platforms"] = list(by_name.values())
        with io.open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
        time.sleep(PLAT_DELAY)

    for pl in PLATFORMS:
        if pl == "x":
            continue  # already handled above
        got = None
        for attempt in (1, 2):
            print(">>", tag, "/", pl, "(try %d)" % attempt, flush=True)
            cmd = [PY, SKILL, q, "--platforms", pl, "--limit", str(LIMIT),
                   "--tbs", TBS, "--json"]
            try:
                p = subprocess.run(cmd, capture_output=True, timeout=240)
                new = json.loads(p.stdout.decode("utf-8", "replace"))
                got = next((e for e in new.get("platforms", []) if e["name"] == pl), None)
            except Exception as e:
                print("   !! ", str(e)[:100], flush=True)
                got = None
            if got and got.get("results"):
                print("   ->", len(got["results"]), "results", flush=True)
                break
            err = str((got or {}).get("error", ""))[:80]
            if "429" in err and attempt == 1:
                print("   rate limited, waiting %ds" % RETRY_WAIT, flush=True)
                time.sleep(RETRY_WAIT)
                continue
            print("   -> empty:", err or "no results", flush=True)
            break

        if got and got.get("results"):
            by_name[pl] = got
        elif pl not in by_name:
            by_name[pl] = {"name": pl, "label": pl, "kind": "social",
                           "fulltext": pl == "x", "error": "no results", "results": []}
        doc["platforms"] = list(by_name.values())
        with io.open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
        time.sleep(PLAT_DELAY)

    n = sum(len(p.get("results") or []) for p in doc["platforms"])
    total += n
    print("   =", tag, n, "items", flush=True)

print("social total:", total)
if xai_cost["calls"]:
    print("xai x_search: %d call(s) | %d posts | %d users | %d+%d tok | ~$%.2f" %
          (xai_cost["calls"], xai_cost["posts_fetched"], xai_cost["users_fetched"],
           xai_cost["input_tokens"], xai_cost["output_tokens"],
           xai_cost["estimated_usd"]))
