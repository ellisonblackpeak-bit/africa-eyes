# -*- coding: utf-8 -*-
"""Bundle index.html + data.js into ONE self-contained HTML file.

The published preview link (workbuddy_sites_deploy) mints a new public domain on
every deploy and the old one starts answering 400 / 12803 within the hour, so it
cannot be handed to readers as a daily-use address. A library-hosted page keeps
the same link indefinitely. That page has no backend, hence:

  * data is inlined instead of fetched from data.js;
  * window.__STATIC__ is set so index.html skips the /version poll and the two
    live-search tabs (they would only ever 404).

Output: dist/africa-eyes.html
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")
DATA = os.path.join(HERE, "data.js")
OUTDIR = os.path.join(HERE, "dist")
OUT = os.path.join(OUTDIR, "africa-eyes.html")

TAG = '<script src="data.js" onerror="window.__DATA_MISSING__=1"></script>'


def main():
    html = io.open(INDEX, encoding="utf-8").read()
    raw = io.open(DATA, encoding="utf-8").read()

    m = re.search(r"window\.SITE_DATA\s*=\s*", raw)
    if not m:
        sys.exit("data.js does not contain window.SITE_DATA")
    body = raw[m.end():].strip()
    if body.endswith(";"):
        body = body[:-1]
    # A literal </script> anywhere inside the JSON would close the inline block.
    body = body.replace("</", "<\\/")

    if TAG not in html:
        sys.exit("data.js script tag not found in index.html")

    inline = ('<script>window.__STATIC__=1;window.SITE_DATA=' + body + ';</script>')
    out = html.replace(TAG, inline)

    if not os.path.isdir(OUTDIR):
        os.makedirs(OUTDIR)
    io.open(OUT, "w", encoding="utf-8").write(out)

    gen = re.search(r'"generatedAt":\s*"([^"]+)"', body)
    print("wrote %s (%.1f KB) generatedAt=%s"
          % (OUT, len(out.encode("utf-8")) / 1024.0, gen.group(1) if gen else "?"))


if __name__ == "__main__":
    main()
