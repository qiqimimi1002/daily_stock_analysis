# Phase 2B corporate-action source acceptance

Status: **CONDITIONAL PASS**. This is a research-source decision, not a model
feature and not approval to start Short-term v1.

## Fixed source roles

The formal primary evidence is the issuer/exchange implementation disclosure,
captured through an official exchange or CNINFO interface. AKShare
`stock_dividend_cninfo` and `stock_allotment_cninfo` are interface wrappers,
not a new source of truth. The independent cross evidence is AKShare/Sina
`stock_history_dividend_detail` (`分红` or `配股`). Baostock
`query_dividend_data(..., yearType="operate")` is an additional cross-check for
cash, stock-dividend and capitalization terms. Baostock daily
`tradestatus="0"` is the raw-history cross-check for suspension dates.

The implementation announcement/capture time, record date, ex-date, gross
cash per old share, stock-dividend/capitalization ratio, rights ratio and
price, payment/listing date, suspension interval and resumption date are
contract fields. Plan dates, generic progress labels, provider display text
and adjustment factors are auxiliary only. Baostock's plan-announcement date
must not be substituted for the later implementation-announcement timestamp.
The wrapper interfaces are documented in the
[AKShare stock API reference](https://akshare.akfamily.xyz/data/stock/stock.html).

## Real-source evidence (2026-08-21 snapshot)

Raw rows were held in memory only. The values below are a minimal diagnostic
summary, not a redistributable market-data file.

| Event | Source agreement | Raw-price diagnostic |
|---|---|---|
| `600519`, cash dividend, record 2024-06-18, ex/pay 2024-06-19, CNY 30.876 per share | CNINFO, Sina, Baostock and Eastmoney agreed on terms and effective dates. Baostock separately reported the earlier plan date 2024-04-03. | Both fixed raw sources agreed on prior close 1521.50, ex-date open 1497.99 and close 1501.00. Exchange-style reference 1490.624; raw close change -1.3474%, distribution economic return +0.6820%. |
| `600519`, cash dividend, record 2024-12-19, ex/pay 2024-12-20, CNY 23.882 per share | The same four interfaces agreed on terms and effective dates; Baostock plan date was 2024-11-09. | Prior close 1551.01, ex-date open 1531.13, close 1522.00, reference 1527.128; raw close change -1.8704%, economic return -0.3306%. |
| `001387`, 10 shares transfer 3 plus cash CNY 3.5, record 2024-05-31, ex/pay/listing 2024-06-03 | CNINFO, Sina and Baostock agreed on 0.30 capitalization and CNY 0.35 cash per old share. Eastmoney agreed on record/ex dates. | Both fixed raw sources agreed on prior close 22.45, ex-date open 16.83 and close 16.49. Reference 17.00; the raw -26.5479% close jump becomes -2.9532% holder economic return. This is the verified same-day combined-action case. |
| `600030`, rights issue, 10 rights 1.5 at CNY 14.43, record 2022-01-18, suspension 2022-01-19 through 2022-01-26, ex/resumption 2022-01-27, rights listing 2022-02-15 | Issuer disclosure and Sina agreed on ratio, price, record/ex/listing dates. Issuer disclosure fixed the payment and suspension schedule. The CNINFO call timed out in this run, so it was not counted as confirming evidence. | Baostock marked all six frozen-calendar sessions as `tradestatus=0`, then a real 2022-01-27 bar (prior close 25.70, open 24.81, close 24.15). Reference 24.23. The frozen Sina raw cross-source had a TLS failure, so this event's raw-price cross-check failed closed and remains single-source diagnostic evidence. |

Across the successful dual-source windows, dates and OHLCV matched. Amount
differences were provider rounding only and stayed within the already frozen
CNY 0.50 raw-history tolerance. No qfq/hfq series was requested or used.

## Formal processing boundary

1. Raw daily bars remain immutable and unadjusted. A separate action overlay
   may calculate a diagnostic exchange reference price:
   `[(previous close - cash) + rights price * rights ratio] /
   [1 + stock/transfer ratio + rights ratio]`.
   This follows the current
   [SSE trading-rule formula](https://www.sse.com.cn/lawandrules/sselawsrules2025/fund/trading/c/c_20260424_10817739.shtml),
   while issuer-specific special handling remains authoritative.
2. For cash plus stock/transfer only, holder economic return is
   `[event close * (1 + stock/transfer ratio) + cash] / previous close - 1`.
   It is a derived calculation with its own version; it never replaces raw
   OHLC. Rights issues require an explicit subscription/election policy and
   remain `review_required`; the contract does not manufacture a return.
3. Provider-carried OHLC on `tradestatus=0` rows is discarded. Suspended dates
   have no OHLCV input and are never forward-filled. The first active
   resumption bar remains a raw market observation. If an action is effective
   on resumption, the action overlay and market move remain separate.
4. Source absence, duplicate/unsorted events, date or term conflict, an event
   outside the frozen Phase 2B-0B1 calendar, a missing resumption bar, or any
   source/network failure stops the acceptance path. No different provider or
   price basis is substituted.
5. `known_at`, `source_data_as_of` and `fetched_at` must be timezone-aware and
   no later than `market_data_at`. A date-only current snapshot cannot prove
   what was known at a historical signal time.

## Vintage, licence and archive boundary

The tested interfaces expose current snapshots and do not provide a historical
vintage. Retrospective agreement validates terms, not point-in-time
availability. Formal model use therefore requires prospective private capture
of the issuer implementation disclosure and both normalized source snapshots,
with immutable source, symbol, requested range, fetch/as-of time,
schema/calculation version, row/event counts and content/manifest SHA-256.

The [AKShare MIT licence](https://github.com/akfamily/akshare/blob/main/LICENSE),
the [Baostock package licence](https://pypi.org/project/baostock/) and the
[Baostock disclaimer](https://baostock.com/disclaimer) do not establish
redistribution rights for upstream event or market rows. Provider/issuer terms
must be checked for the intended archive. Until explicit redistribution
permission exists, raw event and market payloads stay private; the Public
repository and Public Artifacts may contain metadata, counts and hashes only.

True stock splits/reverse splits, a standalone stock dividend distinct from
capitalization, cross-year distributions and special/differential formulas
were not independently verified in this sample. They remain fail-closed,
`review_required` inputs rather than inferred cases.
