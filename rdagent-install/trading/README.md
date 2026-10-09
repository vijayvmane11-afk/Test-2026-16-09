# Daily top-50 list from an RD-Agent `fin_factor` result

RD-Agent finds factors and backtests them on fixed historical dates. It does not update prices or
tell you what to trade today. This folder adds that missing daily step for a `fin_factor` result:
after each market close, it updates prices, recomputes your factors, retrains RD-Agent's
LightGBM model and prints the sells and buys for the next session. It covers China (the default)
and the US (`REGION=us`, see [US market](#us-market)).

It reproduces RD-Agent's setup exactly. That setup is in
`rdagent/scenarios/qlib/experiment/factor_template/conf_combined_factors.yaml` and Qlib's
`TopkDropoutStrategy`:

| | |
|---|---|
| Universe | CSI 300 members on the scoring day |
| Features | the 20 base factors in `base_factors.json` + your RD-Agent factor `.py` files |
| Model | LightGBM, same hyperparameters, predicting the return from the next close to the one after |
| Rule | hold 50 stocks; each day sell at most the 5 weakest holdings when better stocks are available, buy the best-ranked stocks you don't own, equal weight |

The only deliberate difference is a fixed LightGBM `seed`, so that two runs on the same data give
the same list. RD-Agent sets no seed.

**Only for `fin_factor` results.** A `fin_quant` or `fin_model` winner also has a generated
`model.py` (a PyTorch network). This script does not handle that model.

> This is an engineering tool, not financial advice. A backtest is not a forecast. Paper-trade it
> before you put money behind it.

## Files

- `daily_top50.sh`: the daily job. Runs the four steps below in the right conda envs.
- `top50.py`: does the work. Its subcommands are `build-pv`, `factors` and `score`.

## One-time setup

**1. A separate copy of the price data.** The trading job updates its own copy, so RD-Agent's
`~/.qlib/qlib_data/cn_data` stays unchanged while research runs are using it:

```bash
cp -r ~/.qlib/qlib_data/cn_data ~/.qlib/qlib_data/cn_data_live
```

**2. Qlib's price collector.** It is in the Qlib source tree, not in the pip package. Use the same
commit as the `rdagent4qlib` env:

```bash
git clone https://github.com/microsoft/qlib.git ~/qlib
cd ~/qlib && git checkout 2fb9380b342556ddb50a4b24e4fe8655d548b2b8
conda activate rdagent4qlib
pip install -r ~/qlib/scripts/data_collector/yahoo/requirements.txt \
  -c ~/rdagent-guide/rdagent-install/constraints-qlib-py310.txt
conda deactivate
```

**3. Your strategy files.** In the Web UI, open the run's **RESULT** tab. Click **download_all** on
**every loop marked Success**, because successful loops build on each other. Put everything into
one folder:

```
~/rd-trading/strategy/
  base_factors.json      the 20 base factors (identical in every loop; keep one copy)
  SMA_5.py               every factor file from every successful loop
  SMA_10.py
  ...
```

`descriptions.md` files are not needed. For your Loop 1 result, the folder holds `base_factors.json`
plus `SMA_5.py`, `SMA_10.py`, `SMA_20.py` and `SMA_50.py`.

**4. Your holdings.** Create `~/rd-trading/holdings.txt` with the Qlib codes you hold, one per line,
for example `SH600519`. Leave it empty on day one. The first run then buys the top 50.

## Daily run

```bash
bash ~/rdagent-guide/rdagent-install/trading/daily_top50.sh
```

It prints, for example:

```
Scores from close of 2026-10-09 (300 stocks). Trade these next session:
  SELL (5): ...
  BUY  (5): ...
  Top 50 by score: ...
Files: ~/rd-trading/picks/2026-10-09_scores.csv, ~/rd-trading/picks/2026-10-09_orders.csv
```

`*_scores.csv` ranks all CSI 300 stocks. `*_orders.csv` lists the sells and buys.

After you place the trades, **edit `holdings.txt` to match what you actually own.** The next day's
orders are computed from that file.

**Timing.** The scores use prices up to today's close. In RD-Agent's backtest, the trades for those
scores happen at the **next** day's close. Trading near the close of the next session matches the
backtest most closely.

**Automate it.** Shanghai closes at 15:00 China time (07:00 UTC). Run the job at 09:30 UTC, Monday to
Friday, to leave time for Yahoo to publish the day:

```bash
crontab -e
# add:
30 9 * * 1-5 bash $HOME/rdagent-guide/rdagent-install/trading/daily_top50.sh >> $HOME/rd-trading/daily.log 2>&1
```

Settings are environment variables at the top of `daily_top50.sh`: `REGION` (`cn` or `us`),
`MARKET`, `WORK_DIR`, `STRATEGY_DIR`, `LIVE_DATA`, `QLIB_SRC`, `HOLDINGS`, and `SKIP_UPDATE=1` to
score without downloading prices. The
rolling training window (train from 2008, validate on the 2 years ending 3 months ago) can be
changed with `top50.py score --train-start/--train-end/--valid-start/--valid-end`.

## US market

Use this for a result from an RD-Agent run on US data (see `../us-market/README.md`). Set
`REGION=us` and everything else follows: the S&P 500 universe, the US price collector, the data copy
`~/.qlib/qlib_data/us_data_live` and the work folder `~/rd-trading-us`, so China and US holdings
never mix.

One-time setup, after `../us-market/get_us_data.sh download`:

```bash
cp -r ~/.qlib/qlib_data/us_data ~/.qlib/qlib_data/us_data_live
mkdir -p ~/rd-trading-us/strategy && touch ~/rd-trading-us/holdings.txt
```

Put the US run's factor files and `base_factors.json` in `~/rd-trading-us/strategy/`, as in step 3
above. Holdings are plain tickers, one per line, for example `AAPL`.

```bash
REGION=us bash ~/rdagent-guide/rdagent-install/trading/daily_top50.sh
```

New York closes at 16:00 Eastern, which is 20:00 UTC in summer and 21:00 UTC in winter. Run the job at
21:30 UTC, Monday to Friday:

```bash
30 21 * * 1-5 REGION=us bash $HOME/rdagent-guide/rdagent-install/trading/daily_top50.sh >> $HOME/rd-trading-us/daily.log 2>&1
```

The price file for the factors holds every stock that has ever been in the S&P 500, the same as
RD-Agent's US price file after `switch_market.sh us`. Tested on Qlib's US data (last day 2020-11-10):
the job scored 503 S&P 500 stocks. The US Yahoo update was not run in that test.

## Running it next to `fin_quant` or `fin_factor`

You do **not** need to stop RD-Agent. The two jobs share nothing that either one changes:

- **Price data:** the trading job writes only `cn_data_live` (or `us_data_live`). RD-Agent reads
  only `cn_data` (or `us_data`). The script refuses to run if `LIVE_DATA` points at either.
- **Files:** the trading job writes only to `~/rd-trading` (or `~/rd-trading-us`). RD-Agent writes
  to `~/RD-Agent`.
- **CPU:** both are heavy. LightGBM uses 20 threads for a few minutes, and a research run slows down
  during that time. On a small VM, schedule the trading job when you can accept that.

When a research run finds a better factor set, replace the files in `strategy/`. Re-test on recent
data before switching.

## Known limits

- **The first price update is slow.** Qlib's bundled data ends on 2020-09-25. The first run downloads
  every stock from then until today from Yahoo, which can take hours. Later runs fetch only the new
  days.
- **Data quality.** Yahoo is a free source, and its China A-share data has gaps and late corrections.
  The collector also rebuilds the CSI 300 member list by scraping csindex.com.cn. That page changes
  occasionally and can break the step. For real money, use a paid data feed.
- **Tested** on Qlib's bundled data (last day 2020-09-25) with two SMA factors: the factor, training
  and order steps all ran, and two runs gave identical lists. The Yahoo price update was not run in
  that test.
