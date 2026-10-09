"""Update a Qlib US data folder from Yahoo, limited to S&P 500 members.

Qlib's own `collector.py update_data_to_bin --region US` (at the commit RD-Agent pins) has four
problems for this use:

- it downloads every listed US ticker (about 19,600, many delisted or warrants), about two days;
- its S&P 500 member refresh reads a Wikipedia table that moved to "Historical components of the
  S&P 500" in August 2026, and crashes;
- members added since the data was built get only a few days of prices;
- Wikipedia writes BRK.B and BF.B, while Yahoo and Qlib use BRK-B and BF-B.

This script runs the same Qlib collector, normalizer and dumper, but:

1. refreshes instruments/sp500.txt from Wikipedia's current pages;
2. updates only stocks that are S&P 500 members since the data's last day (plus the ^GSPC, ^NDX and
   ^DJI indexes);
3. downloads the full history of members that have no prices in the data yet, or whose prices stop
   before the data's last day (a ticker reused by a different company, such as CEG).

Yahoo refuses an end date after today, and the end date is excluded, so the data reaches the last
trading day before today (VM time). Run it after midnight to include the previous session.

Run it in the rdagent4qlib env. It needs the Qlib source checkout and fake-useragent (see README.md).
"""

import argparse
import datetime
import os
import shutil
import struct
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


def last_price_index(qlib_dir, code):
    """Calendar index of the symbol's last close, or -1 if it has none."""
    f = qlib_dir / "features" / code.lower() / "close.day.bin"
    if not f.is_file() or f.stat().st_size < 8:
        return -1
    with f.open("rb") as fp:
        start = int(struct.unpack("<f", fp.read(4))[0])
    return start + f.stat().st_size // 4 - 2


def forget_symbols(qlib_dir, codes):
    """Delete the symbols' price folders and all.txt rows, so the dumper writes them as new stocks."""
    for code in codes:
        shutil.rmtree(qlib_dir / "features" / code.lower(), ignore_errors=True)
    all_txt = qlib_dir / "instruments" / "all.txt"
    lines = all_txt.read_text().splitlines()
    all_txt.write_text("".join(f"{line}\n" for line in lines if line.split("\t")[0].upper() not in codes))


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

    def fetch(symbols, start, tag, extend, replace=()):
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
        # Old prices of a reused ticker are replaced only once Yahoo has returned the new ones
        got = {f.stem.upper() for f in (work / f"{tag}_normalize").glob("*.csv")}
        forget_symbols(qlib_dir, {c for c in replace if c in got})
        DumpDataUpdate(
            data_path=str(work / f"{tag}_normalize"),
            qlib_dir=str(qlib_dir),
            exclude_fields="symbol,date",
            max_workers=run.max_workers,
        ).dump()

    try:
        last_day = (qlib_dir / "calendars" / "day.txt").read_text().split()[-1]

        # 1. Refresh the member list first (backup kept next to it), so members added since the
        #    data was built are updated too. If Wikipedia fails, the old list is used.
        if not args.skip_members:
            from data_collector.us_index.collector import SP500Index

            inst = qlib_dir / "instruments" / "sp500.txt"
            backup = inst.with_name(f"sp500.txt.bak-{today}")
            shutil.copy(inst, backup)
            before = sp500_members(qlib_dir, "2099-12-31")
            try:
                make_sp500_index(SP500Index)(index_name="SP500", qlib_dir=str(qlib_dir)).parse_instruments()
                after = sp500_members(qlib_dir, "2099-12-31")
                print(f"== Member list refreshed: {len(after)} current members, "
                      f"added {len(after - before)}, removed {len(before - after)}")
            except Exception as e:  # keep updating prices with the old list
                shutil.copy(backup, inst)
                print(f"WARNING: member refresh failed ({e!r}); using the old sp500.txt")

        # 2. Members whose prices reach the data's last day: fetch from that day to today
        members = sp500_members(qlib_dir, last_day)
        last_idx = len((qlib_dir / "calendars" / "day.txt").read_text().split()) - 1
        current = [c for c in sorted(members | set(INDEX_TICKERS)) if has_features(qlib_dir, c)]
        existing = [c for c in current if last_price_index(qlib_dir, c) >= last_idx]
        print(f"== Updating {len(existing)} symbols from {last_day} to before {today}")
        fetch(existing, last_day, "update", extend=True)

        # 3. Members with no prices, or with older prices that stop before the last day (a ticker
        #    reused by a different company, such as CEG): fetch their full history
        stale = sorted(set(current) - set(existing) - set(INDEX_TICKERS))
        new = sorted(c for c in members if not has_features(qlib_dir, c))
        if new or stale:
            print(f"== Fetching full history for {len(new)} members without prices: {' '.join(new)}")
            print(f"   and {len(stale)} whose prices stop before {last_day}: {' '.join(stale)}")
            fetch(new + stale, args.history_start, "new", extend=False, replace=set(stale))

        days = (qlib_dir / "calendars" / "day.txt").read_text().split()
        missing = sorted(
            c for c in sp500_members(qlib_dir, "2099-12-31") if last_price_index(qlib_dir, c) < len(days) - 1
        )
        print(f"Done. Last day in data: {days[-1]}. "
              f"Current members without prices on that day: {' '.join(missing) or 'none'}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
