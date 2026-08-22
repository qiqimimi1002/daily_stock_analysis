# Tushare `rt_k` 第一阶段：许可与安全边界

## 当前状态

本仓库当前是 **Public Fork**，`rt_k` 接入保持 Draft、未合并且不接生产。
独立的 Private 采集端已配置 Repository Secret，并在 2026-08-20 完成一次
`600000.SH` 收盘后、所有者本人可见的单股只读调用；该调用不属于盘中验收，
也不代表本 Public PR 已通过真实行情验收。Private 原始响应、Token 和运行产物
没有进入本仓库。

公开仓库只保留无凭证的 Provider 实现和明确标记为合成数据的契约测试。
没有生产 workflow 注入 `TUSHARE_TOKEN`，没有 PR workflow 读取 Secret，也没有
Public 真实行情验收入口。Provider 不被 Market Screener、Daily Stock、scheduler
或 research 调用。

基准：`main` `01b8c5337ee52c23cceb532a08f3367911aa1d48`。

## 当前分支安全审计

整改前的未提交工作树曾包含两项不应进入 Public 仓库的设计：

- 在生产全市场初筛步骤中注入 `secrets.TUSHARE_TOKEN`；
- 新增同仓库 PR/manual workflow，调用真实 `rt_k` 并上传验收摘要。

两项均已在提交前删除。审计结果如下：

| 检查项 | 结果 |
| --- | --- |
| Token 明文、长度、前后缀或哈希 | 未产生、未提交 |
| Public PR中的完整 `rt_k` 响应 | 未调用、未产生、未提交；Private单股调用产物未复制入本仓库 |
| 真实付费行情 fixture | 未产生；测试文件全部标记为合成数据 |
| 原始行情快照 | 未产生、未提交 |
| 公开日志或 Artifact | 未触发；相关验收 workflow 已删除 |
| 生产 Secret 接线 | 已撤回，正式 workflow 与 `main` 保持一致 |
| 生产筛选/回退/调度 | 未接入、未改变 |

注意：仓库在本分支之前已经有其他 Tushare 历史数据能力和通用
`TUSHARE_TOKEN` 配置；本审计只覆盖本次 `rt_k` 第一阶段新增内容，不替代对
既有能力的单独许可审计。

## 无凭证适配器契约

`data_provider/tushare_rt_k_provider.py` 只定义请求、字段/单位映射、时间校验、
有限重试和进程内同日缓存。代码中没有凭证；只有私有、受控运行环境显式提供
Token 环境变量时才可能调用接口；GitHub 环境中该变量只能由受控的 Secret
注入。Public CI 只注入 Fake Client 和虚构 Token 占位符，不访问网络。

股票代码统一通过 `validate_rt_k_stock_codes()` 校验。输入只能是1至5个以逗号
分隔的严格大写代码，每个代码必须匹配 `^[0-9]{6}\.(SH|SZ)$`；首尾空格可
剥离，但不得转换大小写、修复后缀或静默去重。空元素、重复项、错误格式和超过
5只均在读取 `TUSHARE_TOKEN`、创建HTTP Client或发起请求之前失败。通配符和
全市场请求不属于本阶段契约。

官方 `rt_k` 字段映射冻结为：

| `rt_k` | 内部字段 | 口径 |
| --- | --- | --- |
| `ts_code` | `code` | 去后缀后仍复用现有主板/ST规则 |
| `close` | `close` / `price` | 盘中最新价，不称最终收盘价 |
| `pre_close` | `prev_close` | 供应商昨收 |
| `open/high/low` | 同名字段 | 未复权盘中日K |
| `vol` | `volume` | 股 |
| `amount` | `amount` | 元 |
| `trade_time` | `market_data_at` | `Asia/Shanghai`，不以任务时间伪造 |

`change_pct` 只由 `(close - pre_close) / pre_close * 100` 计算。未完成的
当日日K不得进入历史特征序列。认证/权限错误不重试；只有超时、429和可恢复
5xx使用有限指数退避；速率上限不超过每分钟50次。

## 私有采集端与 Public 模型端边界

当前采用隔离架构，而不是让 Public workflow 直接读取付费源：

```text
私有受控采集端
  ├─ Secret 注入并调用 rt_k
  ├─ 保存原始响应（私有、最短保留期、访问审计）
  ├─ 完成字段校验、主板过滤和既定模型计算
  └─ 输出经许可确认的候选级最小结果
                 ↓
Public 模型端
  ├─ 验证签名/哈希、交易日、as_of、schema和来源标签
  ├─ 只接收候选股票、时间戳、必要价格字段、评分和完整性 hash
  └─ 不获得 Token、全市场原始行、订单簿或可还原完整响应的数据
```

原始数据只能保存在私有存储，不进入 Git、Public Actions 日志、
`market_snapshot.json`、公开缓存或 Public Artifact。Public 输出统一显示：
`数据来源：Tushare数据`。

候选级 schema、必要价格字段的精确范围、签名方式和发布通道尚未实施。Private
采集端不向本 Public 仓库发布任何真实价格、原始响应或可还原付费数据的内容。

## 许可边界

Tushare 当前数据服务协议将付费服务描述为个人、不可转让、非商业、仅供个人
查看使用，并要求 Token 保密。个人、Private、所有者本人可见的只读研究与
Public 发布保持隔离。未来如需公开或对外提供数据，必须单独确认：

1. 是否允许公开展示候选级衍生指标，具体字段、粒度和保留时间；
2. 是否允许 Public 项目接收候选、必要价格、评分和完整性 hash；
3. 必须使用的来源标识、免责声明和删除/撤回机制；
4. 是否需要商业授权或其他独立许可。

个人、Private、所有者本人可见的只读研究可以独立进行。若未来需要公开实时
价格、候选级衍生结果或对外提供下载，再单独确认许可。当前 Public PR 仍不得
读取 Secret、运行真实 `rt_k`、上传真实行情、转 Ready、合并或部署；Private
盘中验收也尚未执行。

参考：

- <https://tushare.pro/document/2?doc_id=372>
- <https://tushare.pro/document/1?doc_id=409>
- <https://tushare.pro/document/1?doc_id=405>
