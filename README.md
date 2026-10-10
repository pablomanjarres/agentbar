<h1 align="center">agentbar</h1>

<p align="center"><em>A macOS menu bar pair for Claude Code and OpenAI Codex: how much of your rate-limit window is gone, and what the tokens would have cost on the API.</em></p>

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

If you run Claude Code and Codex side by side, the thing you actually want to know is which one is about to hit a wall, and how much the day cost. Both tools keep that on disk already, in different shapes, and neither surfaces it while you work. agentbar is a single SwiftBar plugin that reads both and puts them side by side in the menu bar: percentage of each rate-limit window burned, when it resets, and API-equivalent spend for today, this month and all time. It is one file of stdlib Python with no runtime dependencies of its own.

## Highlights

- **Each agent wears its own mark beside its own number.** The menu bar shows two items side by side: the Claude glyph with `41%·77%`, then the Codex pet with `12%  $37`. SwiftBar draws one image per item, so the same file runs twice, as `agentbar.1m.py` and as an `agentbar-codex.1m.py` symlink, and `role_for` picks the title from the name it was run under. Both open the same menu. There is no colored warning dot: the numbers already say how close you are.
- **The Claude number is your whole pool, not one account.** With claude-swap rotating several logins, one account hitting 100% only means auto-switch moves on. `pooled_pct` sums every account's use, clamped to its auto-switch cap, over the sum of the caps: four accounts make a 400% pool, so one spent account moves the number by 25%. The pool follows claude-swap's own rotation, so hiding an account from the menu does not drop it. An account at its cap on any window counts as spent, and one with no reading or a failed refresh stays out rather than passing as unused. An account capped below 100% in claude-swap's `autoswitch-policy.json` is tagged `reserve · cap N%` in its row; caps are validated exactly as the daemon does. API-key accounts are billed per token and stay out of the pool.
- **Codex gauges come from the account, not from parsing logs.** `fetch_codex_usage` reads `chatgpt.com/backend-api/wham/usage` with the token in `~/.codex/auth.json`. Every documented `/backend-api/codex/*` usage path returns 403; that one is what the CLI itself calls, and it needs the `originator` header. It returns the plan, an account-level window pair, and per-model buckets like `GPT-5.3-Codex-Spark` that carry their own 5h window.
- **The token walk survives compaction and repeated events.** Codex writes `info.total_token_usage` as a running total per session, and a compaction resets it mid-file. `parse_rollout` differences consecutive readings and treats a decrease as a fresh baseline. Summing the sibling `last_token_usage` instead double-counts: 68,437,053 tokens against a true 66,512,469 on one real session. The test suite asserts the walk reproduces each session's final cumulative figure exactly.
- **A model with no price is a warning, not a silent zero.** `gpt-5.3-codex-spark` is a ChatGPT-Pro-only research preview with no API price at all, so agentbar ships no number for it and says so in the menu. The aggregators that quote a Spark price are copying its parent model's row. The same guard caught a real gap on the Claude side, where ccusage's bundled price snapshot silently valued every Opus 5 token at $0.
- **Claude totals do not shrink when transcripts age out.** Claude Code deletes JSONL transcripts after `cleanupPeriodDays`, and ccusage recomputes from whatever still exists, so its lifetime totals used to fall over time. `update_ledger` keeps a per-day high-water mark in `.cache/cost-ledger.json` and month and all-time are summed from that instead.
- **The Codex pet lives in the menu bar and reacts to your Codex limits.** Codex ships desktop pets, so agentbar wears one. `asar_lookup` parses the Electron archive inside your own Codex install (its own app, or inside ChatGPT.app) with four `struct.unpack` calls and pulls out Seedy's sprite sheet, no node and no bundled copy, then `pet_icon` crops the frame that matches your state: idle when you have headroom, sitting at his laptop while you are burning, shouting when a window is nearly gone. The art belongs to OpenAI, so it is read from your install and cached, never committed here, and a test enforces that.
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
    ├── test_pet.py       # asar lookup, pet folders, mood mapping, no art in the repo
    └── test_title.py     # pooled Claude title and the separate Codex item
```

SwiftBar runs the file once a minute and renders whatever it prints. Everything expensive sits behind a TTL in `~/.swiftbar/.cache/stats.json`, split into a fast local lane and a slow network lane, so the common refresh touches no network at all. The same file re-invokes itself with an argument to handle menu clicks (`switch`, `refresh-stats`, `rebuild-ledger`, `pause-auto`).

Both agents are drawn by the same code. `print_gauges` takes `(label, window)` pairs from either side and sizes its label column to the widest one, which is why Codex's longer bucket names line up with Claude's `5h` and `7d`. `print_unpriced` is shared the same way.

## What's inside

| Path | What it is |
|---|---|
| `agentbar.1m.py` | Plugin entry point, menu renderer, and both data lanes |
| `tests/test_codex.py` | Checks for the Codex lane, network opt-in behind `--live` |
| `tests/test_degraded.py` | Renders the menu with each agent missing, so neither lane can depend on the other |
| `tests/test_pet.py` | Pet moods, the asar reader, local pet folders, and a guard that no sprite art is committed |
| `tests/test_title.py` | The pooled Claude percentage, reserve tags, and the separate Codex item |
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
ln -s "$PWD/agentbar/agentbar.1m.py" ~/.swiftbar/agentbar-codex.1m.py
```

Point SwiftBar at `~/.swiftbar` and both items appear within a minute. Skip the second link if you only want the Claude item: the spend then moves onto it, and the pet, which lives on the Codex item, is not shown. ⌘-drag either one to reorder them. Run it in a terminal to see the raw menu it prints:

```bash
python3 agentbar.1m.py              # render the menu
python3 agentbar.1m.py refresh-stats # force both lanes to refetch
python3 tests/test_codex.py --live   # run the checks, including the network one
```

Each lane is independent. Codex works with only `codex login` done, Claude works with only ccusage installed, and a lane with no data says so instead of showing zeros.

Auto-switch controls are under its status row. Other controls and billing are under **Settings**. Option-click the menu icon to reveal SwiftBar's built-in tools.

### Configuration

**Codex prices.** Rates ship in `DEFAULT_CODEX_PRICES` and are overridden per model, without editing the plugin, from `~/.config/agentbar/codex-prices.json`. Values are USD per 1M tokens, where `cached` prices the `cached_input_tokens` slice *of* `input_tokens` rather than an extra charge on top:

```json
{ "gpt-5.7": { "in": 5.00, "cached": 0.50, "out": 30.00 } }
```

A model that burns tokens with no price shows up as a warning row rather than quietly reading as $0. Bump `CODEX_SCAN_VERSION` if you change the parser, so cached scans get re-read.

**Hidden accounts.** `~/.config/agentbar/hidden-accounts.json` can hide selected account slots from the menu without changing stored logins or auto-switch settings:

```json
{ "accounts": ["1"] }
```

**The pet (optional).** The Codex item wears a Codex pet. It needs Pillow to crop the art once per sprite version:

```bash
pip install pillow
```

Seedy is the default and comes out of your Codex install. To wear another pet, write its name to `~/.config/agentbar/pet`. A pet bundled with Codex works by name. A custom pet you made in Codex lives in the cloud, so put a local copy in `~/.config/agentbar/pets/<name>/`, in the same shape Codex uses for `~/.codex/pets`:

```text
~/.config/agentbar/pets/kaiser/
├── pet.json          # {"displayName": "Kaiser", "spritesheetPath": "spritesheet.png"}
└── spritesheet.png   # the 8-column, 208px-row sheet Codex exports
```

Tall pets are drawn as a bust so their face reads at menu bar size. Without Pillow or any pet art, the Codex item falls back to an SF Symbol and nothing else changes. "Hide the Codex pet" in the menu turns him off for good. Everything outside this one feature is stdlib.

**Claude Console spend (optional).** Drop an Anthropic admin key at `~/.swiftbar/.secrets/anthropic-admin-key` to add a Console API credits row. Without one the menu keeps a grey row saying the lane is not tracked and where to put the key.

## License

MIT.

---

<p align="center">
  <a href="https://pablomanjarres.com/oss/agentbar">Landing</a> ·
  <a href="https://pablomanjarres.com/portfolio/projects/agentbar">Portfolio write-up</a> ·
  Built by Pablo Manjarres
</p>
