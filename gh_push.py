"""Push selected files to GitHub through the REST API.

The local network blocks github.com:443 (the system proxy answers 502 on
CONNECT), so `git push` cannot work here. api.github.com is reachable, which is
enough to build a commit by hand: blob per file -> tree -> commit -> patch ref.

Usage:  python gh_push.py [--msg-file M] [--dry-run] <token|$GH_TOKEN> <file> [<file> ...]
The new commit's parent is whatever `main` currently points at, so a commit
pushed from elsewhere (the scheduled bot) is not overwritten.

提交信息为什么走 --msg-file：2026-11-04 用 `GH_COMMIT_MSG="$(cat ...)"` 传中文
多行消息，里面的 `|` 被 bash 当成管道，报了
`command substitution: line 60: syntax error near unexpected token '|'`，
整次推送没发出去。消息一旦经过 shell 就有可能被解释，所以直接从文件读。
令牌同理：优先读环境变量 GH_TOKEN，避免出现在进程命令行里被日志记下。
"""
import base64
import io
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = "ellisonblackpeak-bit/africa-eyes"


def req(token, path, method="GET", payload=None, tries=6):
    """单个 API 调用，带重试。

    本机到 api.github.com 的链路会随机断在半路：2026-11-04 推 6 个文件，
    第 4 个 blob 直接 SSL EOF（`UNEXPECTED_EOF_WHILE_READING`）。这类断连是
    代理造成的瞬时故障，重试即可；已经上传成功的 blob 会变成孤儿对象，
    GitHub 自己回收，不影响正确性——所以整条链路可以放心整体重跑。
    """
    data = json.dumps(payload).encode() if payload is not None else None
    last = None
    for i in range(tries):
        r = urllib.request.Request(API + path, data=data, method=method)
        r.add_header("Authorization", "token " + token)
        r.add_header("Accept", "application/vnd.github+json")
        if data:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=90) as fh:
                return json.loads(fh.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            # 4xx 里只有 429/5xx 值得重试，其余重试也没用
            if e.code < 500 and e.code != 429:
                raise SystemExit("%s %s -> HTTP %d %s" % (method, path, e.code, body))
            last = "HTTP %d %s" % (e.code, body)
        except (urllib.error.URLError, ssl.SSLError, socket.timeout, OSError) as e:
            last = "%s: %s" % (type(e).__name__, e)
        if i < tries - 1:
            wait = min(2 ** i, 15)
            print("    ! %s  %s  -> %ds 后重试 (%d/%d)"
                  % (path.split("?")[0][-38:], last[:70], wait, i + 1, tries - 1))
            time.sleep(wait)
    raise SystemExit("重试 %d 次仍失败: %s %s -> %s" % (tries, method, path, last))


def main():
    argv = sys.argv[1:]
    msg_file = None
    dry = False
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--msg-file":
            i += 1
            msg_file = argv[i]
        elif a == "--dry-run":
            dry = True
        else:
            rest.append(a)
        i += 1

    token = os.environ.get("GH_TOKEN") or (rest.pop(0) if rest else "")
    files = rest
    if not token:
        sys.exit("no token: set GH_TOKEN or pass it as the first argument")
    if not files:
        sys.exit("no files given")

    head = req(token, "/repos/%s/commits/main" % REPO)
    parent = head["sha"]
    base_tree = head["commit"]["tree"]["sha"]
    print("remote head %s  (%s)" % (parent[:8], head["commit"]["message"].splitlines()[0][:60]))

    tree = []
    for path in files:
        with io.open(path, "rb") as fh:
            raw = fh.read()
        if dry:
            print("  [dry] %-22s %6d bytes" % (path, len(raw)))
            continue
        blob = req(token, "/repos/%s/git/blobs" % REPO, "POST",
                   {"content": base64.b64encode(raw).decode(), "encoding": "base64"})
        tree.append({"path": path.replace(os.sep, "/"), "mode": "100644",
                     "type": "blob", "sha": blob["sha"]})
        print("  blob %-22s %6d bytes -> %s" % (path, len(raw), blob["sha"][:8]))

    if dry:
        print("dry run, nothing pushed")
        return

    new_tree = req(token, "/repos/%s/git/trees" % REPO, "POST",
                   {"base_tree": base_tree, "tree": tree})
    if msg_file:
        msg = io.open(msg_file, encoding="utf-8").read().strip()
    else:
        msg = os.environ.get("GH_COMMIT_MSG") or "chore: update from local"
    commit = req(token, "/repos/%s/git/commits" % REPO, "POST",
                 {"message": msg, "tree": new_tree["sha"], "parents": [parent]})
    req(token, "/repos/%s/git/refs/heads/main" % REPO, "PATCH",
        {"sha": commit["sha"], "force": False})
    print("pushed %s" % commit["sha"])
    print("message: %s" % msg.splitlines()[0])


if __name__ == "__main__":
    main()
