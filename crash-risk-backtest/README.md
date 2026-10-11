# Crash-risk status backtest

Rebuilds the Market Fragility Index's weekly crash-risk status (Calm / Watch / Warning / Danger)
from its published rules and measures S&P 500 returns after each status.

```
python3 backtest.py --start 2010-01-01            # default: 2010 to latest
python3 backtest.py --start 2011-01-01 --elevated-mult 1.3
```

Data: downloads S&P 500 (Yahoo), VIX (GitHub mirror of CBOE), BAA10Y / DGS10 / T10Y3M (FRED)
and caches them in `data/`. You can also drop CSVs (`date,value`) named `SPX.csv`, `VIX.csv`,
`BAA10Y.csv`, `DGS10.csv`, `T10Y3M.csv` there yourself. Results go to `out/`.

See the docstring in `backtest.py` for the rules and the assumptions the artifact leaves open.
