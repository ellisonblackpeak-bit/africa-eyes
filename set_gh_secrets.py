# -*- coding: utf-8 -*-
"""Push repository Actions secrets without ever printing their values."""
import base64
import io
import json
import os
import sys
import urllib.request

from nacl import encoding, public

TOKEN = os.environ["GH_TOKEN"]
REPO = os.environ.get("GH_REPO", "ellisonblackpeak-bit/africa-eyes")
API = "https://api.github.com"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw.strip() else {}


def seal(pub_key_b64, value):
    pk = public.PublicKey(pub_key_b64.encode(), encoding.Base64Encoder())
    box = public.SealedBox(pk)
    return base64.b64encode(box.encrypt(value.encode())).decode()


def local_key(server, name):
    p = os.path.expanduser("~/.workbuddy-ai/mcp.json")
    d = json.load(io.open(p, encoding="utf-8"))
    return ((d.get("mcpServers") or {}).get(server) or {}).get("env", {}).get(name, "")


def tavily_pool():
    """Every Tavily key on this machine, in priority order, as one string.

    GitHub Secrets hold a single value, so the pool is joined with commas and
    parsed back by sources.tavily_keys() / fetch_news.py.
    """
    p = os.path.expanduser("~/.workbuddy-ai/mcp.json")
    d = json.load(io.open(p, encoding="utf-8"))
    srv = d.get("mcpServers") or {}
    out = []

    def add(k):
        k = (k or "").strip()
        if k.startswith("tvly-") and k not in out:
            out.append(k)

    fo = (srv.get("tavily-failover") or {}).get("env") or {}
    for i in range(1, 30):
        add(fo.get("TAVILY_API_KEY_%d" % i))
    add(fo.get("TAVILY_API_KEY"))
    add(((srv.get("tavily-mcp") or {}).get("env") or {}).get("TAVILY_API_KEY"))
    return ",".join(out)


def main():
    pairs = {
        "XAI_API_KEY": local_key("xai-xsearch", "XAI_API_KEY"),
        "TAVILY_API_KEYS": tavily_pool(),
    }
    key = call("GET", "/repos/%s/actions/secrets/public-key" % REPO)
    for name, value in pairs.items():
        if not value:
            print("%-16s SKIP (no local value)" % name)
            continue
        sealed = seal(key["key"], value)
        call("PUT", "/repos/%s/actions/secrets/%s" % (REPO, name),
             {"encrypted_value": sealed, "key_id": key["key_id"]})
        shown = ("%d keys" % (value.count(",") + 1)) if name == "TAVILY_API_KEYS" else ("%d chars" % len(value))
        print("%-16s set (%s)" % (name, shown))

    names = [s["name"] for s in call("GET", "/repos/%s/actions/secrets" % REPO).get("secrets", [])]
    print("repo secrets now:", names)


if __name__ == "__main__":
    main()
