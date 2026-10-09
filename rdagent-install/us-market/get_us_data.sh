#!/usr/bin/env bash
# Download Qlib's US daily data, repair it for the Qlib commit RD-Agent pins, and optionally bring
# it up to date from Yahoo. Setup and usage: README.md in this folder.
set -euo pipefail

US_DATA="${US_DATA:-$HOME/.qlib/qlib_data/us_data}"   # where RD-Agent will read US data
QLIB_SRC="${QLIB_SRC:-$HOME/qlib}"                    # Qlib source checkout (price collector)
URL="https://github.com/SunsetWolf/qlib_dataset/releases/download/v0/qlib_data_us_1d_latest.zip"

# shellcheck disable=SC1091
source "$(conda info --base)/etc/profile.d/conda.sh"

case "${1:-}" in
  download)
    if [ -d "$US_DATA/features" ]; then
      echo "$US_DATA already has data. Delete it first to download again." >&2
      exit 1
    fi
    mkdir -p "$US_DATA"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    echo "== Downloading Qlib US data (about 450 MB)"
    curl -fL --progress-bar -o "$tmp/us.zip" "$URL"
    conda activate rdagent
    python -m zipfile -e "$tmp/us.zip" "$US_DATA/"
    conda deactivate
    bash "$0" fix
    ;;

  fix)
    # The zip was written by a newer Qlib: feature folders are named "_aapl" and member lists have a
    # 4th column. The pinned Qlib then finds no data at all. Both fixes are safe to repeat.
    echo "== Repairing the format of $US_DATA"
    renamed=0
    for d in "$US_DATA"/features/_*; do
      [ -e "$d" ] || continue
      base="$(basename "$d")"
      mv "$d" "$US_DATA/features/${base#_}"
      renamed=$((renamed + 1))
    done
    for f in "$US_DATA"/instruments/*.txt; do
      cut -f1-3 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    done
    echo "Renamed $renamed feature folders; member lists cut to 3 columns."
    conda activate rdagent4qlib
    python - "$US_DATA" <<'PY'
import sys
import qlib
from qlib.data import D

qlib.init(provider_uri=sys.argv[1], region="us")
cal = D.calendar(freq="day")
n = len(D.list_instruments(D.instruments("sp500"), as_list=True))
spx = D.features(["^gspc"], ["$close"], start_time=cal[-20], end_time=cal[-1])
assert n > 0 and not spx.empty, "Qlib still reads no data"
print(f"OK: calendar {cal[0].date()} to {cal[-1].date()}, {n} S&P 500 members, ^gspc readable")
PY
    conda deactivate
    ;;

  update)
    # S&P 500 members only, plus a member-list refresh and full history for new members (see
    # yahoo_update.py). The first run fills 2020-11 to yesterday: about an hour.
    echo "== Updating $US_DATA from Yahoo"
    conda activate rdagent4qlib
    python "$(dirname "$0")/yahoo_update.py" --qlib-dir "$US_DATA" --qlib-src "$QLIB_SRC"
    conda deactivate
    echo "Last trading day in data: $(tail -1 "$US_DATA/calendars/day.txt")"
    ;;

  *)
    echo "usage: $0 download|fix|update" >&2
    exit 2
    ;;
esac
