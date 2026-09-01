#!/usr/bin/env python3
"""SwiftBar plugin: one menu bar item for Claude Code and OpenAI Codex.

Both agents answer the same two questions: how much of the rate-limit window
is gone, and what the tokens would have cost at API rates.

Claude data sources (all local):
  - ~/.claude-swap-backup/sequence.json + cache/usage.json  (written by the
    `cswap auto` daemon every 60-90s; read here, never fetched)
  - ccusage (npm) over ~/.claude/projects JSONL transcripts, --offline pricing,
    asked for Claude alone: ccusage 20 otherwise folds every agent it finds,
    Codex included, into the same row

Codex data sources:
  - ~/.codex/sessions/**/rollout-*.jsonl for token usage, differenced per
    session and priced per model (see parse_rollout)
  - chatgpt.com/backend-api/wham/usage for live rate-limit windows, on the
    slow lane only

--offline keeps this off the network on a 1m cadence, but ccusage's bundled price
snapshot lags new models and prices unknown ones at $0 *silently* -- that hid all
Opus 5 spend here until ~/.claude/ccusage.json got a pricingOverrides entry.
Anything still unpriced now surfaces as a warning row (see unpriced_models).

Month + Total are summed from .cache/cost-ledger.json (a per-day high-water mark),
not from ccusage's live totals -- ccusage recomputes from transcripts that Claude
Code deletes after 30 days, so its cumulative figures shrink over time.

Actions re-invoke this file with argv: switch <n> | refresh-stats | rebuild-ledger
"""
import base64
import datetime
import io
import json
import os
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
PLUGIN = os.path.realpath(__file__)
CSWAP_ROOT = os.path.join(HOME, ".claude-swap-backup")
CACHE_DIR = os.path.join(HOME, ".swiftbar", ".cache")
STATS_PATH = os.path.join(CACHE_DIR, "stats.json")
LEDGER_PATH = os.path.join(CACHE_DIR, "cost-ledger.json")
CLAUDE_SETTINGS = os.path.join(HOME, ".claude", "settings.json")
RESEED_MARGIN_DAYS = 2  # see reseed_floor()
TUI_CMD = os.path.join(CACHE_DIR, "cswap-tui.command")
AUTO_LOG = os.path.join(HOME, "Library/Logs/claude-swap-auto.log")
CSWAP = os.path.join(HOME, ".local/bin/cswap")
AUTO_SERVICE = "com.pablo.claude-swap"
AUTO_PLIST = os.path.join(HOME, "Library/LaunchAgents", AUTO_SERVICE + ".plist")
AUTO_DOMAIN = f"gui/{os.getuid()}"
AUTO_TARGET = f"{AUTO_DOMAIN}/{AUTO_SERVICE}"
HIDE_EMAILS_FLAG = os.path.join(CACHE_DIR, "hide-emails")
PAUSE_FLAG = os.path.join(CACHE_DIR, "auto-paused")
SECRETS_DIR = os.path.join(HOME, ".swiftbar", ".secrets")
ADMIN_KEY_PATH = os.path.join(SECRETS_DIR, "anthropic-admin-key")
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
COST_REPORT_URL = "https://api.anthropic.com/v1/organizations/cost_report"
OAUTH_BETA = "oauth-2025-04-20"  # matches cswap's oauth.py
UA = "claude-swiftbar-plugin/1.0"

# --- Codex / ChatGPT -------------------------------------------------------
CODEX_ROOT = os.path.join(HOME, ".codex")
CODEX_AUTH = os.path.join(CODEX_ROOT, "auth.json")
CODEX_SESSIONS = os.path.join(CODEX_ROOT, "sessions")
CODEX_SCAN_PATH = os.path.join(CACHE_DIR, "codex-scan.json")
# Bump whenever parse_rollout() changes shape or fixes a miscount: the scan
# cache is keyed on each transcript's (mtime, size), which do NOT change when
# the parser does, so without this an upgrade would serve the old numbers for
# every already-seen session, forever.
CODEX_SCAN_VERSION = 2
CONFIG_DIR = os.path.join(HOME, ".config", "agentbar")
CODEX_PRICES_PATH = os.path.join(CONFIG_DIR, "codex-prices.json")
# Every documented /backend-api/codex/* usage path 403s; this is the one the CLI
# itself reads, and it only answers with the originator header set.
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_UA = "codex_cli_rs/0.142.5"
CODEX_MARK = "\u2733"  # the mark next to Claude's circled number in the title

# --- the Codex pet ---------------------------------------------------------
# Codex ships desktop "pets": one sprite sheet per pet inside the app's asar.
# The art is OpenAI's, so this plugin does NOT carry a copy. It reads the sheet
# out of the local Codex install, crops the frames it needs once, and caches
# them. No Codex app, no pet. No Pillow, no pet. Nothing else breaks either way.
CODEX_ASAR = "/Applications/Codex.app/Contents/Resources/app.asar"
PET_DIR = os.path.join(CACHE_DIR, "pet")
HIDE_PET_FLAG = os.path.join(CACHE_DIR, "hide-pet")
# Bump when the crop changes shape: moods, cell geometry or bar height. The
# cache key is the asar's mtime, which does not move when this file does, so
# without it a fix would never reach anyone who already has cached frames.
PET_CACHE_VERSION = 2
PET_NAME = "seedy"
PET_LABEL = "Seedy"
PET_BLURB = "Small green shoots for new ideas."
PET_BAR_PX = 36  # 18pt at 144 dpi, matching ICON. See the dpi= on save().
# Frame geometry read off the sheet the Codex app animates. Columns and cell
# height have been stable across sprite versions; a sheet that does not divide
# cleanly is treated as unknown art and the pet is skipped rather than drawn
# from a garbled crop.
PET_COLS = 8
PET_CELL_H = 208
# (row, col) per mood. Rows are the app's own animation states: 0 idle,
# 7 "running" (sitting at a laptop while a task runs), 5 "failed".
PET_MOODS = {
    "calm": (0, 0),
    "working": (7, 0),
    "strained": (5, 0),
    "spent": (5, 2),
}
# Gap between the Claude glyph and the pet, in the 144 dpi pixel space both
# are drawn in, so the pair reads as one item rather than two.
PET_GAP_PX = 5
PET_CAPTIONS = {
    "calm": "plenty of headroom",
    "working": "on the clock",
    "strained": "running low",
    "spent": "out of window",
}

ENV = dict(os.environ)
ENV["PATH"] = ":".join(
    ["/opt/homebrew/bin", "/usr/local/bin", os.path.join(HOME, ".local/bin"), "/usr/bin", "/bin"]
)

# Official Claude Code glyph (claude-code-color.svg), 36x36 @144dpi PNG (renders 18pt)
ICON = (
    "iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAACXBIWXMAABYlAAAWJQFJUiTwAAAArUlEQVR42u2XsQ2AIBBFmccp"
    "WII4ii3zsISdE9CYMAEdEygmkFxIRCQENf7iFQpfXsPdydZpZG+CQQhCEHpAiHtkJ3iJ0LFx64SE0G+EhoCpONSQfDOhuKYrhDTJ"
    "Q6iJUFqs5pPgga0QsiSfrs3p+azjjSoCQp8UQuuAUGsh4VGehQRVwJHqqy6IVd2Rd/F7S3gWd2ZqkWkdsmA+lpnWIWqGfAh9Tgg/"
    "ihCCUAt2zWWEo4rPulAAAAAASUVORK5CYII="
)

GRAY = "#8e8e93"
RUST = "#e07a42"
GREEN = "#34a853"
ORANGE = "#ff9500"
RED = "#ff3b30"
CIRCLED = "➊➋➌➍➎➏➐➑➒➓"

FAST_TTL = 50      # seconds: today + active block
MONTH_TTL = 900    # seconds: credits + Console API cost (slow lane)


def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=ENV)
        return p.stdout if p.returncode == 0 else None
    except Exception:
        return None


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def atomic_write(path, obj):
    # pid-scoped: the menu's actions run refresh_stats in a second process while
    # the 1m tick may be mid-write, and a shared ".tmp" name let one rename the
    # other's file away (FileNotFoundError on os.replace, blank menu that tick).
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def ccusage(args):
    out = run(["ccusage"] + args + ["--json", "--offline"], timeout=60)
    return json.loads(out) if out else None


def ccusage_claude(args):
    """`ccusage claude <cmd>`: Claude alone.

    ccusage before 17 has no per-agent commands, and there the combined command
    is Claude alone anyway, so fall back to it. Rows that name another agent
    are dropped on that path: a ccusage 20 whose scoped call failed for some
    other reason must not fold Codex back into the ledger through max().
    """
    out = ccusage(["claude"] + args)
    if out is not None:
        return out
    out = ccusage(args)
    if out and isinstance(out.get("daily"), list):
        out["daily"] = [
            r
            for r in out["daily"]
            if set((r.get("metadata") or {}).get("agents") or ["claude"]) == {"claude"}
        ]
    return out


def row_day(row):
    """The day a ccusage row is for: `date` from the per-agent commands
    (`ccusage claude daily`), `period` from the combined `ccusage daily` that
    ccusage_claude() falls back to on versions without per-agent commands."""
    return row.get("date") or row.get("period")


def reseed_floor():
    """First day a ledger re-seed may overwrite.

    Inside Claude Code's transcript cleanup (cleanupPeriodDays, default 30),
    with margin: a day being cleaned up reads too LOW, the one direction the
    high-water mark cannot recover from. Verified on a real ledger: without
    the floor, a 32-day-old $32.50 would have been re-seeded to $0.83.
    """
    keep = (load_json(CLAUDE_SETTINGS) or {}).get("cleanupPeriodDays")
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        keep = 30
    days = max(keep - RESEED_MARGIN_DAYS, 0)
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def update_ledger(rows, reseed=False):
    """Per-day high-water mark of cost + tokens. Returns the whole ledger.

    ccusage keeps no store of its own: every total is recomputed from the JSONL
    transcripts under ~/.claude/projects, and Claude Code deletes those once they
    pass cleanupPeriodDays (default 30). So Month and Total used to *shrink* as
    history aged out -- nothing in the chain remembered what was already spent.
    This file does. A finished day's cost only ever grows, so max() is safe, and
    it also absorbs a transient dip if ccusage ever reads a transcript mid-write.

    Days deleted before this ledger existed are gone for good; it protects from
    here on. "Rebuild ledger" in the menu re-seeds it from what ccusage can still
    see (use only if a day is ever recorded too high).

    reseed=True takes ccusage's figure as-is for every day it can still see,
    from reseed_floor() on, and keeps older days untouched. Used once, when the
    source moved to Claude-only: the combined command had folded Codex spend
    into the marks, and max() would have kept the inflated ones forever. A day
    inside the window with no Claude row had no Claude spend, so its mark (Codex
    alone) goes. An empty read re-seeds nothing.
    """
    led = load_json(LEDGER_PATH) or {}
    changed = False
    rows = [r for r in rows or [] if row_day(r)]
    reseed = reseed and bool(rows)
    if reseed:
        floor = reseed_floor()
        seen = {row_day(r) for r in rows}
        for day in [d for d in led if d >= floor and d not in seen]:
            del led[day]
            changed = True
    for r in rows:
        day = row_day(r)
        prev = led.get(day) or {}
        cost = r.get("totalCost") or 0
        tokens = r.get("totalTokens") or 0
        if not reseed or day < floor:
            cost = max(cost, prev.get("cost") or 0)
            tokens = max(tokens, prev.get("tokens") or 0)
        if cost != prev.get("cost") or tokens != prev.get("tokens"):
            led[day] = {"cost": cost, "tokens": tokens}
            changed = True
    if changed:
        os.makedirs(CACHE_DIR, exist_ok=True)
        atomic_write(LEDGER_PATH, led)
    return led


def unpriced_models(row):
    """Models in a ccusage row that burned tokens but priced at $0.

    ccusage has no price for them, so their spend is missing from every total.
    Fix by adding the model to pricingOverrides in ~/.claude/ccusage.json.
    """
    names = []
    for m in (row or {}).get("modelBreakdowns") or []:
        name = m.get("modelName") or ""
        if not name or name.startswith("<"):  # e.g. <synthetic>
            continue
        tokens = sum(
            m.get(k) or 0
            for k in ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens")
        )
        if tokens > 0 and not (m.get("cost") or 0):
            names.append(name)
    return names


def http_json(url, headers, timeout=15):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def keychain_username():
    """Mirror cswap/Claude Code: $USER, then the OS username."""
    user = os.environ.get("USER")
    if user:
        return user
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        return "claude-code-user"


def active_oauth_token():
    """Access token of the ACTIVE Claude Code login (same Keychain item cswap swaps)."""
    out = run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            "Claude Code-credentials",
            "-a",
            keychain_username(),
            "-w",
        ],
        timeout=10,
    )
    if not out:
        return None
    try:
        return (json.loads(out.strip()).get("claudeAiOauth") or {}).get("accessToken")
    except Exception:
        return None


def fetch_credit_state():
    """Usage-credits + extra-usage state for the active account (Anthropic OAuth endpoint)."""
    tok = active_oauth_token()
    if not tok:
        return None
    try:
        data = http_json(
            OAUTH_USAGE_URL,
            {"Authorization": f"Bearer {tok}", "anthropic-beta": OAUTH_BETA, "User-Agent": UA},
        )
    except Exception:
        return None
    return {"extra_usage": data.get("extra_usage"), "spend": data.get("spend")}


def fetch_api_month_cost():
    """Console API credits burned this month, via the Admin cost report.

    Returns None when no admin key file is configured (the menu then prints a
    "not tracked" row saying where to put one), {"error": ...} on fetch failure,
    {"month_usd": float} on success.
    Amounts come back as decimal strings in cents.
    """
    try:
        with open(ADMIN_KEY_PATH) as f:
            key = f.read().strip()
    except OSError:
        return None
    if not key:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    params = {"starting_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": "31"}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "User-Agent": UA}
    total_cents = 0.0
    try:
        for _ in range(6):  # paginate defensively
            url = COST_REPORT_URL + "?" + urllib.parse.urlencode(params)
            data = http_json(url, headers)
            for bucket in data.get("data", []):
                for row in bucket.get("results", []):
                    total_cents += float(row.get("amount") or 0)
            if not data.get("has_more"):
                break
            params["page"] = data.get("next_page")
        return {"month_usd": total_cents / 100.0}
    except Exception as e:
        return {"error": type(e).__name__}


def minor_to_usd(obj):
    """{'amount_minor': 1234, 'exponent': 2} -> 12.34; tolerant of missing fields."""
    if not isinstance(obj, dict) or obj.get("amount_minor") is None:
        return None
    try:
        return float(obj["amount_minor"]) / (10 ** int(obj.get("exponent") or 2))
    except Exception:
        return None


# --------------------------------------------------------------------------
# Codex / ChatGPT
#
# Same two questions as the Claude side, different plumbing: how much of the
# rate-limit window is already gone (live, from the account) and what the tokens
# would have cost on the API (local, from the rollout transcripts).
# --------------------------------------------------------------------------

# $ per 1M tokens. "cached" prices the cached_input_tokens slice *of* input_tokens,
# it is not an extra charge on top. From developers.openai.com/api/docs/pricing,
# cross-checked against litellm; extend or override in
# ~/.config/agentbar/codex-prices.json rather than editing this file.
#
# gpt-5.3-codex-spark is deliberately absent. It is a ChatGPT-Pro-only research
# preview with no API price at all, so any number here would be invented -- the
# aggregators that quote one are copying the parent gpt-5.3-codex row. It shows
# up in the unpriced warning row instead, the same way ccusage gaps do.
DEFAULT_CODEX_PRICES = {
    "gpt-5.6-sol": {"in": 4.00, "cached": 0.40, "out": 20.00},
    "gpt-5.5": {"in": 5.00, "cached": 0.50, "out": 30.00},
    "gpt-5.4": {"in": 2.50, "cached": 0.25, "out": 15.00},
    "gpt-5.4-mini": {"in": 0.75, "cached": 0.075, "out": 4.50},
    "gpt-5.3-codex": {"in": 1.75, "cached": 0.175, "out": 14.00},
    "gpt-5.1-codex-mini": {"in": 0.25, "cached": 0.025, "out": 2.00},
}

TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def codex_prices():
    """Shipped prices overlaid with the user's file, so a new model needs no edit."""
    prices = dict(DEFAULT_CODEX_PRICES)
    for name, price in (load_json(CODEX_PRICES_PATH) or {}).items():
        if isinstance(price, dict) and {"in", "cached", "out"} <= set(price):
            prices[name] = price
    return prices


def codex_auth():
    """(access_token, account_id) of the logged-in ChatGPT account, or (None, None)."""
    tokens = (load_json(CODEX_AUTH) or {}).get("tokens") or {}
    return tokens.get("access_token"), tokens.get("account_id")


def codex_window(w):
    """One ChatGPT rate-limit window -> the shape the gauge rows already render."""
    if not isinstance(w, dict) or w.get("used_percent") is None:
        return None
    resets_at = w.get("reset_at")
    secs = w.get("reset_after_seconds")
    if secs is None and resets_at:
        secs = resets_at - time.time()
    return {
        "pct": float(w.get("used_percent") or 0),
        "clock": local_clock_ts(resets_at) if resets_at else "?",
        "countdown": human_dur(secs),
        "minutes": int((w.get("limit_window_seconds") or 0) // 60),
        "at": resets_at,
    }


def codex_windows(data):
    """Every rate-limit window on the account, as (label, window) pairs.

    The account-level pair comes first, then the per-model buckets from
    additional_rate_limits (GPT-5.3-Codex-Spark carries its own 5h, for one).
    A model bucket that merely restates an account window is dropped rather than
    drawn twice, but only against the ACCOUNT windows: two different models can
    legitimately share a length, a reset and a usage figure (any two sitting at
    0% right after a reset), and collapsing those hides a real limit.
    """
    out = []
    account_sigs = set()
    top = data.get("rate_limit") or {}
    for key in ("primary_window", "secondary_window"):
        w = codex_window(top.get(key))
        if w:
            account_sigs.add((w["minutes"], w["at"], w["pct"]))
            out.append((win_label(w["minutes"]), w))

    seen_labels = {label for label, _ in out}
    for extra in data.get("additional_rate_limits") or []:
        name = (extra.get("limit_name") or "model").replace("GPT-", "")
        scoped = extra.get("rate_limit") or {}
        for key in ("primary_window", "secondary_window"):
            w = codex_window(scoped.get(key))
            if not w or (w["minutes"], w["at"], w["pct"]) in account_sigs:
                continue
            label = f"{name} {win_label(w['minutes'])}"
            if label in seen_labels:
                continue
            seen_labels.add(label)
            out.append((label, w))
    return out


def fresh_windows(windows):
    """Drop windows whose own reset time has already gone by.

    codex_usage is deliberately kept when a refresh fails, but a window past its
    reset_at describes a window that no longer exists. Letting one keep the menu
    bar red for hours is worse than showing nothing, and it would make a liar of
    the promise that red means something is about to stop working.
    """
    now = time.time()
    return [(label, w) for label, w in windows if not w.get("at") or w["at"] > now]


def fetch_codex_usage():
    """Live plan, rate-limit windows and credit state for the Codex login.

    The Claude-side mirror of this is fetch_credit_state(). Returns None when
    Codex is not logged in at all and {"error": ...} when the call fails, so a
    network blip keeps the last good reading instead of blanking the lane.
    """
    token, account = codex_auth()
    if not token:
        return None
    try:
        data = http_json(
            CODEX_USAGE_URL,
            {
                "Authorization": f"Bearer {token}",
                "chatgpt-account-id": account or "",
                "User-Agent": CODEX_UA,
                "originator": "codex_cli_rs",
            },
        )
        # parsing stays inside the guard: wham/usage is undocumented, and a
        # shape change (reset_at arriving as an ISO string, say) would otherwise
        # raise out of main() and take the whole menu bar item down with it
        return codex_state(data)
    except Exception as e:
        return {"error": type(e).__name__}


def codex_state(data):
    """The slice of a wham/usage payload this plugin draws."""
    credits = data.get("credits") or {}
    return {
        "plan": data.get("plan_type"),
        "email": data.get("email"),
        "windows": codex_windows(data),
        "limit_reached": bool((data.get("rate_limit") or {}).get("limit_reached")),
        "credits": {
            "has": bool(credits.get("has_credits")),
            "unlimited": bool(credits.get("unlimited")),
            "balance": credits.get("balance"),
        },
        "at": time.time(),
    }


def parse_rollout(path):
    """Per-day, per-model token deltas from one Codex rollout transcript.

    info.total_token_usage is CUMULATIVE for the session and a compaction resets
    it mid-file, so consecutive readings are differenced and any decrease starts
    a fresh baseline. Summing the sibling last_token_usage instead double-counts
    repeated events (68,437,053 against a true 66,512,469 on the largest local
    session); differencing reproduces the final cumulative figure exactly.
    """
    days = {}
    prev = dict.fromkeys(TOKEN_FIELDS, 0)
    model = None
    try:
        handle = open(path, errors="replace")
    except OSError:
        return days
    with handle:
        for line in handle:
            # cheap prefilter: most lines are tool output and hold neither field
            if '"model"' not in line and '"token_count"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            payload = rec.get("payload") or {}
            if rec.get("type") == "turn_context":
                model = payload.get("model") or model
                continue
            if payload.get("type") != "token_count":
                continue
            cur = (payload.get("info") or {}).get("total_token_usage") or {}
            if not cur:
                continue
            cur = {k: cur.get(k) or 0 for k in TOKEN_FIELDS}
            if cur["total_tokens"] < prev["total_tokens"]:
                prev = dict.fromkeys(TOKEN_FIELDS, 0)  # compacted, counter restarted
            delta = {k: max(0, cur[k] - prev[k]) for k in TOKEN_FIELDS}
            prev = cur
            if delta["total_tokens"] <= 0:
                continue
            if not (delta["input_tokens"] or delta["output_tokens"]):
                # Codex Desktop's imported legacy threads carry one token_count
                # with only total_tokens filled in: no input, no output, and no
                # turn_context to name a model. Nothing was billed, so counting
                # it would only feed an "unknown" bucket and a false unpriced
                # warning. A real turn always moves input or output.
                continue
            try:
                day = (
                    datetime.datetime.fromisoformat(
                        (rec.get("timestamp") or "").replace("Z", "+00:00")
                    )
                    .astimezone()
                    .date()
                    .isoformat()
                )
            except Exception:
                continue
            bucket = days.setdefault(day, {}).setdefault(
                model or "unknown", dict.fromkeys(TOKEN_FIELDS, 0)
            )
            for k in TOKEN_FIELDS:
                bucket[k] += delta[k]
    return days


def codex_scan():
    """Daily Codex token totals per model, cached one entry per transcript.

    Rollouts are append-only, but a delta walk needs the whole file, so the unit
    of caching is a file keyed on (mtime, size): untouched sessions are reused and
    only new or grown ones are re-read. Without it the plugin would re-parse every
    transcript it has ever seen, once a minute.
    """
    cache = load_json(CODEX_SCAN_PATH) or {}
    if cache.get("version") != CODEX_SCAN_VERSION:
        cache = {}  # parser changed under the cache; re-read everything once
    known = cache.get("files") or {}
    fresh, changed = {}, False
    for root, _dirs, names in os.walk(CODEX_SESSIONS):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(root, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            key = f"{int(stat.st_mtime)}:{stat.st_size}"
            hit = known.get(path)
            if hit and hit.get("key") == key:
                fresh[path] = hit
                continue
            fresh[path] = {"key": key, "days": parse_rollout(path)}
            changed = True
    if fresh and (changed or len(fresh) != len(known)):
        os.makedirs(CACHE_DIR, exist_ok=True)
        atomic_write(
            CODEX_SCAN_PATH, {"version": CODEX_SCAN_VERSION, "files": fresh}
        )

    days = {}
    for entry in fresh.values():
        for day, models in (entry.get("days") or {}).items():
            target = days.setdefault(day, {})
            for model, counts in models.items():
                bucket = target.setdefault(model, dict.fromkeys(TOKEN_FIELDS, 0))
                for k in TOKEN_FIELDS:
                    bucket[k] += counts.get(k) or 0
    return days


def codex_cost(model, counts, prices):
    """API-equivalent USD for one model's tokens, or None when it has no price.

    input_tokens already contains cached_input_tokens, so the cached slice is
    subtracted out and billed at the cheaper rate instead of being added on top.
    """
    price = prices.get(model)
    if not price:
        return None
    cached = counts.get("cached_input_tokens") or 0
    fresh = max(0, (counts.get("input_tokens") or 0) - cached)
    written = counts.get("output_tokens") or 0
    return (
        fresh * price["in"] + cached * price["cached"] + written * price["out"]
    ) / 1e6


def codex_summary():
    """Today / month / all-time Codex spend and tokens, plus any unpriced models."""
    days = codex_scan()
    prices = codex_prices()
    today = datetime.date.today().isoformat()
    out = {
        "today": {"cost": 0.0, "tokens": 0},
        "month": {"cost": 0.0, "tokens": 0},
        "alltime": {"cost": 0.0, "tokens": 0, "days": len(days)},
        "unpriced": [],
        "last_day": max(days) if days else None,
    }
    unpriced = set()
    for day, models in days.items():
        for model, counts in models.items():
            tokens = counts.get("total_tokens") or 0
            cost = codex_cost(model, counts, prices)
            if cost is None:
                unpriced.add(model)
                cost = 0.0
            out["alltime"]["cost"] += cost
            out["alltime"]["tokens"] += tokens
            if day[:7] == today[:7]:
                out["month"]["cost"] += cost
                out["month"]["tokens"] += tokens
            if day == today:
                out["today"]["cost"] += cost
                out["today"]["tokens"] += tokens
    out["unpriced"] = sorted(unpriced)
    return out


def asar_lookup(path, wanted):
    """Byte range of the first archived file whose path contains `wanted`.

    An Electron asar is a small pickle-framed JSON directory followed by every
    file concatenated, so the whole lookup is four uint32s and one json.loads.
    Reading it directly avoids shelling out to node just to fetch one sprite.
    """
    with open(path, "rb") as f:
        if struct.unpack("<I", f.read(4))[0] != 4:
            return None
        header_size = struct.unpack("<I", f.read(4))[0]
        f.read(4)
        json_size = struct.unpack("<I", f.read(4))[0]
        header = json.loads(f.read(json_size).decode("utf-8"))
        data_at = 8 + header_size

        found = []

        def walk(node, prefix=""):
            for name, entry in (node.get("files") or {}).items():
                full = prefix + "/" + name
                if "files" in entry:
                    walk(entry, full)
                elif wanted in full.lower() and not found:
                    # entries marked "unpacked" live beside the archive in
                    # app.asar.unpacked and carry no offset at all
                    if "offset" in entry:
                        found.append(entry)

        walk(header)
        if not found:
            return None
        entry = found[0]
        f.seek(data_at + int(entry["offset"]))
        return f.read(int(entry["size"]))


def pet_icon(mood):
    """base64 PNG of one pet mood, or None when the pet cannot be drawn.

    Cropped from the sprite sheet in the user's own Codex install and cached per
    (sheet mtime, mood), so Pillow is touched on a version change and never on a
    routine refresh.
    """
    if os.path.exists(HIDE_PET_FLAG) or mood not in PET_MOODS:
        return None
    try:
        stamp = int(os.path.getmtime(CODEX_ASAR))
    except OSError:
        return None  # Codex not installed
    cached = os.path.join(
        PET_DIR, f"{PET_NAME}-{mood}-v{PET_CACHE_VERSION}-{stamp}.b64"
    )
    try:
        with open(cached) as f:
            return f.read()
    except OSError:
        pass
    try:
        from PIL import Image  # optional: no Pillow, no pet
    except Exception:
        return None
    try:
        # asar_lookup reads a third-party binary that Codex rewrites on update;
        # a truncated or mid-write archive must not take the menu bar down
        blob = asar_lookup(CODEX_ASAR, PET_NAME + "-spritesheet")
        if not blob:
            return None
        sheet = Image.open(io.BytesIO(blob)).convert("RGBA")
        cw = sheet.width // PET_COLS
        if sheet.width % PET_COLS or sheet.height % PET_CELL_H:
            return None  # unfamiliar art, better nothing than a garbled crop
        row, col = PET_MOODS[mood]
        if (row + 1) * PET_CELL_H > sheet.height:
            return None
        cell = sheet.crop((col * cw, row * PET_CELL_H, (col + 1) * cw, (row + 1) * PET_CELL_H))
        cell = cell.crop(cell.getbbox() or (0, 0, cw, PET_CELL_H))
        scale = PET_BAR_PX / cell.height
        cell = cell.resize(
            (max(1, round(cell.width * scale)), PET_BAR_PX), Image.NEAREST
        )
        buf = io.BytesIO()
        # ICON carries pHYs 5669 (144 dpi) and draws at 18pt. Pillow writes no
        # pHYs by default, so the frame was read as 72 dpi and drawn at DOUBLE
        # size: clipped in the bar, and towering over 11pt text in the dropdown.
        cell.save(buf, format="PNG", optimize=True, dpi=(144, 144))
        data = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None
    try:
        os.makedirs(PET_DIR, exist_ok=True)
        tmp = cached + ".tmp"
        with open(tmp, "w") as f:
            f.write(data)
        os.replace(tmp, cached)
    except OSError:
        pass
    return data


def title_icon(mood):
    """Both marks in one image: the Claude glyph, then the pet.

    SwiftBar allows one image per line, and dropping the Claude glyph for the
    pet quietly removed the only sign that this item tracks Claude at all. They
    are composited instead, at the same 36px/144 dpi as ICON so the pair keeps
    the height a menu bar item is allowed.

    Falls back to the bare glyph whenever the pet cannot be drawn, which is the
    behaviour every other pet path already has.
    """
    pet = pet_icon(mood)
    if not pet:
        return ICON
    # Keyed on the asar's mtime as well, exactly like pet_icon: without it a new
    # sprite sheet regenerates the frame but this composite keeps serving the
    # old one until PET_CACHE_VERSION is bumped in source.
    try:
        stamp = int(os.path.getmtime(CODEX_ASAR))
    except OSError:
        return ICON
    cached = os.path.join(
        PET_DIR, f"title-{PET_NAME}-{mood}-v{PET_CACHE_VERSION}-{stamp}.b64"
    )
    try:
        with open(cached) as f:
            return f.read()
    except OSError:
        pass
    try:
        from PIL import Image

        left = Image.open(io.BytesIO(base64.b64decode(ICON))).convert("RGBA")
        right = Image.open(io.BytesIO(base64.b64decode(pet))).convert("RGBA")
        height = max(left.height, right.height)
        out = Image.new("RGBA", (left.width + PET_GAP_PX + right.width, height), (0, 0, 0, 0))
        out.alpha_composite(left, (0, height - left.height))
        out.alpha_composite(right, (left.width + PET_GAP_PX, height - right.height))
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True, dpi=(144, 144))
        data = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ICON
    try:
        os.makedirs(PET_DIR, exist_ok=True)
        tmp = cached + ".tmp"
        with open(tmp, "w") as f:
            f.write(data)
        os.replace(tmp, cached)
    except OSError:
        pass
    return data


def pet_mood(worst, busy, limit_reached):
    """Which frame the pet wears, from how close either agent is to its wall."""
    if limit_reached or worst >= 95:
        return "spent"
    if worst >= 80:
        return "strained"
    return "working" if busy else "calm"


def refresh_stats(force=False, active_num=None):
    os.makedirs(CACHE_DIR, exist_ok=True)
    st = load_json(STATS_PATH) or {}
    now = time.time()
    changed = False

    if force or now - st.get("fastAt", 0) > FAST_TTL:
        # ccusage 20 scans every agent it can find and folds them into one row,
        # Codex included. This lane is Claude's; Codex is walked separately
        # below, so the combined command counted it on both sides of the menu.
        daily = ccusage_claude(["daily"])
        if daily:
            today = datetime.date.today().isoformat()
            rows = daily.get("daily") or []
            row = next((r for r in rows if row_day(r) == today), None)
            st["today"] = {
                "cost": row.get("totalCost", 0) if row else 0,
                "tokens": row.get("totalTokens", 0) if row else 0,
            }
            st["unpriced"] = unpriced_models(row)
            # Month + Total come from the ledger, not from ccusage's live totals:
            # ccusage only sees transcripts that still exist, so its figures shrink
            # as Claude Code's 30-day cleanup eats history. See update_ledger().
            # The first Claude-only pass re-seeds the days ccusage still sees, so
            # marks that had Codex folded in come back down once.
            led = update_ledger(rows, reseed=st.get("ledgerAgent") != "claude")
            if rows:  # an empty read must not spend the one-time re-seed
                st["ledgerAgent"] = "claude"
            st["alltime"] = {
                "cost": sum(v.get("cost") or 0 for v in led.values()),
                "tokens": sum(v.get("tokens") or 0 for v in led.values()),
                "days": len(led),
            }
            st["month"] = {
                "cost": sum(
                    v.get("cost") or 0 for d, v in led.items() if d[:7] == today[:7]
                )
            }
            st["fastAt"] = now
            changed = True
        # Codex spend is a local file walk behind a per-transcript cache, so it
        # rides the fast lane with ccusage rather than the network lane below.
        st["codex_spend"] = codex_summary()
        changed = True
        blocks = ccusage_claude(["blocks"])
        if blocks:
            act = next((b for b in blocks.get("blocks", []) if b.get("isActive")), None)
            if act:
                st["block"] = {
                    "cost": act.get("costUSD", 0),
                    "perHour": (act.get("burnRate") or {}).get("costPerHour", 0),
                    "projCost": (act.get("projection") or {}).get("totalCost", 0),
                    "end": act.get("endTime"),
                    "tokens": (act.get("tokenCounts") or {}),
                }
            else:
                st["block"] = None
            changed = True

    # (no `ccusage monthly` call: the month is summed from the ledger above, which
    # matches it exactly and keeps the figure from shrinking when history is pruned)

    # credits + Console API cost: slow lane, also refetch when the active account changed
    acct_changed = active_num is not None and (st.get("credits") or {}).get("acct") != active_num
    # "codex_usage" missing means this cache predates the Codex lane, so an
    # upgrade fetches once immediately instead of showing "usage unavailable"
    # until the shared slow-lane timer happens to come round.
    first_codex = "codex_usage" not in st
    if force or acct_changed or first_codex or now - st.get("creditsAt", 0) > MONTH_TTL:
        cred = fetch_credit_state()
        if cred is not None:
            cred["acct"] = active_num
            st["credits"] = cred
        st["api_cost"] = fetch_api_month_cost()
        codex = fetch_codex_usage()
        if codex is None:
            st["codex_usage"] = None  # Codex never logged in on this Mac
            st["codex_error"] = None
        elif codex.get("error"):
            # keep the last good windows and label them stale rather than
            # blanking the lane on one failed request
            st["codex_error"] = codex["error"]
        else:
            st["codex_usage"] = codex
            st["codex_error"] = None
        st["creditsAt"] = now
        changed = True

    if changed:
        atomic_write(STATS_PATH, st)
    return st


def bar(pct, width=10):
    filled = max(0, min(width, round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def state_color(pct):
    if pct >= 90:
        return RED
    if pct >= 75:
        return ORANGE
    return GREEN


def money(x):
    return f"${x:,.2f}"


def tokens_h(n):
    n = float(n or 0)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}{suf}"
    return f"{n:.0f}"


def rel_age(ts):
    s = max(0, int(time.time() - ts))
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60}m ago"
    return f"{s // 3600}h ago"


def local_clock(iso):
    try:
        return local_clock_ts(
            datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        )
    except Exception:
        return "?"


def local_clock_ts(ts):
    """Unix seconds -> local HH:MM (Codex reports resets as epoch, Claude as ISO)."""
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%H:%M")
    except Exception:
        return "?"


def human_dur(seconds):
    """Seconds -> the compact countdown shape cswap already writes ("4h 12m")."""
    try:
        s = max(0, int(seconds))
    except Exception:
        return "?"
    d, h, m = s // 86400, s // 3600 % 24, s // 60 % 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def win_label(minutes):
    """Rate-limit window length -> the short label used in the gauge rows."""
    if not minutes:
        return "win"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def print_gauges(windows):
    """One bar row per rate-limit window. Shared by the Claude and Codex lanes.

    Codex labels are wider than Claude's ("5.3-Codex-Spark 5h"), so the column
    is sized to the widest label present instead of a fixed width.
    """
    windows = list(windows)
    if not windows:
        return
    pad = max(5, max(len(label) for label, _ in windows))
    for label, w in windows:
        pct = w.get("pct", 0)
        if w.get("stale"):
            # the window already reset, so this percentage describes nothing.
            # Say so in grey rather than drawing a reassuring green bar.
            age = f" · {human_dur(w['age'])} old" if w.get("age") else ""
            print(
                f"   {label:<{pad}} {'·' * 10} stale reading{age} "
                f"| font=Menlo size=11 trim=false color={GRAY}"
            )
            continue
        reset = f"resets {w.get('clock', '?')} ({w.get('countdown', '?')})"
        line = (
            f"   {label:<{pad}} {bar(pct)} {pct:>3.0f}% used · "
            f"{100 - pct:.0f}% left · {reset}"
        )
        print(f"{line} | font=Menlo size=11 trim=false color={state_color(pct)}")


def print_rows(rows):
    """Label + value lines for a spend block."""
    for label, text in rows:
        print(f"{label:<6} {text} | font=Menlo size=12 trim=false")


def print_unpriced(models, hint):
    """Warn about models that burned tokens at $0.

    Their spend is missing from every total above, so the figure reads low with
    no other sign that anything is wrong. This is exactly how ccusage's stale
    price snapshot once hid all Opus 5 spend.
    """
    if not models:
        return
    print(
        f"⚠ {', '.join(models)}: no price — spend above is too low "
        f"| size=11 color={ORANGE} trim=false"
    )
    print(f"   {hint} | size=11 color={GRAY} trim=false")


def daemon_running():
    return run(["/usr/bin/pgrep", "-f", "cswap auto"], timeout=10) is not None


def launchctl(*args):
    return run(["/bin/launchctl", *args], timeout=20)


def auto_paused():
    return os.path.exists(PAUSE_FLAG)


def pause_auto():
    # KeepAlive would revive a plain kill, so bootout removes the job entirely
    # and disable keeps it out across logins/reboots until the user resumes.
    launchctl("bootout", AUTO_TARGET)
    launchctl("disable", AUTO_TARGET)
    os.makedirs(CACHE_DIR, exist_ok=True)
    open(PAUSE_FLAG, "w").close()


def resume_auto():
    launchctl("enable", AUTO_TARGET)                  # clear the persistent disable first
    launchctl("bootstrap", AUTO_DOMAIN, AUTO_PLIST)   # RunAtLoad starts it again
    if os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)


def last_log_events():
    """Return (last_event_dict, last_switch_dict) from the auto log tail."""
    last, last_switch = None, None
    try:
        with open(AUTO_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
        for line in lines:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            last = ev
            if ev.get("event") == "switch":
                last_switch = ev
    except Exception:
        pass
    return last, last_switch


def ensure_tui_command():
    if not os.path.exists(TUI_CMD):
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(TUI_CMD, "w") as f:
            f.write(f"#!/bin/zsh\nexec {CSWAP} tui\n")
        os.chmod(TUI_CMD, 0o755)
    if not os.path.isdir(SECRETS_DIR):
        os.makedirs(SECRETS_DIR, exist_ok=True)
        os.chmod(SECRETS_DIR, 0o700)


def handle_action(argv):
    if argv[0] == "switch" and len(argv) > 1:
        subprocess.run([CSWAP, "switch", argv[1]], env=ENV, timeout=120)
    elif argv[0] == "refresh-stats":
        refresh_stats(force=True)
    elif argv[0] == "rebuild-ledger":
        # Drop the high-water marks and re-seed from what ccusage can still see.
        # Only for a day recorded too high -- this permanently forgets any day
        # whose transcripts have already been cleaned up.
        try:
            os.remove(LEDGER_PATH)
        except OSError:
            pass
        refresh_stats(force=True)
    elif argv[0] == "toggle-emails":
        if os.path.exists(HIDE_EMAILS_FLAG):
            os.remove(HIDE_EMAILS_FLAG)
        else:
            os.makedirs(CACHE_DIR, exist_ok=True)
            open(HIDE_EMAILS_FLAG, "w").close()
    elif argv[0] == "toggle-pet":
        if os.path.exists(HIDE_PET_FLAG):
            os.remove(HIDE_PET_FLAG)
        else:
            os.makedirs(CACHE_DIR, exist_ok=True)
            open(HIDE_PET_FLAG, "w").close()
    elif argv[0] == "pause-auto":
        pause_auto()
    elif argv[0] == "resume-auto":
        resume_auto()
    sys.exit(0)


def display_email(email, hidden):
    if not hidden:
        return email
    if email.endswith("@token.local"):
        return "API key account"
    local, _, domain = email.partition("@")
    return f"{local[:1]}•••@{domain[:1]}•••"


def claude_window(w):
    """Normalise one cswap window, recomputing its countdown from resets_at.

    cswap stores `countdown` and `clock` as strings frozen at fetch time, so a
    cache that stopped updating keeps reading like a live number: this Mac spent
    two weeks showing "resets 2h 24m" for a window that had expired on Aug 17,
    because the daemon was crashing every tick and nothing here noticed.

    resets_at is the only self-dating field, so the countdown is derived from it
    and a window whose reset has already gone by is marked stale instead of being
    drawn as a healthy gauge.
    """
    if not isinstance(w, dict):
        return None
    out = {
        "pct": float(w.get("pct") or 0),
        "clock": w.get("clock", "?"),
        "countdown": w.get("countdown", "?"),
        "stale": False,
        "age": None,
    }
    raw = w.get("resets_at")
    if not raw:
        return out  # scoped entries carry only a pct
    try:
        at = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except Exception:
        return out
    left = at - time.time()
    if left <= 0:
        out["stale"] = True
        out["age"] = -left
        out["countdown"] = "expired"
    else:
        out["countdown"] = human_dur(left)
    out["clock"] = local_clock_ts(at)
    return out


def account_windows(lastgood):
    """Yield (label, window) for every rate-limit window an account has."""
    if not lastgood:
        return
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = claude_window(lastgood.get(key))
        if w:
            yield label, w
    for scoped in lastgood.get("scoped") or []:
        w = claude_window(scoped)
        if w:
            yield scoped.get("name", "model")[:5], w


def print_daemon(daemon, hidden):
    """The claude-swap auto-switch lane. Only drawn when cswap is set up."""
    if auto_paused():
        print(f"Auto-switch: PAUSED (you turned it off) | color={ORANGE} size=12")
        print(
            f"   stays on the current account until you resume | size=11 color={GRAY} trim=false"
        )
        print(
            f"▶ Resume auto-switch | bash={PLUGIN} param1=resume-auto "
            f"terminal=false refresh=true size=12 color={GREEN}"
        )
    elif daemon:
        last, last_switch = last_log_events()
        detail = ""
        if last:
            try:
                ts = datetime.datetime.fromisoformat(
                    last["ts"].replace("Z", "+00:00")
                ).timestamp()
                detail = f" · last check {rel_age(ts)}"
            except Exception:
                pass
        # A live process is not a working one. cswap kept ticking for two weeks
        # while every tick died on an OverflowError, and this row stayed green
        # the whole time because it only checked that the daemon existed.
        failing = (last or {}).get("event") == "error"
        if failing:
            print(f"Auto-switch: running but FAILING{detail} | color={RED} size=12")
            print(
                f"   {str((last or {}).get('message'))[:70]} | "
                f"size=11 color={ORANGE} trim=false"
            )
        else:
            print(f"Auto-switch: running{detail} | color={GREEN} size=12")
        if last_switch:
            to = display_email((last_switch.get("to") or {}).get("email", "?"), hidden)
            print(f"   last switch → {to} ({last_switch.get('ts', '')}) | size=11 color={GRAY} trim=false")
        print(
            f"⏸ Pause auto-switch (stay on this account) | bash={PLUGIN} param1=pause-auto "
            f"terminal=false refresh=true size=12 color={ORANGE}"
        )
    else:
        print(f"Auto-switch daemon NOT running | color={RED} size=12")
        print(
            f"   ↳ start: launchctl kickstart {AUTO_TARGET} | "
            f"bash=/bin/launchctl param1=kickstart param2={AUTO_TARGET} "
            f"terminal=false refresh=true size=11 trim=false"
        )


def main():
    ensure_tui_command()
    seq = load_json(os.path.join(CSWAP_ROOT, "sequence.json")) or {}
    usage = (load_json(os.path.join(CSWAP_ROOT, "cache", "usage.json")) or {}).get("accounts", {})
    accounts = seq.get("accounts", {})
    active = seq.get("activeAccountNumber")
    order = seq.get("sequence") or sorted(int(k) for k in accounts)
    daemon = daemon_running()
    # Claude account gauges, the switcher and the daemon lane all come from
    # claude-swap. Codex-only users have none of it, and telling them a daemon
    # they never installed is down is a false alarm, so those lanes stay hidden.
    has_cswap = bool(accounts)
    hidden = os.path.exists(HIDE_EMAILS_FLAG)
    stats = refresh_stats(active_num=active)

    # ---- menu bar title ----
    act_usage = (usage.get(str(active)) or {}).get("lastGood") or {}

    def live_pct(key):
        """Percentage for one window, or None once its reset has gone by."""
        w = claude_window(act_usage.get(key))
        return None if not w or w.get("stale") else w["pct"]

    p5 = live_pct("five_hour")
    p7 = live_pct("seven_day")
    codex_usage = stats.get("codex_usage")
    # a cached reading is kept across a failed refresh, so expired windows are
    # dropped before anything colours the menu bar off them
    codex_wins = fresh_windows((codex_usage or {}).get("windows") or [])
    codex_pcts = [w["pct"] for _, w in codex_wins]
    # the warning colour tracks whichever agent is closest to its wall
    all_pcts = [
        w["pct"] for _, w in account_windows(act_usage) if not w.get("stale")
    ] + codex_pcts
    worst = max(all_pcts) if all_pcts else 0
    prefix = ""
    if has_cswap and not daemon:
        prefix = "⚠️ "
    elif worst >= 90:
        prefix = "🔴 "
    elif worst >= 75:
        prefix = "🟠 "
    circ = CIRCLED[active - 1] if active and active <= 10 else "•"
    # an account can report one window without the other; format whatever is there
    shown = [f"{pct:.0f}%" for pct in (p5, p7) if pct is not None]
    claude_bit = f"{circ} {'·'.join(shown)}" if shown else circ
    # both agents share one title line so neither is hidden behind a cycle
    codex_bit = f"  {CODEX_MARK} {max(codex_pcts):.0f}%" if codex_pcts else ""
    today = stats.get("today")
    block = stats.get("block")
    codex_today = ((stats.get("codex_spend") or {}).get("today") or {}).get("cost") or 0
    # the pet reacts to whichever agent is closest to its wall. limit_reached
    # gets the same freshness gate as the windows: once they have all expired the
    # cached flag describes a window that is already over.
    busy = bool((block or {}).get("perHour")) or codex_today > 0
    hit_limit = bool((codex_usage or {}).get("limit_reached")) and bool(codex_wins)
    mood = pet_mood(worst, busy, hit_limit)
    # two images: the pair for the menu bar title, the pet alone for its own row
    pet = pet_icon(mood)
    icon = title_icon(mood)
    # ONE title line. SwiftBar cycles multiple title lines in the bar but also
    # repeats every one of them at the top of the dropdown, so a second line
    # showed the icon and its text twice over. Spend rides along here instead,
    # and the hourly burn stays in the Block row below where it has room.
    spent = (today or {}).get("cost", 0) + codex_today
    money_bit = f"  ${spent:,.0f}" if spent else ""
    print(f"{prefix}{claude_bit}{codex_bit}{money_bit} | image={icon}")
    print("---")

    # ---- accounts ----
    if has_cswap:
        print(f"Claude Max accounts · active window used (5h·7d) | size=11 color={GRAY}")
    else:
        print(
            f"Claude accounts · claude-swap not set up, gauges off | size=11 color={GRAY}"
        )
    for num in order:  # empty when claude-swap has no accounts
        meta = accounts.get(str(num), {})
        email = meta.get("email", f"account {num}")
        is_active = num == active
        u = usage.get(str(num), {})
        lastgood = u.get("lastGood")
        circn = CIRCLED[num - 1] if num <= 10 else str(num)
        is_token_acct = email.endswith("@token.local")
        marker = "  ← active" if is_active else ""
        color = f" color={RUST}" if is_active else ""
        print(f"{circn} {display_email(email, hidden)}{marker} |{color} size=13")
        if is_token_acct:
            print(
                f"   API billing · excluded from auto-switch · manual only "
                f"| size=11 color={GRAY} trim=false"
            )
        if lastgood:
            print_gauges(account_windows(lastgood))
            err = u.get("lastError")
            if err:
                print(f"   ⚠ {str(err)[:60]} | size=11 color={ORANGE} trim=false")
        elif not is_token_acct:
            print(f"   no usage data yet | size=11 color={GRAY} trim=false")
        if not is_active:
            print(
                f"   ↳ switch to this account | bash={PLUGIN} param1=switch "
                f"param2={num} terminal=false refresh=true size=11 trim=false"
            )

    # ---- codex / chatgpt account ----
    print("---")
    if codex_usage:
        plan = (codex_usage.get("plan") or "?").title()
        who = display_email(codex_usage.get("email") or "?", hidden)
        print(f"Codex · ChatGPT {plan} · {who} | size=11 color={GRAY}")
        print_gauges(codex_wins)
        if not codex_wins and (codex_usage.get("windows") or []):
            print(
                f"   windows expired · awaiting a fresh reading "
                f"| size=11 color={GRAY} trim=false"
            )
        if codex_usage.get("limit_reached"):
            print(f"   ⚠ rate limit reached | size=11 color={RED} trim=false")
        credits = codex_usage.get("credits") or {}
        if credits.get("unlimited"):
            note = "credits: unlimited"
        elif credits.get("has"):
            note = f"credits: balance {credits.get('balance')}"
        else:
            note = "credits: none (plan allowance only)"
        print(f"   {note} | size=11 color={GRAY} trim=false")
        if pet:
            # the pet alone here: this row is about him, and the composite
            # would paste the Claude glyph in front of his own name
            print(
                f"   {PET_LABEL} · {PET_CAPTIONS.get(mood, mood)} | image={pet} "
                f"size=11 color={GRAY} trim=false"
            )
        if stats.get("codex_error"):
            print(
                f"   ⚠ refresh failed ({stats['codex_error']}) · showing the reading from "
                f"{rel_age(codex_usage.get('at', 0))} | size=11 color={ORANGE} trim=false"
            )
    elif os.path.exists(CODEX_AUTH):
        why = f" ({stats['codex_error']})" if stats.get("codex_error") else ""
        print(f"Codex · ChatGPT | size=11 color={GRAY}")
        print(f"   usage unavailable{why} | size=11 color={ORANGE} trim=false")
    else:
        print(f"Codex · not signed in — run: codex login | size=11 color={GRAY}")

    # ---- spend stats ----
    print("---")
    print(f"API-equivalent spend · this Mac | size=11 color={GRAY}")
    month = stats.get("month")
    alltime = stats.get("alltime")
    print(f"Claude Code | size=11 color={GRAY}")
    if today or alltime:
        rows = []
        if today:
            rows.append(("Today", f"{money(today['cost'])} · {tokens_h(today['tokens'])} tok"))
        if block:
            rows.append(
                (
                    "Block",
                    f"{money(block['cost'])} · {money(block['perHour'])}/hr → "
                    f"{money(block['projCost'])} by {local_clock(block.get('end') or '')}",
                )
            )
        elif block is None and "fastAt" in stats:
            rows.append(("Block", "no active 5h block"))
        if month:
            rows.append(("Month", money(month["cost"])))
        if alltime:
            rows.append(
                (
                    "Total",
                    f"{money(alltime['cost'])} · {tokens_h(alltime['tokens'])} tok "
                    f"· {alltime['days']}d",
                )
            )
        print_rows(rows)
        print_unpriced(
            stats.get("unpriced"), "add it to pricingOverrides in ~/.claude/ccusage.json"
        )
    else:
        print(f"   not available yet (ccusage) | size=11 color={GRAY}")

    codex_spend = stats.get("codex_spend") or {}
    print(f"Codex · ChatGPT | size=11 color={GRAY}")
    if (codex_spend.get("alltime") or {}).get("days"):
        print_rows(
            [
                (
                    "Today",
                    f"{money(codex_spend['today']['cost'])} · "
                    f"{tokens_h(codex_spend['today']['tokens'])} tok",
                ),
                (
                    "Month",
                    f"{money(codex_spend['month']['cost'])} · "
                    f"{tokens_h(codex_spend['month']['tokens'])} tok",
                ),
                (
                    "Total",
                    f"{money(codex_spend['alltime']['cost'])} · "
                    f"{tokens_h(codex_spend['alltime']['tokens'])} tok "
                    f"· {codex_spend['alltime']['days']}d",
                ),
            ]
        )
        print_unpriced(
            codex_spend.get("unpriced"), f"add a price in {CODEX_PRICES_PATH}"
        )
    else:
        print(f"   no Codex sessions on this Mac | size=11 color={GRAY}")

    # the two numbers that answer "what have these agents cost me", combined
    both_month = ((month or {}).get("cost") or 0) + (
        (codex_spend.get("month") or {}).get("cost") or 0
    )
    claude_all = alltime or {}
    codex_all = codex_spend.get("alltime") or {}
    both_all = (claude_all.get("cost") or 0) + (codex_all.get("cost") or 0)
    both_tokens = (claude_all.get("tokens") or 0) + (codex_all.get("tokens") or 0)
    if both_all or both_month:
        print(f"Both agents | size=11 color={GRAY}")
        print(f"{'Month':<6} {money(both_month)} | font=Menlo size=12 trim=false")
        print(
            f"{'Total':<6} {money(both_all)} · {tokens_h(both_tokens)} tok "
            f"| font=Menlo size=12 color={RUST} trim=false"
        )

    # ---- credits & billing lanes ----
    print("---")
    active_email = display_email(accounts.get(str(active), {}).get("email", "?"), hidden)
    whose = f" · {active_email}" if has_cswap else ""
    print(f"Credits & billing{whose} | size=11 color={GRAY}")
    credits = stats.get("credits")
    if credits:
        sp = credits.get("spend") or {}
        eu = credits.get("extra_usage") or {}
        if sp.get("enabled"):
            used = minor_to_usd(sp.get("used"))
            line = f"Usage credits: {money(used or 0)} used"
            bal = minor_to_usd(sp.get("balance"))
            if bal is not None:
                line += f" · balance {money(bal)}"
            if sp.get("percent"):
                line += f" ({sp['percent']}%)"
            color = state_color(float(sp.get("percent") or 0))
            print(f"{line} | font=Menlo size=12 trim=false color={color}")
        else:
            print(f"Usage credits: off | size=12 color={GRAY}")
        if eu.get("is_enabled"):
            used = eu.get("used_credits")
            dp = eu.get("decimal_places")
            shown = money(used / 10 ** dp) if used is not None and dp else str(used)
            line = f"Extra usage: {shown} used"
            if eu.get("monthly_limit"):
                lim = eu["monthly_limit"] / 10 ** dp if dp else eu["monthly_limit"]
                line += f" of {money(lim) if dp else lim}"
            print(f"{line} | font=Menlo size=12 trim=false")
        else:
            print(f"Extra usage: off | size=12 color={GRAY}")
    else:
        print(f"credit state unavailable | size=11 color={GRAY}")
    api_cost = stats.get("api_cost")
    if api_cost is None:
        print(
            f"API credits (Console): not tracked · add admin key to "
            f"~/.swiftbar/.secrets/anthropic-admin-key | size=11 color={GRAY}"
        )
    elif api_cost.get("error"):
        print(f"API credits (Console): fetch failed ({api_cost['error']}) | size=11 color={ORANGE}")
    else:
        print(
            f"API credits (Console): {money(api_cost['month_usd'])} this month "
            f"| font=Menlo size=12 trim=false"
        )

    # ---- auto-switch daemon ----
    if has_cswap:
        print("---")
        print_daemon(daemon, hidden)

    # ---- actions ----
    print("---")
    pet_label = "Show the Codex pet" if os.path.exists(HIDE_PET_FLAG) else "Hide the Codex pet"
    print(f"{pet_label} | bash={PLUGIN} param1=toggle-pet terminal=false refresh=true")
    toggle_label = "Show emails" if hidden else "Hide emails"
    print(
        f"{toggle_label} | bash={PLUGIN} param1=toggle-emails terminal=false refresh=true"
    )
    if has_cswap:
        print(
            f"Open cswap dashboard (TUI) | bash=/usr/bin/open param1={TUI_CMD} terminal=false"
        )
    print(
        f"Open Codex usage settings | bash=/usr/bin/open "
        f"param1=https://chatgpt.com/codex/settings/usage terminal=false"
    )
    if has_cswap:
        print(f"Open auto-switch log | bash=/usr/bin/open param1={AUTO_LOG} terminal=false")
    print(
        f"Refresh stats now | bash={PLUGIN} param1=refresh-stats terminal=false refresh=true"
    )
    print(
        f"Rebuild spend ledger | bash={PLUGIN} param1=rebuild-ledger "
        f"terminal=false refresh=true alternate=true"
    )
    print(f"claude-swap + ccusage + codex · updates every 1m | size=10 color={GRAY}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        handle_action(sys.argv[1:])
    main()
