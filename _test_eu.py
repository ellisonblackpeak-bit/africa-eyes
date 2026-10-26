# -*- coding: utf-8 -*-
"""欧洲模块 + 恐组织/中资资产扩充的规则回归测试。

覆盖三件事：
  1. 欧洲能否被正确识别（含 EU 缩写、中文国名、法语 "eu" 不得误伤）
  2. G7 的「主题 × 关系」双条件是否够严（不该进的必须挡住）
  3. 13 个指定恐组织与 4 个中资能源资产能否被认出

用法： python _test_eu.py    期望：全部 PASS。
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "build_data.py"), encoding="utf-8").read()

ns = {"re": re}
# TOPIC_RX is rebuilt inside the CTY_MARKERS slice; its source dict lives earlier
# and is not needed here.
ns["TOPIC_MARKERS"] = {}
# ── CN_RX / CN_RX_INTL / CN_ASSET_RX ───────────────────────────────────────
exec(src[src.index('CN_RX = re.compile(r"""('):src.index("US_ABBR = re.compile")], ns)
# ── CTY_MARKERS / _rx / zh_cty_hits / ALL_CTYS / EU_ABBR_RX / DOMAIN_CTY ──
exec(src[src.index("CTY_MARKERS = {"):src.index("def classify_cty")], ns)
# classify_cty 本体（上面的切片止于其定义处）
_k = src.index("def classify_cty")
exec(src[_k:src.index("\n\n\n", _k)], ns)
# ── TERROR_RX / terror_hit ────────────────────────────────────────────────
exec(src[src.index("TERROR_RX = re.compile("):src.index("def terror_hit")], ns)
exec(src[src.index("def terror_hit"):src.index("\n\n\n", src.index("def terror_hit"))], ns)
# ── EU_TOPIC_RX ───────────────────────────────────────────────────────────
exec(src[src.index("EU_TOPIC_RX = re.compile("):src.index("def _gate_periphery")], ns)

CN = ns["CN_RX"]; ASSET = ns["CN_ASSET_RX"]; EUABBR = ns["EU_ABBR_RX"]
CTY = ns["CTY_RX"]; ZH = ns["zh_cty_hits"]; TOPIC = ns["EU_TOPIC_RX"]
TERROR = ns["terror_hit"]; DOM = ns["DOMAIN_CTY"]

fail = 0


def check(label, got, want, text):
    global fail
    ok = got == want
    if not ok:
        fail += 1
    print("%s  %-22s want=%-5s got=%-5s  %s"
          % ("PASS" if ok else "FAIL", label, want, got, text[:56]))


print("── ① 欧洲识别 ──")
check("EU 缩写", bool(EUABBR.search("EU imposes tariffs on Chinese EVs")), True,
      "EU imposes tariffs on Chinese EVs")
check("EU 缩写·不误伤法语", bool(EUABBR.search("il a eu un accident a Niamey")), False,
      "il a eu un accident a Niamey")
check("国家名", bool(CTY["eu"].search("Germany bans Huawei from 5G core")), True,
      "Germany bans Huawei from 5G core")
check("欧洲统称", bool(CTY["eu"].search("European leaders meet on China trade")), True,
      "European leaders meet on China trade")
check("中文·欧盟", "eu" in ZH("欧盟对华电动车加征关税"), True, "欧盟对华电动车加征关税")
check("中文·德国", "eu" in ZH("德国政府审查中资并购案"), True, "德国政府审查中资并购案")
CLASSIFY = ns["classify_cty"]
check("中文·法国在非洲→不归欧", "eu" in CLASSIFY("尼日尔军政府驱逐法国驻军", ""), False,
      "尼日尔军政府驱逐法国驻军")
check("中文·欧盟制裁尼日尔→归欧", "eu" in CLASSIFY("欧盟制裁尼日尔军政府", ""), True,
      "欧盟制裁尼日尔军政府")
check("英文·法国从尼日尔撤军→不归欧",
      "eu" in CLASSIFY("France withdraws troops from Niger after coup", ""), False,
      "France withdraws troops from Niger after coup")
check("英文·欧盟制裁尼日尔→归欧",
      "eu" in CLASSIFY("EU sanctions Niger junta over coup", ""), True,
      "EU sanctions Niger junta over coup")
check("域名·politico.eu", DOM.get("politico.eu") == "eu", True, "politico.eu")
check("域名·lemonde.fr", DOM.get("lemonde.fr") == "eu", True, "lemonde.fr")
check("域名·全球媒体不归欧", "eu" in [DOM.get("reuters.com"), DOM.get("bbc.com")], False,
      "reuters.com / bbc.com")

print("\n── ② G7 主题词（缺此词则不入欧洲模块）──")
for t, want in [
    ("EU slaps tariffs on Chinese electric vehicles", True),
    ("Europe and US agree on critical minerals deal", True),
    ("NATO allies raise defence spending targets", True),
    ("EU and Japan sign semiconductor cooperation", True),
    ("Europe risks US junk-food trajectory, expert warns", False),
    ("French presidential hopeful slips in new poll", False),
    ("Serbia crowds celebrate after Vucic admits defeat", False),
]:
    check("EU_TOPIC", bool(TOPIC.search(t)), want, t)

print("\n── ③ 恐组织（用户点名的 13 个）──")
for g, t in [
    ("JNIM", "JNIM claims attack on Mali army convoy"),
    ("MPLJ", "MPLJ fighters spotted near Agadez"),
    ("EIGS", "EIGS militants kill 20 soldiers in Tillaberi"),
    ("ISSP", "ISSP expands control in Menaka region"),
    ("ISIS", "ISIS claims responsibility for bombing"),
    ("ISGS", "ISGS cell dismantled near Gao"),
    ("AQIM", "AQIM veteran returns to Sahel"),
    ("Boko Haram", "Boko Haram raids village in Borno"),
    ("ISWAP", "ISWAP fighters surrender in Borno"),
    ("FLA", "FLA rejects the Algiers accord in Azawad"),
    ("MNLA", "MNLA reopens offices in Kidal, Mali"),
    ("MAA", "MAA clashes with rival groups near Timbuktu"),
    ("Lakurawa", "Lakurawa gunmen attack Sokoto community"),
]:
    check(g, TERROR(t), True, t)

print("\n── ④ 中资能源资产 ──")
for t in [
    "Le pipeline Niger-Benin reprend son debit apres l attaque",
    "SORAZ refinery halts output after pipeline sabotage",
    "CNPC Niger evacuates staff from Agadem block",
    "WAPCo declares force majeure on gas supply",
    "尼日尔-贝宁原油管道恢复输油",
    "津德尔炼油厂因管道中断停产",
    "中石油尼日尔分公司调整作业",
]:
    check("CN_ASSET", bool(ASSET.search(t)), True, t)

print("\n── ⑤ 反例：不该被当成中资资产 ──")
for t in [
    "Dangote refinery raises petrol output in Lagos",
    "Nigeria opens new refinery in Port Harcourt",
]:
    check("CN_ASSET(负)", bool(ASSET.search(t)), False, t)

print("\n%d 项，%d 失败" % (0, fail) if not fail else "\n%d 失败" % fail)
sys.exit(1 if fail else 0)
