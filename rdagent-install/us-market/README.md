# Switching RD-Agent to the US market

RD-Agent v1.0.0 hardcodes China in its Qlib scenarios (`fin_factor`, `fin_model`, `fin_quant`): the
data folder, `region: cn`, the CSI 300 universe, the CSI 300 index as benchmark, China's ±10% price
limit and China's trading costs. None of these are `.env` settings. This folder switches them to the
S&P 500 and back.

| Setting | China (RD-Agent default) | US (after the switch) |
|---|---|---|
| Data folder | `~/.qlib/qlib_data/cn_data` | `~/.qlib/qlib_data/us_data` |
| `region` | `cn` | `us` |
| Universe | `csi300` | `sp500` |
| Benchmark | `SH000300` (CSI 300 index) | `^gspc` (S&P 500 index) |
| Price limit | `0.095` | none |
| Costs | 0.05% buy, 0.15% sell, ¥5 minimum | 0.05% buy, 0.05% sell, no minimum |

The US costs are an assumption for a commission-free broker plus slippage. Change them in
`switch_market.sh` to match your broker before you trust a backtest.

## Files

- `get_us_data.sh`: downloads Qlib's US data, repairs it for the Qlib version RD-Agent uses, and
  updates it from Yahoo.
- `switch_market.sh`: edits RD-Agent's templates for the US (`us`) or restores China (`cn`).

## Steps

Run these on the VM. RD-Agent must not be running while you switch.

**1. Get the US data** (450 MB, 2000-01-03 to 2020-11-10):

```bash
bash ~/rdagent-guide/rdagent-install/us-market/get_us_data.sh download
```

It ends with `OK: calendar 1999-12-31 to 2020-11-10, 744 S&P 500 members, ^gspc readable`. The zip
was written by a newer Qlib than the one RD-Agent pins: its stock folders start with `_` and its
member lists have an extra column, so without the repair Qlib reads no data at all. `download` runs
the repair for you. `get_us_data.sh fix` repeats it and is safe to run again.

**2. Bring it up to date from Yahoo.** This needs the Qlib source checkout from the trading README
(`~/qlib`). The first run fills November 2020 to today for about 500 stocks and can take hours:

```bash
bash ~/rdagent-guide/rdagent-install/us-market/get_us_data.sh update
```

**3. Switch RD-Agent to the US:**

```bash
bash ~/rdagent-guide/rdagent-install/us-market/switch_market.sh us
```

It edits five config templates, the script that builds the price file for generated factor code
(now limited to stocks that have ever been in the S&P 500), and the experiment table the LLM reads.
It then deletes the China price files and `pickle_cache/` in `~/RD-Agent`, so RD-Agent rebuilds
them from US data with Docker on the next run. Your old runs and logs stay. If any edit does not
apply, the script restores the files and changes nothing.

**4. Set the dates.** Add or change these lines in `~/RD-Agent/.env`. Use the `QLIB_FACTOR_` lines
for `fin_factor` and the `QLIB_QUANT_` lines for `fin_quant`:

```bash
QLIB_FACTOR_TRAIN_START=2008-01-01
QLIB_FACTOR_TRAIN_END=2018-12-31
QLIB_FACTOR_VALID_START=2019-01-01
QLIB_FACTOR_VALID_END=2020-12-31
QLIB_FACTOR_TEST_START=2021-01-01
QLIB_FACTOR_TEST_END=
```

An empty `TEST_END` means "up to the last day in the data". If you skip step 2, the data ends on
2020-11-10 and these dates leave no test period. Then use, for example, train 2008-2016, validate
2017-2018 and test from 2019-01-01.

**5. Start a fresh run:**

```bash
cd ~/RD-Agent
rdagent fin_factor --loop-n 10
```

Factors and results from China runs don't carry over. The US research starts from scratch, and
China and US numbers are not comparable.

**Switch back to China** at any time (with RD-Agent stopped):

```bash
bash ~/rdagent-guide/rdagent-install/us-market/switch_market.sh cn
```

To run both markets at the same time, use a second RD-Agent checkout (for example
`RDAGENT_DIR=~/RD-Agent-us`, with its own `.env`) and switch only that one.

## Trading a US result

The daily job in `../trading/` takes `REGION=us`. Copy the data once so the trading job never
changes what research reads:

```bash
cp -r ~/.qlib/qlib_data/us_data ~/.qlib/qlib_data/us_data_live
REGION=us bash ~/rdagent-guide/rdagent-install/trading/daily_top50.sh
```

See [the trading README](../trading/README.md#us-market) for the folder layout and the cron time.

## Limits

- **Survivorship bias.** Qlib's `sp500.txt` has 755 entries, and 505 of them are still marked as
  members. Companies that left the index, including failed ones, are under-represented, so US
  backtests look better than reality.
- **Data quality.** Yahoo is a free source. Splits, dividends and delistings are sometimes late or
  wrong. Use a paid feed for real money.
- **Tested** in a sandbox with Qlib's US data (last day 2020-11-10): the download and repair, the
  switch and its revert on an RD-Agent v1.0.0 checkout, the switched price-file script (660 stocks,
  1.8 million rows), and RD-Agent's switched baseline backtest with Qlib (S&P 500, test from
  2019-01-01, benchmark `^gspc`). Not tested: the Yahoo update and a full `rdagent fin_factor` run.

> This is an engineering tool, not financial advice. Paper-trade before using real money.
