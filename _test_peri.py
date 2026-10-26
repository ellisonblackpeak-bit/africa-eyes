# -*- coding: utf-8 -*-
"""周边国家本地语源的两道新规则回归测试。

1. _RSS_CTY_MARKERS 的原生文字国家锚定（fetch_news.py）
2. _GATE_MULTI_HARD_RX 的多语种硬新闻词（build_data.py）

用法： python _test_peri.py    期望：全部 PASS。
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))

# ── 1. fetch_news 的国家锚定 ────────────────────────────────────────────────
fsrc = open(os.path.join(HERE, "fetch_news.py"), encoding="utf-8").read()
a = fsrc.index("_RSS_CTY_MARKERS = {")
b = fsrc.index("def fetch_rss")
fns = {"re": re}
exec(fsrc[a:b], fns)
ANCHOR = fns["_rss_anchor_ok"]

# ── 2. build_data 的多语种硬新闻词 ──────────────────────────────────────────
bsrc = open(os.path.join(HERE, "build_data.py"), encoding="utf-8").read()
c = bsrc.index("_GATE_MULTI_HARD_RX = re.compile(")
d = bsrc.index("def _own_periph_cty")
bns = {"re": re}
exec(bsrc[c:d], bns)
HARD = bns["_GATE_MULTI_HARD_RX"]

# (feed_code, 标题, 是否应保留)  —— 全部取自真实抓到的标题
ANCHOR_CASES = [
    # TASS：俄国自己的新闻要留，写别国的要挡
    ("ru", "Путин обсудил с Совбезом РФ ситуацию вокруг договора о СНВ", True),
    ("ru", "Минобороны сообщило об ударе по позициям ВСУ под Купянском", True),
    ("ru", "Путин подписал указ о весеннем призыве", True),
    ("ru", "В Новосибирске автобус съехал в кювет, пострадали пять человек", False),
    ("ru", "Лула да Силва переизбран президентом Бразилии", False),
    ("ru", "Землетрясение произошло у берегов Индонезии", False),
    # 尼泊尔
    ("np", "नेपाल र भारतबीच ऊर्जा व्यापार सम्झौता", True),
    ("np", "काठमाडौंमा प्रधानमन्त्रीको अध्यक्षतामा बैठक", True),
    ("np", "ब्राजिलमा राष्ट्रपति निर्वाचन सम्पन्न", False),
    # 印度
    ("in", "भारत और चीन के बीच सीमा वार्ता फिर शुरू", True),
    ("in", "यमन में हूती विद्रोहियों पर 753 हमले", False),
    # 韩国：国名不出现的本国政治要靠弱锚定救回来
    ("kr", "대통령, 국방 예산 증액 방침 발표", True),
    ("kr", "룰라, 브라질 대선서 4선 성공", False),
]

HARD_CASES = [
    ("Путин подписал указ о санкциях", True),
    ("Минобороны: ракетный удар по складу", True),
    ("대통령이 국회 연설을 한다", True),
    ("नेपालको संसदमा ऊर्जा विधेयक पेश", True),
    ("အစိုးရက စီးပွားရေး ပြုပြင်ပြောင်းလဲမှု ကြေညာ", True),
    # 软新闻必须过不了这道闸
    ("Футбол: сборная России обыграла Казахстан", False),
    ("क्रिकेट: नेपालले ओमानलाई हरायो", False),
    ("아이돌 그룹 새 앨범 발매", False),
    ("В Новосибирске автобус съехал в кювет", False),
]

fail = 0
print("── 国家锚定 ──")
for code, title, want in ANCHOR_CASES:
    got = ANCHOR(code, title)
    ok = got == want
    fail += 0 if ok else 1
    print("%s  %-5s want=%-5s got=%-5s  %s"
          % ("PASS" if ok else "FAIL", code, want, got, title[:52]))

print("\n── 多语种硬新闻词 ──")
for title, want in HARD_CASES:
    got = bool(HARD.search(title))
    ok = got == want
    fail += 0 if ok else 1
    print("%s  want=%-5s got=%-5s  %s"
          % ("PASS" if ok else "FAIL", want, got, title[:52]))

print("\n%d/%d 通过，%d 失败" % (len(ANCHOR_CASES) + len(HARD_CASES) - fail,
                               len(ANCHOR_CASES) + len(HARD_CASES), fail))
sys.exit(1 if fail else 0)
