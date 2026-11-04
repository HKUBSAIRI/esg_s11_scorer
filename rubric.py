# -*- coding: utf-8 -*-
"""定性指标的评分标准（rubric）：**数据，不是代码**。

设计取向（与上一版的关键区别）
------------------------------
上一版把定性指标拆成 29 个二元要素（是/部分/否），再在 ``scoring.py`` 里用 if/elif
把要素组合拼成档位。那条链路的问题不是不严谨，而是**不必要地复杂**：判断「有没有制度、
有没有执行」本来就是一个语义判断，拆成 29 个格子后每一格都要配词表、配正则、配补丁，
最后还是靠人写的规则把格子拼回档位——中间那层只增加了维护成本和误判面。

现在每个定性评分点只有三样东西：

    检索关键词（只用于**召回**段落，不参与计分）
    + 100/50/0 三档判据原文（照抄评分标准表，不转述）
    + 一次判档（LLM 读段落 + 判据 → 选档；无 LLM 时按证据层级兜底）

所以改口径 = 改这个文件里的一句话；不需要动词表仓库、不需要加正则补丁。

权威来源
--------
- 档位与判据：``S1.1当前具体方案.docx`` 的评分标准表（逐字照抄，只改标点）
- 检索关键词与边界情形：``S1.1_定性指标评分细则.md`` 的证据组词表（只取词，不取分档逻辑）

``RUBRIC_VERSION`` 改动必须 +0.1：它进判档缓存指纹，改了不升版本会让旧结论被复用。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

RUBRIC_VERSION = "rubric-1.0"
PROMPT_VERSION = "qual-prompt-1.0"


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
    def tier_values(self) -> tuple[int, ...]:
        return tuple(self.tiers)

    def to_dict(self) -> dict:
        return {"key": self.key, "ind": self.ind, "part": self.part, "name": self.name,
                "tiers": {str(k): v for k, v in self.tiers.items()},
                "groups": [{"group": g, "keywords": list(ws)} for g, ws in self.groups],
                "keywords": list(self.keywords), "note": self.note, "gate": self.gate}


# ---------------------------------------------------------------- 五个定性评分点
RUBRICS: dict[str, Rubric] = {r.key: r for r in (
    # ---- S1.1.1 C：文中 "或未披露离职率" 是硬性 0 分条件，故单列 gate
    Rubric(
        key="S1.1.1-C", ind="S1.1.1", part="C", name="披露质量与就业终止保障",
        tiers={
            100: "建立明确的劳动合同、解雇及员工安置保障制度；详细披露离职率构成，"
                 "提供实际离职保障结果或纠正成效的充分证据。",
            50: "建立劳动合同管理及合法解雇制度，但缺乏实际保障结果或纠正成效的充分证据；"
                "或仅有原则性声明。",
            0: "未识别到相关内容；或未披露离职率。",
        },
        groups=(
            ("E1 劳动合同保障", (
                "劳动合同", "雇佣合同", "聘用合约", "employment contract", "labour contract",
                "labor contract", "合同签订", "合同管理", "续签",
            )),
            ("E2 解雇及裁员程序", (
                "解除劳动合同", "终止雇佣", "termination of employment", "termination",
                "severance", "裁员", "redundancy", "经济补偿", "补偿金", "通知期",
                "notice period",
            )),
            ("E3 员工安置保障", (
                "转岗", "再就业", "redeployment", "outplacement", "员工安置",
            )),
            ("E4 离职率构成与保障结果", (
                "离职率", "turnover rate", "留存率", "retention rate", "自愿离职",
                "voluntary turnover", "离职面谈", "exit interview",
            )),
            # 召回扩展：报告常只写 employment / separation / handover，不写全称
            ("补充·召回扩展", (
                "employment", "separation", "handover", "labour law", "labor law",
                "劳动合同法", "雇佣关系", "解雇", "离职", "员工手册", "employee handbook",
            )),
        ),
        note="离职率构成＝自愿/非自愿拆分或留存率反推；仅写「依法用工」不算制度。",
        gate="未披露本期离职率",
    ),
    # ---- S1.1.2 C
    Rubric(
        key="S1.1.2-C", ind="S1.1.2", part="C", name="薪酬支付保障",
        tiers={
            100: "建立明确的薪酬支付保障制度，并披露已经实施的对应行动（如按规定周期发放工资、"
                 "开展工资核算与复核、支付加班报酬、处理工资差错或薪酬申诉）。提供工资支付监测、"
                 "内部审查结果、制度覆盖数据或已发现薪酬问题的有效纠正证据。",
            50: "披露薪酬支付保障制度，但缺乏充分的实际执行结果或纠正成效证据；或仅有声明性文件。",
            0: "未识别到相关内容；或存在经核实的重大欠薪、违法克扣工资等行为且未采取有效纠正措施。",
        },
        groups=(
            ("P1 薪酬支付制度", (
                "薪酬支付", "工资支付", "payroll", "按时足额", "发薪日", "pay day",
                "最低工资", "minimum wage", "薪酬制度", "remuneration policy",
                "compensation policy",
            )),
            ("P1 加班报酬", (
                "加班费", "overtime pay", "overtime remuneration",
            )),
            ("P2 具体执行措施", (
                "工资核算", "工资核对", "工资复核", "工资单", "payslip", "pay slip",
            )),
            ("P3 工资支付监测", (
                "工资检查", "薪酬审查", "pay review",
            )),
            ("P4 内部审查结果", (
                "pay audit",
            )),
            ("P6 问题有效纠正", (
                "薪酬申诉", "工资差错", "补发工资", "欠薪", "wage arrears", "克扣工资",
            )),
            ("P1 同工同酬背景", (
                "同工同酬", "equal pay",
            )),
            # 召回扩展：报告常用 salary / wage / remuneration 的泛称
            ("补充·召回扩展", (
                "salary", "salaries", "wage", "wages", "remuneration", "compensation",
                "工资", "薪资", "薪酬", "报酬", "发薪", "调薪", "salary review",
            )),
        ),
        note="加班费的核算与支付归本项；加班时长/审批/调休归 S1.1.3。绩效奖金只是背景，不替代支付保障。",
    ),
    # ---- S1.1.3
    Rubric(
        key="S1.1.3", ind="S1.1.3", part="", name="工作时间与休息休假保障",
        tiers={
            100: "建立明确的工作时间、加班管理及休息休假保障制度，并提供可核验的执行证据"
                 "（如工时监测、休假执行数据、覆盖情况或针对不合理工时问题的纠正成果）。",
            50: "披露工作时间与休息休假保障制度，但未披露具体实施措施（如工时记录、加班审批或"
                "休假管理程序），缺乏充分的实际执行结果或改善成效证据；或仅声明性文件。",
            0: "未识别到相关内容；或存在经核实的严重超时工作、违法剥夺休息休假权利等情况且"
               "未采取有效纠正措施。",
        },
        groups=(
            ("T1 工作时间制度", (
                "工作时间", "working hours", "标准工时", "工时",
            )),
            ("T2 加班管理制度", (
                "加班", "overtime", "加班审批", "加班上限",
            )),
            ("T3 休息休假保障", (
                "年假", "annual leave", "带薪年假", "paid annual leave", "法定假期",
                "statutory holiday", "公共假期", "public holiday", "病假", "sick leave",
                "休息日", "rest day", "未休年假",
            )),
            ("T4 具体实施措施", (
                "排班", "轮班", "shift", "考勤", "attendance", "工时记录", "time record",
                "工时监测", "休假申请", "leave application", "调休", "time off in lieu",
            )),
            ("T5 执行结果", (
                "休假使用率", "leave utilisation", "leave utilization", "leave taken",
            )),
            ("T6 问题纠正", (
                "超时", "excessive working hours",
            )),
        ),
        note="「员工依法享有年假」只说明权利；「用系统跟踪/记录休假」「设有工时系统」是手段，"
             "不等于执行结果——要有休假使用率、实际休假天数、覆盖比例，或超时问题的检查与整改结论"
             "才算执行证据。加班费支付归 S1.1.2。",
    ),
    # ---- S1.1.4
    Rubric(
        key="S1.1.4", ind="S1.1.4", part="", name="员工福利与支持",
        tiers={
            100: "已披露员工福利或家庭支持制度，并披露已经实施的对应行动，如为员工缴纳社会保险、"
                 "提供补充医疗保障、发放福利补贴、提供托育照护支持或开展员工援助服务。",
            50: "披露员工福利及工作生活支持措施，但缺乏充分的覆盖范围或实际使用证据；"
                "或仅有员工关怀、福利保障、工作生活平衡等声明性文件。",
            0: "未识别到相关内容；或存在已核实的严重福利权益侵害且未有效纠正。",
        },
        groups=(
            ("W1 法定社会保障", (
                "社会保险", "social insurance", "公积金", "housing provident fund",
                "养老保险", "pension", "医疗保险", "medical insurance", "工伤保险",
                "work injury", "失业保险", "unemployment insurance", "生育保险",
                "maternity insurance",
            )),
            ("W2 补充福利", (
                "补充医疗", "supplementary medical", "商业保险", "企业年金", "餐补",
                "meal allowance", "交通补贴", "transport allowance", "住房补贴",
            )),
            ("W3 家庭与生活支持", (
                "托育", "childcare", "产假", "maternity leave", "陪产假", "paternity leave",
                "育儿假", "parental leave", "哺乳", "lactation", "员工援助",
                "employee assistance", "EAP",
            )),
            ("W5 实际覆盖", (
                "福利覆盖率", "参保率",
            )),
            # 召回扩展：W4（制度与取得安排）与 W6（使用成果）在报告里常以泛称出现
            ("补充·召回扩展", (
                "体检", "health check", "员工福利", "employee benefit",
            )),
        ),
        note="「已为员工缴纳/投保」就是已实施行动；覆盖率只是佐证，不是满分前提。"
             "面向社区的慈善支出不计入本项。",
    ),
    # ---- S1.1.5
    Rubric(
        key="S1.1.5", ind="S1.1.5", part="", name="平等机会、反歧视与反骚扰",
        tiers={
            100: "已披露平等机会、反歧视与反骚扰相关制度，并披露已经实施的对应行动，如开展专项培训、"
                 "执行公平招聘或晋升程序、开展公平性审查、实际受理相关投诉或开展事件调查处理；"
                 "对已识别的事件提供调查、受影响人员保护、责任追究及有效纠正证据。",
            50: "已披露平等机会、反歧视与反骚扰相关制度，但未识别到具体执行行动；或仅有平等对待、"
                "反对歧视、禁止骚扰等声明性文件。",
            0: "未识别到相关内容；或存在经核实的严重、持续性歧视、骚扰或系统性机会不公平，且未有效纠正。",
        },
        groups=(
            ("D1 平等机会", (
                "平等机会", "equal opportunity", "多元化", "diversity", "包容", "inclusion",
            )),
            ("D1 正式禁止歧视", (
                "反歧视", "anti-discrimination", "non-discrimination", "不歧视", "禁止歧视",
            )),
            ("D2 正式禁止骚扰", (
                "反骚扰", "anti-harassment", "性骚扰", "sexual harassment", "职场骚扰",
                "workplace harassment", "霸凌", "bullying", "零容忍", "zero tolerance",
            )),
            ("D3/D4 公平招聘与晋升", (
                "公平晋升", "fair promotion", "晋升标准", "招聘标准",
            )),
            ("D5 针对性预防（培训）", (
                "反歧视培训", "diversity training", "harassment training",
                # 泛词：专项培训是「已实施行动」最常见的形态
                "培训", "training",
            )),
            ("D6 专项监测与审查", (
                "公平性审查", "pay equity review",
            )),
            ("D7–D10 举报、调查与追责", (
                "举报", "whistleblow", "投诉", "complaint", "grievance", "匿名",
                "anonymous", "反报复", "non-retaliation", "retaliation", "调查",
                "investigation", "纪律处分", "disciplinary", "问责", "accountability",
            )),
        ),
        note="制度存在不等于已执行；没有已识别事件时不要求个案统计。"
             "「反腐败」「商业道德」不能替代反骚扰。",
    ),
)}

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
    """
    payload = "|".join(
        f"{r.key}:{r.tiers}:{sorted(r.keywords)}:{r.note}:{r.gate}"
        for r in sorted(RUBRICS.values(), key=lambda x: x.key))
    return hashlib.sha256(f"{RUBRIC_VERSION}|{PROMPT_VERSION}|{payload}".encode("utf-8")).hexdigest()[:12]


__all__ = ["Rubric", "RUBRICS", "QUALITATIVE_PARTS", "LEVEL_WORDS",
           "RUBRIC_VERSION", "PROMPT_VERSION", "rubric_hash"]