# 本项目协作约定（esg_s11_scorer）

> 深度文档看 [README.md](README.md)（440 行，讲口径与架构）。本文件只写「动手前必须知道的
> 运行环境、红线、陷阱」，不重复 README。

## 1. 运行环境：命令从 `50-应用` 起，Python 用 3.14

- 包目录是 `50-应用/esg_s11_scorer/`，但**所有命令的工作目录是 `50-应用/`**（它本身是该仓库的
  上一级）。从包目录里跑 `python -m esg_s11_scorer` 会报 `No module named esg_s11_scorer`，
  README 第一节的 `cd 50-应用\esg_s11_scorer` 是错的，照抄会失败。
- 解释器用 `C:\Users\Alpha\AppData\Local\Programs\Python\Python314\python.exe`（装有 pypdf 6.9、
  openpyxl、pandas、pyarrow）。**其他 python（310/311/313）没装依赖**，跑起来是 ModuleNotFoundError。
- **本项目没有装 pytest**，测试是 `unittest`。跑单个文件：
  ```powershell
  cd 50-应用
  python -m unittest esg_s11_scorer.tests.test_settings -v      # 单模块
  python -m unittest esg_s11_scorer.tests.test_settings.TestParseEnvFile.test_basic_kv_and_comment   # 单条
  ```

## 2. 安全红线：`.env` 里的东西一个字都不许外流

`.env`（不入库）里有真实凭据：`ESG_LLM_API_KEY`（DashScope）、`ESG_DB_PASSWORD`（MySQL 48.217.x）、
`ESG_GPU_*_PASSWORD`（内网 10.10.31.12）。

- 不要 `git add .env`、不要把它写进提交信息 / 文档 / 报告 / 测试夹具。
- 需要展示配置时用 `python -m esg_s11_scorer --show-config`，它已经对密钥做位数掩码。
- 改配置改 `.env`；改模板改 `.env.example`（占位符），两者不要混。

## 3. 不要随手跑 `python -m esg_s11_scorer`

默认命令 = 用 `.env` 里那项（现在是 `FY2025/`，**20 份 PDF**）全量重算，并且：

- 会**覆盖** `good_sample/分析指标/打分结果/` 下的全部交付物：`S1.1_批次汇总_2025.xlsx`、
  `FY2025/` 里 20 份单家报告、两张图册表 —— 这批现在是 20 家的 1.4 版本（2026-11-09 16:29），
  重跑会原地改写分数（判档抖动、池子家数变化都会反映进去）；
- `--qualitative llm` 默认调付费模型，20 家 × 5 处判档的 token 实打实花钱。

只想验证代码能不能跑：

```powershell
cd 50-应用
python -m esg_s11_scorer --dry-run --qualitative keyword --build-dir D:\tmp\esgbuild --out D:\tmp\esgout
```

要看真实判档就先把 `--pdf` 指到单份 PDF、`--bench none`，别在默认批上试。

## 4. 测试现状：有 8 条是「红的但情有可原」

`tests/` 共 189 条。其中 8 条断言写死了「本批 5 家」，而 `ESG报告/FY2025/` 现有 **20 份 PDF**，
所以批跑用例必然失败：

| 文件 | 失败用例 | 原因 |
|---|---|---|
| `test_pipeline.py` | `test_batch_ok`、`test_bench_auto` | 家数 20≠5；分位随样本量变 |
| `test_regression.py` | `test_all_parsed`、`test_indicator_scores`、`test_bench_percentiles`、`test_numeric_extraction` | `EXPECT_BATCH` 是 5 家的分 |
| `test_report.py` | `test_batch_workbook`、`test_batch_detail_covers_every_company` | 明细表按 5 个代码断言 |

处理原则：

- **不要为了让测试变绿去改 `EXPECT_BATCH` / 期望代码列表** —— 那些数是人工核证过的口径锚点。
- 要真跑绿，把 `ESG_PDF_DIR` / `REPORT_DIR` 指向只含那 5 家（01177/01513/01801/02269/03692）的目录，
  或者问用户要不要把断言改成「按目录实际家数」。
- 改口径后如果分数真的该变，是**先改断言并说明为什么**，不是反过来。

## 5. 判档两条路，别拿 LLM 当回归基线

- `--qualitative keyword`：离线、确定性，**回归测试锁的就是这条口径**。
- `--qualitative llm`：正式出分用。同一份报告重跑个别档位会变（README 记录过 03692 的 S1.1.5
  由 50→100），**不是 bug**；结果按指纹缓存在 `build/FY2025/<code>/qualitative-llm.json`，
  不重算就不会变。想让分数「任何时候都一样」只能靠缓存命中，或接受抖动。
- 判档结论直接是分（100/50/0），没有词法拼装层；缺判档就抛错，不会静默给 0。

## 6. 改口径的规矩（这几条最容易踩）

- 权重/阈值/规格的唯一信源是 `config.SPECS` / `config.BANDS`；动任何一个数字都要
  `MODEL_VERSION` +0.1（人读的「这次改了口径」标记）。
- 判据原文（100/50/0 三档怎么描述）只在 `rubric.py`，改它要走评审 + 升版本。
- 召回词表在 `keywords.csv`（四列，Excel 可直接改）：**加词/停用不用改代码**，但进程启动时读一次，
  改完要重启；指标缺词条、缺列、`启用` 列乱填都会在导入时带行号报错。改匹配或排序要动
  `MATCH_VERSION`（和词表一起进缓存指纹）。
- 英文按**词首**匹配、中文按子串（`wage` 不命中 `sewage`）；这条由 `tests/test_matching.py` 锁着。
- 新增指标的四步在 README 第五节；`SPECS` 里声明的 `scorer` 没实现会在导入时报错。

## 7. 缓存与指纹：跑得快不代表没生效

- 中间态在 `build/`（含 gold 表 `build/tables/FY{year}/`），**可随时删掉重算**，不入库。
- 指纹里含**源码摘要**（`store.source_digest`）：改了 `extract.py` 一定重算 facts，不需要谁记得升版本。
  所以「第二次 2 秒跑完」= 这次真的没有东西需要重算。
- 怀疑缓存不对、或要确认跑的是当前代码：加 `--force`。
- 对标池每次运行都会落成 `build/bench/FY{year}/{snapshot}.json`；`--bench-snapshot <id>` 读回，
  `--freeze-bench` 只采集+冻结（不算分、不发布）。收集名单在 `build/FY{year}/manifest.csv`，
  回答「池子为什么少了一家」先看它。
- 现在 `build/FY2025/` 已有 **20 家**的产物，`build/FY2024/` 有 5 家（上一年只需被读）。
  注意 `ESG_PRIOR_DIR` 指向的 `ESG报告/FY2024/` 目前**是空的**，上期值靠报告附录 + override 表。

## 8. 架构不变量由测试断言，不是靠自觉

`tests/test_architecture.py` 会挂掉的事，别去绕：

- 渲染层（`report`/`scoreviz`/`qualviz`/`keyword_table`）不得 import 编排层（`pipeline`/`stages`/`store`）。
- 纯计算层（`scoring`/`extract`/`bench`/`metrics`）不得碰 `store`/`tables`。
- `xlstyle` 必须是叶子；模块级不得成环。
- 十六进制颜色、`title`/`head`/`note` 这类 helper 只允许出现在 `xlstyle.py`。

目录保持扁平是刻意的（24 个模块、约 4700 行），加子目录前先问。

## 9. 只读的外部依赖，别顺手改

- `../ESG_insight/app/`（`s11_text` 等）：**只读复用，不修改那个仓库**。位置变了设 `ESG_INSIGHT_APP`。
- `../good_sample/分析指标/`、`../sample/` 是共享数据与交付目录，不是本仓库的测试夹具：
  往里写文件、改文件名都会影响别人，动手前问。
- 本仓库是独立 git 仓库（根 = 包目录），remote `git@github.com:HKUBSAIRI/esg_s11_scorer.git`，分支 `master`。

## 10. Git 与测试的协作方式

- 用户说「提交推送」= 直接 `git add -A` → commit → `git push`，中间不插测试 / lint / 额外检查。
- **不要主动跑整套测试**（189 条，约 55 秒；但批跑用例在 20 份 PDF 上会跑到 8 分钟）。
  除非用户明确要求，或改动本身需要最小验证——那时只跑相关模块。
- 提交信息用中文，写清「为什么改」。
- 工作区里 `build/`、`reports/`、`.env`、`*.log` 已忽略；`.env.example` 是模板，必须入库。
- Windows 上 `LF will be replaced by CRLF` 的警告是常态，忽略即可；不要自作主张加 `.gitattributes`
  做换行归一化（会产生全仓库大 diff）。