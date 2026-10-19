# -*- coding: utf-8 -*-
"""Africa Eyes — self-refreshing cloud service.

Why this exists (and why the static publish alone was not enough)
-----------------------------------------------------------------
`workbuddy_sites_deploy` uploads a *snapshot*: the cloud serves the files it was
given and nothing else. Whatever schedule runs the collectors lives on the
user's own machine, so the page stops refreshing the moment that machine is off.
Answering "publish it to the cloud so I don't have to keep my laptop on" needs
the refresh logic to live on the server, not just the output.

This file is that server. It is a single-port HTTP app that
  1. serves the site (an explicit allowlist — never the key files), and
  2. runs the collection pipeline itself on a timer, in a background thread,
     then swaps in the freshly built data.js.

What runs server-side vs. what does not
---------------------------------------
Works with nothing but a Python interpreter and outbound HTTPS:
  fetch_news.py        RSS feeds + GDELT (both keyless)
  fetch_articles.py    direct scrape of article pages (keyless)
  build_data.py        assembly, tiering, summaries, translation queue
  translate_xai.py     xAI chat completions (one key)
  make_digest.py       the daily Markdown digest for knowledge-base import

Skipped in the sandbox because they need something a sandbox does not have —
the Firecrawl connector proxy on 127.0.0.1, or a Chrome-based scraper:
  fetch_social.py, fetch_people_x.py, fetch_linkedin_bd.py
Those keep running on the local machine when it is on; the pool in
data/pool.json carries their items forward between runs, so social cards do not
disappear from the cloud page just because the API is local-only.
"""
import gzip
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "3000"))

# Refresh cadence. The reader asked for a 07:00 first edition and then one
# rebuild every three hours, so the schedule is a fixed grid anchored at 07:00
# Beijing time (07 / 10 / 13 / 16 / 19 / 22 / 01 / 04) rather than "every N
# minutes from boot" — a fixed grid means the 07:00 edition lands at 07:00 no
# matter when the container happened to start.
REFRESH_HOURS = int(os.environ.get("REFRESH_HOURS", "3"))
ANCHOR_HOUR = int(os.environ.get("ANCHOR_HOUR", "7"))
FIRST_DELAY = int(os.environ.get("FIRST_DELAY", "20"))
# Backwards-compatible: if REFRESH_MINUTES is set explicitly, honour it.
REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "0")) or (REFRESH_HOURS * 60)

# How old data.js may get before ANY incoming request is allowed to kick a
# rebuild. This is the belt to the scheduler's braces: a cloud sandbox can be
# suspended when idle, which would silently kill the background thread. With
# this, opening the page is itself enough to bring the data back up to date.
STALE_MINUTES = int(os.environ.get("STALE_MINUTES", str(max(30, REFRESH_MINUTES - 10))))

CST = timezone(timedelta(hours=8))

# Only these may be served. Everything else — key files, raw snapshots, the
# pipeline scripts themselves — is 404. This is the whole reason for a custom
# handler instead of SimpleHTTPRequestHandler on the directory.
ALLOW = {
    "index.html", "data.js", "manifest.json",
    "favicon-32.png", "apple-touch-icon.png", "apple-touch-icon-precomposed.png",
    "icon-192.png", "icon-192-maskable.png",
    "icon-512.png", "icon-512-maskable.png",
    "sw.js",
}


def py():
    return sys.executable or "python3"


def _limit_child():
    """Cap a pipeline child's address space so a runaway stage dies alone.

    Observed in the cloud: a rebuild cycle started at 13:04 and the container
    stopped answering at 13:05 — the shape of an OOM kill, where the kernel
    picks the largest process. If the child is capped, the child fails and the
    web process survives, which is the only outcome that keeps the link up.
    """
    try:
        import resource
        cap = int(os.environ.get("CHILD_MEM_MB", "640")) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    except Exception:
        pass


def run(script, extra=None, timeout=600, env_extra=None):
    """Run one pipeline stage. Never raises: a failed stage is logged and the
    next one still runs, so a single API outage cannot freeze the whole site."""
    cmd = [py(), os.path.join(ROOT, script)] + (extra or [])
    t0 = time.time()
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    env.update(env_extra or {})
    # Logged BEFORE the stage runs: if the container dies mid-cycle this line is
    # the only evidence of which stage took it down.
    log(">> %s" % script)
    try:
        p = subprocess.run(cmd, cwd=ROOT, timeout=timeout, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           preexec_fn=_limit_child if os.name != "nt" else None)
        out = (p.stdout or b"").decode("utf-8", "replace")
        tail = "\n".join(out.strip().splitlines()[-6:])
        log("[%s] rc=%d %.0fs\n%s" % (script, p.returncode, time.time() - t0, tail))
        return p.returncode
    except subprocess.TimeoutExpired:
        log("[%s] TIMEOUT after %ds" % (script, timeout))
        return 1
    except Exception as e:
        log("[%s] crashed: %s" % (script, e))
        return 1


_log_lock = threading.Lock()
_log_lines = []


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    line = "%s %s" % (stamp, msg)
    print(line, flush=True)
    with _log_lock:
        _log_lines.append(line)
        del _log_lines[:-120]


# ───────────────────── per-visitor search space ─────────────────────
# "Give everyone who opens this link their own search space, so my records are
# invisible to others and never mix with theirs."
#
# Identity is a random 20-hex id that the browser mints once and keeps in
# localStorage plus a first-party cookie. No account, no password, no email —
# the requirement was isolation, not a login wall. Everything a visitor
# produces (search history, cached person profiles, cached multi-source
# results) is written to data/spaces/<id>.json and is only ever read back with
# that same id. A visitor without an id still gets a working search; it simply
# is not persisted.
SPACE_DIR = os.path.join(ROOT, "data", "spaces")
SPACE_RE = re.compile(r"^[0-9a-f]{16,32}$")
SPACE_HISTORY = 60       # keep the last N searches per visitor
_space_lock = threading.Lock()


def _space_path(cid):
    return os.path.join(SPACE_DIR, cid + ".json")


def _load_space(cid):
    if not cid or not SPACE_RE.match(cid):
        return {}
    try:
        with io.open(_space_path(cid), encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def _save_space(cid, obj):
    if not cid or not SPACE_RE.match(cid):
        return
    with _space_lock:
        try:
            os.makedirs(SPACE_DIR, exist_ok=True)
            tmp = _space_path(cid) + ".tmp"
            with io.open(tmp, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, ensure_ascii=False)
            os.replace(tmp, _space_path(cid))
        except Exception:
            log("space save failed for %s\n%s" % (cid, traceback.format_exc()))


def _note_search(space, q, kind, job=""):
    """Append one entry to this visitor's own history (newest first)."""
    if not space.get("created"):
        space["created"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    space["lastSeen"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    hist = space.setdefault("history", [])
    hist.insert(0, {"q": q, "kind": kind, "job": job,
                    "at": datetime.now(CST).strftime("%Y-%m-%d %H:%M")})
    del hist[SPACE_HISTORY:]


# ───────────────────────── interactive person search ─────────────────────────
# Each search is two Grok x_search calls (identify → profile) and takes
# 60-180s. Running it inside the request thread would hold a socket open long
# enough for mobile networks to drop it, so searches run as background jobs and
# the page polls /api/person/result.
_jobs = {}
_jobs_lock = threading.Lock()
JOB_TTL = 1800          # forget finished jobs after 30 minutes
JOB_MAX = 40            # cap memory; oldest finished jobs are dropped first


def _job_gc():
    now = time.time()
    with _jobs_lock:
        for k in [k for k, v in _jobs.items() if now - v.get("_ts", 0) > JOB_TTL]:
            _jobs.pop(k, None)
        if len(_jobs) > JOB_MAX:
            done = sorted([kv for kv in _jobs.items() if kv[1].get("state") != "running"],
                          key=lambda kv: kv[1].get("_ts", 0))
            for k, _ in done[:len(_jobs) - JOB_MAX]:
                _jobs.pop(k, None)


def _run_person_job(jid, q, fresh, handle, cid=""):
    t0 = time.time()
    try:
        import person_search
        space = _load_space(cid)
        res = person_search.search(q, fresh=fresh, handle=handle,
                                   space=space if cid else None)
        if cid:
            _save_space(cid, space)
        with _jobs_lock:
            _jobs[jid].update({"state": "done", "result": res,
                               "elapsed": round(time.time() - t0, 1)})
        log("[person] %r done in %.0fs (%d candidate(s))"
            % (q, time.time() - t0, len(res.get("candidates") or [])))
    except Exception as e:
        log("[person] %r failed: %s" % (q, str(e)[:300]))
        with _jobs_lock:
            _jobs[jid].update({"state": "error", "error": str(e)[:400],
                               "elapsed": round(time.time() - t0, 1)})


def start_person_job(q, fresh=False, handle="", cid=""):
    _job_gc()
    jid = "%x-%x" % (int(time.time() * 1000), abs(hash((q, handle))) % 0xFFFFFF)
    with _jobs_lock:
        _jobs[jid] = {"state": "running", "query": q, "handle": handle,
                      "cid": cid,
                      "startedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "_ts": time.time()}
    threading.Thread(target=_run_person_job, args=(jid, q, fresh, handle, cid),
                     daemon=True).start()
    return jid


# ───────────────────── unified multi-source search ─────────────────────
# "Put xAI, Tavily, Bright Data and the rest into the link itself, callable."
# One query fans out to every provider that is actually configured, in
# parallel, and returns the results grouped by source. A provider that is
# missing or failing never blocks the others — its error is reported alongside.
_query_cache = {}
_query_lock = threading.Lock()
QUERY_TTL = 900          # 15 minutes


def _run_query_job(jid, q, days, cid=""):
    t0 = time.time()
    try:
        import sources
        ck = (q.lower(), days)
        space = _load_space(cid) if cid else {}
        # The 15-minute result cache is scoped to the visitor when we know who
        # they are: two readers asking the same question get their own copy,
        # so nothing one of them searched ever surfaces for the other.
        qc = space.setdefault("qcache", {}) if cid else None
        store = qc if qc is not None else _query_cache
        sk = "%s|%d" % ck if cid else ck
        hit = store.get(sk)
        if hit and time.time() - hit["_ts"] < QUERY_TTL:
            res = hit["result"]
        else:
            res = sources.unified(q, days=days)
            store[sk] = {"_ts": time.time(), "result": res}
            if cid:
                if len(store) > 40:
                    for k in sorted(store, key=lambda k: store[k]["_ts"])[:10]:
                        store.pop(k, None)
                space["qcache"] = store
                _save_space(cid, space)
            else:
                with _query_lock:
                    if len(_query_cache) > 60:
                        for k in sorted(_query_cache, key=lambda k: _query_cache[k]["_ts"])[:20]:
                            _query_cache.pop(k, None)
        with _jobs_lock:
            _jobs[jid].update({"state": "done", "result": res,
                               "elapsed": round(time.time() - t0, 1)})
        log("[query] %r done in %.0fs (%d group(s))"
            % (q, time.time() - t0, len(res.get("groups") or [])))
    except Exception as e:
        log("[query] %r failed: %s" % (q, str(e)[:300]))
        with _jobs_lock:
            _jobs[jid].update({"state": "error", "error": str(e)[:400],
                               "elapsed": round(time.time() - t0, 1)})


def start_query_job(q, days=3, cid=""):
    _job_gc()
    jid = "q%x-%x" % (int(time.time() * 1000), abs(hash(q)) % 0xFFFFFF)
    with _jobs_lock:
        _jobs[jid] = {"state": "running", "query": q, "days": days, "cid": cid,
                      "startedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "_ts": time.time()}
    threading.Thread(target=_run_query_job, args=(jid, q, days, cid), daemon=True).start()
    return jid


def refresh_once(fast=False):
    """One full cycle: collect → build → translate → digest.

    Every collector used here reaches its provider over plain HTTPS, so the
    deployed service no longer depends on the reader's laptop being on:
      fetch_news.py       Tavily (direct API) → RSS → GDELT
      fetch_articles.py   direct scrape of article pages
      fetch_social_x.py   X through xAI's x_search (self-throttled to 3h)
      build_data.py       assembly, tiering, summaries, translation queue
      translate_xai.py    xAI chat completions
      make_digest.py      Markdown digest for knowledge-base import

    `fast=True` is the catch-up path used when a page visit wakes a sleeping
    container: RSS only, no paid Tavily angles, no rate-limited GDELT. The
    reader is waiting, so a two-minute refresh beats a nine-minute one.
    """
    log("=== refresh cycle start%s ===" % (" (fast)" if fast else ""))
    news_env = {"NEWS_FAST": "1"} if fast else {}
    run("fetch_news.py", env_extra=news_env)
    run("fetch_articles.py")      # direct scrape of the article bodies
    run("fetch_social_x.py")      # X via xAI; skips itself if run < 3h ago
    run("build_data.py")
    # Cap the translation batch per cycle. An uncapped pass can hold hundreds of
    # strings at once; a bounded batch keeps each cycle short and spreads any
    # backlog across the next slot instead of blowing up one run.
    if run("translate_xai.py", ["--limit", os.environ.get("TRANSLATE_LIMIT", "150")]) == 0:
        run("build_data.py")      # rebuild so the Chinese lands in data.js
    run("make_digest.py")
    log("=== refresh cycle done ===")


_run_lock = threading.Lock()
_kick_lock = threading.Lock()
_last_kick = [0.0]
_last_done = [""]
START_TS = time.time()


def data_generated_at():
    """The build stamp *inside* data.js.

    File mtime is not trustworthy here: deploying re-uploads the directory and
    the copy in the sandbox gets a fresh mtime, which would make a stale build
    look brand new. The `generatedAt` field is written by build_data.py and
    survives upload, so it is the only honest answer to "when was this built".
    """
    try:
        with io.open(os.path.join(ROOT, "data.js"), encoding="utf-8") as fh:
            m = re.search(r'"generatedAt":\s*"([^"]+)"', fh.read(4000))
        if m:
            return datetime.fromisoformat(m.group(1))
    except Exception:
        pass
    return None


def data_age_min():
    dt = data_generated_at()
    if dt is None:
        return 1e9
    return max(0.0, (datetime.now(CST) - dt).total_seconds() / 60.0)


def _safe_refresh(tag, fast=False):
    """Run one cycle, never two at once. A second caller is told to stand down
    rather than queueing up: the pipeline is expensive and the next scheduled
    slot will pick up whatever the running cycle missed."""
    if not _run_lock.acquire(blocking=False):
        log("refresh already running — %s stands down" % tag)
        return False
    try:
        refresh_once(fast=fast)
        _last_done[0] = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
        return True
    except Exception:
        log("cycle failed (%s)\n%s" % (tag, traceback.format_exc()))
        return False
    finally:
        _run_lock.release()


def maybe_refresh(reason="request"):
    """Kick a rebuild if data.js has gone stale.

    The scheduler below is the primary mechanism, but a cloud sandbox may be
    suspended while idle and that kills background threads. Tying freshness to
    incoming traffic means "open the page and it catches up", which is the
    behaviour the reader actually notices. Rate-limited so a page poll every
    60s cannot start a cycle storm.
    """
    age = data_age_min()
    if age < STALE_MINUTES and not slot_missed():
        return False
    with _kick_lock:
        if time.time() - _last_kick[0] < 600:
            return False
        _last_kick[0] = time.time()
    log("data.js is %.0f min old — refresh triggered by %s" % (age, reason))
    threading.Thread(target=_safe_refresh, args=("on-demand",), kwargs={"fast": True},
                     daemon=True).start()
    return True


def _slot_hours():
    step = max(1, REFRESH_HOURS)
    return sorted({(ANCHOR_HOUR + i * step) % 24 for i in range(24 // step)})


def next_slot(now=None):
    """Next 07:00-anchored slot on the 3-hour grid, in Beijing time."""
    now = now or datetime.now(CST)
    hours = _slot_hours()
    for day in (0, 1):
        base = (now + timedelta(days=day)).replace(minute=0, second=0, microsecond=0)
        for h in hours:
            cand = base.replace(hour=h)
            if cand > now + timedelta(seconds=20):
                return cand
    return now + timedelta(hours=max(1, REFRESH_HOURS))


def last_slot(now=None):
    """The most recent slot that has already passed."""
    now = now or datetime.now(CST)
    hours = _slot_hours()
    for day in (0, -1):
        base = (now + timedelta(days=day)).replace(minute=0, second=0, microsecond=0)
        for h in reversed(hours):
            cand = base.replace(hour=h)
            if cand <= now:
                return cand
    return now - timedelta(hours=max(1, REFRESH_HOURS))


def slot_missed():
    """True when a scheduled slot has passed but data.js predates it.

    This is the part that survives a restart. The cloud sandbox can be stopped
    and started at any moment — observed in practice: a container that booted at
    12:48 was replaced at 13:02, so the 13:00 slot fired into a dead process and
    the page kept the morning build. Comparing data.js mtime against the last
    slot makes the schedule self-healing: whenever the process comes back, it
    notices the missed slot and rebuilds immediately instead of waiting for the
    next one.
    """
    try:
        mt = data_generated_at()
    except Exception:
        mt = None
    if mt is None:
        return True
    return mt < last_slot()


def loop():
    time.sleep(FIRST_DELAY)
    log("scheduler armed | slots %s CST | next %s"
        % ("/".join("%02d" % h for h in _slot_hours()),
           next_slot().strftime("%m-%d %H:%M")))
    while True:
        try:
            if slot_missed():
                missed = last_slot().strftime("%m-%d %H:%M")
                # Never hammer a failing pipeline: one attempt per 10 minutes.
                if time.time() - _last_kick[0] > 600:
                    _last_kick[0] = time.time()
                    log("slot %s passed without a rebuild — catching up now" % missed)
                    _safe_refresh("missed slot %s" % missed)
        except Exception:
            log("scheduler error\n" + traceback.format_exc())
        time.sleep(60)


class Handler(SimpleHTTPRequestHandler):
    server_version = "AfricaEyes"

    def _resolve(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/", ""):
            path = "/index.html"
        name = os.path.basename(path)
        if name in ALLOW:
            return os.path.join(ROOT, name)
        return None

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _cid(self, qs=None):
        """The visitor's own space id, from the query string or the header."""
        v = ""
        if qs:
            v = (qs.get("cid") or [""])[0].strip()
        if not v:
            v = (self.headers.get("X-Client-Id") or "").strip()
        return v if SPACE_RE.match(v or "") else ""

    # ── interactive person search ─────────────────────────────────────────
    # Two-step on purpose. A search is two Grok calls with the x_search tool
    # and routinely takes 60-180s; a single long-lived GET gets dropped by
    # mobile networks and intermediate proxies. So the start call returns a job
    # id immediately and the page polls the result.
    def _api_person(self, qs):
        q = (qs.get("q") or [""])[0].strip()
        if len(q) < 2:
            self._json({"error": "请输入至少 2 个字符的姓名或关键词"}, 400)
            return
        fresh = (qs.get("fresh") or ["0"])[0] in ("1", "true", "yes")
        handle = (qs.get("handle") or [""])[0].strip()
        cid = self._cid(qs)
        job = start_person_job(q, fresh, handle, cid)
        if cid:
            space = _load_space(cid)
            _note_search(space, q, "person", job)
            _save_space(cid, space)
        self._json({"job": job, "query": q, "cid": cid})

    def _api_person_result(self, qs):
        jid = (qs.get("job") or [""])[0]
        cid = self._cid(qs)
        with _jobs_lock:
            rec = _jobs.get(jid)
        if not rec:
            self._json({"state": "unknown", "error": "任务不存在或已过期"}, 404)
            return
        # A job belongs to the space that started it. Anyone else gets the same
        # answer as "no such job" — job ids are not a back door into someone
        # else's search.
        if rec.get("cid") and rec["cid"] != cid:
            self._json({"state": "unknown", "error": "任务不存在或已过期"}, 404)
            return
        self._json(rec)

    def _api_person_recent(self, qs=None):
        """This visitor's own recent searches — never the server's global list."""
        cid = self._cid(qs)
        if not cid:
            self._json({"recent": [], "history": [], "cid": ""})
            return
        try:
            import person_search
            space = _load_space(cid)
            self._json({"recent": person_search.recent(8, space=space),
                        "history": space.get("history", [])[:20],
                        "cid": cid})
        except Exception as e:
            self._json({"recent": [], "history": [], "cid": cid,
                        "error": str(e)[:200]})

    def _api_history(self, qs):
        """Full search history of the calling space. ?clear=1 wipes it."""
        cid = self._cid(qs)
        if not cid:
            self._json({"history": [], "cid": "", "error": "缺少空间标识"}, 400)
            return
        space = _load_space(cid)
        if (qs.get("clear") or ["0"])[0] in ("1", "true", "yes"):
            space["history"] = []
            _save_space(cid, space)
            self._json({"history": [], "cid": cid, "cleared": True})
            return
        self._json({"history": space.get("history", []), "cid": cid,
                    "created": space.get("created", "")})

    def _api_capabilities(self):
        try:
            import sources
            self._json({"providers": sources.capabilities()})
        except Exception as e:
            self._json({"providers": [], "error": str(e)[:200]}, 500)

    def _api_refresh(self, qs=None):
        """Manual trigger, used for verification and as a fallback when the
        reader does not want to wait for the next slot. ?fast=1 → RSS only."""
        fast = bool(qs) and (qs.get("fast") or ["0"])[0] in ("1", "true", "yes")
        started = not _run_lock.locked()
        if started:
            _last_kick[0] = time.time()
            threading.Thread(target=_safe_refresh, args=("manual",),
                             kwargs={"fast": fast}, daemon=True).start()
        self._json({"started": started, "fast": fast, "running": _run_lock.locked(),
                    "nextRefresh": next_slot().strftime("%Y-%m-%d %H:%M")})

    def _api_query(self, qs):
        q = (qs.get("q") or [""])[0].strip()
        if len(q) < 2:
            self._json({"error": "请输入至少 2 个字符的检索词"}, 400)
            return
        try:
            days = int((qs.get("days") or ["3"])[0])
        except Exception:
            days = 3
        cid = self._cid(qs)
        job = start_query_job(q, days, cid)
        if cid:
            space = _load_space(cid)
            _note_search(space, q, "query", job)
            _save_space(cid, space)
        self._json({"job": job, "query": q, "days": days, "cid": cid})

    def do_GET(self):
        if self.path.startswith("/api/"):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            try:
                if u.path == "/api/person":
                    return self._api_person(qs)
                if u.path == "/api/person/result":
                    return self._api_person_result(qs)
                if u.path == "/api/person/recent":
                    return self._api_person_recent(qs)
                if u.path == "/api/history":
                    return self._api_history(qs)
                if u.path == "/api/capabilities":
                    return self._api_capabilities()
                if u.path == "/api/refresh":
                    return self._api_refresh(qs)
                if u.path == "/api/query":
                    return self._api_query(qs)
                if u.path == "/api/query/result":
                    return self._api_person_result(qs)
            except Exception as e:
                log("api error: " + traceback.format_exc())
                return self._json({"state": "error", "error": str(e)[:300]}, 500)
            return self._json({"error": "unknown endpoint"}, 404)

        if self.path.startswith("/status"):
            body = json.dumps({
                "ok": True,
                "schedule": "每天 07:00 首更，此后每 %d 小时一次（北京时间）" % REFRESH_HOURS,
                "slots": sorted({(ANCHOR_HOUR + i * REFRESH_HOURS) % 24
                                 for i in range(24 // REFRESH_HOURS)}),
                "nextRefresh": next_slot().strftime("%Y-%m-%d %H:%M"),
                "lastRefresh": _last_done[0],
                "dataAgeMinutes": round(data_age_min(), 1),
                "staleAfterMinutes": STALE_MINUTES,
                "slotMissed": slot_missed(),
                "startedAt": datetime.fromtimestamp(START_TS, CST).strftime("%Y-%m-%d %H:%M:%S"),
                "uptimeMinutes": round((time.time() - START_TS) / 60, 1),
                "running": _run_lock.locked(),
                "log": _log_lines[-40:],
            }, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Tiny version probe. The page polls this instead of pulling the whole
        # data.js every minute: data.js carries every article body and is a few
        # hundred KB, which would be tens of MB an hour on a phone.
        if self.path.startswith("/version"):
            stamp = ""
            try:
                with io.open(os.path.join(ROOT, "data.js"), encoding="utf-8") as fh:
                    m = re.search(r'"generatedAt":\s*"([^"]+)"', fh.read(4000))
                stamp = m.group(1) if m else ""
            except Exception:
                pass
            # Every poll doubles as a freshness check: if the sandbox was
            # suspended and the scheduler thread died with it, the first visit
            # after waking brings the data back up to date.
            maybe_refresh("version-poll")
            _age = data_age_min()
            body = json.dumps({
                "generatedAt": stamp,
                "ageMinutes": round(_age, 1),
                "nextRefresh": next_slot().strftime("%Y-%m-%d %H:%M"),
                "lastRefresh": _last_done[0],
                "staleAfterMinutes": STALE_MINUTES,
                "stale": _age > STALE_MINUTES,
                "running": _run_lock.locked(),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        target = self._resolve()
        if not target or not os.path.exists(target):
            self.send_error(404)
            return
        # Opening the page is also a freshness check (see maybe_refresh).
        if target.endswith("index.html"):
            maybe_refresh("page-open")
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".json": "application/json; charset=utf-8",
                 ".png": "image/png"}.get(os.path.splitext(target)[1], "text/plain")
        try:
            with io.open(target, "rb") as fh:
                body = fh.read()
        except Exception:
            self.send_error(500)
            return

        etag = '"%x-%x"' % (len(body), int(os.path.getmtime(target)))
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return

        # gzip compressible text. data.js is mostly Chinese, which compresses to
        # roughly a quarter of its size — this is the difference between a usable
        # page and a slow one on a phone.
        accepts_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "")
        if accepts_gzip and ctype.startswith(("text/", "application/javascript",
                                              "application/json")) and len(body) > 1024:
            body = gzip.compress(body, 6)
            encoding = "gzip"
        else:
            encoding = None

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Vary", "Accept-Encoding")
        # No caching for data.js: the page re-checks it on a timer.
        self.send_header("Cache-Control",
                         "no-cache" if target.endswith("data.js")
                         else "public, max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def main():
    log("starting on port %d | daily first edition %02d:00 CST, then every %dh"
        % (PORT, ANCHOR_HOUR, REFRESH_HOURS))
    threading.Thread(target=loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
