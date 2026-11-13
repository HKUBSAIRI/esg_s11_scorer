# web/ —— 三个页面，同一个视觉语言

| 文件 | 打开方式 | 回答的问题 |
|---|---|---|
| `index.html` | 双击（`data.js` 是普通脚本，`file://` 也能读） | 这家公司的这个指标取到什么值、分是怎么算出来的 |
| `explorer.html` | `python -m esg_s11_scorer.explorer` 后访问 `http://127.0.0.1:8765` | 这句话凭什么归到这个指标、在第几页 |
| `disputes.html` | `python -m esg_s11_scorer.disputes` 后访问 `http://127.0.0.1:8766` | 这家公司的每个指标，三家模型给的是不是一个档；全批哪些项有分歧 |

三个页面都不算分：看板的数字来自 `data.js`（`board.py` 导出），段落浏览器只做解析与召回，
分歧页只做「三票判档 + 总汇」，不写 `build/`、不碰交付目录。

## 〇、判档分歧页（disputes.html）

- **单家判档**：拖入 PDF → 五个指标各三票（每家一段理由 + 引用页码），
  橙色边框的是三家不一致的项。一次上传真的调模型：**5 项 × 3 票 = 15 次**。
- **全批总汇**：读 `reports/ensemble.json`（体检产物），画成 **公司 × 指标** 的矩阵
  （20 行 × 5 列）：绿 = 三家一致、琥珀 = 有分歧、灰 = 闸门；格内直接并排三家档位，
  行末是这家「分歧项 / 可判项」。支持「仅有分歧」筛选与公司/代码搜索；
  点任一格 → 右侧抽屉看三家各自的档位、理由与引用。
- 没有 `reports/ensemble.json` 时页面给提示——先跑
  `python -m esg_s11_scorer.ensemble`（约 2 分钟、288 次调用、命中缓存则 0 次）。
- 判档复用 `ensemble.ballot`，与 `reports/ensemble.md` 同一套调用与判据，所以页面上的
  「三家不一致」和报告里的「分裂」是同一件事。

## 一、指标看板（index.html）

- 左栏切换公司；点指标行展开分项、缺口与依据原文；地址可直达 `index.html#02269/S1.1.1`。
- 页面不计算分数，也不带解释性弹层 / 图例 / 引导文案：数字与依据都来自 `data.js`。

### 重新生成数据

工作目录是 `50-应用`（包目录的上一级）：

```powershell
python -m esg_s11_scorer.board --year 2025          # 默认导出该年度全部出过分的公司（当前 20 家）
python -m esg_s11_scorer.board --year 2025 --codes 02269,03692   # 只导这几家
```

`board.py` 只读 `build/FY2025/<code>/` 的阶段产物（facts / prior / qualitative-*），
用与交付工作簿相同的 `pipeline.build_scorecard` 重建分数，再摊平成前端要的形状。
去掉 `--codes` 就导出该年度全部出过分的公司；`--mode keyword` 可强制走关键词兜底。

页脚的对标家数分两个：`n=` 是 A 项的分母（有本期流失率的公司），括号里的「改善率 n」是 B 项
的分母（本期与上期都有值）——当前 16 / 9，不同指标数不到 3 家时该指标记 N/A。

不传 `--codes` 时整批一个口径：全部公司都有 `qualitative-llm` 产物才用 LLM，
否则退回 `qualitative-keyword`，避免同一页里两种判档口径混排。

## 二、段落浏览器（explorer.html）

```powershell
cd 50-应用
python -m esg_s11_scorer.explorer                # http://127.0.0.1:8765
python -m esg_s11_scorer.explorer --port 9000 --open
```

- 拖入 PDF（或点「选择 PDF」）：文本层在**本机内存**里解析，不落盘、不上传。
- 左栏是五个指标（＋「数值证据」）；点指标看召回的段落，按命中词数排序，可切「按页码」。
- 每段给出页码、命中的词（页面里加底色）、所属证据组；点段落跳到右侧 PDF 的对应页。
- 「检索词表」展开是该指标全部词条（组名即细则里的 E1/T1/P1…），加词改 `keywords.csv` 后重启即可。
- 「数值证据」列出流失率/薪酬差距/员工数：值、年度、页码、抽取方式、置信度与原句。
- 「下载 JSON」把整份分析（含每段命中词与字符区间）存下来，便于复核或喂给别的脚本。

后端是 `explorer.py`（标准库 `http.server`，没有新依赖）：

| 接口 | 说明 |
|---|---|
| `GET /` | 页面 |
| `GET /api/health` | 服务与指标列表 |
| `POST /api/analyze?name=<文件名>` | 请求体是 PDF 字节；返回分页文本摘要、五个指标的段落、数值事实 |

解析链路全部复用打分管道：`s11_text.load_pdf` → `s11_text.sentences` →
`qualitative.recall`（同一份 `keywords.csv`、同一条「命中数 → 是否带数据 → 页码」排序），
数值用 `extract.extract`。所以页面里看到的段落，与 CLI 在判档时召回给模型的是同一批——
**这里不判档、不算分**，改口径仍然只改 `rubric.py` / `keywords.csv`。

单份上限 120 MB（本批最大 44 MB）。扫描件没有文本层，会在上传后直接报「整份 PDF 都提不出文本层」，
需要先 OCR。同一份文件按 `sha256 + 文件名` 缓存最近 6 份，重复打开是秒开的。
