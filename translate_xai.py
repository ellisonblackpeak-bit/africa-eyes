# -*- coding: utf-8 -*-
"""Automatic English→Chinese translation for the Africa Eyes queue.

Why this exists
---------------
Until now the translation step was the one part of the pipeline that needed an
agent in the loop: build_data.py writes data/queue.json, and a human-driven
session had to read it, translate it, and call apply_zh.py. That made "refresh
every hour, unattended" impossible — the 7am run would fetch fresh English copy
and the site would sit there showing 待译 until someone showed up.

This script closes that gap with a plain xAI chat-completions call. Translation
is a mechanical task, so a fast non-reasoning model is both accurate enough and
cheap (measured ~$0.0014 for a short pair; a full 90-item hour with article
bodies lands around $0.30).

Design
------
* Batches by *token budget*, not item count: titles are tiny, article bodies are
  up to 4000 chars, so a fixed batch size would either waste calls on titles or
  blow the context on bodies.
* Asks for a strict JSON object back and validates every key — a missing key is
  simply retried in a second, smaller batch; a malformed reply never poisons the
  cache.
* Parallel-safe with the cache: results go into data/zh_auto.json and then
  through apply_zh.py, so the same content hash never gets translated twice
  across runs.
* Glossary is passed in the system prompt so entity names stay consistent with
  the batches that were translated by hand.

Usage:
  python translate_xai.py            # translate the whole queue
  python translate_xai.py --chars 300000   # stop after ~300k source chars
  python translate_xai.py --limit 40 # cap items this run
  python translate_xai.py --model grok-4.20-0309-non-reasoning
"""
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
QUEUE = os.path.join(HERE, "data", "queue.json")
CACHE = os.path.join(HERE, "data", "zh_cache.json")
OUT = os.path.join(HERE, "data", "zh_auto.json")

# ── providers ──
# Two interchangeable chat-completions back-ends. The default is MiniMax-M3 when
# its key is present, xAI Grok otherwise: MiniMax is reachable from this machine
# (api.x.ai is not — the system proxy blocks it) and its M3 model is cheaper per
# character while translating these news items at least as well.
#
# `TRANSLATE_PROVIDER` overrides the choice; an unknown value falls back to auto.
# Whichever runs, the other is kept as a fallback, so a provider outage degrades
# the Chinese coverage instead of failing the whole refresh.
PROVIDERS = {
    "minimax": {
        "url": lambda: (os.environ.get("MINIMAX_BASE_URL")
                        or "https://api.minimaxi.com/v1").rstrip("/") + "/chat/completions",
        "model": lambda: os.environ.get("MINIMAX_MODEL") or "MiniMax-M3",
        "key": lambda: (os.environ.get("MINIMAX_API_KEY") or "").strip(),
        # M3 answers with a <think> block unless thinking is switched off, and
        # those tokens are billed. Off, the same translation costs ~15 tokens
        # instead of ~72.
        "extras": {"thinking": {"type": "disabled"}},
        "json_mode": True,
    },
    "xai": {
        "url": lambda: os.environ.get("XAI_BASE_URL")
                        or "https://api.x.ai/v1/chat/completions",
        "model": lambda: os.environ.get("XAI_MODEL")
                         or "grok-4.20-0309-non-reasoning",
        "key": lambda: "",       # filled by api_key() below, which has fallbacks
        "extras": {},
        "json_mode": True,
    },
}


def pick_provider():
    """Which back-end to translate with, and which to fall back to.

    MiniMax-M3 first, xAI second — the reader's instruction (2026-10-22):
    "如果 minimax-m3 能实现，优先 minimax-m3". It is cheaper per character and
    translates this copy well, so it takes the ordinary load.

    What it cannot always take, measured on this site's own content:

      · Its content filter rejects batches carrying kidnapping, sanctions or
        militant attacks — 422 new_sensitive (1027) on a four-item batch whose
        subjects were exactly that. Ordinary economic and political copy
        passes; the security copy is what trips it.
      · Its JSON occasionally breaks at production batch sizes (batch 3 of 66
        in the 12:51 run, JSONDecodeError at line 12 column 28, three retries).

    Neither is fatal any more, because the caller now degrades per batch:
    a 422 or a broken parse splits the batch and retries on xAI for the items
    M3 could not return. So M3 handles the bulk and Grok handles the exceptions,
    instead of one bad batch poisoning the whole run.
    """
    want = (os.environ.get("TRANSLATE_PROVIDER") or "").strip().lower()
    have = {name: bool(cfg["key"]()) for name, cfg in PROVIDERS.items()}
    have["xai"] = bool(api_key())
    if want in PROVIDERS and have.get(want):
        first = want
    elif have.get("minimax"):
        first = "minimax"
    elif have.get("xai"):
        first = "xai"
    else:
        first = "xai"
    rest = [n for n in PROVIDERS if n != first and have.get(n)]
    return first, (rest[0] if rest else None)

# ~2600 chars of source per call keeps the reply comfortably inside the output
# limit while a 4000-char article body still fits in one or two calls.
BATCH_CHARS = 2600

GLOSSARY = (
    "Barrick→巴里克，Orano→欧安诺，Somaïr/SOMAIR→索迈尔，Arlit→阿尔利特，"
    "Imouraren→伊穆拉伦，ECOWAS→西非国家经济共同体，AES→萨赫勒国家联盟，"
    "JNIM→支持伊斯兰与穆斯林组织，ISWAP→伊斯兰国西非省，"
    "NNPC→尼日利亚国家石油公司，NCDMB→尼日利亚本地内容开发与监督委员会，"
    "GoldBod→加纳黄金局，PINL→尼日利亚管道与储运公司，GIPA→加纳投资促进中心，"
    "MPLJ→争取正义与解放运动，FPL→解放阵线党，CBN→尼日利亚中央银行，"
    "NERC→尼日利亚电力监管委员会，EFCC→经济和金融犯罪委员会，"
    "INEC→独立国家选举委员会，NAFDAC→国家食品药品监督管理总局"
)

SYSTEM = (
    "你是国际新闻编译。把用户给出的英文原文逐条翻译成简体中文，只输出一个 JSON 对象，"
    "键与输入完全一致，值是对应中文。规则：\n"
    "1. 逐条完整翻译，不概括、不删减、不合并；field=body 的是新闻全文，必须整篇译完。\n"
    "2. 专有名词按术语表统一：" + GLOSSARY + "。表外专名保留英文，不音译生造。\n"
    "3. 原文以「……」或「...」结尾表示被截断，译文结尾保留「……」。\n"
    "4. 保留数字、金额、日期、机构缩写；货币符号保留原样。\n"
    "5. 不加评论、不补充原文没有的事实、不添加标题或说明文字。\n"
    "6. 严禁把英文原文照抄进译文：每个值都必须含中文汉字，否则视为未完成。\n"
    "7. field=title 的是新闻标题：译成中文报刊标题的写法——补出被英文省略的主语"
    "（国家、机构、人物），动词用主动语态，不要写成名词堆叠；"
    "不要把「U.S.」「U.K.」译成半截的「U.」；标题不保留英文站名。\n"
    "8. 译文要通顺合规，不出现「的的」「了了」这类叠字，也不留英文语序的"
    "长定语（如「由…所进行的…」改为「…进行的…」）。"
)


# ── translation validation ────────────────────────────────────────────────
# A reply only counts as a translation if it actually contains Chinese. On the
# 14:12 build, 72 cache entries held the English source verbatim — the model had
# echoed the input — and every "is title_zh set?" check downstream said the page
# was fully translated while 52 headlines stayed English. Validate characters,
# never emptiness.
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def has_cn(s):
    return bool(_CJK.search(s or ""))


def zh_ok(en, zh):
    if not zh or not str(zh).strip():
        return False
    if has_cn(zh):
        return True
    if has_cn(en):                     # source already Chinese — identity is fine
        return True
    return not re.search(r"[A-Za-z]", en or "")


def api_key():
    k = (os.environ.get("XAI_API_KEY") or "").strip()
    if k:
        return k
    # Project-local key file — this is what the cloud service reads, because a
    # deployed sandbox has no ~/.workbuddy-ai to look in. The server's allowlist
    # never serves this file, so it stays private even though it ships with the
    # project.
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


class _BadOutput(Exception):
    """The provider answered, but the reply could not be parsed."""


def call(payload_items, provider, tries=3):
    """Translate one batch with `provider`. Returns {key: chinese} for what came back."""
    cfg = PROVIDERS[provider]
    body = "\n\n".join(
        "### %s\n%s" % (it["key"], it["en"]) for it in payload_items)
    prompt = (
        "翻译下面 %d 条。输出 JSON 对象，键为每条的 16 位哈希，值为中文译文。\n\n%s"
        % (len(payload_items), body))
    payload = {
        "model": cfg["model"](),
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 8000,
    }
    if cfg.get("json_mode"):
        payload["response_format"] = {"type": "json_object"}
    payload.update(cfg.get("extras") or {})
    data = json.dumps(payload).encode()
    url = cfg["url"]()
    key = cfg["key"]() or api_key()

    for attempt in range(tries):
        req = urllib.request.Request(
            url, data=data,
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
            txt = (doc.get("choices") or [{}])[0].get("message", {}).get("content", "")
            # M3 emits a <think> block when thinking is on (or when a caller
            # forgets to switch it off). Strip it before parsing rather than
            # failing the batch on it.
            txt = re.sub(r"<think>[\s\S]*?</think>", "", txt or "").strip()
            txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
            got = _salvage_json(txt, [it["key"] for it in payload_items])
            if isinstance(got, dict):
                return {k: str(v).strip() for k, v in got.items()
                        if isinstance(v, str) and v.strip()}
            return {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:160]
            print("      ! %s http %d %s" % (provider, e.code, detail), flush=True)
            if e.code == 429:
                time.sleep(20 * (attempt + 1))
                continue
            # 422 is MiniMax's content filter (new_sensitive). Retrying the same
            # batch cannot succeed, so fail fast and let the caller hand the
            # batch to the other provider.
            if e.code in (400, 404, 401, 403, 422):
                return {}
            time.sleep(6)
        except Exception as e:
            print("      ! %s %s %s" % (provider, type(e).__name__, str(e)[:120]),
                  flush=True)
            time.sleep(6)
    return {}


def _salvage_json(txt, keys):
    """Recover what can be recovered from a malformed batch reply.

    M3 occasionally emits a JSON object that breaks on a long Chinese value — a
    quote or a line break inside the string is enough. `json.loads` then throws
    away the WHOLE batch, and because that looks like a provider outage the run
    falls back to xAI and the cost saving is lost. Measured in the 2026-10-22
    cloud run: three JSONDecodeError, then "minimax returned nothing".

    So parse tolerantly: split on the key markers we ourselves put in the
    prompt ("### <hash>") and pull each value out on its own. Anything that
    still cannot be read is left for the retry loop to re-request.
    """
    out = {}
    if not txt:
        return out
    txt = re.sub(r"<think>[\s\S]*?</think>", "", txt)
    # Preferred path: the whole thing is valid JSON.
    try:
        got = json.loads(txt.strip())
        if isinstance(got, dict):
            return {k: str(v).strip() for k, v in got.items()
                    if isinstance(v, str) and v.strip()}
    except Exception:
        pass
    # Second: pull "key": "value" pairs individually, tolerating unescaped
    # quotes by matching up to the last quote before the next key or the brace.
    for k in keys:
        m = re.search(r'"%s"\s*:\s*"([\s\S]*?)"\s*(?:,\s*"|\}\s*$)' % re.escape(k),
                      txt)
        if not m:
            m = re.search(r'"%s"\s*:\s*"([\s\S]*)"' % re.escape(k), txt)
        if m:
            val = m.group(1)
            val = val.replace('\\"', '"').replace('\\n', ' ')
            if val.strip():
                out[k] = val.strip()
    return out


def merge_into_cache(keep):
    """Fold this run's translations into data/zh_cache.json.

    build_data.py only ever reads the cache, so without this step a translated
    queue sits in zh_auto.json and the page keeps showing 待译 — which is
    exactly the "titles and summaries are still in English" complaint. The
    merge is keyed by the same content hash, so it is idempotent.
    """
    if not keep:
        return 0
    cache = {}
    if os.path.exists(CACHE):
        try:
            with io.open(CACHE, encoding="utf-8") as fh:
                cache = json.load(fh) or {}
        except Exception:
            cache = {}
    before = len(cache)
    for k, v in keep.items():
        if isinstance(v, str) and v.strip():
            cache[k] = v.strip()
    # Purge entries that are not actually Chinese: they came from an earlier run
    # where the model echoed the source, and they poison every later build.
    dropped = [k for k, v in cache.items()
               if isinstance(v, str) and v.strip() and not has_cn(v)]
    for k in dropped:
        cache.pop(k, None)
    if dropped:
        print("purged %d non-Chinese cache entries" % len(dropped), flush=True)
    with io.open(CACHE, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=0)
    return len(cache) - before


def main():
    limit = None
    budget = 0
    provider, fallback = pick_provider()
    _P = [provider, fallback]      # mutable so the closure can switch provider

    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
        if a == "--chars" and i + 1 < len(args):
            budget = int(args[i + 1])
        if a == "--provider" and i + 1 < len(args):
            provider = args[i + 1].strip().lower()

    if not os.path.exists(QUEUE):
        print("no queue file — nothing to do")
        return
    with io.open(QUEUE, encoding="utf-8") as fh:
        queue = json.load(fh)
    cache = {}
    if os.path.exists(CACHE):
        try:
            with io.open(CACHE, encoding="utf-8") as fh:
                cache = json.load(fh)
        except Exception:
            cache = {}

    todo = [it for it in queue
            if it.get("key") and it.get("en")
            and not zh_ok(it["en"], cache.get(it["key"]))]
    # Titles and summaries first: they are what the page shows before any tap,
    # so a truncated run still leaves a fully-Chinese front page.
    order = {"title": 0, "summary": 1, "snippet": 1, "body": 2}
    todo.sort(key=lambda x: order.get(x.get("field"), 3))
    if limit:
        todo = todo[:limit]

    _pname = PROVIDERS[provider]["model"]()
    print("queue %d | cached %d | translating %d (provider: %s / %s)"
          % (len(queue), len(cache), len(todo), provider, _pname), flush=True)
    if fallback:
        print("   fallback available: %s (%s)"
              % (_P[1], PROVIDERS[_P[1]]["model"]()), flush=True)
    if not todo:
        return

    key = api_key()
    if not key:
        print("FATAL: no xAI key")
        sys.exit(1)

    # Pack into batches under BATCH_CHARS, but never split a body item.
    batches, cur, chars = [], [], 0
    for it in todo:
        n = len(it["en"])
        if cur and chars + n > BATCH_CHARS:
            batches.append(cur)
            cur, chars = [], 0
        cur.append(it)
        chars += n
    if cur:
        batches.append(cur)

    # Character budget instead of an item cap.
    #
    # The old `--limit 220` was the reason the 展开全文 panel kept showing English:
    # the queue is sorted titles → summaries → bodies, a full round holds well
    # over 220 strings once titles and summaries are counted, so the body items
    # at the tail were *never* reached. Every refresh translated the same front
    # matter and left 70 of 99 article bodies in English. A character budget
    # bounds the cost just as well while still letting the run walk past the
    # front matter into the bodies.
    if budget > 0:
        kept, used = [], 0
        for b in batches:
            n = sum(len(x["en"]) for x in b)
            if kept and used + n > budget:
                break
            kept.append(b)
            used += n
        print("budget %d chars -> %d/%d batches" % (budget, len(kept), len(batches)),
              flush=True)
        batches = kept
    print("batches:", len(batches), flush=True)

    done, keep = 0, {}

    def translate_batch(batch, depth=0):
        """Translate a batch; on a partial or total miss, split and retry.

        Three failure modes, three different responses — the distinction is
        what keeps MiniMax usable as the primary model:

          · CONTENT FILTER (422) or BROKEN JSON — the provider is alive but
            refused or garbled THIS batch. Send it to the other provider rather
            than retrying the same text, and do not abandon MiniMax for the
            whole run: the next batch is probably ordinary copy it handles
            happily and more cheaply.
          · TOTAL SILENCE — the endpoint itself is down. Switch providers for
            the rest of the run, once.
          · PARTIAL — some keys came back. Split the remainder and retry at
            half size, which is what fixed the 38-item batch that came back
            echoed in English.
        """
        prov = _P[0]
        other = _P[1]
        try:
            got = call(batch, prov)
        except _BadOutput:
            got = {}                       # provider answered, output unusable
        if (not got or len(got) < len(batch)) and other:
            # Hand the missed items to the other provider, keeping whatever the
            # first one already returned. This is the per-batch degradation:
            # M3 takes the bulk, Grok takes what M3 refuses.
            missing = [it for it in batch if not zh_ok(it["en"], got.get(it["key"]))]
            if missing and len(missing) == len(batch):
                print("   ~ %s missed all %d — retrying on %s"
                      % (prov, len(batch), other), flush=True)
            elif missing:
                print("   ~ %s missed %d/%d — retrying those on %s"
                      % (prov, len(missing), len(batch), other), flush=True)
            if missing:
                try:
                    got2 = call(missing, other)
                    got.update(got2)
                except _BadOutput:
                    pass
        good = {it["key"]: got[it["key"]] for it in batch
                if zh_ok(it["en"], got.get(it["key"]))}
        if len(good) == len(batch) or depth >= 3 or len(batch) == 1:
            return good
        rest = [it for it in batch if it["key"] not in good]
        mid = max(1, len(rest) // 2)
        for part in (rest[:mid], rest[mid:]):
            if part:
                good.update(translate_batch(part, depth + 1))
        return good

    for bi, batch in enumerate(batches, 1):
        print(">> batch %d/%d (%d items, %d chars)"
              % (bi, len(batches), len(batch), sum(len(x["en"]) for x in batch)), flush=True)
        good = translate_batch(batch)
        for k, v in good.items():
            keep[k] = v
        done += len(good)
        print("   -> %d/%d translated" % (len(good), len(batch)), flush=True)
        # Write incrementally so a crash mid-run never loses finished work.
        if keep:
            with io.open(OUT, "w", encoding="utf-8") as fh:
                json.dump(keep, fh, ensure_ascii=False, indent=1)
        left = [it for it in batch if it["key"] not in good]
        if left:
            print("   still untranslated after retries: %d" % len(left), flush=True)
        time.sleep(1)

    print("translated %d/%d -> %s" % (done, len(todo), OUT), flush=True)
    added = merge_into_cache(keep)
    print("merged into zh_cache.json (+%d entries)" % added, flush=True)
    # Tell the caller how much is still outstanding so a truncated run is
    # visible in the Actions log rather than being mistaken for "all done".
    left = len(todo) - done
    print("queue left after this run: %d string(s)" % left, flush=True)
    if left > 0:
        print("NEXT_ROUND_NEEDED", flush=True)


if __name__ == "__main__":
    main()
