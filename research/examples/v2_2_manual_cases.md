# V2.2 synthetic manual checks

These cases use only the artificial data in `v2_2_test_prices.json`. They are
not real securities research, trading returns, or investment recommendations.
All three signals use a reference price of `100` on 2026-01-05. The fixture
omits 2026-01-07 as an artificial exchange holiday.

## Case 1: normal price path (`600100`)

The first three future trading dates are 2026-01-06, 2026-01-08, and
2026-01-09. Their closes are `104`, `107`, and `96`; highs are `105`, `108`,
and `106`; lows are `98`, `101`, and `95`.

- 1-day return: `104 / 100 - 1 = 4.00%`.
- 3-day return: `96 / 100 - 1 = -4.00%`.
- 3-day maximum upside: `108 / 100 - 1 = 8.00%`.
- 3-day maximum adverse excursion: `95 / 100 - 1 = -5.00%`.
- 3-day close-path maximum drawdown: peak close `107` to later close `96`,
  `96 / 107 - 1 = -10.280374%`.
- 5-day return: `110 / 100 - 1 = 10.00%`; maximum upside `12.00%`;
  maximum adverse excursion `-6.00%`; maximum drawdown remains `-10.280374%`.

## Case 2: suspended target (`600200`)

The 3-day target remains 2026-01-09. That row is explicitly suspended with no
transaction, so status is `suspended`; close and return are null. The next
available close on 2026-01-12 is not substituted and the horizon is not
extended.

## Case 3: corporate action (`600300`)

The raw, unadjusted price path matches Case 1, but a cash-dividend event is
recorded on 2026-01-08. Mature windows containing that date are marked
`corporate_action_review`. Raw observation metrics may be retained for audit,
but the status prevents them from being treated as clean inputs to later win
rate statistics.
