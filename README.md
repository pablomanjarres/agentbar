<h1 align="center">agentbar</h1>

<p align="center"><em>One macOS menu bar item for Claude Code and OpenAI Codex: how much of your rate-limit window is gone, and what the tokens would have cost on the API.</em></p>

<p align="center">
  <img alt="Python 3" src="https://img.shields.io/badge/Python_3-3776AB?style=flat&logo=python&logoColor=white" />
  <img alt="macOS" src="https://img.shields.io/badge/macOS-000000?style=flat&logo=apple&logoColor=white" />
  <img alt="SwiftBar" src="https://img.shields.io/badge/SwiftBar-plugin-1E1E1E?style=flat" />
  <img alt="License MIT" src="https://img.shields.io/badge/license-MIT-c8542a?style=flat" />
  <img alt="status shipped" src="https://img.shields.io/badge/status-shipped-success?style=flat" />
  <a href="https://pablomanjarres.com/portfolio/projects/agentbar"><img alt="Portfolio" src="https://img.shields.io/badge/portfolio-pablomanjarres.com-c8542a?style=flat" /></a>
  <a href="https://pablomanjarres.com/oss/agentbar"><img alt="Landing" src="https://img.shields.io/badge/landing-pablo--oss-c8542a?style=flat" /></a>
</p>

<p align="center"><img src="https://pablomanjarres.com/portfolio/previews/agentbar.png" alt="agentbar in the macOS menu bar" width="720" /></p>

If you run Claude Code and Codex side by side, the thing you actually want to know is which one is about to hit a wall, and how much the day cost. Both tools keep that on disk already, in different shapes, and neither surfaces it while you work. agentbar is a single SwiftBar plugin that reads both and puts them in one menu bar item: percentage of each rate-limit window burned, when it resets, and API-equivalent spend for today, this month and all time. It is one file of stdlib Python with no runtime dependencies of its own.

## Highlights

- **Both agents live on one title line, not a cycle.** The title renders `➊ 41%·77%  ✳ 12%`: the active Claude account with its 5h and 7d windows, then Codex's worst window. The warning color tracks whichever agent is closest to its limit, so a red menu bar always means something is actually about to stop working.
- **Codex gauges come from the account, not from parsing logs.** `fetch_codex_usage` reads `chatgpt.com/backend-api/wham/usage` with the token in `~/.codex/auth.json`. Every documented `/backend-api/codex/*` usage path returns 403; that one is what the CLI itself calls, and it needs the `originator` header. It returns the plan, an account-level window pair, and per-model buckets like `GPT-5.3-Codex-Spark` that carry their own 5h window.
- **The token walk survives compaction and repeated events.** Codex writes `info.total_token_usage` as a running total per session, and a compaction resets it mid-file. `parse_rollout` differences consecutive readings and treats a decrease as a fresh baseline. Summing the sibling `last_token_usage` instead double-counts: 68,437,053 tokens against a true 66,512,469 on one real session. The test suite asserts the walk reproduces each session's final cumulative figure exactly.
- **A model with no price is a warning, not a silent zero.** `gpt-5.3-codex-spark` is a ChatGPT-Pro-only research preview with no API price at all, so agentbar ships no number for it and says so in the menu. The aggregators that quote a Spark price are copying its parent model's row. The same guard caught a real gap on the Claude side, where ccusage's bundled price snapshot silently valued every Opus 5 token at $0.
- **Claude totals do not shrink when transcripts age out.** Claude Code deletes JSONL transcripts after `cleanupPeriodDays`, and ccusage recomputes from whatever still exists, so its lifetime totals used to fall over time. `update_ledger` keeps a per-day high-water mark in `.cache/cost-ledger.json` and month and all-time are summed from that instead.
- **The Codex pet lives in the menu bar and reacts to your limits.** Codex ships desktop pets, so agentbar wears one. `asar_lookup` parses the Electron archive inside your own Codex.app with four `struct.unpack` calls and pulls out Seedy's sprite sheet, no node and no bundled copy, then `pet_icon` crops the frame that matches your state: idle when you have headroom, sitting at his laptop while you are burning, shouting when a window is nearly gone. The art belongs to OpenAI, so it is read from your install and cached, never committed here, and a test enforces that.
- **Scanning is incremental and versioned.** Rollout transcripts are cached one entry per file, keyed on `(mtime, size)`, so a 1 minute refresh re-reads only sessions that changed: 0.20s cold over 41 transcripts, 0.00s warm. The cache carries a schema version because neither mtime nor size changes when the parser does, and without it a fix would keep serving the old numbers forever.

## How it works

```text
agentbar/
├── agentbar.1m.py     # the whole plugin: SwiftBar renderer + both data lanes
│                      #   fast lane  (50s): ccusage daily/blocks, Codex rollout scan
│                      #   slow lane (15m): Claude OAuth credits, Console cost report,
│                      #                    Codex wham/usage
└── tests/
    ├── test_codex.py     # pricing, delta walk, window dedupe, scan cache
    ├── test_degraded.py  # renders the menu with each agent missing
    └── test_pet.py       # asar lookup, mood mapping, and no art in the repo
```

SwiftBar runs the file once a minute and renders whatever it prints. Everything expensive sits behind a TTL in `~/.swiftbar/.cache/stats.json`, split into a fast local lane and a slow network lane, so the common refresh touches no network at all. The same file re-invokes itself with an argument to handle menu clicks (`switch`, `refresh-stats`, `rebuild-ledger`, `pause-auto`).

Both agents are drawn by the same code. `print_gauges` takes `(label, window)` pairs from either side and sizes its label column to the widest one, which is why Codex's longer bucket names line up with Claude's `5h` and `7d`. `print_unpriced` is shared the same way.

## What's inside

| Path | What it is |
|---|---|
| `agentbar.1m.py` | Plugin entry point, menu renderer, and both data lanes |
| `tests/test_codex.py` | Checks for the Codex lane, network opt-in behind `--live` |
| `tests/test_degraded.py` | Renders the menu with each agent missing, so neither lane can depend on the other |
| `tests/test_pet.py` | Pet moods, the asar reader, and a guard that no sprite art is committed |
| `~/.swiftbar/.cache/` | `stats.json` TTL cache, `cost-ledger.json` high-water marks, `codex-scan.json` per-transcript scan |
| `~/.config/agentbar/codex-prices.json` | Optional per-model price overrides |

## Tech stack

Python 3 stdlib only, no pip installs · SwiftBar · ccusage (Claude spend) · claude-swap (Claude accounts) · macOS Keychain · Codex CLI rollout transcripts

## Getting started

```bash
brew install --cask swiftbar          # if you do not have it
npm install -g ccusage                # powers the Claude spend lane (17+ for `ccusage claude`)

git clone https://github.com/pablomanjarres/agentbar
ln -s "$PWD/agentbar/agentbar.1m.py" ~/.swiftbar/agentbar.1m.py
```

Point SwiftBar at `~/.swiftbar` and the item appears within a minute. Run it in a terminal to see the raw menu it prints:

```bash
python3 agentbar.1m.py              # render the menu
python3 agentbar.1m.py refresh-stats # force both lanes to refetch
python3 tests/test_codex.py --live   # run the checks, including the network one
```

Each lane is independent. Codex works with only `codex login` done, Claude works with only ccusage installed, and a lane with no data says so instead of showing zeros.

### Configuration

**Codex prices.** Rates ship in `DEFAULT_CODEX_PRICES` and are overridden per model, without editing the plugin, from `~/.config/agentbar/codex-prices.json`. Values are USD per 1M tokens, where `cached` prices the `cached_input_tokens` slice *of* `input_tokens` rather than an extra charge on top:

```json
{ "gpt-5.6-terra": { "in": 2.00, "cached": 0.20, "out": 12.00 } }
```

A model that burns tokens with no price shows up as a warning row rather than quietly reading as $0. Bump `CODEX_SCAN_VERSION` if you change the parser, so cached scans get re-read.

**The pet (optional).** Seedy needs a local Codex install for the art and Pillow to crop it once per sprite version:

```bash
pip install pillow
```

Without either, the menu bar falls back to the Claude Code glyph and nothing else changes. "Hide the Codex pet" in the menu turns him off for good. Everything outside this one feature is stdlib.

**Claude Console spend (optional).** Drop an Anthropic admin key at `~/.swiftbar/.secrets/anthropic-admin-key` to add a Console API credits row. Without one the menu keeps a grey row saying the lane is not tracked and where to put the key.

## License

MIT.

---

<p align="center">
  <a href="https://pablomanjarres.com/oss/agentbar">Landing</a> ·
  <a href="https://pablomanjarres.com/portfolio/projects/agentbar">Portfolio write-up</a> ·
  Built by Pablo Manjarres
</p>
