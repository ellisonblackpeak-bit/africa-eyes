# -*- coding: utf-8 -*-
"""手动触发云端重建，并（可选）等到跑完。

为什么需要：本机 github.com:443 被代理挡着，push 只能走 api.github.com 的
REST 接口；触发 Actions 也一样，只能在 API 上 POST dispatch。本地跑
build_data.py 只是自测，线上是快照，不触发就永远是上一期。

用法：
    python _dispatch.py                 # 只触发，不等
    python _dispatch.py --wait          # 触发后轮询到结束，打印结论
    python _dispatch.py --wait --wf translate-topup.yml
令牌：环境变量 GH_TOKEN，或第一个非选项参数。
退出码：0 表示触发成功（--wait 时表示作业成功），非 0 表示失败。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = "ellisonblackpeak-bit/africa-eyes"
DEFAULT_WF = "refresh-and-deploy.yml"


def req(token, path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(API + path, data=data, method=method)
    r.add_header("Authorization", "token " + token)
    r.add_header("Accept", "application/vnd.github+json")
    if data:
        r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=60) as fh:
        body = fh.read().decode()
        return fh.status, (json.loads(body) if body.strip() else {})


def main():
    argv = sys.argv[1:]
    wait = "--wait" in argv
    wf = DEFAULT_WF
    if "--wf" in argv:
        wf = argv[argv.index("--wf") + 1]
    rest = [a for i, a in enumerate(argv)
            if a not in ("--wait", "--wf") and (i == 0 or argv[i - 1] != "--wf")]
    token = os.environ.get("GH_TOKEN") or (rest[0] if rest else "")
    if not token:
        sys.exit("no token: set GH_TOKEN or pass it as the first argument")

    status, _ = req(token, "/repos/%s/actions/workflows/%s/dispatches" % (REPO, wf),
                    "POST", {"ref": "main"})
    print("dispatch %s -> HTTP %s" % (wf, status))
    if not wait:
        return 0

    # 触发后要等它出现在 runs 列表里，否则拿到的还是上一轮
    run_id = None
    for _ in range(20):
        time.sleep(6)
        _, data = req(token, "/repos/%s/actions/workflows/%s/runs?per_page=1" % (REPO, wf))
        runs = data.get("workflow_runs") or []
        if runs and runs[0].get("status") in ("queued", "in_progress"):
            run_id = runs[0]["id"]
            break
    if not run_id:
        print("找不到刚触发的 run（可能还在排队），请稍后自己看 Actions 页面")
        return 3

    print("run %s 已启动，轮询中（云端一轮约 30—40 分钟）…" % run_id)
    deadline = time.time() + 75 * 60
    last = None
    while time.time() < deadline:
        time.sleep(45)
        _, data = req(token, "/repos/%s/actions/runs/%s" % (REPO, run_id))
        st = data.get("status")
        if st != last:
            print("  %s  status=%s  conclusion=%s"
                  % (time.strftime("%H:%M:%S"), st, data.get("conclusion")))
            last = st
        if st == "completed":
            print("结论: %s" % data.get("conclusion"))
            print("详情: %s" % data.get("html_url"))
            return 0 if data.get("conclusion") == "success" else 1
    print("等待超时，请自行查看: https://github.com/%s/actions/runs/%s" % (REPO, run_id))
    return 4


if __name__ == "__main__":
    sys.exit(main())
