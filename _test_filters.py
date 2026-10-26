# -*- coding: utf-8 -*-
"""过滤规则回归测试。每条都来自真实抓到的标题或用户明确举的例子。

用法： python _test_filters.py
期望：全部 PASS。
"""
import io, os, re, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "build_data.py"), encoding="utf-8").read()
a = src.index("SPORT_RX = re.compile")
b = src.index("def is_off_scope")
ns = {"re": re}
# CN_RX（含 CN_RX_INTL 合并）单独取：is_soft / is_fluff 都要用它
c0 = src.index("CN_RX = re.compile")
c1 = src.index("# NOTE: the country abbreviation")
exec(src[c0:c1], ns)
exec(src[a:b], ns)
k = src.index("def is_off_scope"); k2 = src.index("\n\n\n", k); exec(src[k:k2], ns)
soft, fluff, off = ns["is_soft"], ns["is_fluff"], ns["is_off_scope"]

# (标题, 期望动作) 动作: drop=应丢弃 / keep=应保留
CASES = [
    # ── 用户明确不要的 ──
    ("Two container trucks overturn in Lagos, kill mother, children", "drop"),
    ("Mali : deux morts dans un accident de la route a Segou", "drop"),
    ("Niger : un homme interpelle pour vol de moto a Niamey", "drop"),
    ("Niger : le cholera fait 12 morts a Maradi", "drop"),
    ("Nigeria confirms first Ebola case in woman traveling from Uganda", "drop"),
    ("Ebola: NCDC urges hospitals to act before laboratory confirmation", "drop"),
    ("Unemployed man jailed 10 years for attempted robbery", "drop"),
    ("Niger : un divorce devant le tribunal de Niamey", "drop"),
    ("Three dead, three missing in motorboat accident in eastern Nepal", "drop"),
    # ── 用户要的：政治 / 经济 / 国防 / 安全 / 能源 / 矿产 / 通道 ──
    ("Putin Releases American Jailed in Russia, Agrees to Trilateral Peace Talks", "keep"),
    ("Japan Sends Senior Diplomat to Russia to Seek Release of Arrested Journalist", "keep"),
    ("15 ans de prison requis contre l ancien maire du District de Bamako", "keep"),
    ("Taraba Attack Death Toll Hits 58 As Residents Lament Neglect", "keep"),
    ("Le Parlement algerien approuve l envoi de forces au Niger", "keep"),
    ("Niger : l armee neutralise plusieurs jihadistes a Tillaberi", "keep"),
    ("Arlit : la SOMAIR suspend la production d uranium", "keep"),
    ("Mali : le JNIM bloque les camions de carburant vers Bamako", "keep"),
    ("Niger imposes curfew on petrol sales amid national fuel shortage", "keep"),
    ("Mali : la mine d or de Loulo augmente sa production", "keep"),
    ("Algeria to send troops to Niger in first foreign deployment in over 50 years", "keep"),
    # ── 涉华一律保留（用户的核心例外） ──
    ("Nigeria, China Deepen Film Industry Cooperation", "keep"),
    ("Nollywood Eyes $10B Asian Market In Landmark Nigeria-China Film Pact", "keep"),
    ("Chinese worker killed in Lagos factory accident", "keep"),
    ("Niger : un chauffeur chinois libere apres enlevement", "keep"),
    ("塔拉巴州与中国企业就固体矿产和基础设施开发签署谅解备忘录", "keep"),
    # ── 文体娱乐 / 软闻要丢 ──
    ("NFF hails Alex Iwobi on another milestone", "drop"),
    ("Finale du tournoi qualificatif de l UFOA B : le Niger bat la Cote d Ivoire aux tirs au but", "drop"),
    ("Nigerian actor Francis Duru die at age 57", "drop"),
    ("Shark kpai 65-year-old swimmer for South Africa east coast", "drop"),
    # ── 政治评论不能被动物词误杀 ──
    ("Tinubu: Lion at home, lamb abroad? By Suleiman A. Suleiman", "keep"),
    ("CSOs, Activists Fault Omo-Agege On Death Threat Over Poll Rigging", "keep"),
]

fails = 0
print("%-6s %-5s %-5s %-5s  %s" % ("结果", "soft", "fluff", "offscope", "标题"))
for title, want in CASES:
    s, f, o = soft(title), fluff(title), off(title)
    got = "drop" if (s or f or o) else "keep"
    ok = got == want
    if not ok:
        fails += 1
    print("%-6s %-5s %-5s %-5s  %s"
          % ("PASS" if ok else "FAIL", s, f, o, title[:60]))

print("\n%d/%d 通过，%d 失败" % (len(CASES) - fails, len(CASES), fails))
sys.exit(1 if fails else 0)
