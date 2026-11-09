# S1.1 指标看板

打开 `index.html` 即可（`data.js` 是普通脚本，`file://` 下也能读，不需要起服务）。

- 左栏切换公司；点指标行展开分项、缺口与依据原文；地址可直达 `index.html#02269/S1.1.1`。
- 页面不计算分数，也不带解释性弹层 / 图例 / 引导文案：数字与依据都来自 `data.js`。

## 重新生成数据

工作目录是 `50-应用`（包目录的上一级）：

```powershell
python -m esg_s11_scorer.board --year 2025 \
    --codes 02269,03692,01513,01801,01177
```

`board.py` 只读 `build/FY2025/<code>/` 的阶段产物（facts / prior / qualitative-*），
用与交付工作簿相同的 `pipeline.build_scorecard` 重建分数，再摊平成前端要的形状。
去掉 `--codes` 就导出该年度全部出过分的公司；`--mode keyword` 可强制走关键词兜底。

不传 `--codes` 时整批一个口径：全部公司都有 `qualitative-llm` 产物才用 LLM，
否则退回 `qualitative-keyword`，避免同一页里两种判档口径混排。