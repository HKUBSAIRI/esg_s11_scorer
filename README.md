# esg_s11_scorer · S1.1 五项指标打分模型

> 输入一份或多份 ESG 报告 PDF，输出「每家 5 个指标分 + 每个分数的分项与证据 + 全批对标分位」。
> 纯规则、确定性、可复现：同一份 PDF 任何时候跑都是同一个分数，不调用任何模型 API。

```powershell
cd 50-应用\esg_s11_scorer
python -m esg_s11_scorer --show-config   # 先看输入/输出现在指向哪、来自哪个变量
python -m esg_s11_scorer                 # 路径写在 .env 里，命令就这么短
```

输出（默认落 `50-应用/good_sample/分析指标/打分结果/`）：

```
打分结果/
├── FY2025/
│   ├── S1.1_02269_打分报告.xlsx      # 每家一份：总览 / 分项判分 / 数据取值 / 29 要素 / 运行台账
│   └── ...
├── S1.1_批次汇总_2025.xlsx           # 汇总 / 对标分位 / 要素矩阵 / 缺口清单 / 运行台账
└── overrides_FY2025.csv              # 人工兜底值（可选）
```

---

## 〇、配置：路径写在 `.env`，不写在代码里

原因不只是「好看」：这套模型的卖点是**可复现**。路径一旦硬编码进 `config.py`，
「换一批 PDF」和「改判分规则」就会混在同一次提交里——分数变了，说不清是数据变了还是模型变了。
所以两类事实分开存：

| 类别 | 放哪 | 改动的影响 |
|---|---|---|
| **本机事实**：PDF 在哪、输出到哪、哪一年 | 环境变量 / `.env`（`settings.py`） | 只换输入输出，分数不变 |
| **模型事实**：权重、阈值、判分规则 | `config.py` 的 `SPECS` / `BANDS` | 必须 +0.1 版本号并重跑校准 |

```powershell
cd 50-应用\esg_s11_scorer
python -m esg_s11_scorer --init-env      # 由 .env.example 生成 .env（已存在则不覆盖）
python -m esg_s11_scorer --show-config   # 每个变量的生效值 / 来源 / 路径是否存在
```

`.env` 已按本仓库现状写好（5 家 FY2025 → `打分结果/`），所以日常只需要 `python -m esg_s11_scorer`。
优先级：**命令行参数 > 系统环境变量 > `.env` > 默认值**。

| 变量 | 作用 | 默认 |
|---|---|---|
| `ESG_YEAR` | 报告年度 | 2025 |
| `ESG_PDF_DIR` | 输入：某个年份目录，或单份 PDF 的完整路径 | `{ESG_REPORT_ROOT}/FY{ESG_YEAR}` |
| `ESG_PRIOR_DIR` | 上一年报告目录（取上期流失率/上期薪酬差距） | 自动取 PDF 同级 `FY{year-1}` |
| `ESG_OUT_DIR` | 输出目录 | `good_sample/分析指标/打分结果` |
| `ESG_REPORT_ROOT` | 报告根目录（其下有 `FY2025/`、`FY2024/`） | `good_sample/分析指标/ESG报告` |
| `ESG_BENCH` / `ESG_BENCH_CSV` | 对标来源与外部对标表 | `auto` / 无 |
| `ESG_OVERRIDE_CSV` | 人工兜底值表 | 无 |
| `ESG_INSIGHT_APP` / `ESG_STANDARD_DOC` | 只读依赖：要素词表仓库、标准原文 | 同级仓库路径 |
| `ESG_PROJECT_ROOT` | 项目根；**相对路径都按它解析** | 本包的上两级目录 |
| `ESG_SAMPLE_PDF_DIR` / `ESG_VERDICTS` | 校准集（`calibrate.py`） | `sample/01-源PDF` 等 |

临时换一批，不必动 `.env`：

```powershell
python -m esg_s11_scorer --pdf "D:/另一批/FY2025" --year 2026 --out "D:/结果"   # 只本次
$env:ESG_PDF_DIR = "D:/另一批/FY2025"; python -m esg_s11_scorer                # 本窗口内
python -m esg_s11_scorer --env-file .env.2026                                  # 换整套配置
```

`.env` 不进版本库（`.gitignore` 已排除），模板 [`.env.example`](.env.example) 是唯一信源，
`tests/test_settings.py` 会断言「模板里没漏任何变量」。配错的路径由 `--show-config` 直接点名。

---

## 一、当前结果（5 家生物制药 FY2025）

| 代码 | 公司 | S1.1.1 | S1.1.2 | S1.1.3 | S1.1.4 | S1.1.5 | 加权（严格） | 加权（归一） |
|---|---|---|---|---|---|---|---|---|
| 02269 | 药明生物 | 67.5 | 80.0 | 50 | 50 | 50 | 63.4 | 63.4 |
| 01513 | 丽珠医药 | 85.0 | 15.0 | 50 | 50 | 50 | 48.2 | 58.8 |
| 03692 | 翰森制药 | 25.0 | 35.0 | 50 | 50 | 100 | 46.8 | 51.2 |
| 01801 | 信达生物 | 42.5 | 15.0 | 50 | 100 | 50 | 45.1 | 55.6 |
| 01177 | 中国生物制药 | 30.0 | 15.0 | 50 | 50 | 50 | 34.5 | 45.0 |

A/B 为批内分位（n=5，同批同行业互为对标池）。药明生物的五项分与
[`分析指标/药明生物/S1.1_02269_指标分析.md`](../good_sample/分析指标/药明生物/S1.1_02269_指标分析.md)
的人工分析**完全一致**（该文按严格口径 45/80/50/50/50；本模型的 67.5 是「批内分位」口径，
A/B 由 N/A 变为 75/75）。

---

## 二、架构：七段管道，每段产物落地

```
PDF ──① parse ── 页级文本（复用 ESG_insight/app/s11_text）
        │                 ↓ build/FY{year}/{code}/parse.json
        ├─② facts ── 数值抽取（表头年份对齐）+ 人工兜底
        │                 ↓ facts.json  ← 流失率/上期/薪酬差距/离职人数/员工数/集体谈判
        ├─③ elements ── 29 要素词法判定 + 7 条增强补丁
        │                 ↓ elements.json
        ├─④ prior ── 上期值三级回退（读**上一年已落地的 facts**，不再解析 FY-1 的 PDF）
        │                 ↓ prior.json
        ├─⑤ bench ── 对标分位（批内 / 外接 CSV / 关闭）        ← 全批屏障，单份算不了
        │                 ↓ 快照 id
        ├─⑥ score ── 0.4A+0.3B+0.3C 与三档制（纯算术，无模型）
        │                 ↓ score.json
        └─⑦ report ── 先 publish 成 gold 表，再由表渲染 xlsx
```

**每段都能单独重跑**，判据是产物里的指纹（见第七节）：改阈值只重算 score，改抽取正则
只重算 facts 之后，换一份 PDF 才回到 parse。首次跑 5 家约 1 分钟，第二次同一批约 2 秒。

**报告只渲染、不计算**：分数、分位、档位在 `score` 阶段就写进 gold 表，`report.py`
拿到的是只读视图。于是「表里的数」和「报告里的数」不可能各说一套。

**复用而非重写**：解析与 29 要素词表来自 `50-应用/ESG_insight/app`（只读引用，不改该仓库），
本包只新增「数值抽取、对标分位、打分编排、产物落地、报告」。好处是词表只维护一处；
代价是本包依赖该目录存在——换了位置就设 `ESG_INSIGHT_APP`（见第〇节），不用改代码。

```powershell
python -m esg_s11_scorer --dry-run        # 只算分不写报告（中间产物照常落地）
python -m esg_s11_scorer --force          # 忽略已有产物，全段重算
python -m esg_s11_scorer --build-dir D:\cache\esg   # 换一个中间态目录
```

---

## 三、口径约定（重要，别只看一个数）

### 1. N/A ≠ 0

分项不可评时记 `None`，报告同时给两个数：

| 数 | 含义 | 用在哪 |
|---|---|---|
| 得分（严格） | 标准公式原样算，N/A 按 0 计入 | 与历史 10 样本口径可比 |
| 得分（归一） | 只按「可观测权重」折算成百分制 | 判断这家在这一项上表现如何 |
| 可达上限 | 可观测权重 ×100 | 提醒「这个分数最多只能到几」 |

例：某家没披露性别薪酬差距 → S1.1.2 的 A（40%）与 B（30%）都 N/A，可达上限 30 分；
严格分 15、归一 50。写「15 分」会被误读成「薪酬保障很差」，写「50（可达 30 分上限）」才准确。

五项权重合计 100%（`25+30+15+15+15`），所以「严格」与「归一」**都是百分制**，
差的只是 N/A 怎么算——满披露时两列数值相同。口径的唯一信源是 `config.SPECS`，
下面第 4 小节那张表由 `tests/test_docs.py` 与代码逐格比对。

### 2. A/B 是相对量，必须有对标池

| `--bench` | 行为 | 适用 |
|---|---|---|
| `auto`（默认） | 用本批报告互为对标（n≥3 才算） | 一次跑同行业多份，n=5 起可参考 |
| `csv` | 读外部对标表 `code,year,turnover,prior_turnover` | 已建行业库，或想固定口径 |
| `none` | A 记 N/A；B 退化为「改善率为正即 100」 | 单份报告、与 10 样本历史口径对齐 |

分位定义：流失率升序排名（越低越好）、改善率降序排名（越高越好），并列取平均位次；
≤20%→100、≤40%→75、≤60%→50、≤80%→25、其余 0。

### 3. 上期值的三级回退

1. 本报告附录表里有上一年度列 → 直接用；
2. 没有 → 去 `--prior-dir`（默认同级 `FY{year-1}`）找同代码报告，取其报告年度值；
3. 仍没有 → B 记 N/A，**不拿行业均值冒充上期**。

抽不到但确实有值时，用 `--override` 的 CSV 手工补（记 `source=override`，报告里标明）。
本批就有一条：03692 的 FY2024 附表是转置排版（标签列与数值列分离），自动抽取不适用。

### 4. 权重与阈值速查（唯一信源 = `config.SPECS` / `config.BANDS`）

| 指标 | 名称 | 权重 | 结构 | 量化口径 / 档位入口 |
|---|---|---|---|---|
| S1.1.1 | 雇佣关系与就业保障 | 25 | A 40% + B 30% + C 30% | 流失率分位（低者优）；改善率 = -(本期-上期)/上期×100%；C 要合同制度 + 终止保障 + 自愿离职构成 |
| S1.1.2 | 薪酬保障 | 30 | A 40% + B 30% + C 30% | 差距绝对值 ≤3%→100、≤15%→50；改善率 ≥5% **或**本期差距 ≤3% → B=100（上期差距为 0 时改看本期是否 ≤2%） |
| S1.1.3 | 工作时间与休息休假保障 | 15 | 三档 100/50/0 | 100 分档唯一入口＝休假执行数据（S1.1.3-5） |
| S1.1.4 | 员工福利与支持 | 15 | 三档 100/50/0 | 100 分档唯一入口＝福利覆盖/使用数据（S1.1.4-5） |
| S1.1.5 | 平等机会、反歧视与反骚扰 | 15 | 三档 100/50/0 | 100 分档＝反骚扰机制（-4）+ 事件统计与处置（-6） |

改任何一个数字都要 +0.1 `MODEL_VERSION`：指纹只看源码，版本号是人读的「这次改了口径」标记。

---

## 四、校准与测试：模型 vs 两套人工答案

```powershell
cd 50-应用
python esg_s11_scorer/calibrate.py                        # 产出 reports/calibration.md / .json
python -m unittest discover -s esg_s11_scorer/tests -t .  # 80 条回归测试（约 2.5 分钟）
```

校准集路径同样走配置（`ESG_SAMPLE_PDF_DIR`、`ESG_VERDICTS`），样本换了不用改脚本。

| 校准集 | 要素一致率 | 指标分（偏差≤5 分） |
|---|---|---|
| 10 份样本 × 29 要素（专家裁定 290 格） | **221/290 = 76.2%** | S1.1.1 8/10、S1.1.2 7/10、S1.1.3 8/10、S1.1.4 10/10、S1.1.5 5/10 |
| 药明生物（本组手工 29 要素 + 五项分） | **26/29** | **5/5 完全一致** |

差异集中在两处，且方向可解释：

- **要素级「是→部分」29 格**：词法层要求「同句有制度名词＋管理动作」，专家接受跨句合并。
  方向是保守的（不会虚高分数）。
- **S1.1.5 的 5/10**：专家分含「具名制度 vs 笼统声明」的人工判断，29 要素矩阵推不出来
  （原始 10 样本工作簿对该项也是硬编码名单）。模型用明规则替代，长篇报告准确，
  极短章节偏宽松——这是本模型最弱的一项，已在报告里标注。

**回归测试锁什么**（`tests/`，80 条）：

| 文件 | 锁定的内容 |
|---|---|
| `test_regression.py` | 5 家 FY2025 的 5 项分逐格一致；药明 29 要素与 3 处**已登记**分歧；10 样本一致率 ≥75% |
| `test_extract.py` | 5 家的流失率/上期/差距绝对值；药明四列年份对齐；改善率算子 |
| `test_pipeline.py` | 批跑成功、`ScoreData` 类型、上期三级回退（含 0.0 不得当缺失）、分位样本量 |
| `test_judge.py` | 7 条补丁各自生效；增强层不得就地改上游判定对象 |
| `test_report.py` | 工作簿可写出、单元格是真数值、三档制「档位」行存在、分位值正确；报告里的每一家都能在 gold 表查到 |
| `test_settings.py` | `.env` 解析（引号/注释/反斜杠/BOM）、优先级、相对路径、坏值回退、模板不漏变量 |
| `test_store.py` | 产物增删读、损坏即报错、表列类型往返（bool/float/None/中文）、整表覆盖写幂等 |
| `test_stages.py` | 序列化不丢字段（含 `best` 指回人工值）、三级回退与出处、**缓存只在该重算时重算**、表里的分==卡里的分 |

---

## 五、加一个新指标（S1.2 / S2.x …）

1. 在 `config.SPECS` 追加一条规格：指标号、名称、权重、`kind`（weighted/tiered）、`scorer` 名。
2. 在 `scoring.py` 写同名判分函数（入参是要素判定 + 数值，输出 `IndScore`）。
3. 若要新的量化字段，在 `extract.FIELD_LABELS` 加标签词 + `FIELD_BOUNDS` 加取值域。
4. 新增要素词表进 `s11_elements.py`（ESG_insight 仓库，需走该仓库的分支 + PR 流程）。
5. 改完把 `config.MODEL_VERSION` +0.1，并重跑 `calibrate.py`。

规格里声明的 `scorer` 若没实现，`scoring.py` 导入时直接报错（不会静默出 0 分）。

---

## 六、目录与文件

```
esg_s11_scorer/               ← 独立 git 仓库（2026-11-03 初始化）
├── pyproject.toml            依赖与包元数据（pypdf / openpyxl）
├── requirements.txt
├── settings.py   ★本机配置    .env / 环境变量 → 生效路径（带来源追踪，可 --show-config）
├── .env.example              配置模板（入库）；.env 由 --init-env 生成，不入库
├── models.py     ★领域模型    CompanyRun / NumFact / Extraction / Component / IndScore / ScoreCard / ScoreData / Patch / PeerRow
│                              + 可落地的 ElementJudgement / 只读的 CompanyView / Batch
├── metrics.py    ★口径算子    改善率唯一实现（含 fmt_pct）
├── config.py                 版本号、阈值、指标规格（数据化）+ 与 s11_elements 的一致性校验
├── extract.py                数值抽取：表头年份对齐、句式兜底、置信度、交叉校验
├── judge_plus.py             29 要素判定增强：7 条补丁
├── bench.py                  批内分位 / 外接对标表 / 导出对标库
├── scoring.py                五项指标算分（N/A、可达上限、归一）+ build_card 装配
├── stages.py     ★阶段计算    六段各自的纯函数：parse/facts/elements/prior/score（不碰文件）
├── store.py      ★落地层      阶段产物 JSON + gold 表（parquet/JSONL）+ 指纹
├── tables.py     ★表契约      facts/elements/indicators/components/companies/bench_* 的字段与装配
├── pipeline.py   ★编排层      七段 DAG、缓存复用、跨年读表、发布
├── report.py                 每家工作簿 + 批次汇总（只读表，不计算）
├── cli.py / __main__.py      命令行（--show-config / --init-env / --env-file / --force / --dry-run）
├── calibrate.py              回测与校准（→ reports/）
├── tests/                    80 条 unittest（见第四节）
├── build/                    管道中间态（.gitignore，可随时删掉重算）
└── reports/                  校准产物（.gitignore，可随时重生成）
```

依赖方向（无环）：

```
settings → config → { extract, judge_plus, bench, scoring } → models
                                ↓
                     stages → pipeline → { tables, store } → report
```

`models` 不 import 本包任何模块；`store` 只依赖标准库；`stages` 不碰文件；
`tables` 只认 `store` + `models`；`report` 只认 `models`（不 import pipeline，也不 import bench）。
外部类型 `Doc` / `ElementResult` 只出现在 `stages` 与 `TYPE_CHECKING` 里——
下游读产物时不再需要 `s11_judge`，历史数据不会被上游实现变更绑死。

---

## 七、管道：产物、指纹、怎么查数

### 1. `build/` 里有什么

```
build/
├── FY2025/                      ← 分区键 = (year, code)
│   ├── _meta.json               ← 本年批次：模型版本、对标快照、家数
│   ├── 02269/
│   │   ├── parse.json           ← 页级文本（最贵的一步，全生命周期只算一次）
│   │   ├── facts.json           ← 抽到的数值 + 人工兜底（含 best 择优结果）
│   │   ├── elements.json        ← 29 要素判定 + 生效补丁 + 自愿离职口径
│   │   ├── prior.json           ← 上期值从哪来（本报告附录 / 上一年报告 / 人工）
│   │   └── score.json           ← 记分卡（公司行 + 指标行 + 分项行）
│   └── ...
├── FY2024/                      ← 只会有 parse/facts：上一年只需被读，不需出分
└── tables/FY2025/               ← gold 表（parquet；没装 pyarrow 时自动落 JSONL）
    ├── facts / elements / patches       证据层：一行一个数值/判定/补丁
    ├── indicators / components / companies   结果层：分项与总分（含口径与快照 id）
    └── bench_rows / bench_meta          口径层：这一年拿谁比的、n 是多少
```

每个产物都是带信封的 JSON：`{"stage","code","year","key","producer","produced_at","payload"}`。
信封把「能不能复用」和「产出内容」分开——查指纹不用解析 payload。

### 2. 指纹：为什么不重算，或为什么必须重算

| 阶段 | key 由什么决定 | 什么变了会触发重算 |
|---|---|---|
| parse | PDF 字节 + `PARSE_VERSION` + 解析源码 | 换了 PDF、改了 `s11_text`/`stages` |
| facts | parse 的 key + `EXTRACT_VERSION` + 抽取源码 + override 表内容 | 改了正则、改了人工兜底表 |
| elements | parse 的 key + `RULESET_VERSION` + `JUDGE_VERSION` + 增强层源码 | 词表/判定/补丁改了 |
| score | facts+elements+prior 的 key + 对标快照 + `MODEL_VERSION` + 打分源码 | 改阈值、改权重、对标池变了 |

**源码指纹是自动的**（`store.source_digest`）：改了 `extract.py` 就一定会重算 facts，
不需要谁记得升版本号。手写的 `*_VERSION` 只表达「格式/口径变了」这层人读意义。
→ 所以「跑了 2 秒」不代表「没生效」，而是「这次真的没有东西需要改」。

### 3. 查数：不用打开 Excel

```python
import pandas as pd
base = "esg_s11_scorer/build/tables/FY2025"

cos = pd.read_parquet(f"{base}/companies.parquet")     # 一家一行：总分、分位、上期值出处
ind = pd.read_parquet(f"{base}/indicators.parquet")    # 一家一指标一行：严格/归一/可达上限/档位
ind.pivot(index="code", columns="ind", values="strict")   # 就是报告里那张分数表

els = pd.read_parquet(f"{base}/elements.parquet")      # 29 要素 × N 家，带证据句
els.query("ind == 'S1.1.1' and verdict == '否'")[["code", "eid", "reason"]]
```

### 4. 对标快照：批内分位让分数「不独立」

A/B 是相对量，**加进第 6 家公司会改变前 5 家的 A/B 分**。这不是 bug，是批内分位的定义。
改造后这件事是**可见**的：`bench_meta.snapshot` + `companies.bench_snapshot` 记下当次
用了哪个池子，快照一变，全部 score 就重算并在台账里写出新 id。

要「补一家不影响老分数」，就得先定死池子：把当次 `bench_rows.parquet` 导出成行业库，
之后一律用 `--bench csv` 跑（见第五节 `bench.export_rows()`）。这是口径决策，不是代码决策。

---

## 八、已知限制

1. **只覆盖 S1.1**。其他议题（S1.2 职业健康、S2 供应链、E 系列）尚无指标标准文档，
   架构已预留（见第五节），但没有标准就不能凭空造判据。
2. **文本层依赖**。扫描件、图片化表格、纯图 KPI 卡抽不到；`overrides.csv` 是兜底口。
3. **批内分位 ≠ 全市场分位**。n=5 只能做提示性判断；真要行业评级需按
   `bench.export_rows()` 累积成行业库，再用 `--bench csv` 跑。
4. **S1.1.5 偏宽松**（见第四节）。
5. **pyarrow 可选**。装了写 parquet（带列类型、可被 DuckDB/pandas 直读），没装自动落
   JSONL（同样的行与列，只是没有类型与压缩）。两者 schema 一致，代码不用改。
6. **产物会随代码失效**。这是设计而不是缺陷：改了源码，旧 `build/` 就不再被复用。
   想省时间可以接受它重算一遍，想确定「跑的是当前代码」就加 `--force` 清空重来。