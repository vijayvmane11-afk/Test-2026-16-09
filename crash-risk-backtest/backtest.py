"""Backtest of the Market Fragility Index crash-risk status (Calm / Watch / Warning / Danger).

Rebuilds the weekly status from the rules the artifact publishes, then measures
what the S&P 500 did after each status.

Rules (from the artifact's live reading):
  Crash alarms, checked on each week's last close:
    trend   S&P 500 close below its 200-day average
    credit  Baa spread (FRED BAA10Y) at or above its level 13 weeks ago + 0.25pp
    fear    VIX at or above its 1-year average * 1.25
  Status: 2+ alarms -> Danger, 1 alarm -> Warning,
          no alarm  -> Watch if the background model is Elevated, else Calm.
  Background model: logistic regression on monthly inputs
    credit_3m      3-month change in BAA10Y
    ylong_12m      12-month change in the 10-year yield (DGS10)
    curve_resteep  10y-3m curve (T10Y3M) positive now but inverted in the past 24 months
    trend10m       S&P 500 vs its 10-month average
  target = S&P 500 falls 10%+ below the month-end close within the next 3 months.
  It is refit each January on earlier years only (no look-ahead).

Assumptions the artifact does not spell out (change with flags):
  --elevated-mult  Elevated when the model probability > this multiple of the
                   training base rate (default 1.5).
  Curve series and 24-month inversion lookback.
Returns are price-only (no dividends) and ignore trading costs.

Data: put CSVs in ./data (see load_* functions) or let the script download from
FRED, Yahoo Finance and GitHub.
"""
import argparse
import io
import os
import sys
import urllib.request

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
STATUSES = ["Calm", "Watch", "Warning", "Danger"]


# ---------------------------------------------------------------- data

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode()


def _cached(name, fetch):
    path = os.path.join(DATA, name + ".csv")
    if os.path.exists(path):
        s = pd.read_csv(path, index_col=0, parse_dates=True).iloc[:, 0]
    else:
        s = fetch()
        os.makedirs(DATA, exist_ok=True)
        s.to_csv(path)
    return pd.to_numeric(s, errors="coerce").dropna().sort_index()


def load_fred(series):
    def fetch():
        df = pd.read_csv(io.StringIO(_get(
            f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}")))
        df.columns = ["date", series]
        return df.set_index(pd.to_datetime(df["date"]))[series]
    return _cached(series, fetch)


def load_spx():
    def fetch():
        import json
        js = json.loads(_get("https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
                             "?period1=0&period2=9999999999&interval=1d"))["chart"]["result"][0]
        idx = pd.to_datetime(js["timestamp"], unit="s").normalize()
        return pd.Series(js["indicators"]["quote"][0]["close"], index=idx, name="SPX")
    return _cached("SPX", fetch)


def load_vix():
    def fetch():
        df = pd.read_csv(io.StringIO(_get(
            "https://raw.githubusercontent.com/datasets/finance-vix/main/data/vix-daily.csv")))
        return df.set_index(pd.to_datetime(df["DATE"]))["CLOSE"].rename("VIX")
    return _cached("VIX", fetch)


# ---------------------------------------------------------------- model

def fwd_min(close, days):
    """Lowest close over the next `days` sessions, as a return from today."""
    rev = close[::-1].rolling(days, min_periods=1).min()[::-1].shift(-1)
    out = rev / close - 1
    out.iloc[-days:] = np.nan
    return out


def logit_fit(X, y, l2=1e-2, iters=50):
    X = np.column_stack([np.ones(len(X)), X])
    w = np.zeros(X.shape[1])
    for _ in range(iters):  # Newton steps
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (p - y) + l2 * np.r_[0, w[1:]]
        H = (X * (p * (1 - p))[:, None]).T @ X + l2 * np.diag(np.r_[0, np.ones(len(w) - 1)])
        w -= np.linalg.solve(H, g)
    return w


def logit_predict(w, X):
    return 1 / (1 + np.exp(-(w[0] + X @ w[1:])))


def background(spx, baa, dgs10, curve, mult, train_start):
    m = pd.DataFrame({
        "spx": spx.resample("ME").last(),
        "baa": baa.resample("ME").last(),
        "y10": dgs10.resample("ME").last(),
        "curve": curve.resample("ME").last(),
    }).dropna()
    f = pd.DataFrame(index=m.index)
    f["credit_3m"] = m["baa"].diff(3)
    f["ylong_12m"] = m["y10"].diff(12)
    f["curve_resteep"] = ((m["curve"] > 0) &
                          (m["curve"].rolling(24).min() < 0)).astype(float)
    f["trend10m"] = m["spx"] / m["spx"].rolling(10).mean() - 1
    # target: 10%+ fall within ~63 sessions after month end
    dd = fwd_min(spx, 63)
    f["target"] = (dd.reindex(m.index, method="ffill") <= -0.10).astype(float)
    f.loc[dd.reindex(m.index, method="ffill").isna(), "target"] = np.nan
    f = f.dropna(subset=["credit_3m", "ylong_12m", "trend10m"])
    f = f[f.index >= train_start]

    cols = ["credit_3m", "ylong_12m", "curve_resteep", "trend10m"]
    prob = pd.Series(np.nan, index=f.index)
    elevated = pd.Series(False, index=f.index)
    for year in sorted(set(f.index.year)):
        # only rows whose 3-month outcome was known before this year began
        train = f[(f.index < pd.Timestamp(year, 1, 1) - pd.DateOffset(months=3))].dropna()
        if len(train) < 60 or train["target"].sum() < 3:
            continue
        mu, sd = train[cols].mean(), train[cols].std().replace(0, 1)
        w = logit_fit(((train[cols] - mu) / sd).values, train["target"].values)
        rows = f.index.year == year
        prob[rows] = logit_predict(w, ((f.loc[rows, cols] - mu) / sd).values)
        elevated[rows] = prob[rows] > mult * train["target"].mean()
    return pd.DataFrame({"prob": prob, "elevated": elevated})


def weekly_status(spx, vix, baa, bg):
    d = pd.DataFrame({"spx": spx, "vix": vix, "baa": baa}).ffill().dropna()
    d = d[d.index.isin(spx.index)]
    d["trend"] = d["spx"] < d["spx"].rolling(200).mean()
    d["credit"] = d["baa"] >= d["baa"].shift(65) + 0.25
    d["fear"] = d["vix"] >= d["vix"].rolling(252).mean() * 1.25
    w = d.groupby(d.index.to_period("W-FRI")).tail(1).copy()
    w["alarms"] = w[["trend", "credit", "fear"]].sum(axis=1)
    # month-to-date inputs: use the latest month-end estimate available that week
    bgm = bg.copy()
    bgm.index = bgm.index.to_period("M")
    w["elevated"] = [bool(bgm["elevated"].get(t.to_period("M") - 1, False)) for t in w.index]
    w["prob"] = [bgm["prob"].get(t.to_period("M") - 1, np.nan) for t in w.index]
    w["status"] = np.select(
        [w["alarms"] >= 2, w["alarms"] == 1, w["elevated"]],
        ["Danger", "Warning", "Watch"], "Calm")
    return w


# ---------------------------------------------------------------- report

def report(spx, w, start, end):
    w = w[(w.index >= start) & (w.index <= end)].copy()
    for name, n in [("ret1w", 5), ("ret1m", 21), ("ret3m", 63)]:
        fwd = spx.shift(-n) / spx - 1
        w[name] = fwd.reindex(w.index)
    w["fall1m"] = fwd_min(spx, 21).reindex(w.index) <= -0.10
    w["fall3m"] = fwd_min(spx, 63).reindex(w.index) <= -0.10
    w["worst3m"] = fwd_min(spx, 63).reindex(w.index)

    # daily returns attributed to the status set at the prior week's close
    daily = spx.pct_change()
    st_daily = w["status"].reindex(daily.index, method="ffill").shift(1)
    st_daily = st_daily[(st_daily.index > w.index[0]) & (st_daily.index <= end)]
    daily = daily.reindex(st_daily.index)

    rows = []
    for s in STATUSES:
        g = w[w["status"] == s]
        dr = daily[st_daily == s]
        days = len(dr)
        rows.append({
            "status": s,
            "weeks": len(g),
            "share": len(g) / len(w),
            "avg 1w": g["ret1w"].mean(),
            "avg 1m": g["ret1m"].mean(),
            "avg 3m": g["ret3m"].mean(),
            "median 3m": g["ret3m"].median(),
            "% 3m up": (g["ret3m"].dropna() > 0).mean(),
            "P(10% fall 1m)": g["fall1m"].mean(),
            "P(10% fall 3m)": g["fall3m"].mean(),
            "median worst 3m": g["worst3m"].median(),
            "ann. return held": (1 + dr).prod() ** (252 / days) - 1 if days else np.nan,
            "ann. vol held": dr.std() * np.sqrt(252) if days else np.nan,
        })
    table = pd.DataFrame(rows).set_index("status")

    # simple strategies, switching at each weekly close
    def strat(invested):
        r = daily.where(st_daily.isin(invested), 0.0)
        eq = (1 + r).cumprod()
        yrs = len(r) / 252
        return {"CAGR": eq.iloc[-1] ** (1 / yrs) - 1,
                "vol": r.std() * np.sqrt(252),
                "max drawdown": (eq / eq.cummax() - 1).min(),
                "time invested": st_daily.isin(invested).mean()}
    strats = pd.DataFrame({
        "Buy & hold": strat(STATUSES),
        "Out in Danger": strat(["Calm", "Watch", "Warning"]),
        "Out in Warning+Danger": strat(["Calm", "Watch"]),
        "Calm only": strat(["Calm"]),
    }).T

    yearly = (w.groupby([w.index.year, "status"]).size().unstack(fill_value=0)
              .reindex(columns=STATUSES, fill_value=0))
    return w, table, strats, yearly


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--elevated-mult", type=float, default=1.5)
    ap.add_argument("--train-start", default="1990-01-01")
    ap.add_argument("--curve", default="T10Y3M", help="FRED curve series (T10Y3M or T10Y2Y)")
    a = ap.parse_args()

    spx, vix = load_spx(), load_vix()
    baa, dgs10, curve = load_fred("BAA10Y"), load_fred("DGS10"), load_fred(a.curve)
    end = pd.Timestamp(a.end) if a.end else spx.index[-1]

    bg = background(spx, baa, dgs10, curve, a.elevated_mult, pd.Timestamp(a.train_start))
    w = weekly_status(spx, vix, baa, bg)
    w, table, strats, yearly = report(spx, w, pd.Timestamp(a.start), end)

    pd.set_option("display.width", 200, "display.max_columns", 30)
    pct = lambda x: f"{x:.1%}" if pd.notna(x) else "–"
    print(f"Weekly crash-risk status, {w.index[0].date()} to {w.index[-1].date()}\n")
    t = table.copy()
    for c in t.columns:
        if c != "weeks":
            t[c] = t[c].map(pct)
    print(t.T.to_string(), "\n")
    s = strats.copy().map(pct)
    print("Strategies (switch at weekly close, cash earns 0):")
    print(s.to_string(), "\n")
    print("Weeks per status by year:")
    print(yearly.to_string())

    out = os.path.join(HERE, "out")
    os.makedirs(out, exist_ok=True)
    w.to_csv(os.path.join(out, "weekly_status.csv"))
    table.to_csv(os.path.join(out, "by_status.csv"))
    strats.to_csv(os.path.join(out, "strategies.csv"))
    yearly.to_csv(os.path.join(out, "status_by_year.csv"))
    print(f"\nCSV output in {out}")


if __name__ == "__main__":
    sys.exit(main())
