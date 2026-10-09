#!/usr/bin/env bash
# Point RD-Agent's Qlib scenarios (fin_factor, fin_model, fin_quant) at the US or back at China.
# Setup and usage: README.md in this folder.
set -euo pipefail

RDAGENT_DIR="${RDAGENT_DIR:-$HOME/RD-Agent}"
US_DATA="${US_DATA:-$HOME/.qlib/qlib_data/us_data}"
EXP=rdagent/scenarios/qlib/experiment

# Every file RD-Agent v1.0.0 hardcodes China in
TEMPLATES=(
  "$EXP/factor_template/conf_baseline.yaml"
  "$EXP/factor_template/conf_combined_factors.yaml"
  "$EXP/factor_template/conf_combined_factors_sota_model.yaml"
  "$EXP/model_template/conf_baseline_factors_model.yaml"
  "$EXP/model_template/conf_sota_factors_model.yaml"
)
GENERATE="$EXP/factor_data_template/generate.py"
PROMPTS="$EXP/prompts.yaml"
FILES=("${TEMPLATES[@]}" "$GENERATE" "$PROMPTS")

target="${1:-}"
if [ "$target" != us ] && [ "$target" != cn ]; then
  echo "usage: $0 us|cn" >&2
  exit 2
fi

cd "$RDAGENT_DIR"

if pgrep -f "rdagent (fin_factor|fin_model|fin_quant)" > /dev/null; then
  echo "An RD-Agent fin_* run is active. Stop it first (Ctrl+C in its terminal)." >&2
  exit 1
fi

clear_market_caches() {
  # Price files and cached backtests built for the previous market. RD-Agent rebuilds the price
  # files with Docker on the next run (about a minute). Logs and old runs are kept.
  rm -rf git_ignore_folder/factor_implementation_source_data \
    git_ignore_folder/factor_implementation_source_data_debug \
    "$EXP"/factor_data_template/daily_pv_all.h5 \
    "$EXP"/factor_data_template/daily_pv_debug.h5 \
    pickle_cache
  echo "Cleared the previous market's price files and pickle_cache/."
}

if [ "$target" = cn ]; then
  git checkout -- "${FILES[@]}"
  clear_market_caches
  echo "RD-Agent is back on China (cn_data, csi300, SH000300)."
  exit 0
fi

if grep -q "us_data" "$GENERATE"; then
  echo "RD-Agent is already switched to the US. To redo it: $0 cn, then $0 us."
  exit 0
fi
if ! git diff --quiet -- "${FILES[@]}"; then
  echo "These files have local edits. Commit or revert them first:" >&2
  git diff --stat -- "${FILES[@]}" >&2
  exit 1
fi
if [ ! -f "$US_DATA/instruments/sp500.txt" ] || compgen -G "$US_DATA/features/_*" > /dev/null; then
  echo "US data missing or not repaired at $US_DATA. Run get_us_data.sh download first." >&2
  exit 1
fi

# Universe, benchmark and costs. US costs are an assumption (commission-free broker plus
# slippage); edit open_cost/close_cost/min_cost below to match your broker.
sed -i \
  -e 's#~/.qlib/qlib_data/cn_data#~/.qlib/qlib_data/us_data#' \
  -e 's#^\(    region:\) cn$#\1 us#' \
  -e 's#^market: &market csi300$#market: \&market sp500#' \
  -e 's#^benchmark: &benchmark SH000300$#benchmark: \&benchmark "^gspc"#' \
  -e 's#limit_threshold: 0.095$#limit_threshold: null#' \
  -e 's#close_cost: 0.0015$#close_cost: 0.0005#' \
  -e 's#min_cost: 5$#min_cost: 0#' \
  "${TEMPLATES[@]}"

# Price file the generated factor code reads, limited to stocks that have ever been S&P 500 members.
# All 9,000 US symbols would be several times larger and mostly outside the traded universe.
cat > "$GENERATE" <<'GEN'
import qlib

qlib.init(provider_uri="~/.qlib/qlib_data/us_data", region="us")

from qlib.data import D

# US version written by rdagent-guide's us-market/switch_market.sh.
# Every stock that has ever been in the S&P 500, with its full price history.
instruments = D.list_instruments(D.instruments("sp500"), as_list=True)
fields = ["$open", "$close", "$high", "$low", "$volume", "$factor"]
data = D.features(instruments, fields, freq="day").swaplevel().sort_index().loc["2008-12-29":].sort_index()

data.to_hdf("./daily_pv_all.h5", key="data")

# 100 stocks over 2018-2019 for RD-Agent's quick debug runs, as in the China version
debug = data.loc["2018-01-01":"2019-12-31"]
keep = debug.index.get_level_values("instrument").unique()[:100]
debug = debug[debug.index.get_level_values("instrument").isin(keep)]

debug.to_hdf("./daily_pv_debug.h5", key="data")
GEN

# Experiment-setting tables the LLM reads
sed -i -e 's#| CSI300  |#| SP500   |#' "$PROMPTS"

# Every China value must be gone, and every US value present, or the switch is undone.
fail=0
if grep -nE "cn_data|region: cn|csi300|SH000300|0\.095|0\.0015|min_cost: 5|CSI300" "${FILES[@]}"; then
  fail=1
fi
for f in "${TEMPLATES[@]}"; do
  for want in "us_data" "region: us" "&market sp500" '"^gspc"' "limit_threshold: null" "close_cost: 0.0005" "min_cost: 0"; do
    grep -qF -- "$want" "$f" || { echo "$f: missing $want" >&2; fail=1; }
  done
done
grep -qF 'D.instruments("sp500")' "$GENERATE" || { echo "$GENERATE: missing sp500 filter" >&2; fail=1; }
if [ "$fail" = 1 ]; then
  git checkout -- "${FILES[@]}"
  echo "The switch did not apply cleanly (RD-Agent version differs?). Nothing was changed." >&2
  exit 1
fi

clear_market_caches
git diff --stat -- "${FILES[@]}"
cat <<MSG

RD-Agent now uses US data (sp500, benchmark ^gspc). Before the next run, set the dates in
$RDAGENT_DIR/.env (see README.md in this folder), then start a fresh run, for example:
  cd $RDAGENT_DIR && rdagent fin_factor --loop-n 10
Switch back with: $0 cn
MSG
