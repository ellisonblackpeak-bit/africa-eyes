# -*- coding: utf-8 -*-
"""One command: rebuild the pre-rendered page and refresh the library copy.

The library page (workbuddy.cc/p/<nodeId>) keeps ONE stable public link, unlike
the sites sandbox whose public domain is re-minted on every deploy. This script
re-renders dist/africa-eyes-static.html from data.js, imports it over the
existing library node, and re-publishes — the link never changes.

Usage:
    python publish_longterm.py            # rebuild + import + publish
    python publish_longterm.py --no-build # skip the rebuild (re-upload as is)
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "dist", "africa-eyes.html")
NODE_ID = "pm8CGKciTycadAbqTkySuL"
LIB = r"D:/workbuddy/WorkBuddyAI/resources/app.asar.unpacked/resources/plugins/workbuddy-builtin/skills/library"
PY = sys.executable


def sh(args):
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    return p.returncode, out


def main():
    if "--no-build" not in sys.argv:
        rc, _ = sh([PY, os.path.join(HERE, "build_static.py")])
        if rc != 0 or not os.path.exists(PAGE):
            sys.exit("build failed")

    rc, out = sh([PY, os.path.join(LIB, "page", "import_html.py"), PAGE,
                  "--file-name", "Africa Eyes 西非四国动态监测.html",
                  "--node-block-id", NODE_ID])
    if "KS_IMPORT_OK" not in out:
        sys.exit("import failed")

    rc, out = sh([PY, os.path.join(LIB, "page", "publish_page.py"), "--node-id", NODE_ID])
    if "KS_PAGE_PUBLISH" not in out:
        sys.exit("publish failed")
    print("stable link: https://workbuddy.cc/p/%s" % NODE_ID)


if __name__ == "__main__":
    main()
