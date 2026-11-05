# -*- coding: utf-8 -*-
"""尼日利亚 / 尼日尔 / 尼日利亚的尼日尔州 —— 三分回归测试。

读者 2026-11-05 的原话：「没分清尼日利亚、尼日尔、以及尼日利亚的尼日尔州」。
线上实测到的两个具体错例都在这里锁住：

  ① 国别：一张尼日利亚尼日尔州的卡片同时挂「尼日利亚」「尼日尔」两个徽标；
  ② 译文：同一个 "Niger State" 被译成「尼日利亚州」（不存在的行政区划）
     和裸「尼日尔」（读起来就是那个国家）。

用法： python _test_niger.py      期望：全部 PASS。
"""
import io, os, re, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "build_data.py"), encoding="utf-8").read()
a = src.index("OUTLETS = {")
b = src.index("def polish_title")
ns = {"re": re}
exec(src[a:b], ns)
classify_cty = ns["classify_cty"]
zh_cty_hits = ns["zh_cty_hits"]
fix_geo_zh = ns["fix_geo_zh"]
geo_place = ns["geo_place"]

FAIL = []


def ck(label, got, want):
    ok = got == want
    print(("PASS  " if ok else "FAIL  ") + label + "  →  " + repr(got))
    if not ok:
        FAIL.append((label, got, want))


# ── ① 国别判定：尼日利亚的尼日尔州不能算成尼日尔 ────────────────────────
# 线上原卡：「LAKURAWA…21 COMMUNITIES FLEE ATTACKS IN NIGER STATE」
ck("Niger State 标题只判 ng",
   classify_cty("LAKURAWA TERRORISTS REPORTEDLY MOVING TOWARD MABURYA "
                "VILLAGE AS 21 COMMUNITIES FLEE ATTACKS IN NIGER STATE", ""),
   ["ng"])
ck("Niger Delta 只判 ng",
   classify_cty("Niger Delta militants blow up trunk pipeline in Delta State", ""),
   ["ng"])
ck("Niger State 正文 + 别处裸 Niger 仍是 ng",
   classify_cty("Governor flags off rural roads",
                "The project will improve livelihoods across Niger. "
                "Niger State Government said the 30 million dollar grant from "
                "China would be used for grassroots development."),
   ["ng"])
ck("州内城市（Minna）只判 ng",
   classify_cty("Minna residents protest power outage", ""), ["ng"])
ck("尼日尔共和国仍判 ne",
   classify_cty("Niger junta names new cabinet after coup", ""), ["ne"])
ck("尼亚美仍判 ne",
   classify_cty("Niamey denies report of border closure", ""), ["ne"])
ck("两国并列时两个都留（不能被上面的规则吃掉）",
   classify_cty("Nigeria and Niger sign trade and security pact", ""),
   ["ng", "ne"])
ck("Niger State 与尼日尔并列时两个都留（有 NE_STRONG 证据）",
   classify_cty("Niger State farmers host delegation from Niamey", ""),
   ["ng", "ne"])

# ── ② 中文国别标记 ──────────────────────────────────────────────────────
ck("尼日尔州 → ng", zh_cty_hits("尼日尔州政府批准新预算"), {"ng"})
ck("尼日尔州长 → ne（是尼日尔的州长，不是尼日利亚的州）",
   zh_cty_hits("尼日尔州长视察洪灾区"), {"ne"})
ck("尼日尔河三角洲 → ng（不是尼日尔）",
   zh_cty_hits("尼日尔河三角洲武装袭击输油管"), {"ng"})
ck("尼日利亚 → ng，不连带 ne", zh_cty_hits("尼日利亚总统蒂努布讲话"), {"ng"})
ck("尼日尔 → ne", zh_cty_hits("尼日尔与阿尔及利亚签署燃料协议"), {"ne"})

# ── ③ 译文修复 ──────────────────────────────────────────────────────────
en_state = "21 COMMUNITIES FLEE ATTACKS IN NIGER STATE"
ck("「尼日利亚州」→「尼日尔州」",
   fix_geo_zh(en_state, "21个社区逃离尼日利亚州的袭击"),
   "21个社区逃离尼日尔州的袭击")
ck("裸「尼日尔」→「尼日尔州」",
   fix_geo_zh(en_state, "尼日尔政府拨款修路"),
   "尼日尔州政府拨款修路")
ck("已经是「尼日尔州」不再叠加",
   fix_geo_zh(en_state, "尼日尔州政府拨款修路"),
   "尼日尔州政府拨款修路")
ck("英文写 Niger Delta → 补成「尼日尔河三角洲」",
   fix_geo_zh("Niger Delta youths back Tinubu", "尼日尔三角洲青年支持蒂努布"),
   "尼日尔河三角洲青年支持蒂努布")
ck("英文只有 Nigeria，译文误作「尼日尔」→ 改回「尼日利亚」",
   fix_geo_zh("Nigeria unveils new tax policy", "尼日尔公布新税收政策"),
   "尼日利亚公布新税收政策")
ck("英文两国并列时不动「尼日尔」",
   fix_geo_zh("Nigeria and Niger sign pact", "尼日利亚与尼日尔签署协议"),
   "尼日利亚与尼日尔签署协议")
ck("英文里的 Niger State 不会被「只有 Nigeria」那条规则改掉",
   fix_geo_zh("Niger State and Nigeria agree", "尼日尔州与尼日利亚达成一致"),
   "尼日尔州与尼日利亚达成一致")

# ── ④ 卡片上的地区徽标 ──────────────────────────────────────────────────
ck("尼日尔州卡片带 place",
   geo_place("21 communities flee attacks in Niger State", "21个社区逃离尼日尔州袭击",
             ["ng"]), "尼日尔州")
ck("尼日尔河三角洲卡片带 place",
   geo_place("Niger Delta youths back Tinubu", "尼日尔河三角洲青年支持蒂努布", ["ng"]),
   "尼日尔河三角洲")
ck("尼日尔共和国的卡片不带 place",
   geo_place("Niger junta names cabinet", "尼日尔军政府任命内阁", ["ne"]), "")
ck("普通尼日利亚卡片不带 place",
   geo_place("Tinubu meets governors", "蒂努布会见州长", ["ng"]), "")

# ── ⑤ 别把 "Niger state-owned" 当成州名 ────────────────────────────────
# state 在 "state-owned company" 里是形容词。少了这条，尼日尔国家铀矿公司的
# 稿子会被判成尼日利亚，中文还会被改成「尼日尔州有铀矿公司」。
ck("Niger state-owned 仍是 ne",
   classify_cty("Niger state-owned uranium firm seeks buyer", ""), ["ne"])
ck("state-owned 不改写",
   fix_geo_zh("Niger state-owned uranium firm seeks buyer",
              "尼日尔国有铀矿公司寻求买家"),
   "尼日尔国有铀矿公司寻求买家")
ck("同篇有 Niamey 时只修「尼日利亚州」，不动「尼日尔」",
   fix_geo_zh("Niger State delegation meets Niamey officials",
              "尼日利亚州代表团会见尼日尔官员"),
   "尼日尔州代表团会见尼日尔官员")

print()
if FAIL:
    print("FAILED %d / %d" % (len(FAIL), 27))
    for f in FAIL:
        print("   ", f[0], "got", repr(f[1]), "want", repr(f[2]))
    sys.exit(1)
print("ALL PASS (27)")
