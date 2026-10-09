# RD-Agent + Qlib on Ubuntu with OpenRouter: tested installation guide

This guide installs Microsoft [RD-Agent](https://github.com/microsoft/RD-Agent) with its
[Qlib](https://github.com/microsoft/qlib) backend on Ubuntu. It uses OpenRouter for the chat and
embedding models and Qlib's default China A-share data. It was written by reading the RD-Agent source
and then running every step on a clean Ubuntu machine, including both web UIs.

| Tested with | Version |
|---|---|
| OS | Ubuntu 24.04 (22.04 works the same way) |
| RD-Agent | `v1.0.0` (commit `484776c`) |
| Qlib (pinned by RD-Agent) | commit `2fb9380` |
| Python | 3.11 for RD-Agent, 3.10 for the Qlib runtime env |
| LLM gateway | OpenRouter via LiteLLM (`openrouter/...` model names) |

> **Follow the steps in order and copy the commands exactly.** Almost every command matters. The
> [Why the official instructions fail](#why-the-official-instructions-fail) section explains why.

---

## What you end up with

```
~/RD-Agent/                  RD-Agent source (run every rdagent command from here)
  .env                       your OpenRouter settings
conda env "rdagent"          Python 3.11: RD-Agent itself + generated factor code
conda env "rdagent4qlib"     Python 3.10: Qlib backtests (qrun) + generated PyTorch models
docker image "local_qlib"    used once, to build RD-Agent's factor source data from Qlib
~/.qlib/qlib_data/cn_data    Qlib default daily CN data (1999-2020)
```

**Requirements**

- Ubuntu 22.04 or 24.04, x86_64, with a user that has `sudo`.
- **At least 45 GB free disk.** The Docker image alone is about 17 GB.
- 16 GB RAM is recommended; 8 GB works but backtests are slow.
- No GPU needed.
- An OpenRouter API key (`sk-or-v1-...`) with some credit.

---

## Step 1: System packages

```bash
sudo apt update
sudo apt install -y build-essential git curl ca-certificates
```

`build-essential` is required: Qlib compiles C extensions when it is installed.

## Step 2: Docker (runnable without sudo)

Skip the install line if `docker --version` already works.

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
newgrp docker            # or log out and back in
docker run --rm hello-world
```

The last command must print "Hello from Docker!" **without sudo**. RD-Agent calls Docker as your user.

## Step 3: Miniconda and the conda Terms of Service

RD-Agent calls `conda run -n <env>` internally, so **conda is mandatory**. A plain venv breaks factor
execution.

```bash
curl -fsSLo ~/miniconda.sh https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash ~/miniconda.sh -b -p "$HOME/miniconda3"
"$HOME/miniconda3/bin/conda" init bash
source ~/.bashrc          # or close and reopen the terminal

# New Miniconda refuses to create envs until the ToS is accepted:
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r
```

If you already had conda and `conda tos` says "invalid choice", your conda is older and does not need
this step.

## Step 4: Get RD-Agent and this guide's files

```bash
cd ~
git clone --branch v1.0.0 --depth 1 https://github.com/microsoft/RD-Agent.git
git clone https://github.com/vijayvmane11-afk/Test-2026-16-09.git ~/rdagent-guide
```

`~/rdagent-guide/rdagent-install/` contains:

- this guide;
- `constraints-py311.txt` and `constraints-qlib-py310.txt`, the exact package versions tested for the
  two conda envs;
- `check_llm.py`, an LLM connectivity test;
- `trading/`, a daily job that turns a `fin_factor` result into a top-50 list (see
  [Trading a result daily](#trading-a-result-daily)).
- `us-market/`, scripts that switch RD-Agent from China to the US market (see
  [Switching to the US market](#switching-to-the-us-market)).

## Step 5: The `rdagent` environment (Python 3.11)

```bash
conda create -y -n rdagent python=3.11
conda activate rdagent
cd ~/RD-Agent
pip install --upgrade pip
pip install -e . -c ~/rdagent-guide/rdagent-install/constraints-py311.txt
```

> **Use Python 3.11, not 3.10** as RD-Agent's README says. Its `constraints/3.10.txt` pins
> `litellm==1.97.0`, and on Python 3.10 that version fails **every** LLM call with
> ``PydanticUserError: `Message` is not fully defined``.

Check it:

```bash
rdagent --help            # lists fin_factor, fin_model, fin_quant, ui, server_ui, health_check ...
```

## Step 6: The `rdagent4qlib` environment (Python 3.10)

By default RD-Agent runs every Qlib backtest and every generated model in a conda env named
`rdagent4qlib`. If that env is missing, RD-Agent tries to create it silently during your first run.
That hidden setup takes several minutes, pulls about 5 GB of CUDA libraries, and currently produces a
**broken** env (see [Why the official instructions fail](#why-the-official-instructions-fail)). So
create it yourself:

```bash
conda create -y -n rdagent4qlib python=3.10
conda activate rdagent4qlib
C=~/rdagent-guide/rdagent-install/constraints-qlib-py310.txt
pip install --upgrade pip cython -c $C
pip install "git+https://github.com/microsoft/qlib.git@2fb9380b342556ddb50a4b24e4fe8655d548b2b8" -c $C
pip install "mlflow<3.13" catboost xgboost tables -c $C
pip install torch==2.9.1 --index-url https://download.pytorch.org/whl/cpu
conda deactivate
```

- The Qlib commit is the one RD-Agent `v1.0.0` pins.
- **`mlflow<3.13` is essential.** Newer MLflow refuses Qlib's `./mlruns` file store (`MlflowException:
  The filesystem tracking backend ... is in maintenance mode`), which makes **every** backtest fail.
  Upstream Qlib added this pin later; RD-Agent's pinned commit predates it.
- The CPU build of torch is small and enough for the default data. If you have an NVIDIA GPU and want
  GPU training, use `pip install torch==2.9.1` instead (about 5 GB larger).

Check it:

```bash
conda run -n rdagent4qlib python -c "import qlib, torch, mlflow; print('qlib', qlib.__version__, '| torch', torch.__version__, '| mlflow', mlflow.__version__)"
# expected: mlflow 3.12.x (anything below 3.13)
```

## Step 7: Qlib default data

RD-Agent expects the data in exactly this folder:

```bash
mkdir -p ~/.qlib/qlib_data/cn_data
curl -fL -o /tmp/qlib_data_cn_1d_latest.zip \
  https://github.com/SunsetWolf/qlib_dataset/releases/download/v0/qlib_data_cn_1d_latest.zip
conda run -n rdagent python -m zipfile -e /tmp/qlib_data_cn_1d_latest.zip ~/.qlib/qlib_data/cn_data/
rm /tmp/qlib_data_cn_1d_latest.zip
ls ~/.qlib/qlib_data/cn_data                          # calendars  features  instruments
tail -1 ~/.qlib/qlib_data/cn_data/calendars/day.txt   # 2020-09-25
```

This is the same file Qlib's own `get_data` script downloads: 196 MB, 334 MB unpacked. The old
`qlibpublic.blob.core.windows.net` links in many tutorials no longer work.

## Step 8: Build the Qlib Docker image

On the first `fin_factor` run, RD-Agent builds this image behind a silent spinner. The build takes
10-20 minutes and looks hung. Build it now so you can see progress:

```bash
cd ~/RD-Agent
docker build -t local_qlib:latest rdagent/scenarios/qlib/docker
docker images local_qlib          # ~17 GB
```

Later runs reuse Docker's cache, so RD-Agent's own rebuild takes seconds.

## Step 9: Configure OpenRouter

```bash
cd ~/RD-Agent
cat > .env <<'EOF'
BACKEND=rdagent.oai.backend.LiteLLMAPIBackend

# Models: keep the "openrouter/" prefix, then OpenRouter's model id
CHAT_MODEL=openrouter/openai/gpt-4o
EMBEDDING_MODEL=openrouter/openai/text-embedding-3-small

# Used by the agent
OPENROUTER_API_KEY=sk-or-v1-REPLACE_ME

# Used only by `rdagent health_check`, which does not understand OPENROUTER_* (same key)
OPENAI_API_KEY=sk-or-v1-REPLACE_ME
OPENAI_API_BASE=https://openrouter.ai/api/v1
EOF
chmod 600 .env
nano .env        # paste your real key in both places
```

Notes:

- **Always run `rdagent` from `~/RD-Agent`.** It reads `./.env` from the current directory, and it
  writes `log/`, `git_ignore_folder/` and `pickle_cache/` there.
- Do not set `MODEL_COSTEER_ENV_TYPE=docker`. In v1.0.0 it crashes `fin_model` and `fin_quant` at
  startup (`StopIteration` in `QTDockerEnv.prepare`). Leave the default (conda).
- **Choosing a chat model.** It must support JSON output. It should also be in LiteLLM's model list,
  so RD-Agent knows its context size and structured-output support. Check a name with:

  ```bash
  conda run -n rdagent python -c "from litellm import get_model_info as g, supports_response_schema as s; m='openrouter/openai/gpt-4o'; print(m, g(m)['max_input_tokens'], s(model=m))"
  ```

  Known-good names include `openrouter/openai/gpt-4o`, `openrouter/openai/gpt-4.1`,
  `openrouter/openai/gpt-4o-mini` (cheaper) and `openrouter/deepseek/deepseek-chat-v3.1`.
- If you pick a reasoning model that returns `<think>...</think>` text, add `REASONING_THINK_RM=True`.

## Step 10: Verify before the first run

```bash
conda activate rdagent
cd ~/RD-Agent
rdagent health_check
python ~/rdagent-guide/rdagent-install/check_llm.py
```

`health_check` must end with `✅ All tests completed.`, `The docker status is normal` and
`Port 19899 is not occupied`. `check_llm.py` must print three `[PASS]` lines: chat, JSON mode and
embedding. It uses RD-Agent's own LLM code, so if it passes, the agent's LLM calls will work.

## Step 11: First run

```bash
conda activate rdagent
cd ~/RD-Agent
rdagent fin_factor --loop-n 1
```

The CLI options use dashes: `--loop-n`, not `--loop_n`. What happens on the first run:

1. Docker builds the factor source data from `~/.qlib` (about 1 minute, first time only). The result
   goes to `git_ignore_folder/factor_implementation_source_data*`.
2. The LLM proposes a hypothesis and factors.
3. The generated factor code runs in `rdagent`.
4. The LLM reviews the code.
5. The baseline and new-factor LightGBM backtests run in `rdagent4qlib` (about 1-2 minutes each).
6. The LLM writes feedback on the results.

The run exits with code 0 when done. The trace is saved in `log/<timestamp>/`.

The other Qlib scenarios work the same way:

```bash
rdagent fin_model --loop-n 1     # model evolution (trains a generated PyTorch model)
rdagent fin_quant --loop-n 1     # factor + model joint evolution
```

To run for a time budget instead of a loop count, use `--all-duration 2h`.

## Step 12: UI 1, the Streamlit log viewer (`rdagent ui`)

This UI shows runs started from the CLI (the `log/` folder).

```bash
conda activate rdagent
cd ~/RD-Agent
rdagent ui --port 19899 --log-dir log/
```

1. Open <http://localhost:19899>.
2. In the left sidebar, choose your run under **Select from log**. `health_check` and `check_llm.py`
   also create small folders there; pick the one from your `fin_*` run.
3. Click **All Loops**.

You get Metrics charts (baseline vs. each round), a Hypotheses table, and Research, Development and
Feedback panels for each loop. Stop the UI with Ctrl-C.

## Step 13: UI 2, the Web UI (`rdagent server_ui`)

This UI can **start runs from the browser** and stream them live. It only lists runs started from the
Web UI itself (stored in `git_ignore_folder/traces/`); use Step 12 for CLI runs.

**13a. Node.js 22.** The frontend uses Vite 8, which needs Node ≥ 20.19. Ubuntu's own `nodejs`
package is too old: v12 on 22.04, v18 on 24.04.

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
source ~/.bashrc          # or close and reopen the terminal
nvm install 22
node --version            # v22.x
```

**13b. Build the frontend.** Plain `npm install` fails with `ERESOLVE` because RD-Agent's
`package.json` combines vite 8 with `@vitejs/plugin-vue` 5, so `--legacy-peer-deps` is required:

```bash
cd ~/RD-Agent/web
npm ci --legacy-peer-deps
npm run build:flask       # writes to ~/RD-Agent/git_ignore_folder/static
```

**13c. Start the server** (from `~/RD-Agent`, in the `rdagent` env):

```bash
conda activate rdagent
cd ~/RD-Agent
export UI_SERVER_AUTH_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
echo "Open: http://127.0.0.1:19899/?token=$UI_SERVER_AUTH_TOKEN"
rdagent server_ui --port 19899
```

**13d. Use it.**

1. Open the printed URL once. The token is then stored in a cookie.
2. Go to **Playground**, then **Select a scenario for your analysis**.
3. Choose the scenario **Finance Data Building** (this is `fin_factor`) and a loop count, then click
   **GENERATE**.
4. A **"User Interaction Required"** dialog appears. Click **SKIP**, or type an instruction and click
   **SUBMIT**. **The run waits indefinitely until you answer.**
5. To stop answering prompts, switch on **Auto Skip Interaction** (bottom-left).

The **PROCESS** tab streams research, code and feedback live. **RESULT** shows the metrics and lets you
download the generated factor files.

Only one UI can use port 19899 at a time. Give the other UI a different `--port`.

### Opening the UIs on a remote server (e.g. an Azure or AWS VM)

Both UIs speak **plain HTTP only**. If the browser uses `https://`, the server gets TLS bytes it cannot
read and closes the connection. Chrome then shows **`ERR_EMPTY_RESPONSE`** ("didn't send any data").

**Option A: SSH tunnel (recommended; no firewall changes, nothing exposed).** On your laptop:

```bash
ssh -L 19899:localhost:19899 you@<server-ip>
```

Keep that SSH session open, and open `http://localhost:19899` (Streamlit) or
`http://localhost:19899/?token=<token>` (Web UI) in your laptop's browser.

**Option B: direct access by IP.** All of these are needed:

1. Make the server listen on all interfaces:
   - Web UI: `rdagent server_ui --port 19899 --host 0.0.0.0` (the default is 127.0.0.1 only).
   - Streamlit: `rdagent ui` already listens on all interfaces.
2. Open TCP port 19899 in the cloud firewall. On Azure: VM → Networking → Add inbound port rule,
   destination port 19899, ideally with the source restricted to your own IP.
3. Type the address with an explicit `http://`, e.g. `http://<server-ip>:19899/?token=<token>`. If
   Chrome still switches to https, turn off "Always use secure connections" in Chrome's security
   settings, or use Option A.

The Web UI token gives full control of the agent, so keep it secret. Don't leave the port open to
the whole internet.

---

## Daily use

```bash
conda activate rdagent
cd ~/RD-Agent
rdagent fin_factor --loop-n 5                 # or fin_model / fin_quant
rdagent ui --port 19899 --log-dir log/        # view results
```

Resume an interrupted run from its saved session:
`rdagent fin_factor --path log/<run>/__session__/<loop>/<step>`.

## Trading a result daily

RD-Agent never updates prices and never re-runs a finished result on new data. Every loop
backtests the same fixed dates (2017-01-01 to 2020-08-01 by default), so running it around the clock
gives you better research, not a list of stocks to trade today. To trade a `fin_factor` result, use
[`trading/`](trading/README.md). After each close it updates a separate copy of the prices,
recomputes your factors, retrains the model and prints tomorrow's sells and buys. It can run while
RD-Agent is running.

## Switching to the US market

RD-Agent's Qlib scenarios are hardcoded to China (CSI 300, China costs and price limits). To research
the S&P 500 instead, follow [`us-market/`](us-market/README.md). It downloads and repairs Qlib's US
data, switches RD-Agent's templates with one command (`switch_market.sh us`, undone with
`switch_market.sh cn`), and lists the date settings for a fresh run. The trading job takes `REGION=us`
for US results.

---

## Why the official instructions fail

These are the problems found in RD-Agent v1.0.0, and what this guide does about each one:

| # | Problem | Symptom | Fixed in step |
|---|---|---|---|
| 1 | README says Python 3.10; `constraints/3.10.txt` pins `litellm==1.97.0`, which is broken on 3.10 | Every LLM call fails: ``Message is not fully defined`` | 5 (Python 3.11) |
| 2 | Qlib runtime installs the newest MLflow, which rejects Qlib's file store | Every backtest fails: `MlflowException ... maintenance mode`, then `FactorEmptyError`/`CoderError` | 6 (`mlflow<3.13`) |
| 3 | Backtests run in the conda env `rdagent4qlib` by default, which the README never mentions; the auto-created env is built mid-run and can end up half-installed | Long silent stall on first run; `ModuleNotFoundError: No module named 'qlib'` or `torch` | 6 |
| 4 | Web UI runs always validate features in `rdagent4qlib`; the first auto-creation fails validation because of a bug | "Base feature validation failed. Asking user to revise." | 6 |
| 5 | `MODEL_COSTEER_ENV_TYPE=docker` (suggested in the docs) is broken for model scenarios | `fin_model`/`fin_quant` crash with `StopIteration` | 9 (keep conda) |
| 6 | Fresh Miniconda requires accepting the ToS; RD-Agent's internal `conda create` cannot answer the prompt | `CondaToSNonInteractiveError` | 3 |
| 7 | `rdagent health_check` only recognizes `OPENAI_*`/`DEEPSEEK_*` variables | "No valid configuration was found", then a crash | 9 (duplicate key as `OPENAI_*`) |
| 8 | `web/package.json` peer-dependency conflict | `npm install` fails with `ERESOLVE` | 13b |
| 9 | The frontend needs Node ≥ 20.19 | Vite build errors | 13a |
| 10 | Web UI runs wait for interactive input | Web UI run "hangs" | 13d |
| 11 | First Docker build is silent for 10-20 min | Looks frozen | 8 |
| 12 | Old Qlib data URLs (Azure blob) are dead | Data download fails | 7 |
| 13 | Unpinned dependencies drift over time | "Worked last month, fails today" | 5 (constraints file) |

## Troubleshooting

| Symptom | Fix |
|---|---|
| ``PydanticUserError: `Message` is not fully defined`` | You are on Python 3.10. Recreate the `rdagent` env with `python=3.11` (Step 5). |
| `MlflowException: The filesystem tracking backend ... maintenance mode` | `conda run -n rdagent4qlib pip install "mlflow<3.13"` |
| `No module named 'qlib'` / `'torch'` inside a run | `rdagent4qlib` is missing or half-built. Run `conda env remove -n rdagent4qlib`, then redo Step 6. |
| `permission denied ... /var/run/docker.sock` | Your user is not in the `docker` group. Redo Step 2 and log out and back in. |
| `CondaToSNonInteractiveError` | Run the two `conda tos accept` commands from Step 3. |
| `AuthenticationError` / 401 from OpenRouter | Check the key in both places in `.env`, and that your OpenRouter account has credit. |
| `No such option: --loop_n` | Use dashes: `--loop-n`. |
| Changed `.env` but nothing changed | You ran `rdagent` from another folder. Always `cd ~/RD-Agent` first. |
| Web UI shows "UI_SERVER_AUTH_TOKEN must be configured" | Export the token in the same shell before `rdagent server_ui`. |
| Browser shows `ERR_EMPTY_RESPONSE` / "didn't send any data" | You used `https://`. The UIs are HTTP only: use `http://<ip>:19899`, or an SSH tunnel (see "Opening the UIs on a remote server"). |
| Browser shows `ERR_CONNECTION_REFUSED` from another machine | The Web UI listens on 127.0.0.1 by default. Start it with `--host 0.0.0.0`, or use an SSH tunnel. |
| Browser spins, then times out, from another machine | The cloud firewall (e.g. Azure NSG) is blocking the port. Add an inbound rule for 19899, or use an SSH tunnel. |
| Web UI page is blank / 404 | Run `npm run build:flask` (Step 13b), and start the server from `~/RD-Agent`. |
| Want a completely fresh start | `cd ~/RD-Agent && rm -rf log git_ignore_folder/RD-Agent_workspace git_ignore_folder/traces pickle_cache prompt_cache.db` |

## Uninstall

```bash
conda env remove -n rdagent
conda env remove -n rdagent4qlib
docker rmi local_qlib:latest
rm -rf ~/RD-Agent ~/.qlib ~/rdagent-guide
```
