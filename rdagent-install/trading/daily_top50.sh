#!/usr/bin/env bash
# Daily top-50 list for an RD-Agent fin_factor result. Run after the market close.
# Setup and usage: README.md in this folder.
set -euo pipefail

REGION="${REGION:-cn}"                                         # cn (CSI 300) or us (S&P 500)
case "$REGION" in
  cn) DEFAULT_MARKET=csi300; DEFAULT_WORK_DIR="$HOME/rd-trading" ;;
  us) DEFAULT_MARKET=sp500; DEFAULT_WORK_DIR="$HOME/rd-trading-us" ;;
  *) echo "REGION must be cn or us" >&2; exit 1 ;;
esac
WORK_DIR="${WORK_DIR:-$DEFAULT_WORK_DIR}"                      # outputs, holdings.txt, scratch files
STRATEGY_DIR="${STRATEGY_DIR:-$WORK_DIR/strategy}"             # base_factors.json + factor .py files
LIVE_DATA="${LIVE_DATA:-$HOME/.qlib/qlib_data/${REGION}_data_live}"  # trading copy of the Qlib data
MARKET="${MARKET:-$DEFAULT_MARKET}"                            # Qlib universe to score
QLIB_SRC="${QLIB_SRC:-$HOME/qlib}"                             # Qlib source checkout (price collector)
HOLDINGS="${HOLDINGS:-$WORK_DIR/holdings.txt}"                 # what you hold now, one code per line
SKIP_UPDATE="${SKIP_UPDATE:-0}"                                # 1 = use the data as it is

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOP50="$HERE/top50.py"
mkdir -p "$WORK_DIR"
cd "$WORK_DIR"   # Qlib writes its ./mlruns here

# shellcheck disable=SC1091
source "$(conda info --base)/etc/profile.d/conda.sh"

if [ "$LIVE_DATA" = "$HOME/.qlib/qlib_data/cn_data" ] || [ "$LIVE_DATA" = "$HOME/.qlib/qlib_data/us_data" ]; then
  echo "LIVE_DATA must not be RD-Agent's own data folder. Use a separate copy (see README.md)." >&2
  exit 1
fi

if [ "$SKIP_UPDATE" != "1" ]; then
  echo "== 1/4 Updating prices in $LIVE_DATA"
  conda activate rdagent4qlib
  if [ "$REGION" = us ]; then
    # S&P 500 members only; Qlib's own US update fetches ~19,600 tickers (see us-market/)
    python "$HERE/../us-market/yahoo_update.py" --qlib-dir "$LIVE_DATA" --qlib-src "$QLIB_SRC"
  else
    rm -rf "$WORK_DIR/yahoo_source" "$WORK_DIR/yahoo_normalize"
    # Yahoo refuses an end date after today and excludes the end date, so this fetches up to
    # yesterday. That is why the cron jobs in README.md run after midnight.
    (cd "$QLIB_SRC/scripts/data_collector/yahoo" &&
      python collector.py update_data_to_bin \
        --source_dir "$WORK_DIR/yahoo_source" \
        --normalize_dir "$WORK_DIR/yahoo_normalize" \
        --region CN \
        --qlib_data_1d_dir "$LIVE_DATA" \
        --end_date "$(date +%F)")
  fi
  conda deactivate
fi
echo "Last trading day in data: $(tail -1 "$LIVE_DATA/calendars/day.txt")"

echo "== 2/4 Building daily_pv.h5"
conda activate rdagent4qlib
if [ "$REGION" = us ]; then PV_MARKET="$MARKET"; else PV_MARKET=all; fi  # as RD-Agent's generate.py
python "$TOP50" build-pv --data-dir "$LIVE_DATA" --out "$WORK_DIR/daily_pv.h5" \
  --region "$REGION" --market "$PV_MARKET"
conda deactivate

echo "== 3/4 Computing factors from $STRATEGY_DIR"
conda activate rdagent
python "$TOP50" factors --strategy-dir "$STRATEGY_DIR" --pv "$WORK_DIR/daily_pv.h5" \
  --out "$WORK_DIR/combined_factors.parquet"
conda deactivate

echo "== 4/4 Training the model and scoring today's stocks"
conda activate rdagent4qlib
python "$TOP50" score --data-dir "$LIVE_DATA" --strategy-dir "$STRATEGY_DIR" \
  --factors "$WORK_DIR/combined_factors.parquet" --holdings "$HOLDINGS" --out-dir "$WORK_DIR/picks" \
  --region "$REGION" --market "$MARKET"
conda deactivate
