"""Daily top-50 list for an RD-Agent `fin_factor` result.

Three subcommands, called in order by daily_top50.sh (see README.md in this folder):

    build-pv   rdagent4qlib env   Qlib data -> daily_pv.h5 (the price file RD-Agent factors read)
    factors    rdagent env        run every factor .py in the strategy folder -> combined_factors.parquet
    score      rdagent4qlib env   retrain RD-Agent's LightGBM setup, score today's CSI 300,
                                  and turn the scores into sells and buys for the next session

The model, features and trading rule copy RD-Agent's
rdagent/scenarios/qlib/experiment/factor_template/conf_combined_factors.yaml and Qlib's
TopkDropoutStrategy (topk 50, n_drop 5).
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

PV_FIELDS = ["$open", "$close", "$high", "$low", "$volume", "$factor"]

# Same hyperparameters as RD-Agent's conf_combined_factors.yaml
LGB_KWARGS = {
    "loss": "mse",
    "colsample_bytree": 0.8879,
    "learning_rate": 0.2,
    "subsample": 0.8789,
    "lambda_l1": 205.6999,
    "lambda_l2": 580.9768,
    "max_depth": 8,
    "num_leaves": 210,
    "num_threads": 20,
    "seed": 42,  # not in RD-Agent's config; fixed so reruns on the same data give the same list
}


def build_pv(args):
    import qlib
    from qlib.data import D

    qlib.init(provider_uri=args.data_dir, region="cn")
    # Same query as RD-Agent's factor_data_template/generate.py
    df = D.features(D.instruments(), PV_FIELDS, freq="day").swaplevel().sort_index().loc[args.start :]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_hdf(out, key="data")
    print(f"daily_pv.h5: {len(df):,} rows, last date {df.index.get_level_values('datetime').max().date()}")


def _to_datetime_instrument(res):
    if isinstance(res, pd.Series):
        res = res.to_frame()
    names = list(res.index.names)
    if names == ["instrument", "datetime"]:
        res = res.swaplevel()
    elif names != ["datetime", "instrument"]:
        raise ValueError(f"unexpected index {names}")
    return res


def run_factors(args):
    strategy = Path(args.strategy_dir)
    pv = Path(args.pv).resolve()
    files = sorted(strategy.glob("*.py"))
    if not files:
        sys.exit(f"No factor .py files in {strategy}")

    frames = []
    for f in files:
        # RD-Agent runs each factor as factor.py next to daily_pv.h5 and reads back result.h5
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "daily_pv.h5").symlink_to(pv)
            shutil.copy(f, tmp / "factor.py")
            subprocess.run([sys.executable, "factor.py"], cwd=tmp, check=True)
            res = _to_datetime_instrument(pd.read_hdf(tmp / "result.h5"))
        if res.shape[1] == 1 and res.columns[0] in (0, None):
            res.columns = [f.stem]
        print(f"{f.name}: {list(res.columns)}")
        frames.append(res)

    combined = pd.concat(frames, axis=1).sort_index()
    combined = combined.loc[:, ~combined.columns.duplicated(keep="last")]
    combined.columns = pd.MultiIndex.from_product([["feature"], combined.columns])
    combined.to_parquet(args.out, engine="pyarrow")
    print(f"combined factors: {combined.shape[1]} columns -> {args.out}")


def _segments(last, args):
    valid_end = args.valid_end or (last - pd.DateOffset(months=3)).strftime("%Y-%m-%d")
    valid_start = args.valid_start or (pd.Timestamp(valid_end) - pd.DateOffset(years=2)).strftime("%Y-%m-%d")
    train_end = args.train_end or (pd.Timestamp(valid_start) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    test_start = (pd.Timestamp(valid_end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "train": (args.train_start, train_end),
        "valid": (valid_start, valid_end),
        "test": (test_start, last.strftime("%Y-%m-%d")),
    }


def topk_dropout(scores, holdings, topk, n_drop):
    """Qlib TopkDropoutStrategy with method_sell="bottom", method_buy="top"."""
    last = scores.reindex(holdings).sort_values(ascending=False).index
    candidates = scores[~scores.index.isin(last)].sort_values(ascending=False).index
    today = list(candidates)[: n_drop + topk - len(last)]
    comb = scores.reindex(last.union(pd.Index(today))).sort_values(ascending=False).index
    sell = list(last[last.isin(list(comb)[-n_drop:])]) if n_drop else []
    buy = today[: len(sell) + topk - len(last)]
    return sell, buy


def score(args):
    import qlib
    from qlib.contrib.model.gbdt import LGBModel
    from qlib.data import D
    from qlib.data.dataset import DatasetH
    from qlib.data.dataset.handler import DataHandlerLP

    qlib.init(provider_uri=args.data_dir, region="cn")
    last = pd.Timestamp(D.calendar(freq="day")[-1])
    seg = _segments(last, args)
    print("segments:", seg)

    base = json.loads(Path(args.strategy_dir, "base_factors.json").read_text())
    handler = DataHandlerLP(
        instruments=args.market,
        start_time=seg["train"][0],
        end_time=seg["test"][1],
        data_loader={
            "class": "NestedDataLoader",
            "kwargs": {
                "dataloader_l": [
                    {
                        "class": "qlib.contrib.data.loader.Alpha158DL",
                        "kwargs": {
                            "config": {
                                "label": [["Ref($close, -2)/Ref($close, -1) - 1"], ["LABEL0"]],
                                "feature": [list(base.values()), list(base.keys())],
                            }
                        },
                    },
                    {
                        "class": "qlib.data.dataset.loader.StaticDataLoader",
                        "kwargs": {"config": str(Path(args.factors).resolve())},
                    },
                ]
            },
        },
        learn_processors=[
            {"class": "DropnaLabel"},
            {"class": "CSZScoreNorm", "kwargs": {"fields_group": "label"}},
        ],
    )
    dataset = DatasetH(handler=handler, segments=seg)
    model = LGBModel(**LGB_KWARGS)
    model.fit(dataset)
    pred = model.predict(dataset, segment="test")

    scores = pred.xs(last, level="datetime").dropna().sort_values(ascending=False)
    if scores.empty:
        sys.exit(f"No scores for {last.date()}. Check that the price update reached this date.")

    holdings_path = Path(args.holdings)
    holdings = []
    if holdings_path.exists():
        holdings = [s.strip() for s in holdings_path.read_text().split() if s.strip()]
    sell, buy = topk_dropout(scores, holdings, args.topk, args.n_drop)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    day = last.strftime("%Y-%m-%d")
    ranked = scores.rename("score").to_frame()
    ranked.insert(0, "rank", range(1, len(ranked) + 1))
    ranked.to_csv(out / f"{day}_scores.csv", index_label="instrument")
    orders = pd.DataFrame({"action": ["SELL"] * len(sell) + ["BUY"] * len(buy), "instrument": sell + buy})
    orders.to_csv(out / f"{day}_orders.csv", index=False)

    print(f"\nScores from close of {day} ({len(scores)} stocks). Trade these next session:")
    print(f"  SELL ({len(sell)}): {' '.join(sell) or '-'}")
    print(f"  BUY  ({len(buy)}): {' '.join(buy) or '-'}")
    print(f"  Top {args.topk} by score: {' '.join(scores.index[: args.topk])}")
    print(f"Files: {out / (day + '_scores.csv')}, {out / (day + '_orders.csv')}")
    print(f"After you trade, update {holdings_path} to what you actually hold.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build-pv")
    b.add_argument("--data-dir", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--start", default="2008-12-29")
    b.set_defaults(func=build_pv)

    f = sub.add_parser("factors")
    f.add_argument("--strategy-dir", required=True)
    f.add_argument("--pv", required=True)
    f.add_argument("--out", required=True)
    f.set_defaults(func=run_factors)

    s = sub.add_parser("score")
    s.add_argument("--data-dir", required=True)
    s.add_argument("--strategy-dir", required=True)
    s.add_argument("--factors", required=True)
    s.add_argument("--holdings", required=True)
    s.add_argument("--out-dir", required=True)
    s.add_argument("--market", default="csi300")
    s.add_argument("--topk", type=int, default=50)
    s.add_argument("--n-drop", type=int, default=5)
    s.add_argument("--train-start", default="2008-01-01")
    s.add_argument("--train-end", help="default: day before valid start")
    s.add_argument("--valid-start", help="default: 2 years before valid end")
    s.add_argument("--valid-end", help="default: 3 months before the last trading day")
    s.set_defaults(func=score)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
