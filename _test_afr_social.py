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
    "HARDSHIP_RX",
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

print("\n── F. 本地民生一律不收（读者 2026-11-05）──")
# 这批用例全部来自线上真实漏网条目。它们的共同点是**都带一个政治或经济
# 关键词**——naira / budget / lawmakers / Senate——所以白名单第一版全部
# 放行了。HARDSHIP_RX 必须在政治经济之前判，否则一个"奈拉"就能开门。
chk("现金救助金", "FG begins payment of N50,000 cash grant to vulnerable citizens",
    "尼日利亚联邦政府开始向处境脆弱的国民发放5万奈拉救助金", ["ng"], True)
chk("救济金科普稿", "What you should know about FG's N50,000 relief",
    "关于联邦政府发放5万奈拉救济金，您应该了解什么", ["ng"], True)
chk("养老金欠款", "FG pays N13.87bn pension arrears to 18,822 retirees",
    "尼日利亚联邦政府向18,822名退休人员支付138.7亿奈拉养老金欠款", ["ng"], True)
chk("学生贷款", "APC credits Tinubu for N372bn student loan and education reform",
    "全进步大会党将3720亿奈拉学生贷款和教育改革归功于蒂努布", ["ng"], True)
chk("基层医疗", "Zamfara prioritises primary healthcare in 2027 budget",
    "赞法拉州在2027年预算中优先发展基层医疗", ["ng"], True)
chk("老年照护", "Lagos extends financial aid and healthcare to aged caregivers",
    "拉各斯延长对老年照护者的财政援助和医疗保障", ["ng"], True)
chk("学校捐赠", "Senator donates laptops to schools in Kogi",
    "娜塔莎参议员任职三周年，为科吉州学校配备笔记本电脑", ["ng"], True)
chk("残障福利", "APC candidate promises welfare support for persons with disability",
    "拉各斯全进步大会党大会候选人承诺为残障人士提供福利支持", ["ng"], True)
chk("教育惩戒", "Parliament committee calls for safe discipline in secondary schools",
    "议会人权委员会呼吁在中学重新推行安全的惩戒措施", ["ng"], True)
chk("燃气泄漏", "Ogun and FG probe repeated gas leaks in Ijebu-Ode",
    "奥贡州与联邦政府调查伊杰布奥德反复发生的燃气泄漏事件", ["ng"], True)
chk("人事任命·委员", "Kano Assembly confirms two commissioner nominees",
    "卡诺州议会将于星期二审查两名州务委员候选人", ["ng"], True)
chk("人事任命·发言人", "Governor Yusuf appoints spokesperson, commissioner",
    "州长 Yusuf 任命新闻发言人兼委员", ["ng"], True)
chk("品牌奖软新闻", "Big Bull Rice, Chicken Republic, Gino and NNPC win Brand of the Year",
    "Big Bull Rice、Chicken Republic、Gino 和 NNPC 荣获世界品牌奖", ["ng"], True)
chk("英文洪灾", "Flood displaces 3,000 residents in Benue",
    "贝努埃州洪灾致 3000 名居民流离失所", ["ng"], True)

print("\n── G. 民生词不能盖过涉华：中国是主体就要留 ──")
chk("中资企业捐物资给洪灾灾民",
    "Chinese firm donates relief materials to flood victims in Benue",
    "中资企业向贝努埃州洪灾灾民捐赠物资", ["ng"], False)
chk("中国援建医院",
    "China-funded hospital opens in Abuja",
    "中国援建医院在阿布贾落成", ["ng"], False)
chk("中资矿企与非法采金",
    "Chinese nationals arrested over illegal mining in Ghana",
    "加纳逮捕涉嫌非法采金的中国公民", ["gh"], False)
chk("中石油炼油厂",
    "SORAZ refinery resumes operations in Zinder",
    "SORAZ 炼油厂在津德尔恢复运营", ["ne"], False)

print("\n── H. 地名里藏着国名／石油词的假阳性 ──")
# 2026-11-05 实测：「卡齐纳」的 Ka·tsina 命中了埃维语的 tsina（中国），
# 「津德尔」命中了 NIGER_OIL_RX 的 \bzinder\b。两条本地稿因此被当成
# 涉华／石油留下。与当年 \bus\b 命中代词 us 是同一个家族。
chk("卡齐纳传统制度不得判为涉华", "Katsina: we restored dignity of traditional institution",
    "拉达：我们恢复了尊严，并加强了卡齐纳的传统制度", ["ng"], True)
chk("津德尔移民安置不得判为石油", "IOM hands reintegration packages to returning migrants in Zinder",
    "津德尔：国际移民组织向返乡的尼日利亚移民发放重返社会安置包", ["ne"], True)
chk("卡齐纳警方不得判为涉华", "Police arrest three over political violence in Katsina",
    "卡蒂纳警方因政治暴力逮捕3人", ["ng"], True)

print("\n── I. 词表自身：多语种恐怖组织写法 ──")
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
