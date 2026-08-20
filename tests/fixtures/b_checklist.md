# 场景 B 验收检查点（汇总质量）

## 验收结论（2026-08-18 真实 DeepSeek 跑通）

### 样本 1：`tests/fixtures/b_docs/annual_review.txt`（原验收样本）
- 原样本的"冲突"实为自洽时间序列（Q1 1200 万 → Q2 1500 万；3 月启动 → 5 月灰度），LLM 正确按时间线处理，未误报冲突——行为合理。
- 国际化战略有备注段（s4）覆盖，非真遗漏。
- 汇总报告有 [来源:s1~s4] 标注、无编造 ✓。

### 样本 2：`tests/fixtures/b_docs/conflict_review.txt`（强冲突样本，新增）
埋入：营收 1500 万 vs 1200 万（同口径矛盾）、利润 300 vs 180（矛盾）、客户甲多段重复、团队规模无覆盖。
**四规则全部触发**（task_952ac1b7，CLI 已可打印审计段）：
- 冲突裁决 ✓：营收/利润两处矛盾 → 结论"存疑：数据矛盾需核实"
- 去重 ✓：客户甲跨段分析（正确判定内容不同不构成重复）
- 覆盖遗漏 ✓：4 项标记（客户占比数值/新客策略/客服满意度/团队规模影响评估）
- 无编造 ✓：全结论带 [来源:sN]

## 运行命令

```bash
PYTHONPATH=src python -m maestro.cli run \
  --scenario b --worker embedded --input tests/fixtures/b_docs/annual_review.txt
# 强冲突样本
PYTHONPATH=src python -m maestro.cli run \
  --scenario b --worker embedded --input tests/fixtures/b_docs/conflict_review.txt
# 审计段在 CLI 输出尾部（本次完善：status 命令补打 summary.md 审计段）
```

## 记录
- merge 四规则实现于 `src/maestro/merge.py`（summary + audit 两次 LLM 调用，audit 输出严格 JSON）。
- 本次后端完善：`cli.py _print_status` 补打 summary.md 的「## 审计」段（此前 CLI 只显示汇总正文，冲突裁决/遗漏不可见）。
