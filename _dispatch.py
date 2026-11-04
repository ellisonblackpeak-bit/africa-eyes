# -*- coding: utf-8 -*-
"""手动触发云端重建，并（可选）等到跑完。

为什么需要：本机 github.com:443 被代理挡着，push 只能走 api.github.com 的
REST 接口；触发 Actions 也一样，只能在 API 上 POST dispatch。本地跑
build_data.py 只是自测，线上是快照，不触发就永远是上一期。

用法：
    python _dispatch.py                 # 只触发，不等
    python _dispatch.py --wait          # 触发后轮询到结束，打印结论
    python _dispatch.py --wait --wf translate-topup.yml
    python _dispatch.py --watch         # 不触发，只盯最新一轮（会误触发的场合用）
令牌：环境变量 GH_TOKEN，或第一个非选项参数。
退出码：0 表示触发成功（--wait 时表示作业成功），非 0 表示失败。

网络重试复用 gh_push.req：本机到 api.github.com 会随机断在 TLS 握手上
（`UNEXPECTED_EOF_WHILE_READING`），2026-11-04 一次推送 6 个文件就在第 4 个
断掉。重试逻辑只写一份，两个脚本共用。
"""
import os
import sys
import time

from gh_push import REPO, req

DEFAULT_WF = "refresh-and-deploy.yml"


def main():
    argv = sys.argv[1:]
    wait = "--wait" in argv
    watch = "--watch" in argv
    wf = DEFAULT_WF
    if "--wf" in argv:
        wf = argv[argv.index("--wf") + 1]
    rest = [a for i, a in enumerate(argv)
            if a not in ("--wait", "--watch", "--wf")
            and (i == 0 or argv[i - 1] != "--wf")]
    token = os.environ.get("GH_TOKEN") or (rest[0] if rest else "")
    if not token:
        sys.exit("no token: set GH_TOKEN or pass it as the first argument")

    if not watch:
        req(token, "/repos/%s/actions/workflows/%s/dispatches" % (REPO, wf),
            "POST", {"ref": "main"})
        print("dispatch %s -> ok" % wf)
    if not (wait or watch):
        return 0

    # 触发后要等它出现在 runs 列表里，否则拿到的还是上一轮
    run_id = None
    for _ in range(20):
        time.sleep(6)
        data = req(token, "/repos/%s/actions/workflows/%s/runs?per_page=1" % (REPO, wf))
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
        data = req(token, "/repos/%s/actions/runs/%s" % (REPO, run_id))
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
