# esg_s11_scorer · S1.1 五项指标打分模型

> 输入一份或多份 ESG 报告 PDF，输出「每家 5 个指标分 + 每个分数的分项与证据 + 全批对标分位」。
> 定量分档（A/B）是纯算术，可复现；定性五处（C 项与 S1.1.3/4/5）由 LLM 盲评判档，档位即分（100/50/0），
> 无 key 时退离线关键词兜底。判档结论按指纹缓存，同一份报告重跑不会重复调模型。

```powershell
cd 50-应用                       # 工作目录必须是包目录的上一级；在包目录里跑会 ModuleNotFoundError
python -m esg_s11_scorer --show-config   # 先看输入/输出现在指向哪、来自哪个变量
python -m esg_s11_scorer                 # 默认只出批次汇总（不出单家报告）
python -m esg_s11_scorer --company-reports   # 需要单家工作簿时才加这个开关
```

解释器要用装了依赖的那一个（本机是 `C:\Users\Alpha\AppData\Local\Programs\Python\Python314\python.exe`，
pypdf / openpyxl / pyarrow 只装在这里）。`python` 若指向 3.10/3.11/3.13，第一条命令就 `ModuleNotFoundError`。

输出（默认落 `50-应用/good_sample/分析指标/打分结果/`）：

```
打分结果/
├── S1.1_批次汇总_2025.xlsx           # 默认就这一本：汇总 / 对标分位 / 分项明细 / 运行台账
├── FY2025/                           # 图册每次跑都写；单家报告加 --company-reports 才有
│   ├── S1.1_02269_打分报告.xlsx      # 每家一份：总览 / 分项明细 / 数据取值 / 运行台账
│   ├── S1.1_得分构成_2025.xlsx       # 得分构成图册：五项贡献、折合综合分、堆叠图
│   └── S1.1_定性判档_2025.xlsx       # 判档图册：档位矩阵、逐条明细、档位柱图
└── overrides_FY2025.csv              # 人工兜底值（可选）
```

单家报告是按需产物：要看某一家时 `--company-reports` 重画一遍（读 gold 表，不重算分数，几秒）。
批次汇总里已有「每家 × 5 个指标」的分项明细，日常评审不必逐家开文件。

> **默认命令会覆盖交付物，先看清楚再跑。** 当前 `ESG_PDF_DIR` 指向的 FY2025 有 **20 份 PDF**，
> 而 `打分结果/` 里的汇总与单家报告还是 **5 家交付版**（2026-11-09）。直接跑默认命令会拿 20 家结果
> 冲掉那 5 家交付物。只想验证代码能不能跑：加 `--dry-run`，或把 `--out` 指到临时目录。

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
cd 50-应用                       # 同样是包目录的上一级
python -m esg_s11_scorer --init-env      # 由 .env.example 生成 .env（已存在则不覆盖）
python -m esg_s11_scorer --show-config   # 每个变量的生效值 / 来源 / 路径是否存在
```

`.env` 已按本仓库现状写好（FY2025 目录 20 份 PDF、上期 FY2024 5 份 → `打分结果/`），
日常只需要 `python -m esg_s11_scorer`——但先看开头那条提醒：默认批现在是 20 份，
会覆盖 `打分结果/` 里的 5 家交付物。
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
| `ESG_INSIGHT_APP` / `ESG_STANDARD_DOC` | 只读依赖：文本解析模块、标准原文 | 同级仓库路径 |
| `ESG_PROJECT_ROOT` | 项目根；**相对路径都按它解析** | 本包的上两级目录 |
| `ESG_SAMPLE_PDF_DIR` / `ESG_VERDICTS` | 校准集（`calibrate.py`） | `sample/01-源PDF` 等 |
| `ESG_LLM_*` | 定性判档的 OpenAI 兼容端点 / 模型 / 密钥 | 无（缺则退回关键词兜底） |
| `ESG_DB_*` | MySQL 连接（**备用**，打分链路不读库） | 无 |
| `ESG_GPU_*` | 深圳中心 GPU 服务器账号（**备用**，本包不登服务器） | 无 |

`ESG_DB_*` / `ESG_GPU_*` 两组来自 `01-配置与环境/配置.md`，只是把同一份凭据收进配置层：
以后加「分数入库」「从服务器拉语料」的阶段时，用 `settings.db_params()`（直接喂
`pymysql.connect(**params)`）和 `settings.gpu_account("zixin")` 取值，别再在脚本里硬编码密码。
`--show-config` 里密码只显示位数掩码；`tests/test_settings.py` 会校验半配（缺库名、缺 host）会出警告。

### I/O：输入、输出、中间态各归各位

| 角色 | 变量 | 当前落点（本机） | 说明 |
|---|---|---|---|
| 输入 | `ESG_PDF_DIR` | `good_sample/分析指标/ESG报告/FY{year}` | 年份目录，或单份 PDF 的完整路径 |
| 上期 | `ESG_PRIOR_DIR` | `…/ESG报告/FY{year-1}` | 只用其中同代码报告的上期事实 |
| 输出 | `ESG_OUT_DIR` | `good_sample/分析指标/打分结果` | 其下 `FY{year}/` 放每家报告与两本图册；批次汇总落在本层 |
| 中间态 | `ESG_BUILD_DIR` | `esg_s11_scorer/build` | 阶段产物 + gold 表，删了重算即可（不进版本库） |
| 人工兜底 | `ESG_OVERRIDE_CSV` | `打分结果/overrides_FY{year}.csv` | 今年没有该文件不算错，只是不读 |
| 盲评别名 | `ESG_ALIAS_CSV` | `打分结果/aliases_FY{year}.csv` | 同上（本项目尚未启用） |

两个规矩：

1. **路径里的 `{year}` / `{year-1}` 会按 `ESG_YEAR` 展开**。换年度只改 `ESG_YEAR` 一行，
   输入目录、上期目录、兜底表文件名一起跟随；把 `FY2025` 写死会**静默串档**
   （今年批次套用去年的人工值，报告看着正常、数是错的）。
2. **年度与目录名对不上会报警**：`--show-config` 末尾打印输入/上期/输出/中间态四行，
   若 `ESG_YEAR=2026` 而路径仍是 `FY2025`，直接提示「疑似串档」。

加一个新年度（例如 FY2026）：把 PDF 放进 `ESG报告/FY2026/`（上一年的留在 `FY2025/`），
`.env` 里把 `ESG_YEAR` 改成 `2026`，跑 `--show-config` 确认四行都指向 2026 再执行。

临时换一批，不必动 `.env`：

```powershell
python -m esg_s11_scorer --pdf "D:/另一批/FY2025" --year 2026 --out "D:/结果"   # 只本次
$env:ESG_PDF_DIR = "D:/另一批/FY2025"; python -m esg_s11_scorer                # 本窗口内
python -m esg_s11_scorer --env-file .env.2026                                  # 换整套配置
```

`.env` 不进版本库（`.gitignore` 已排除），模板 [`.env.example`](.env.example) 是唯一信源，
`tests/test_settings.py` 会断言「模板里没漏任何变量」。配错的路径由 `--show-config` 直接点名。

`.env` 里的 `ESG_LLM_API_KEY`、`ESG_DB_PASSWORD`、`ESG_GPU_*_PASSWORD` 是真的凭据：
不写进提交信息 / 文档 / 报告 / 测试夹具，也不贴到聊天里。要展示配置就用 `--show-config`，
它把密钥显示成位数掩码（如「已配置（sk-ws-…共 117 位）」）。

---

## 一、交付结果（5 家生物制药 FY2025，LLM 判档口径）

先说清楚现在的家数：`ESG报告/FY2025/` 现有 **20 份 PDF**，全批已跑通（20 家都 `status=ok`，
gold 表里也是 20 家）；但**交付物仍是 5 家版本**——`打分结果/S1.1_批次汇总_2025.xlsx` 与
`FY2025/` 下的单家报告都是 2026-11-09 那次 5 家交付的产物。下表照抄这 5 家；
20 家的分数不在 README 复述（会随重跑、判档抖动、对标池变化，以 workbook 与 gold 表为准）。

| 代码 | 公司 | S1.1.1 | S1.1.2 | S1.1.3 | S1.1.4 | S1.1.5 | 加权（严格） | 加权（归一） |
|---|---|---|---|---|---|---|---|---|
| 02269 | 药明生物 | 67.5 | 65.0 | 50 | 100 | 100 | 73.9 | 73.9 |
| 01513 | 丽珠医药 | 85.0 | 15.0 | 50 | 100 | 100 | 63.2 | 73.8 |
| 03692 | 翰森制药 | 25.0 | 35.0 | 50 | 100 | 100 | 54.2 | 58.8 |
| 01801 | 信达生物 | 42.5 | 15.0 | 50 | 100 | 100 | 52.6 | 63.1 |
| 01177 | 中国生物制药 | 30.0 | 15.0 | 50 | 100 | 100 | 49.5 | 60.0 |

口径：`--qualitative llm`（2026-11-09 重跑，与 11-04 交付物 4/5 家逐格一致）。
A/B 为批内分位（交付批次 n=5，同批同行业互为对标池）；C 项与 S1.1.3/4/5 取 LLM 判档结论。

全批 20 家一起跑时，对标池是 **n=16**（另外 4 家没披露本期流失率，A 项记 N/A，也不进分位池），
所以同一家公司的 A/B 分会与 5 家批次不同。这是批内分位的定义，不是 bug（见第七节第 4 小节）。

两件必须知道的事：

1. **LLM 判档不保证逐比特复现**。同一份报告重跑，个别档位会变（11-09 复跑时 03692 的
   S1.1.5 由 50 变 100，其余 4 家完全一致）。要「同一份报告任何时候同分」，只有靠
   `build/` 里的产物缓存命中；重算就必须接受这个抖动。
2. **回归测试锁的是 keyword 口径**（离线、确定性），不是本表。`tests/test_regression.py`
   里的 `EXPECT_BATCH` 是 `--qualitative keyword` 的结果；本表是 llm 口径，两者本就不同。

---

## 二、架构：六段管道 + 一次渲染，每段产物落地

```
PDF ──① parse ── 页级文本（复用 ESG_insight/app/s11_text）
        │                 ↓ build/FY{year}/{code}/parse.json
        ├─② facts ── 数值抽取（表头年份对齐）+ 人工兜底
        │                 ↓ facts.json  ← 流失率/上期/薪酬差距/离职人数/员工数/集体谈判
        ├─③ prior ── 上期值三级回退（读**上一年已落地的 facts**，不再解析 FY-1 的 PDF）
        │                 ↓ prior.json
        ├─④ qualitative ── 定性判档（LLM 盲评 / 关键词兜底）
        │                 ↓ qualitative-<模式>.json ← S1.1.1-C、S1.1.2-C、S1.1.3/4/5 的 100/50/0
        ├─⑤ bench ── 对标分位（批内 / 外接 CSV / 关闭）        ← 全批屏障，单份算不了
        │                 ↓ 快照 id
        ├─⑥ score ── 0.4A+0.3B+0.3C 与三档制（纯算术；输入只有 facts 与判档结论）
        │                 ↓ score.json
        └─⑦ report ── 先 publish 成 gold 表，再由表渲染 xlsx
```

**每段都能单独重跑**，判据是产物里的指纹（见第七节）：改阈值只重算 score，改抽取正则
只重算 facts 之后，换一份 PDF 才回到 parse。首次跑整批的主要开销在 parse（20 份 PDF 逐页抽文本），
耗时随份数近似线性增长；第二次同一批是缓存命中，秒级返回。

**报告只渲染、不计算**：分数、分位、档位在 `score` 阶段就写进 gold 表，`report.py`
拿到的是只读视图。于是「表里的数」和「报告里的数」不可能各说一套——前提是两边读的是同一次发布
的同一张表；表被新批次覆盖后，磁盘上的旧 workbook 不会自动跟着变，这也是第一节要说明家数的原因。

**复用而非重写**：文本解析来自 `50-应用/ESG_insight/app/s11_text.py`（只读引用，不改该仓库），
本包只新增「数值抽取、定性判档、对标分位、打分编排、产物落地、报告」。好处是同一种 PDF 解析
只有一处实现；代价是本包依赖该目录存在——换了位置就设 `ESG_INSIGHT_APP`（见第〇节），不用改代码。

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
| `auto`（默认） | 用本批报告互为对标（n≥3 才算） | 一次跑同行业多份；n 越大越有意义（当前全批 n=16） |
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

全批 20 家的 `prior_source` 分布（可查 `companies.parquet`）：本报告附录 **7** 家、
FY2024 产物 **1** 家、override **1** 家、缺失 **11** 家。缺失多不是实现问题——多数报告的
上一年度列只出现在叙述里，没有可对齐的表；这时 B 项记 N/A，不做猜测。

另外要注意家数：`ESG报告/FY2024/` 现有 **5 份** PDF，所以「第 2 级回退」只可能命中那 5 家；
其余公司要么靠报告附录，要么靠 override 表。

### 4. 权重与阈值速查（唯一信源 = `config.SPECS` / `config.BANDS`）

| 指标 | 名称 | 权重 | 结构 | 量化口径 / 档位入口 |
|---|---|---|---|---|
| S1.1.1 | 雇佣关系与就业保障 | 25 | A 40% + B 30% + C 30% | 流失率分位（低者优）；改善率 = -(本期-上期)/上期×100%；C 取判档（合同制度 + 终止保障 + 自愿离职构成） |
| S1.1.2 | 薪酬保障 | 30 | A 40% + B 30% + C 30% | 差距绝对值 ≤3%→100、≤15%→50；改善率 ≥5% **或**本期差距 ≤3% → B=100（上期差距为 0 时改看本期是否 ≤2%）；C 取判档（薪酬制度 + 审查/纠正成效） |
| S1.1.3 | 工作时间与休息休假保障 | 15 | 三档 100/50/0 | 档位来自判档：100＝制度 + 休假执行数据 |
| S1.1.4 | 员工福利与支持 | 15 | 三档 100/50/0 | 档位来自判档：100＝福利覆盖/使用证据 |
| S1.1.5 | 平等机会、反歧视与反骚扰 | 15 | 三档 100/50/0 | 档位来自判档：100＝反骚扰机制 + 事件处置证据 |

改任何一个数字都要 +0.1 `MODEL_VERSION`：指纹只看源码，版本号是人读的「这次改了口径」标记。
当前口径版本是 `s11-model-1.3`（交付 workbook 的「运行台账」页也会写出它）。

### 5. 定性判档只有两条路：`llm` 与 `keyword`

| `--qualitative` | 行为 | 用途 |
|---|---|---|
| `llm`（默认） | 盲评（送模型前脱敏公司名）→ 档位 100/50/0 + 理由 + 依据原文 | 正式出分 |
| `keyword` | 词表召回 + 证据层级规则，离线、确定性 | 无 key 时的兜底；回归基线 |

**判档结论直接是分**：C 项（30%）与 S1.1.3/4/5 取 100/50/0，没有任何词法拼装层。
缺判档就抛错，不会静默拼一个「看着正常、口径却过期」的数。

---

## 四、校准与测试：模型 vs 两套人工答案

```powershell
cd 50-应用
python esg_s11_scorer/calibrate.py            # 默认 llm 判档；产出 reports/calibration.md / .json
python esg_s11_scorer/calibrate.py keyword    # 离线兜底口径（快、确定性、可当基线）
python -m unittest discover -s esg_s11_scorer/tests -t .  # 189 条回归测试（约 55 秒）
```

**当前状态：189 条里 8 条是红的，且情有可原。** 这 8 条都把「本批 5 家」写死在断言里
（`EXPECT_BATCH`、期望公司代码列表），而 `ESG报告/FY2025/` 现有 20 份 PDF，批跑时家数和对标池都变了：

| 文件 | 失败用例 | 原因 |
|---|---|---|
| `test_pipeline.py` | `test_batch_ok`、`test_bench_auto` | 家数 20≠5；分位随样本量变 |
| `test_regression.py` | `test_all_parsed`、`test_indicator_scores`、`test_bench_percentiles`、`test_numeric_extraction` | `EXPECT_BATCH` 是 5 家的分 |
| `test_report.py` | `test_batch_workbook`、`test_batch_detail_covers_every_company` | 明细表按 5 个代码断言 |

这些期望值是人工核证过的**口径锚点**，不要为了让测试变绿去改它们（口径真变了，是「先改断言并说明为什么」）。
要让它们绿，把 `ESG_PDF_DIR` / `REPORT_DIR` 指向只含那 5 家（01177/01513/01801/02269/03692）的目录即可。
另外：整套 189 条约 55 秒，但批跑用例对着 20 份 PDF 会跑到 8 分钟量级——除非必要，别随手跑整套。

校准集路径同样走配置（`ESG_SAMPLE_PDF_DIR`、`ESG_VERDICTS`），样本换了不用改脚本。

下表是 **`keyword` 口径**（离线、确定性、可复现，能当基线）：

| 校准集 | 指标分（偏差≤5 分即算一致） |
|---|---|
| 10 份样本（专家裁定五项分） | S1.1.1 **3/10**、S1.1.2 **5/10**、S1.1.3 **9/10**、S1.1.4 **5/10**、S1.1.5 **3/10** |
| 药明生物（本组手工五项分） | 模型 (45/65/50/50/50) vs 手工 (45/80/50/50/50)，最大偏差 15 分 |

只比指标分，不比「逐格判定」：本包已不含词法判定层，没有比这更细的可比粒度。
`llm` 口径要实跑才有数（`python esg_s11_scorer/calibrate.py`），且同一批重跑会有抖动，
不适合当基线。

差异集中在三处，且方向可解释：

- **短章节的 A/B 记 N/A**：这 10 份多是极短 ESG 章节，没有上期流失率列，B 项按 N/A 处理
  而不是猜一个值——严格分因此偏低（S1.1.1 3/10 主要来自这里）。
- **S1.1.4 的 5/10**：样本多为「有福利声明、无覆盖/使用数据」，判档容易读成 100 分档，
  专家给 50。这是当前判档规则最需要收紧的一处（`rubric.py` 的 S1.1.4 档位描述）。
- **S1.1.5 的 3/10**：专家分含「具名制度 vs 笼统声明」的人工判断，判档在长篇报告上准确，
  极短章节偏宽松。

**回归测试锁什么**（`tests/`，189 条）：

| 文件 | 锁定的内容 |
|---|---|
| `test_regression.py` | 5 家 FY2025 的 5 项分逐格一致；药明五项分与手工分差 ≤15 分 |
| `test_extract.py` | 5 家的流失率/上期/差距绝对值；药明四列年份对齐；改善率算子 |
| `test_pipeline.py` | 批跑成功、`ScoreData` 类型、上期三级回退（含 0.0 不得当缺失）、分位样本量 |
| `test_scoring.py` | 流失率 0% 当最好、两个薪酬阈值（3% 与 2%）不混用、C 项只认判档 |
| `test_report.py` | 工作簿可写出、单元格是真数值、三档制「档位」行存在、分位值正确；报告里的每一家都能在 gold 表查到 |
| `test_scoreviz.py` | 得分构成图册算术自洽：分项折合分之和 == 指标贡献之和 == 加权总分 |
| `test_qualviz.py` | 判档图册结构、热图格式、图表存在、口径可追溯 |
| `test_settings.py` | `.env` 解析（引号/注释/反斜杠/BOM）、优先级、相对路径、坏值回退、模板不漏变量 |
| `test_store.py` | 产物增删读、损坏即报错、表列类型往返（bool/float/None/中文）、整表覆盖写幂等 |
| `test_stages.py` | 序列化不丢字段（含 `best` 指回人工值）、三级回退与出处、**缓存只在该重算时重算**、表里的分==卡里的分 |
| `test_wiring.py` | 判档结论原样变分（0 与 100 也一样）、缺判档抛错、判档来源不混批 |
| `test_qualitative.py` | 召回不夹带判据外段落、关键词兜底档位、档位约束（不联网） |
| `test_blind.py` | 盲评脱敏：公司名不得出现在送模型的文本里（别名替换彻底、不误伤正文、有残留即拒发） |
| `test_keywords_io.py` | 词表 CSV 往返不改组序/词序、缺列或未知指标带行号报错、包内词表能还原 rubric |
| `test_matching.py` | 英文按词首、中文按子串（`wage` 不命中 `sewage`、`diversity` 不命中 `biodiversity`）、排序规则 |
| `test_keyword_table.py` | 关键词清单两列，按顿号切开的词与 rubric 完全一致（少一个词就算错） |
| `test_docs.py` | README 口径表与 `config.SPECS` 逐格一致（指标/名称/权重/合计 100%） |
| `test_architecture.py` | 依赖方向（渲染层不 import 编排层、纯计算不碰存储、xlstyle 是叶子、模块级无环）；颜色与表头 helper 只许在 `xlstyle` |

---

## 五、加一个新指标（S1.2 / S2.x …）

1. 在 `config.SPECS` 追加一条规格：指标号、名称、权重、`kind`（weighted/tiered）、`scorer` 名。
2. 在 `scoring.py` 写同名判分函数（入参是抽取到的数值 + 判档结论，输出 `IndScore`）。
3. 若要新的量化字段，在 `extract.FIELD_LABELS` 加标签词 + `FIELD_BOUNDS` 加取值域。
4. 若要新的定性判据，在 `rubric.py` 写三档原文 + 在 `keywords.csv` 加召回词（词表是 Excel 表，不用改代码）。
5. 改完把 `config.MODEL_VERSION` +0.1，并重跑 `calibrate.py`。

规格里声明的 `scorer` 若没实现，`scoring.py` 导入时直接报错（不会静默出 0 分）。

### 词表怎么改（不改代码）

定性召回的检索词表在 `keywords.csv`（四列：`指标` / `证据组` / `关键词` / `启用`），Excel 双击就能改：

1. **加词**：一行一个词。`指标` 写 rubric 的 key（`S1.1.1-C` / `S1.1.2-C` / `S1.1.3` / `S1.1.4` / `S1.1.5`）；
   `证据组` 决定它在关键词表里归到哪一行（T1/T2… 或自建组名），组序 = 表里第一次出现的先后。
2. **停用**：`启用` 写「否」，词留在文件里，随时翻回来——改词表最容易犯的错是删掉再想不起原文。
3. **生效**：重启进程（词表在进程启动时读一次）。要换整套词表，设 `ESG_S11_KEYWORDS=<另一个 CSV>`。
4. **校验**：五个指标每个都要有词条，少一个在导入时直接报错；缺列、指标名写错、启用列乱填都会报错并带行号——
   不会出现「表里有词、跑起来没命中」这种静默失效。改完 `rubric_hash()` 自动变化，旧判档缓存不会复用。
5. **匹配规则**（2026-11 起）：中文按子串，英文按**词首**匹配——`工时` 命中「标准工时制」，
   但 `wage` 不命中 `sewage`、`EAP` 不命中 `cheaper`、`diversity` 不命中 `biodiversity`；
   词尾不设限，单复数照旧命中（`employee benefit` 命中 `employee benefits`）。
6. **排序规则**：命中词数多的段落在前；港交所合规套话（"significant impact on the issuer"）
   与 GRI 索引行（`403-2`、`2-19`）降权，同分时带年份/比例的段落优先——判据要的是本期执行证据。
   改匹配或排序要动 `MATCH_VERSION`（它和词表一起进缓存指纹）。

当前词表 350 个词（S1.1.1-C 67 / S1.1.2-C 66 / S1.1.3 78 / S1.1.4 63 / S1.1.5 76），
含四个指标的繁体写法组（`补充·繁体写法`）——本包没有繁简归一，港版报告靠这组词兜。

权威口径（100/50/0 判据原文、边界提示）仍在 `rubric.py`：那里改一个字就要走代码评审并 +0.1 版本号。

---

## 六、目录与文件

```
esg_s11_scorer/               ← 独立 git 仓库（2026-11-03 初始化）
├── pyproject.toml            依赖与包元数据（pypdf / openpyxl）
├── requirements.txt
├── settings.py   ★本机配置    .env / 环境变量 → 生效路径（带来源追踪，可 --show-config）
├── .env.example              配置模板（入库）；.env 由 --init-env 生成，不入库
├── models.py     ★领域模型    CompanyRun / NumFact / Extraction / Component / IndScore / ScoreCard / ScoreData / PeerRow
│                              + 只读的 CompanyView / Batch
├── metrics.py    ★口径算子    改善率唯一实现（含 fmt_pct）
├── config.py                 版本号、阈值、指标规格（数据化）
├── rubric.py                 定性 rubric：三档判据原文 + 边界提示（词表不在这里）
├── keywords.csv  ★词表配置   五指标 × 证据组 × 关键词（Excel 可编辑；ESG_S11_KEYWORDS 可换）
├── keywords_io.py            词表读写与校验（缺列/未知指标/启用列 → 带行号报错）
├── extract.py                数值抽取：表头年份对齐、句式兜底、置信度、交叉校验
├── qualitative.py            定性判档：召回段落 + LLM 判档 + 关键词兜底
├── llm.py                    OpenAI 兼容客户端（无第三方依赖，可换端点）
├── blind.py                  盲评脱敏（公司名 → [COMPANY]，残留即不发送）
├── bench.py                  批内分位 / 外接对标表 / 导出对标库
├── scoring.py                五项指标算分（N/A、可达上限、归一）+ build_card 装配
├── stages.py     ★阶段计算    各段纯函数：parse/facts/prior（不碰文件）
├── store.py      ★落地层      阶段产物 JSON + gold 表（parquet/JSONL）+ 指纹
├── tables.py     ★表契约      facts/indicators/components/companies/bench_* 的字段与装配
├── pipeline.py   ★编排层      六段 DAG（解析/抽取/上期/判档/对标/打分）、缓存复用、跨年读表、发布
├── report.py                 每家工作簿 + 批次汇总（只读表，不计算）
├── scoreviz.py               得分构成图册（五项贡献、折合综合分、堆叠图）
├── qualviz.py                定性判档图册（档位矩阵、明细、档位柱图）
├── xlstyle.py    ★样式层     各本册子共用的配色/表头/注释条/行高估算（唯一信源）
├── keyword_table.py          定性关键词清单 xlsx（→ 打分结果/S1.1_定性关键词表.xlsx）
├── board.py                  指标看板数据导出（→ web/data.js；分数由 pipeline 现算，本层不判分）※
├── web/                      静态看板（index.html + app.js + data.js，双击即开，不用起服务）※
├── cli.py / __main__.py      命令行（--show-config / --init-env / --env-file / --force / --dry-run）
├── calibrate.py              回测与校准（→ reports/）
├── tests/                    回归测试（18 个文件、189 条，见第四节）
├── build/                    管道中间态（.gitignore，可随时删掉重算）
└── reports/                  校准产物（.gitignore，可随时重生成）
```

依赖方向（无环）：

```
settings → config → { extract, bench, scoring } → models
                          ↓
               stages → pipeline → { tables, store } → report
```

`models` 不 import 本包任何模块；`store` 只依赖标准库；`stages` 不碰文件；
`tables` 只认 `store` + `models`；`report` 只认 `models`（不 import pipeline，也不 import bench）。
外部类型 `Doc` / `ElementResult` 只出现在 `stages` 与 `TYPE_CHECKING` 里——
下游读产物时不再需要 `s11_judge`，历史数据不会被上游实现变更绑死。

**这些约束由 [`tests/test_architecture.py`](tests/test_architecture.py) 断言，不是靠自觉**：
渲染层（`report`/`scoreviz`/`qualviz`/`keyword_table`）不得 import 编排层，纯计算层不得碰
`store`/`tables`，`xlstyle` 必须是叶子，模块级不得成环；十六进制颜色与 `title`/`head`/`note`
这类 helper 只允许出现在 `xlstyle.py`。目录保持扁平是刻意的——分层靠的是依赖方向，
26 个模块、约 4900 行还没到需要靠子目录来导航的规模。

两个不常被提起的入口（都在 `50-应用` 下跑，读的是同一批 `build/` 产物）：

```powershell
python -m esg_s11_scorer.board --year 2025                # → web/data.js；打开 web/index.html 看
python -m esg_s11_scorer.board --year 2025 --codes 02269,03692   # 只导这几家
```

看板回答的是「这个指标取到什么值、这分是怎么算出来的」：分数由 `pipeline.build_scorecard`
现算，`board` 只做搬运，不新增任何计分规则。上面目录里标 ※ 的 `board.py` / `web/` 目前只在工作区，
还没提交入库——提到它们是因为本地已经能用，而不是说 `git clone` 下来就有。

---

## 七、管道：产物、指纹、怎么查数

### 1. `build/` 里有什么

```
build/
├── FY2025/                      ← 分区键 = (year, code)；当前 20 家
│   ├── _meta.json               ← 本年批次：模型版本、对标快照、家数
│   ├── 02269/
│   │   ├── parse.json           ← 页级文本（最贵的一步，全生命周期只算一次）
│   │   ├── facts.json           ← 抽到的数值 + 人工兜底（含 best 择优结果）
│   │   ├── prior.json           ← 上期值从哪来（本报告附录 / 上一年报告 / 人工）
│   │   ├── qualitative-llm.json ← 五处判档：档位、理由、依据段落、出处
│   │   └── score.json           ← 记分卡（公司行 + 指标行 + 分项行）
│   └── ...                      ← 另有 19 家
├── FY2024/                      ← 只有 parse/facts：上一年只需被读，不需出分（当前 5 家）
└── tables/FY2025/               ← gold 表（parquet；没装 pyarrow 时自动落 JSONL）
    ├── facts                     证据层：一行一个数值（当前 112 行）
    ├── indicators / components / companies   结果层：分项与总分（含口径与快照 id）
    └── bench_rows / bench_meta          口径层：这一年拿谁比的、n 是多少（当前 n=16）
```

每个产物都是带信封的 JSON：`{"stage","code","year","key","producer","produced_at","payload"}`。
信封把「能不能复用」和「产出内容」分开——查指纹不用解析 payload。

### 2. 指纹：为什么不重算，或为什么必须重算

| 阶段 | key 由什么决定 | 什么变了会触发重算 |
|---|---|---|
| parse | PDF 字节 + `PARSE_VERSION` + 解析源码 | 换了 PDF、改了 `s11_text`/`stages` |
| facts | parse 的 key + `EXTRACT_VERSION` + 抽取源码 + override 表内容 | 改了正则、改了人工兜底表 |
| qualitative | parse 的 key + 闸门位（有无本期流失率）+ rubric + 模型 + 别名表 + 模式 | 改了 rubric/词表/模型/别名表 |
| score | facts+prior+判档 的 key + 对标快照 + `MODEL_VERSION` + 打分源码 | 改阈值、改权重、对标池变了 |

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

els = pd.read_parquet(f"{base}/components.parquet")    # 一家一指标一分项：得分、判据、证据原文
els.query("ind == 'S1.1.1' and score.isna()")[["code", "comp_key", "note"]]
```

### 4. 对标快照：批内分位让分数「不独立」

A/B 是相对量，**加进第 6 家公司会改变前 5 家的 A/B 分**。这不是 bug，是批内分位的定义。
改造后这件事是**可见**的：`bench_meta.snapshot` + `companies.bench_snapshot` 记下当次
用了哪个池子，快照一变，全部 score 就重算并在台账里写出新 id。
当前 20 家批次的快照是 `3b5c7f7ebbcb8813`（n=16），5 家交付版本是 `5d15888ffac3888c`（n=5，
可在 workbook 的「运行台账」页查到）。两个快照下的分数不可直接比大小——这也是 README 不逐家
复述 20 家分数的原因之一。

要「补一家不影响老分数」，就得先定死池子：把当次 `bench_rows.parquet` 导出成行业库，
之后一律用 `--bench csv` 跑（见第五节 `bench.export_rows()`）。这是口径决策，不是代码决策。

---

## 八、已知限制

1. **只覆盖 S1.1**。其他议题（S1.2 职业健康、S2 供应链、E 系列）尚无指标标准文档，
   架构已预留（见第五节），但没有标准就不能凭空造判据。
2. **文本层依赖**。扫描件、图片化表格、纯图 KPI 卡抽不到；`overrides.csv` 是兜底口。
3. **批内分位 ≠ 全市场分位**。20 家的对标池只有 n=16（4 家缺本期流失率），仍只能做提示性判断；
   真要行业评级需按 `bench.export_rows()` 累积成行业库，再用 `--bench csv` 跑。
4. **S1.1.5 偏宽松**（见第四节）。
5. **pyarrow 可选**。装了写 parquet（带列类型、可被 DuckDB/pandas 直读），没装自动落
   JSONL（同样的行与列，只是没有类型与压缩）。两者 schema 一致，代码不用改。
6. **产物会随代码失效**。这是设计而不是缺陷：改了源码，旧 `build/` 就不再被复用。
   想省时间可以接受它重算一遍，想确定「跑的是当前代码」就加 `--force` 清空重来。
7. **批跑用例与家数耦合**。8 条测试把「本批 5 家」写死，输入变成 20 份 PDF 就必红；
   这是断言口径问题，不是代码回归（见第四节的处理原则）。
8. **上期只有 5 份**。`ESG报告/FY2024/` 现有 5 份 PDF，其余 15 家的上期值只能靠报告附录
   或 `overrides` 表，`prior_source=缺失` 是常态（20 家里 11 家）。
9. **交付物与 gold 表可能不同步**。workbook 是某次运行的快照，gold 表会被后续运行覆盖；
   两者家数/快照不一致时，以「你要交付的那次运行」为准，别混着引用。