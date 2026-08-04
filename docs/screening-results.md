# 全市场初筛结果读取

## 2026-08-04 读取失败结论

截至 2026-08-04 12:59（Asia/Shanghai）再次查询，GitHub Actions 中仍没有当天的
“全市场初筛”计划运行记录。因此，10:00 读取时的准确状态是
`not_started`，不是筛选代码失败、排队或运行中。

已核对的证据：

- 工作流处于 `active` 状态；
- cron 仍为 `40 1 * * 1-5`，即工作日北京时间 09:40；
- 当天没有 run ID，所以也没有队列时间、Artifact 或候选结果可供读取；
- 前一个计划运行是 run 13（ID `30814967880`），实际于
  2026-08-03 20:46:24 启动，20:49:36 完成，结论为 `success`；
- run 13 的 Artifact 是 `market-screening-13`（ID `8856401519`），包含
  初筛 JSON、空的候选代码文件和一份初筛报告；60 只预选股票的历史行情
  均获取失败，因此候选数为 0，深度分析步骤按设计跳过，没有三只深度报告。

工作流没有时区配置错误，也没有证据表明 10:00 时处于 GitHub 队列中；
更准确的根因是 GitHub 计划触发时间不具备准点保证，而原读取端又没有先查询
Actions 运行状态，也没有固定的完成结果入口。

## 固定结果入口

成功合并并完成下一次运行后，工作流会维护独立的
`screening-results` 分支。该分支只保存运行产物和元数据，不向 `main` 写入
每日文件。

固定入口：

```text
https://raw.githubusercontent.com/qiqimimi1002/daily_stock_analysis/screening-results/latest/manifest.json
```

目录结构：

```text
latest/
  manifest.json
  market_screening.json
  screened_codes.txt
  reports/
history/YYYY-MM-DD/
  manifest.json
  market_screening.json
  screened_codes.txt
  reports/
```

发布步骤使用 `continue-on-error`。发布失败会在 Actions 中显示警告，但不会
改变原初筛、深度分析或 Artifact 上传结果。

## 运行清单

`data/screening_run_manifest.json` 会与原结果一起上传到动态 Artifact，并被
复制到固定结果分支。主要字段包括：

- 交易日期、工作流名称、run ID、run number、Artifact 名称；
- 开始时间、完成时间和最终状态；
- 初筛结果生成时间、行情时间（源文件提供时）、数据源和模型版本；
- 全市场记录、预选、历史行情成功/失败、证据增强成功/失败数量；
- 候选数量、候选代码、深度分析代码和深度报告；
- 初筛 JSON、代码文件、报告文件的 SHA-256；
- 平均和最低证据覆盖率；
- 完整性检查结果和明确错误代码。
- 固定分支中 `latest/` 与 `history/YYYY-MM-DD/` 的机器读取路径。

完成态 `status` 使用：

- `screening_completed`：初筛成功，但本次未请求深度分析；
- `success`：要求的步骤及文件完整，或无候选而无需深度分析；
- `partial_success`：初筛产物存在，但深度报告或完整性检查不完整；
- `failure`：初筛步骤失败或没有可验证的初筛 JSON。

## 读取端状态判定

运行尚未完成时没有最终 Artifact，因此读取端必须先查询 GitHub Actions，
不能只搜索仓库文件：

1. 当天没有 run：`not_started`；
2. run 的 GitHub 状态为 `queued`：`queued`；
3. run 的 GitHub 状态为 `in_progress`：`in_progress`；
4. run 已完成且 conclusion 不是 success：`failure`；
5. run 成功但无法下载 Artifact 或固定入口：`artifact_read_failure`；
6. run 成功且可读取 manifest：采用 manifest 中的完成态。

读取固定入口时必须核对 `trade_date` 和 `run_id`，避免把前一交易日的
`latest` 误当作当天结果。固定入口发布失败时，回退到对应 run 的
`market-screening-<run_number>` Artifact。

## 读取时间建议

10:20 可以作为第一次状态检查，但不能保证已经有结果：本次实际到 10:23
仍为 `not_started`。因此不建议继续单纯推迟固定时间；读取任务应在
`not_started`、`queued` 或 `in_progress` 时如实报告状态，并在 10:40 或
11:00 再检查。11:00 盘中复盘必须验证候选清单的 `trade_date`，不得使用
前一交易日结果代替。

本链路不修改 V2.1 条件、100 分权重、主板/ST 过滤、09:40 cron、正式报告
或研究依赖。
