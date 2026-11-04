# -*- coding: utf-8 -*-
"""非洲四国「非涉华 / 非涉美 / 非三类」一律不收 —— 白名单口径回归测试。

读者 2026-11-04 的口径（原话，逐条对照）：
  「非洲四国除了涉及中国的因素外的新闻、动态，我只关心
    一是涉及 MPLJ、JNIM、ISIS、Lakurawa、EIGS 等组织的新闻、动态，
       你要讲这些组织翻译成英语、法语和非洲四国本地语言进行关键词检索；
    二是涉及中石油在尼日尔项目，比如 CNPC、SORAZ、尼日尔-贝宁石油管道项目；
    三是涉及非洲四国政治、经济、能源、矿产、非法黄金开采等」
  「我也关心美国在非洲四国涉及政治、经济、能源、矿产、军事等的活动情况」

所以判定是**白名单**：涉华 / 涉美 / 三类之一 → 留，否则删。

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
    "POLICY_ANCHOR_RX", "ECON_RX", "ILLEGAL_MINING_RX", "DEFENSE_POLICY_RX",
    "AFR4_CTYS", "US_RX", "US_ABBR", "has_us", "is_africa_off_topic",
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
OFF = ns["is_africa_off_topic"]

fail = 0
total = 0


def chk(label, title, zh, cty, want_drop):
    global fail, total
    total += 1
    got = OFF(title, zh, "", cty)
    ok = got == want_drop
    if not ok:
        fail += 1
    print("%s  %-26s %-4s %s"
          % ("PASS" if ok else "FAIL", label,
             "删" if got else "留", (zh or title)[:46]))


print("── A. 非涉华 / 非涉美 / 不在三类 → 应删 ──")
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
chk("治安·抢劫", "Armed robbery gang arrested in Kumasi",
    "库马西武装抢劫团伙被捕", ["gh"], True)
chk("法语·事故", "Un camion citerne explose à Niamey", "", ["ne"], True)
chk("文体·电影", "Nollywood film wins continental award",
    "尼日利亚电影获非洲大奖", ["ng"], True)
chk("文化·选美", "Miss Nigeria pageant holds final in Lagos",
    "尼日利亚小姐选美决赛在拉各斯举行", ["ng"], True)

print("\n── B. 涉华 → 应留（含多语种写法）──")
chk("中文·中资", "Chinese firm's site attacked in Zamfara",
    "中资企业工地遭袭", ["ng"], False)
chk("中文·中国公民", "Gunmen abduct Chinese nationals in Ogun",
    "中国公民在奥贡州被绑架", ["ng"], False)
chk("法语·chinois", "Une entreprise chinoise touchée par les inondations",
    "", ["ne"], False)
chk("豪萨语·Sin", "Kamfanin Sin ya samu hatsari a Nijar", "", ["ne"], False)
chk("约鲁巴语·Ṣáínà", "Ṣáínà yoo kọ́ ọ̀nà ní Èkó", "", ["ng"], False)
chk("伊博语·Chaịna", "Chaịna na Naịjirịa na-arụ ọrụ", "", ["ng"], False)
chk("契维语·Kyina", "Kyina adwuma wɔ Ghana", "", ["gh"], False)
chk("班巴拉语·Sini", "Sini ka baara bɛ Bamako", "", ["ml"], False)
chk("涉华·使馆援助", "Chinese embassy donates relief to flood victims",
    "中国使馆向洪灾灾民捐赠物资", ["ng"], False)

print("\n── C. 涉美 → 应留（读者 2026-11-04 追加）──")
chk("美军·基地", "Pentagon expands drone base in Niger",
    "五角大楼扩大在尼日尔的无人机基地", ["ne"], False)
chk("美方·制裁", "Washington sanctions Niger officials",
    "美国制裁尼日尔官员", ["ne"], False)
chk("美企·锂矿", "American firm wins lithium mining contract",
    "美国企业中标锂矿开采合同", ["ml"], False)
chk("美军·训练", "US troops train Nigerian army in the north",
    "美军在尼日利亚北部训练政府军", ["ng"], False)
chk("法语·américain", "Les États-Unis renforcent leur présence au Sahel",
    "", ["ml"], False)
chk("豪萨语·Amurka", "Amurka ta tura sojoji Nijar", "", ["ne"], False)

print("\n── D1. 恐怖组织（多语种）→ 应留 ──")
chk("JNIM", "JNIM attacks village in Mopti, 20 killed",
    "JNIM 袭击莫普提村庄致 20 人死亡", ["ml"], False)
chk("GSIM·法语", "Le GSIM revendique l'attaque de Mopti", "", ["ml"], False)
chk("MPLJ", "MPLJ fighters spotted near Niono", "", ["ml"], False)
chk("EIGS", "EIGS claims responsibility for the ambush", "", ["ne"], False)
chk("ISIS", "ISIS-linked cell dismantled in Kano", "", ["ng"], False)
chk("Daesh·法语", "Daech revendique l'attentat", "", ["ml"], False)
chk("Lakurawa", "Lakurawa gunmen attack Sokoto community",
    "Lakurawa 武装袭击索科托社区", ["ng"], False)
chk("ISWAP", "ISWAP raids military base in Borno",
    "ISWAP 袭击博尔诺军事基地", ["ng"], False)
chk("AQIM", "AQIM veteran returns to the Sahel", "", ["ml"], False)
chk("博科圣地·中文", "Boko Haram bombing kills civilians",
    "博科圣地炸弹袭击致平民死亡", ["ng"], False)

print("\n── D2. 中石油尼日尔项目 → 应留 ──")
chk("CNPC", "CNPC evacuates staff from Agadem block", "", ["ne"], False)
chk("SORAZ", "SORAZ refinery halts output after pipeline sabotage",
    "津德尔炼油厂因管道中断停产", ["ne"], False)
chk("尼贝管道", "Niger-Benin pipeline resumes flow",
    "尼日尔-贝宁原油管道恢复输油", ["ne"], False)

print("\n── D3. 政治 / 经济 / 能源 / 矿产 / 非法黄金开采 → 应留 ──")
chk("政治·议会", "Nigeria parliament passes 2027 budget",
    "尼日利亚议会通过 2027 年预算", ["ng"], False)
chk("政治·政变", "Niger coup leaders appoint civilian PM",
    "尼日尔政变领导人任命文职总理", ["ne"], False)
chk("经济·关税", "Ghana raises cocoa export tariff",
    "加纳上调可可出口关税", ["gh"], False)
chk("能源·铀矿", "Niger uranium output falls after mine shutdown",
    "尼日尔铀矿停产致产量下降", ["ne"], False)
chk("矿产·锂", "Mali signs lithium concession with foreign miner",
    "马里与外资矿企签署锂矿特许经营协议", ["ml"], False)
chk("非法采金·galamsey", "Galamsey crackdown nets 40 illegal miners",
    "加纳打击非法采金，抓获 40 名非法矿工", ["gh"], False)
chk("非法采金·orpaillage", "Orpaillage clandestin : 12 interpellations à Kayes",
    "", ["ml"], False)
chk("非法采金·中文", "Illegal gold mining spreads in Zamfara",
    "扎姆法拉非法采金蔓延", ["ng"], False)

print("\n── E. 周边国家不受这条规则影响 ──")
chk("菲律宾台风", "Typhoon kills 50 in the Philippines",
    "台风致菲律宾 50 人死亡", ["ph"], False)
chk("缅甸洪灾", "Floods displace thousands in Myanmar",
    "缅甸洪灾致数千人流离失所", ["mm"], False)
chk("越南选美", "Miss Vietnam final held in Ho Chi Minh City",
    "越南小姐决赛在胡志明市举行", ["vn"], False)

print("\n── F. 词表自身：多语种恐怖组织写法 ──")
TER = ns["TERROR_RX"]
POS = ["JNIM", "GSIM", "MPLJ", "EIGS", "ISGS", "ISSP", "ISIS", "Daesh", "Daech",
       "Lakurawa", "Lukarawa", "ISWAP", "AQIM", "AQMI", "Boko Haram",
       "État islamique", "داعش", "博科圣地", "伊斯兰国", "恐怖主义"]
for t in POS:
    total += 1
    if not TER.search(t):
        fail += 1
        print("FAIL  未识别: %s" % t)
print("PASS  %d 个多语种写法全部识别" % len(POS))

print("\n%d 项，%d 失败" % (total, fail))
sys.exit(1 if fail else 0)
