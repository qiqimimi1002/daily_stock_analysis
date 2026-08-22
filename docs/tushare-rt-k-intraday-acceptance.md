# Tushare `rt_k` 盘中只读验收

## 冻结基准与状态

- Public Draft PR：#20
- 验收基准提交：`8009de5b4aaac85e27d4ff7487128c6f9e15de7a`
- 状态：**Offline PASS / Awaiting intraday acceptance**
- 第一只股票：`600000.SH`
- 计划窗口：下一交易日北京时间 09:35—09:40
- 新鲜度上限：180秒

该基准已通过105项适配器、安全边界和相关回归测试。盘中验收不得改变
Provider、Secret、工作流、生产筛选或调度；PR继续保持Draft。

## 离线辅助工具

```powershell
python scripts/validate_tushare_intraday_acceptance.py `
  --artifact-dir <解压后的私有Artifact目录> `
  --runtime-dir <仅含本次运行日志的目录> `
  --expected-code 600000.SH `
  --as-of <检查时的Asia/Shanghai时间>
```

工具只读取已有文件，不导入Provider、不读取Secret、不创建HTTP客户端、不请求
行情、不写文件。机器可读JSON输出到stdout，人可读检查表输出到stderr；退出码
0表示机器检查PASS，退出码1表示FAIL。

## 机器检查

1. Artifact必须且只能包含：
   - `acceptance-summary.json`
   - `redaction-scan.json`
2. 两个文件必须是合法JSON、符合字段白名单且各自不超过128 KiB。
3. 运行身份必须为 `single_stock`、`600000.SH`、`tushare`、`rt_k`，返回1行。
4. 规范化列必须包含 `close`（盘中最新价）、`prev_close`、OHLC、
   `volume`、`amount`、`trade_time`及`market_data_at`。
5. `volume_unit=shares`、`amount_unit=yuan`、`market_state=intraday`、
   `quality_status=ok`。
6. `market_data_at`和`generated_at`必须为 `Asia/Shanghai` 的 `+08:00`
   时间，顺序正确；行情时间不得在检查时间之后或早于180秒以上。
7. `raw_market_data_persisted=false`，规范化内容hash为64位小写SHA-256。
8. Redaction报告必须为 `passed` 且findings为空。
9. Artifact和指定运行日志中不得出现Secret名称/赋值、Authorization、Bearer、
   凭证形态、HTTP头/响应正文或原始付费行情行。

真实价格值按安全边界不会进入脱敏Artifact。工具通过必需规范化列、
`quality_status=ok`以及冻结Provider的fail-closed契约确认 price(`close`)、
`prev_close`、OHLC、volume和amount已通过数值校验，不会重新读取或落盘这些值。

## 人工复核

- GitHub Run来自Private仓库`main`，actor为仓库所有者，且仅请求`600000.SH`。
- Provider步骤成功，日志没有认证、权限或异常重试迹象。
- GitHub Artifact列表中没有第三个文件、缓存、原始响应或调试产物。
- 日志未打印真实价格行、请求头、响应体、Token或Secret。
- 盘中结果仍只供仓库所有者本人查看，不复制到Public PR或公开位置。

## PASS / FAIL

所有机器检查均为PASS时，工具的 `overall_status` 才能为 `PASS`。任何安全、
Artifact/schema、新鲜度、时区、市场状态、单位、质量状态或泄漏检查失败，整体
立即为 `FAIL`，没有可覆盖关键失败的warning。最终盘中验收还需要人工复核全部
完成；离线PASS不能冒充真实盘中验收。
