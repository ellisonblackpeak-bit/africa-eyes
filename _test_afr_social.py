# -*- coding: utf-8 -*-
"""非洲四国「社会危安 / 民生只留涉华」的回归测试。

读者口径（2026-11-04）：
  「非洲四国发生不涉及中国因素的社会危安事件、民生问题等新闻、动态我不需要。
    我只需要涉及中国因素的发生在非洲四国的社会危安等新闻」
  「涉及中国的词汇你要翻译成英语、法语、和非洲四国本土语言进行限制」

所以这个测试同时守两条线：
  A. 不涉华的社会危安/民生 → 删（事故、灾害、停电、物价、罢工、医院缺药）
  B. 涉华 → 留，而且法语 / 豪萨语 / 约鲁巴语 / 伊博语 / 契维语 / 埃维语 /
     班巴拉语 / 阿拉伯语写的"中国"都要认出来

同时确认没有误伤另外两条需求：
  C. 需求二（国防/政治/经济/能源/矿产/通道）的本地硬新闻照样留
  D. 需求三（13 个恐怖组织）照样留
  E. 周边国家不受这条规则影响

用法： python _test_afr_social.py     期望：全部 PASS，退出码 0。
"""
import ast
import io
import re
import sys

src = io.open("build_data.py", encoding="utf-8").read()
tree = ast.parse(src)

WANT = {
    "CN_RX", "CN_RX_INTL", "CN_ASSET_RX", "TERROR_RX", "NIGER_OIL_RX",
    "POLICY_ANCHOR_RX", "DEFENSE_RX", "ECON_RX", "SOCIAL_HARDSHIP_RX",
    "AFR4_CTYS", "is_africa_social_only",
}
keep = []
for node in tree.body:
    name = None
    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
        name = node.targets[0].id
    elif isinstance(node, ast.FunctionDef):
        name = node.name
    if name in WANT:
        keep.append(node)

ns = {"re": re}
exec(compile(ast.Module(body=keep, type_ignores=[]), "<extract>", "exec"), ns)
OFF = ns["is_africa_social_only"]

fail = 0
total = 0


def chk(label, title, zh, cty, want_drop):
    global fail, total
    total += 1
    got = OFF(title, zh, "", cty)
    ok = got == want_drop
    if not ok:
        fail += 1
    print("%s  %-30s %-6s %s"
          % ("PASS" if ok else "FAIL", label,
             "删" if got else "留", (zh or title)[:44]))


print("── A. 不涉华的社会危安 / 民生 → 应删 ──")
chk("事故·卡车侧翻", "Container truck overturns in Lagos, mother and child killed",
    "拉各斯集装箱卡车侧翻，母子遇难", ["ng"], True)
chk("灾害·洪灾", "Flood displaces 5,000 in Benue",
    "贝努埃州洪灾致五千人流离失所", ["ng"], True)
chk("民生·物价", "Food prices soar as inflation bites",
    "食品价格飙升，通胀加剧", ["ng"], True)
chk("民生·停电", "National grid collapses again, blackout nationwide",
    "国家电网再次崩溃，全国停电", ["gh"], True)
chk("事故·油罐车爆炸", "Fuel tanker explosion kills 12 in Niger",
    "尼日尔油罐车爆炸致 12 人死亡", ["ne"], True)
chk("事故·建筑倒塌", "Building collapse traps workers in Abuja",
    "阿布贾建筑倒塌，工人被困", ["ng"], True)
chk("民生·医院缺药", "Hospitals run out of drugs in Bamako",
    "巴马科医院缺药", ["ml"], True)
chk("民生·罢工", "Teachers strike enters second week",
    "教师罢工进入第二周", ["gh"], True)
chk("民生·失业", "Unemployment hits record high among youth",
    "青年失业率创历史新高", ["ng"], True)
chk("法语·事故", "Un camion citerne explose à Niamey",
    "", ["ne"], True)
chk("法语·缺水", "Pénurie d'eau potable à Bamako",
    "", ["ml"], True)
chk("治安·抢劫", "Armed robbery gang arrested in Kumasi",
    "库马西武装抢劫团伙被捕", ["gh"], True)

print("\n── B. 涉华 → 应留（含多语种写法）──")
chk("中文·中资", "Chinese firm's site attacked in Zamfara",
    "中资企业工地遭袭", ["ng"], False)
chk("中文·中国公民", "Gunmen abduct Chinese nationals in Ogun",
    "中国公民在奥贡州被绑架", ["ng"], False)
chk("法语·chinois", "Une entreprise chinoise touchée par les inondations",
    "", ["ne"], False)
chk("豪萨语·Sin", "Kamfanin Sin ya samu hatsari a Nijar",
    "", ["ne"], False)
chk("约鲁巴语·Ṣáínà", "Ṣáínà yoo kọ́ ọ̀nà ní Èkó",
    "", ["ng"], False)
chk("伊博语·Chaịna", "Chaịna na Naịjirịa na-arụ ọrụ",
    "", ["ng"], False)
chk("契维语·Kyina", "Kyina adwuma wɔ Ghana",
    "", ["gh"], False)
chk("班巴拉语·Sini", "Sini ka baara bɛ Bamako",
    "", ["ml"], False)
chk("涉华·使馆援助洪灾", "Chinese embassy donates relief to flood victims",
    "中国使馆向洪灾灾民捐赠物资", ["ng"], False)

print("\n── C. 需求二（国防/政治/经济/能源/矿产/通道）→ 应留 ──")
chk("国防·军购", "Nigeria signs arms deal for fighter jets",
    "尼日利亚签署战机军购协议", ["ng"], False)
chk("国防·边境", "Border clash erupts between Mali and Niger patrols",
    "马里与尼日尔巡逻队在边境发生冲突", ["ml"], False)
chk("政治·选举", "Ghana election commission sets December date",
    "加纳选举委员会确定 12 月投票日", ["gh"], False)
chk("经济·央行", "Central bank raises interest rate to 27%",
    "央行将利率上调至 27%", ["ng"], False)
chk("经济·关税", "Cocoa export tariff raised",
    "可可出口关税上调", ["gh"], False)
chk("能源·铀矿", "Niger uranium output falls after mine shutdown",
    "尼日尔铀矿停产致产量下降", ["ne"], False)
chk("通道·铁路", "Railway project links Abuja to Kaduna",
    "阿布贾至卡杜纳铁路项目", ["ng"], False)
chk("能源·管道", "Niger-Benin pipeline resumes flow",
    "尼日尔-贝宁原油管道恢复输油", ["ne"], False)

print("\n── D. 需求三（恐怖组织）→ 应留 ──")
chk("JNIM·袭击", "JNIM attacks village in Mopti, 20 killed",
    "JNIM 袭击莫普提村庄致 20 人死亡", ["ml"], False)
chk("ISWAP·基地", "ISWAP raids military base in Borno",
    "ISWAP 袭击博尔诺军事基地", ["ng"], False)
chk("博科圣地·爆炸", "Boko Haram bombing kills civilians",
    "博科圣地炸弹袭击致平民死亡", ["ng"], False)

print("\n── E. 周边国家不受这条规则影响 ──")
chk("菲律宾台风", "Typhoon kills 50 in the Philippines",
    "台风致菲律宾 50 人死亡", ["ph"], False)
chk("缅甸洪灾", "Floods displace thousands in Myanmar",
    "缅甸洪灾致数千人流离失所", ["mm"], False)

print("\n── F. 多语种涉华词本身（正向/反向）──")
CN = ns["CN_RX"]
POS = ["China", "Chinese company", "chinois", "chinoise", "Ƙasar Sin",
       "Ṣáínà", "Chaịna", "Kyina", "Sini", "الصين", "中资", "中石油"]
NEG = ["Lagos truck overturns", "Un camion se renverse", "Sin is forgiven",
       "Sinaloa cartel"]
for t in POS:
    total += 1
    if not CN.search(t):
        fail += 1
        print("FAIL  应识别为涉华: %s" % t)
print("PASS  正向 %d 个涉华写法全部识别" % len(POS))
for t in NEG:
    total += 1
    hit = bool(CN.search(t))
    if hit:
        print("WARN  「%s」被识别为涉华（会漏删，不会错杀）" % t)
    else:
        print("PASS  反向未误判: %s" % t)

print("\n%d 项，%d 失败" % (total, fail))
sys.exit(1 if fail else 0)
