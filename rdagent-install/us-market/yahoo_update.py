"""Update a Qlib US data folder from Yahoo, limited to S&P 500 members.

Qlib's own `collector.py update_data_to_bin --region US` (at the commit RD-Agent pins) has four
problems for this use:

- it downloads every listed US ticker (about 19,600, many delisted or warrants), about two days;
- its S&P 500 member refresh reads a Wikipedia table that moved to "Historical components of the
  S&P 500" in August 2026, and crashes;
- members added since the data was built get only a few days of prices;
- Wikipedia writes BRK.B and BF.B, while Yahoo and Qlib use BRK-B and BF-B.

This script runs the same Qlib collector, normalizer and dumper, but:

1. updates only stocks that are still S&P 500 members (plus the ^GSPC, ^NDX and ^DJI indexes);
2. refreshes instruments/sp500.txt from Wikipedia's current pages;
3. downloads the full history of members that have no prices in the data yet.

Yahoo refuses an end date after today, and the end date is excluded, so the data reaches the last
trading day before today (VM time). Run it after midnight to include the previous session.

Run it in the rdagent4qlib env. It needs the Qlib source checkout and fake-useragent (see README.md).
"""

import argparse
import datetime
import os
import shutil
import sys
import tempfile
from io import StringIO
from pathlib import Path

INDEX_TICKERS = ["^GSPC", "^NDX", "^DJI"]
SP500_CURRENT_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SP500_CHANGES_URLS = [
    "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500",  # since 2026-08
    SP500_CURRENT_URL,  # before 2026-08: the last table on the list page
]


def yahoo_ticker(symbol):
    """Wikipedia ticker -> Yahoo/Qlib ticker ("BRK.B" -> "BRK-B", stray footnote marks removed)."""
    if not isinstance(symbol, str):
        return None
    s = symbol.replace("|", " ").strip().split(" ")[0].strip().upper()
    return s.replace(".", "-") or None


def sp500_members(qlib_dir, since):
    """Codes in instruments/sp500.txt that were members on or after `since` (YYYY-MM-DD)."""
    f = qlib_dir / "instruments" / "sp500.txt"
    codes = set()
    for line in f.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].strip() >= since:
            codes.add(parts[0].strip().upper())
    return codes


def has_features(qlib_dir, code):
    return (qlib_dir / "features" / code.lower()).is_dir()


def make_sp500_index(base):
    """Qlib's SP500Index, reading the changes table from Wikipedia's current page layout."""
    import pandas as pd
    import requests

    class SP500(base):
        def get_changes(self):
            headers = {"User-Agent": self._ua.random}
            table = None
            for url in SP500_CHANGES_URLS:
                resp = requests.get(url, headers=headers, timeout=60)
                if resp.status_code != 200:
                    continue
                for t in pd.read_html(StringIO(resp.text)):
                    flat = [" ".join(map(str, c)) if isinstance(c, tuple) else str(c) for c in t.columns]
                    if len(flat) >= 4 and "date" in flat[0].lower() and any("add" in c.lower() for c in flat):
                        table = t
                        break
                if table is not None:
                    break
            if table is None:
                raise ValueError("No S&P 500 changes table found on Wikipedia")

            changes = table.iloc[:, [0, 1, 3]].copy()
            changes.columns = [self.DATE_FIELD_NAME, self.ADD, self.REMOVE]
            changes[self.DATE_FIELD_NAME] = pd.to_datetime(changes[self.DATE_FIELD_NAME], errors="coerce")
            changes = changes.dropna(subset=[self.DATE_FIELD_NAME])
            result = []
            for kind, shift in [(self.ADD, 0), (self.REMOVE, -1)]:
                df = changes.copy()
                df[self.CHANGE_TYPE_FIELD] = kind
                df[self.SYMBOL_FIELD_NAME] = df[kind].map(yahoo_ticker)
                df = df.dropna(subset=[self.SYMBOL_FIELD_NAME])
                df[self.DATE_FIELD_NAME] = df[self.DATE_FIELD_NAME].apply(
                    lambda x: self._shift(x, shift)  # noqa: B023
                )
                result.append(df[[self.DATE_FIELD_NAME, self.CHANGE_TYPE_FIELD, self.SYMBOL_FIELD_NAME]])
            return pd.concat(result, sort=False)

        def _shift(self, date, shift):
            from data_collector.utils import get_trading_date_by_shift

            return get_trading_date_by_shift(self.calendar_list, date, shift)

        def filter_df(self, df):
            if "Symbol" in df.columns:
                out = df.loc[:, ["Symbol"]].copy()
                out["Symbol"] = out["Symbol"].map(yahoo_ticker)
                return out.dropna()

    return SP500


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--qlib-dir", required=True, help="Qlib data folder to update, e.g. ~/.qlib/qlib_data/us_data")
    p.add_argument("--qlib-src", default="~/qlib", help="Qlib source checkout (default ~/qlib)")
    p.add_argument("--history-start", default="2000-01-01", help="first day fetched for newly added members")
    p.add_argument("--skip-members", action="store_true", help="don't refresh sp500.txt from Wikipedia")
    args = p.parse_args()

    qlib_dir = Path(args.qlib_dir).expanduser().resolve()
    yahoo_dir = Path(args.qlib_src).expanduser().resolve() / "scripts" / "data_collector" / "yahoo"
    today = datetime.date.today().strftime("%Y-%m-%d")

    # Qlib's collector imports itself as the module "collector" from its own folder
    os.chdir(yahoo_dir)
    sys.path.insert(0, str(yahoo_dir))
    import collector  # noqa: E402
    from dump_bin import DumpDataUpdate  # noqa: E402

    work = Path(tempfile.mkdtemp(prefix="yahoo_update_"))

    def fetch(symbols, start, tag, extend):
        collector.YahooCollectorUS1d.get_instrument_list = lambda self: sorted(symbols)
        run = collector.Run(
            source_dir=str(work / f"{tag}_source"), normalize_dir=str(work / f"{tag}_normalize"), region="US"
        )
        run.download_data(delay=0.5, start=start, end=today)
        if not any((work / f"{tag}_source").glob("*.csv")):
            print(f"Yahoo returned no data for {tag}")
            return
        run.max_workers = max((os.cpu_count() or 2) - 2, 1)
        if extend:
            run.normalize_data_1d_extend(str(qlib_dir))
        else:
            run.normalize_data()
        DumpDataUpdate(
            data_path=str(work / f"{tag}_normalize"),
            qlib_dir=str(qlib_dir),
            exclude_fields="symbol,date",
            max_workers=run.max_workers,
        ).dump()

    try:
        # 1. Current members already in the data: fetch from the data's last day to today
        last_day = (qlib_dir / "calendars" / "day.txt").read_text().split()[-1]
        current = sp500_members(qlib_dir, last_day) | set(INDEX_TICKERS)
        existing = sorted(c for c in current if has_features(qlib_dir, c))
        print(f"== Updating {len(existing)} symbols from {last_day} to before {today}")
        fetch(existing, last_day, "update", extend=True)

        # 2. Refresh the member list (backup kept next to it)
        if not args.skip_members:
            from data_collector.us_index.collector import SP500Index

            inst = qlib_dir / "instruments" / "sp500.txt"
            shutil.copy(inst, inst.with_name(f"sp500.txt.bak-{today}"))
            before = sp500_members(qlib_dir, "2099-12-31")
            make_sp500_index(SP500Index)(index_name="SP500", qlib_dir=str(qlib_dir)).parse_instruments()
            after = sp500_members(qlib_dir, "2099-12-31")
            print(f"== Member list refreshed: {len(after)} current members, "
                  f"added {len(after - before)}, removed {len(before - after)}")

        # 3. Members since the data's start that have no prices yet: fetch their full history
        new = sorted(c for c in sp500_members(qlib_dir, last_day) if not has_features(qlib_dir, c))
        if new:
            print(f"== Fetching full history for {len(new)} members without prices: {' '.join(new)}")
            fetch(new, args.history_start, "new", extend=False)

        missing = sorted(c for c in sp500_members(qlib_dir, "2099-12-31") if not has_features(qlib_dir, c))
        print(f"Done. Last day in data: {(qlib_dir / 'calendars' / 'day.txt').read_text().split()[-1]}. "
              f"Current members without Yahoo data: {' '.join(missing) or 'none'}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
