# -*- coding: utf-8 -*-
"""定性指标的评分标准（rubric）：**数据，不是代码**。

设计取向（与上一版的关键区别）
------------------------------
上一版把定性指标拆成一堆二元判据（是/部分/否），再用 if/elif 组合成档位。那条链路的
问题不是不严谨，而是**不必要地复杂**：判断「有没有制度、有没有执行」本来就是一个语义
判断，拆成几十个格子后每一格都要配词表、配正则、配补丁，最后还是靠人写的规则把格子
拼回档位——中间那层只增加了维护成本和误判面。

现在每个定性评分点有四样东西：

    检索关键词（只用于**召回**段落，不参与计分）
    + 证据组（每组「满足什么才算」，细则里的反例原样带上）
    + 100 分路径（哪几组必须全部成立、哪几组至少一条）
    + 100/50/0 三档判据原文（照抄评分标准表，不转述）
    + 一次判档（LLM 逐组判定 + 选档；无 LLM 时按证据层级兜底）

所以改口径 = 改这个文件里的一句话；不需要动词表仓库、不需要加正则补丁。

权威来源
--------
- 档位与判据：``S1.1当前具体方案.docx`` 的评分标准表（逐字照抄，只改标点）
- 检索关键词与边界情形：``S1.1_定性指标评分细则.md`` 的证据组词表（只取词，不取分档逻辑）
- 词表本体：``keywords.csv``（本模块只读它；改词不必改代码，见文件头注释）

``RUBRIC_VERSION`` 改动必须 +0.1：它进判档缓存指纹，改了不升版本会让旧结论被复用。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from . import keywords_io

RUBRIC_VERSION = "rubric-1.5"         # 1.5：0 分档补「仅有声明性文件按 50」的判定（basis 字段）
PROMPT_VERSION = "qual-prompt-1.3"    # 1.3：输出加 basis（none/statement/system/result/violation）
MATCH_VERSION = "match-extra-1.3"     # 1.3：S1.1.2 补通用薪酬词（remuneration/salary/wage…）；
#                                       1.2 是 LLM 召回跳过非计分组（T7 人权 / D0 人权政策）；
#                                       1.1 是英文词首匹配 + 套话/索引行降权

# 召回口径（段数）放在这里而不是 qualitative.py：它和词表一样是**判档口径**，
# 要进 rubric_hash()，否则改段数会静默复用旧判档。见 §2026-11-09 GT 对账。
RECALL_LIMIT = 24          # 送给 LLM 判档的最多段数（12→24：GT 对账时 9 条证据卡在 13–31 名）
KEYWORD_RECALL_LIMIT = 12  # 关键词兜底只看前 12 段：兜底是安全网，口径「宁低不高」；
#                            段数一多，凑齐「制度＋执行」两层词就容易得多（实测 02269 的
#                            S1.1.1-C 会从 50 跳到 100，而人工举证表判 50）。

# 兜底词表冻结在 v1.4（350 词）：召回词表是**可以继续扩**的（GT 对账一直在往里加同义词），
# 但兜底是确定性回归基线，加词会把它顶歪——实测 child labour 让 02269/03692 的 S1.1.1
# 从 50 跳到 100（GT 判 50 / 15）。所以两条路用两套词：
#   · 送 LLM：全量词表（越全越好）
#   · 兜底  ：扣掉下面这些「v1.5 之后新增」的词，等于仍然是 v1.4 的 350 词
# 新增词时必须同步登记到这里，否则基线会悄悄漂移。见 tests/test_qualitative.py。
BASELINE_EXCLUDED: dict[str, frozenset[str]] = {
    "S1.1.1-C": frozenset({"internal transfer", "reassignment",
                           "child labour", "child labor", "童工", "未成年工", "年龄核实"}),
    "S1.1.3": frozenset({"产假", "陪产假", "育儿假", "maternity leave", "paternity leave",
                         "parental leave", "human rights", "人权"}),
    "S1.1.4": frozenset({"tuition"}),
    "S1.1.5": frozenset({"晋升", "晉升", "内部竞聘", "竞聘", "职业发展路径",
                         "internal promotion", "promotion criteria", "internal recruitment",
                         "career path", "career development", "human rights", "人权",
                         "competency", "job requirements", "fair recruitment"}),
}

# 召回时整组跳过的组：它们不在 ``specs``/100 分路径里，命中再多也不能支撑判决，
# 只会挤掉真正的证据。实测 S1.1.3 有 53% 的席位被 T7（人权）占掉、S1.1.5 有 14% 被 D0 占掉。
# 注意：只影响 **LLM 召回**；``baseline_groups`` 仍用完整 ``groups``，keyword 兜底一字不动。
RECALL_SKIP: dict[str, frozenset[str]] = {
    "S1.1.3": frozenset({"T7 人权与劳工权益尽调"}),
    "S1.1.5": frozenset({"D0 人权政策与尽调"}),
}

# 只给 LLM 召回补的通用词：``keywords.csv`` 里 S1.1.2 只收了「薪酬支付/工资支付/
# remuneration policy」这类**专指**写法，英文报告常用的 "remuneration"、"salary"、
# "collective wage negotiation" 一个都不命中——实测 01530 召回只剩 1 段（内控审计），
# 02096/03320/06160/06821 的薪酬段落全部漏掉。补词只走 ``recall_groups``，
# 不进 ``groups``，所以 keyword 兜底的 350 词基线不受影响。
RECALL_EXTRA: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "S1.1.2-C": (
        ("P* 通用薪酬表述", ("remuneration", "salary", "salaries", "wage", "compensation",
                              "薪酬", "工资", "同工同酬", "equal pay", "living wage")),
    ),
}


def baseline_groups(rub: "Rubric") -> tuple[tuple[str, tuple[str, ...]], ...]:
    """兜底用的词表：现行词表扣掉 ``BASELINE_EXCLUDED`` 里登记的词（= v1.4 的 350 词）。"""
    drop = BASELINE_EXCLUDED.get(rub.key, frozenset())
    groups = []
    for group, words in rub.groups:
        kept = tuple(w for w in words if w not in drop)
        if kept:
            groups.append((group, kept))
    return tuple(groups)


@dataclass(frozen=True)
class GroupSpec:
    """一个证据组在**判档**时的定义（与召回词表无关）。

    ``need`` 直接取自细则「判定满足的证据要求」列，连反例一起带上——模型最容易犯的错
    不是看不出「有制度」，而是把「计划整改」「零投诉」「只倡导多元」当成已成立。
    """

    id: str
    label: str
    need: str


@dataclass(frozen=True)
class PathRule:
    """100 分的证据路径：``require_all`` 全部 yes，且 ``require_any`` 至少一条 yes/partial。

    ``partial`` 不能顶 ``require_all``：细则的「制度」「已实施」都是硬条件，
    只写了一半不能算成立；``require_any`` 是「多条证明路径之一」，partial 可以进。
    """

    require_all: tuple[str, ...] = ()
    require_any: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Rubric:
    """一个定性评分点的全部口径。

    ``key`` 是缓存与报告里的唯一标识：整项定性指标用 ``S1.1.3``，
    加权指标里的定性分项用 ``S1.1.1-C``（A/B 是定量，不走本模块）。
    """

    key: str
    ind: str
    part: str                                   # "C" 或 ""（整项）
    name: str
    tiers: dict[int, str]                       # {100: 判据, 50: 判据, 0: 判据}
    # 关键词按**证据组**存（组名对应细则的 E1–E4 / P1–P6 / T1–T6 / W1–W6 / D1–D10）。
    # 分组不是为了匹配（匹配只看有没有命中），是为了让「这个词为什么在这里」有答案：
    # 出表格、复核口径、加词时不用再翻细则。
    groups: tuple[tuple[str, tuple[str, ...]], ...] = ()
    note: str = ""                              # 判档时提醒模型的边界（如"未披露离职率→0"）
    gate: str = ""                              # 前置事实判据：满足则直接 0 档，不再问模型
    # 证据组定义与 100 分路径：进提示词、也进档位复核。``groups`` 只管召回，
    # ``specs`` 只管判档——两套编号可以同名，但定义互不依赖。
    specs: tuple[GroupSpec, ...] = ()
    path100: PathRule | None = None
    event_groups: tuple[str, ...] = ()          # 有已识别事件时，这些组必须全部成立
    # 档位下限：0 分档在本项不适用时用。S1.1.1 的 0 分只有「未识别相关内容」与
    # 「未披露离职率」两条，后者已由 gate 拦掉；离职率一旦披露，就说明识别到了相关内容，
    # 不能再记「未识别」。人工裁定（02269）也按这条：0 分档不适用，取 50。
    floor_tier: int = 0

    @property
    def keywords(self) -> tuple[str, ...]:
        """展平后的召回词表（去重、保序）。匹配时只看集合，顺序只影响表格呈现。"""
        seen: set[str] = set()
        out: list[str] = []
        for _, words in self.groups:
            for w in words:
                if w not in seen:
                    seen.add(w)
                    out.append(w)
        return tuple(out)

    @property
    def recall_groups(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """送 LLM 判档时实际参与召回的组（扣掉 ``RECALL_SKIP``）。

        与 ``groups`` 分开是刻意的：``groups`` 是词表原件，兜底基线与关键词清单都要看它；
        本属性只描述「哪些词值得占 24 个席位」。
        """
        skip = RECALL_SKIP.get(self.key, frozenset())
        kept = tuple((g, w) for g, w in self.groups if g not in skip)
        return kept + RECALL_EXTRA.get(self.key, ())

    @property
    def recall_keywords(self) -> tuple[str, ...]:
        """``recall_groups`` 展平后的词表（LLM 召回用；兜底仍用 ``keywords``）。"""
        seen: set[str] = set()
        out: list[str] = []
        for _, words in self.recall_groups:
            for w in words:
                if w not in seen:
                    seen.add(w)
                    out.append(w)
        return tuple(out)

    @property
    def tier_values(self) -> tuple[int, ...]:
        return tuple(self.tiers)

    def to_dict(self) -> dict:
        return {"key": self.key, "ind": self.ind, "part": self.part, "name": self.name,
                "tiers": {str(k): v for k, v in self.tiers.items()},
                "groups": [{"group": g, "keywords": list(ws)} for g, ws in self.groups],
                "keywords": list(self.keywords), "note": self.note, "gate": self.gate,
                "specs": [{"id": s.id, "label": s.label, "need": s.need} for s in self.specs],
                "path100": ({"require_all": list(self.path100.require_all),
                             "require_any": list(self.path100.require_any),
                             "note": self.path100.note} if self.path100 else None),
                "event_groups": list(self.event_groups), "floor_tier": self.floor_tier}

# ---------------------------------------------------------------- 五个定性评分点
# 判据（tiers）、边界提示（note）、0 分前置条件（gate）留在代码里——它们是**口径原文**，
# 改一句就该走代码评审与版本号。检索词表相反，是**业务配置**：放在同目录的
# ``keywords.csv``（四列，Excel 可直接编辑），评审会上说「再加一个『年休假』」不必改代码，
# 编辑表格 + 重启进程即可。词表仍进 ``rubric_hash()``，改词必换指纹，缓存不会串。
_SPECS: tuple[Rubric, ...] = (
    # ---- S1.1.1 C：文中 "或未披露离职率" 是硬性 0 分条件，故单列 gate
    Rubric(
        key="S1.1.1-C", ind="S1.1.1", part="C", name="披露质量与就业终止保障",
        tiers={
            100: "建立明确的劳动合同、解雇及员工安置保障制度；详细披露离职率构成，"
                 "提供实际离职保障结果或纠正成效的充分证据。"
                 "核验时对应 E0 至 E4，E0 须达到构成披露要求，E4 的实际结果与有效纠正为两条可选路径。",
            50: "建立劳动合同管理及合法解雇制度，但缺乏实际保障结果或纠正成效的充分证据；"
                "或仅有原则性声明。本档适用前须已披露离职率。",
            0: "未识别到相关内容；或未披露离职率。",
        },
        note="离职率构成＝自愿/非自愿拆分或留存率反推；只有总数不满足 E0，"
             "已披露离职率但构成不完整只表示尚未满足 100 分，不因此记 0。"
             "没有裁员时仍核查 E1–E3 的现行制度，E4 可用正常终止补偿等已发生的保障结果。"
             "仅称「妥善安置」不算 E4。细则本项未列童工/年龄核实证据组，不据此加分。",
        specs=(
            GroupSpec("E0", "离职率构成",
                      "披露当期离职率并拆分自愿/非自愿，或可用留存率反推构成；只有总数不满足"),
            GroupSpec("E1", "劳动合同保障",
                      "说明签订、续签、合同期限或权责管理等实质要求；单独的「依法用工」不满足"),
            GroupSpec("E2", "解雇及裁员程序",
                      "说明通知、协商、合法理由、补偿核算或申诉等实际程序；正常离职手续不算"),
            GroupSpec("E3", "员工安置保障",
                      "说明转岗、再培训、再就业协助等安排及适用对象；普通职业培训不满足"),
            GroupSpec("E4", "实际保障结果或纠正",
                      "与 E1–E3 相关的覆盖、支付、受益或纠正事实，能识别期间与对象；"
                      "仅称「妥善安置」不满足"),
        ),
        path100=PathRule(
            require_all=("E0", "E1", "E2", "E3", "E4"),
            note="E4 的「实际结果」与「有效纠正」是两条可选路径，任一成立即可。"),
        gate="未披露本期离职率",
        floor_tier=50,
    ),
    # ---- S1.1.2 C
    Rubric(
        key="S1.1.2-C", ind="S1.1.2", part="C", name="薪酬支付保障",
        tiers={
            100: "建立明确的薪酬支付保障制度，并披露已经实施的对应行动（如按规定周期发放工资、"
                 "开展工资核算与复核、支付加班报酬、处理工资差错或薪酬申诉）。提供工资支付监测、"
                 "内部审查结果、制度覆盖数据或已发现薪酬问题的有效纠正证据（P3–P6 至少一条）。",
            50: "披露薪酬支付保障制度，但缺乏充分的实际执行结果或纠正成效证据；或仅有声明性文件。",
            0: "未识别到相关内容；或存在经核实的重大欠薪、违法克扣工资等行为且未采取有效纠正措施。",
        },
        note="加班费的核算与支付归本项；加班时长/审批/调休归 S1.1.3。绩效奖金只是背景，不替代支付保障。",
        specs=(
            GroupSpec("P1", "薪酬支付制度",
                      "说明按时足额、发薪日、最低工资、加班报酬等实质要求；"
                      "一般薪酬竞争力声明不满足"),
            GroupSpec("P2", "具体执行措施",
                      "核算、复核、支付、异常处理或员工查询的操作流程；只称「建立薪酬体系」不满足"),
            GroupSpec("P3", "工资支付监测",
                      "当期实际检查/监测的对象、范围和结果；只承诺「定期监测」只能支持 P2"),
            GroupSpec("P4", "内部审查结果",
                      "实际开展的工资支付审查及发现；一般财务审计意见不能替代"),
            GroupSpec("P5", "制度实际覆盖",
                      "数据对应支付保障制度的实际执行并说明范围；"
                      "员工总数或「政策适用于全体员工」不算"),
            GroupSpec("P6", "问题有效纠正",
                      "对应已识别支付问题的补救完成、受影响员工与复核事实；计划补发不满足"),
        ),
        path100=PathRule(require_all=("P1", "P2"),
                         require_any=("P3", "P4", "P5", "P6")),
    ),
    # ---- S1.1.3
    Rubric(
        key="S1.1.3", ind="S1.1.3", part="", name="工作时间与休息休假保障",
        tiers={
            100: "建立明确的工作时间、加班管理及休息休假保障制度（T1–T3），并提供可核验的执行证据"
                 "（T5 工时监测、休假执行数据、覆盖情况，或 T6 针对不合理工时问题的纠正成果）。",
            50: "披露工作时间与休息休假保障制度，但未披露具体实施措施（如工时记录、加班审批或"
                "休假管理程序），缺乏充分的实际执行结果或改善成效证据；或仅声明性文件。",
            0: "未识别到相关内容；或存在经核实的严重超时工作、违法剥夺休息休假权利等情况且"
               "未采取有效纠正措施。",
        },
        note="「员工依法享有年假」只说明权利；「用系统跟踪/记录休假」「设有工时系统」是手段，"
             "不等于执行结果（T4 记录可同时支持 T5，不要求重复披露）。"
             "要有休假使用率、实际休假天数、覆盖比例或超时问题的检查与整改结论，才算执行证据。"
             "仅统计平均工时不能排除部分员工长期超时；仅统计总部休假不代表全部生产基地。"
             "加班费支付归 S1.1.2；职业伤病与安全事故归 S1.2。",
        specs=(
            GroupSpec("T1", "工作时间制度",
                      "明确适用工时安排、排班管理或合理工时控制；不要求各地区采用同一工时数值"),
            GroupSpec("T2", "加班管理制度",
                      "加班申请、批准、时长控制或异常处置安排；只说明支付加班费不满足"),
            GroupSpec("T3", "休息休假保障",
                      "适用休息休假权利及取得方式；只列出假期名称、无保障内容则证据偏弱"),
            GroupSpec("T4", "具体实施措施",
                      "已经采用的考勤、工时记录、排班检查或休假申请机制"),
            GroupSpec("T5", "执行结果",
                      "当期工时监测、异常工时、休假使用率或实际覆盖，能识别对象与范围；"
                      "只有员工人数不满足"),
            GroupSpec("T6", "问题纠正",
                      "针对不合理工时或休假侵害的改正事实及结果；补发工资不能代替恢复休息权"),
        ),
        path100=PathRule(require_all=("T1", "T2", "T3"),
                         require_any=("T5", "T6"),
                         note="T4 可以同时支持 T5，不单独作为 100 分路径。"),
    ),
    # ---- S1.1.4
    Rubric(
        key="S1.1.4", ind="S1.1.4", part="", name="员工福利与支持",
        tiers={
            100: "已披露员工福利或家庭支持制度（W4），并披露已经实施的对应行动"
                 "（W1–W3 中至少一项，如为员工缴纳社会保险、提供补充医疗保障、发放福利补贴、"
                 "提供托育照护支持或开展员工援助服务）。",
            50: "披露员工福利及工作生活支持措施，但缺乏充分的覆盖范围或实际使用证据；"
                "或仅有员工关怀、福利保障、工作生活平衡等声明性文件。",
            0: "未识别到相关内容；或存在已核实的严重福利权益侵害且未有效纠正。",
        },
        note="「已为员工缴纳/投保」就是已实施行动；覆盖率只是佐证，不是满分前提。"
             "W5 覆盖数据与 W6 使用/受益事实都只作佐证，不是 100 分的额外门槛。"
             "「全体员工可申请」只说明资格范围；面向社区的慈善支出不计入本项。",
        specs=(
            GroupSpec("W1", "法定社会保障与基本福利",
                      "说明适用员工的具体保障安排；单纯「遵守社会保障法规」不满足"),
            GroupSpec("W2", "补充福利",
                      "明确项目、对象、标准或领取方式；供应商服务或客户优惠不算员工福利"),
            GroupSpec("W3", "家庭与生活支持",
                      "员工或其家庭可获得的具体资源、服务、假期或经济支持"),
            GroupSpec("W4", "具体制度与取得安排",
                      "能识别至少一类福利的规则和取得方式；只有活动名称或宣传口号不满足"),
            GroupSpec("W5", "实际覆盖",
                      "数据对应具体福利和实际纳入人群，能识别期间与分母；"
                      "「面向全体员工开放」只说明资格"),
            GroupSpec("W6", "实际使用或受益",
                      "当期实际支付、使用或受益事实；只披露预算和未来计划不满足"),
        ),
        path100=PathRule(require_all=("W4",), require_any=("W1", "W2", "W3"),
                         note="W1–W3 里被引用的那一项须写成已实施（已缴纳/已投保/已发放），"
                              "只写资格或计划不算。"),
    ),
    # ---- S1.1.5
    Rubric(
        key="S1.1.5", ind="S1.1.5", part="", name="平等机会、反歧视与反骚扰",
        tiers={
            100: "基础条件：已披露反歧视与反骚扰相关制度（D1、D2），并披露已经实施的对应行动"
                 "（D3–D7 中至少一项，如专项培训、公平招聘或晋升程序、公平性审查、"
                 "实际受理投诉或事件调查）。事件条件：如报告披露已识别事件，"
                 "还须提供调查、受影响人员保护、责任追究及有效纠正证据（D7–D10）。",
            50: "已披露平等机会、反歧视与反骚扰相关制度，但未识别到具体执行行动；或仅有平等对待、"
                "反对歧视、禁止骚扰等声明性文件。",
            0: "未识别到相关内容；或存在经核实的严重、持续性歧视、骚扰或系统性机会不公平，且未有效纠正。",
        },
        note="制度存在不等于已执行；没有已识别事件时不要求个案统计，事件处置记录记不适用。"
             "存在事件但处置证据不足时，不按无事件路径给 100 分。"
             "「反腐败」「商业道德」不能替代反骚扰；投诉不等于查实，"
             "撤诉、和解或笼统结案不自动证明有效纠正。"
             "零投诉可能受渠道可及性影响，不能当作不存在歧视骚扰的独立证明。",
        specs=(
            GroupSpec("D1", "正式禁止歧视",
                      "现行正式政策明确禁止员工相关歧视并说明实质要求；只倡导多元不等于"),
            GroupSpec("D2", "正式禁止骚扰",
                      "现行正式政策明确禁止骚扰；「反腐败」「商业道德」不能替代"),
            GroupSpec("D3", "公平招聘程序",
                      "岗位相关、一致或透明的招聘程序；只披露员工性别比例不满足"),
            GroupSpec("D4", "公平晋升程序",
                      "晋升依据、评审程序或申诉机制；普通培训发展计划不满足"),
            GroupSpec("D5", "针对性预防",
                      "已实施的专项预防活动，能识别内容、对象与实施事实；泛化道德培训不足"),
            GroupSpec("D6", "专项监测或流程审查",
                      "针对招聘晋升公平或歧视骚扰的实际审查、监测及范围；一句「定期审查」不足"),
            GroupSpec("D7", "事件调查",
                      "事件发生后有实际调查和处理状态；仅设举报邮箱不等于已经调查"),
            GroupSpec("D8", "受影响人员保护",
                      "说明保密、反报复、隔离风险或支持措施；有事件时应有实际保护证据"),
            GroupSpec("D9", "责任追究",
                      "对查实责任有适当处理；只有「可予处罚」属于制度，不能替代实际处置"),
            GroupSpec("D10", "有效纠正",
                      "措施对应已识别问题并有实施或效果证据；结案、和解或撤诉不自动证明"),
        ),
        path100=PathRule(require_all=("D1", "D2"),
                         require_any=("D3", "D4", "D5", "D6", "D7")),
        event_groups=("D7", "D8", "D9", "D10"),
    ),
)

KEYWORD_FILE = keywords_io.default_path()
KEYWORD_SOURCE = keywords_io.describe_source(KEYWORD_FILE)
_GROUPS = keywords_io.load(KEYWORD_FILE, known=tuple(r.key for r in _SPECS))

RUBRICS: dict[str, Rubric] = {}
for _spec in _SPECS:
    _words = _GROUPS.get(_spec.key, ())
    if not _words:
        raise keywords_io.KeywordFileError(
            f"{KEYWORD_FILE} 里没有 {_spec.key} 的词条：每个 rubric 至少要有一个证据组")
    RUBRICS[_spec.key] = replace(_spec, groups=_words)

# 加权指标里的定性分项（其余分项是定量，由 facts 算）
QUALITATIVE_PARTS: dict[str, tuple[str, ...]] = {
    "S1.1.1": ("S1.1.1-C",),
    "S1.1.2": ("S1.1.2-C",),
    "S1.1.3": ("S1.1.3",),
    "S1.1.4": ("S1.1.4",),
    "S1.1.5": ("S1.1.5",),
}

# ---------------------------------------------------------------- 兜底：证据层级
# 细则「通用规则 1」的三层证据。命中层级 → 档位（保守：只有"制度＋执行"才给 100）。
LEVEL_WORDS: dict[str, tuple[str, ...]] = {
    "result": ("已实施", "已开展", "已提供", "已缴纳", "已投保", "已发放", "已完成",
               "覆盖率", "使用率", "参保率", "实际", "监测结果", "检查结果", "整改",
               "补发", "复核", "受益", "人次", "implemented", "carried out", "completed",
               "coverage", "uptake", "utilisation", "utilization", "conducted"),
    "system": ("制度", "政策", "规定", "程序", "流程", "负责", "审批", "标准", "机制",
               "管理办法", "policy", "procedure", "system", "guideline", "mechanism",
               "requirements", "protocol"),
    "statement": ("重视", "关怀", "致力于", "零容忍", "依法", "合规", "承诺", "倡导",
                  "尊重", "保障员工", "committed", "zero tolerance", "respect",
                  "dedicated", "comply with"),
}

def rubric_hash() -> str:
    """全部 rubric 的内容哈希：进判档缓存指纹，改一个字就重算。

    关键词取**排序后**的集合：分组方式变了但词没变时，不该让缓存失效——
    分组只影响表格与复核，不改变给模型看的东西。

    ``MATCH_VERSION`` 也进指纹：改的是「怎么匹配」（比如英文改成词首匹配），
    词表一个字没动，召回结果却会变——不带上它，旧结论会被静默复用。

    ``RECALL_LIMIT`` / ``KEYWORD_RECALL_LIMIT`` 同理：段数改了，送模型的段落与兜底档位
    都会变，不进指纹就会复用 12 段时算出的旧判档。
    """
    payload = "|".join(
        f"{r.key}:{r.tiers}:{sorted(r.keywords)}:{r.note}:{r.gate}"
        f":{r.specs}:{r.path100}:{r.event_groups}:{r.floor_tier}:{r.recall_groups}"
        for r in sorted(RUBRICS.values(), key=lambda x: x.key))
    return hashlib.sha256(
        f"{RUBRIC_VERSION}|{PROMPT_VERSION}|{MATCH_VERSION}|"
        f"{RECALL_LIMIT}|{KEYWORD_RECALL_LIMIT}|{payload}".encode("utf-8")).hexdigest()[:12]

__all__ = ["GroupSpec", "PathRule", "Rubric", "RUBRICS", "QUALITATIVE_PARTS", "LEVEL_WORDS",
           "RUBRIC_VERSION", "PROMPT_VERSION", "MATCH_VERSION", "RECALL_SKIP", "RECALL_EXTRA",
           "RECALL_LIMIT", "KEYWORD_RECALL_LIMIT", "rubric_hash"]
