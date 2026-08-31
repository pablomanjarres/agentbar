#!/usr/bin/env python3
"""SwiftBar plugin: one menu bar item for Claude Code and OpenAI Codex.

Both agents answer the same two questions: how much of the rate-limit window
is gone, and what the tokens would have cost at API rates.

Claude data sources (all local):
  - ~/.claude-swap-backup/sequence.json + cache/usage.json  (written by the
    `cswap auto` daemon every 60-90s; read here, never fetched)
  - ccusage (npm) over ~/.claude/projects JSONL transcripts, --offline pricing

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
import datetime
import json
import os
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
CODEX_SCAN_VERSION = 1
CONFIG_DIR = os.path.join(HOME, ".config", "agentbar")
CODEX_PRICES_PATH = os.path.join(CONFIG_DIR, "codex-prices.json")
# Every documented /backend-api/codex/* usage path 403s; this is the one the CLI
# itself reads, and it only answers with the originator header set.
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_UA = "codex_cli_rs/0.142.5"
CODEX_MARK = "\u2733"  # the mark next to Claude's circled number in the title

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
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def ccusage(args):
    out = run(["ccusage"] + args + ["--json", "--offline"], timeout=60)
    return json.loads(out) if out else None


def update_ledger(rows):
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
    """
    led = load_json(LEDGER_PATH) or {}
    changed = False
    for r in rows or []:
        day = r.get("period")
        if not day:
            continue
        prev = led.get(day) or {}
        cost = max(r.get("totalCost") or 0, prev.get("cost") or 0)
        tokens = max(r.get("totalTokens") or 0, prev.get("tokens") or 0)
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

    Returns None when no admin key file is configured (the lane stays hidden),
    {"error": ...} on fetch failure, {"month_usd": float} on success.
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
    drawn twice.
    """
    out, seen = [], set()

    def add(label, w):
        key = (w["minutes"], w["at"], w["pct"])
        if key in seen:
            return
        seen.add(key)
        out.append((label, w))

    top = data.get("rate_limit") or {}
    for key in ("primary_window", "secondary_window"):
        w = codex_window(top.get(key))
        if w:
            add(win_label(w["minutes"]), w)
    for extra in data.get("additional_rate_limits") or []:
        name = (extra.get("limit_name") or "model").replace("GPT-", "")
        sub = extra.get("rate_limit") or {}
        for key in ("primary_window", "secondary_window"):
            w = codex_window(sub.get(key))
            if w:
                add(f"{name} {win_label(w['minutes'])}", w)
    return out


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
    except Exception as e:
        return {"error": type(e).__name__}
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


def refresh_stats(force=False, active_num=None):
    os.makedirs(CACHE_DIR, exist_ok=True)
    st = load_json(STATS_PATH) or {}
    now = time.time()
    changed = False

    if force or now - st.get("fastAt", 0) > FAST_TTL:
        daily = ccusage(["daily"])
        if daily:
            today = datetime.date.today().isoformat()
            row = next((r for r in daily.get("daily", []) if r.get("period") == today), None)
            st["today"] = {
                "cost": row.get("totalCost", 0) if row else 0,
                "tokens": row.get("totalTokens", 0) if row else 0,
            }
            st["unpriced"] = unpriced_models(row)
            # Month + Total come from the ledger, not from ccusage's live totals:
            # ccusage only sees transcripts that still exist, so its figures shrink
            # as Claude Code's 30-day cleanup eats history. See update_ledger().
            led = update_ledger(daily.get("daily", []))
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
        blocks = ccusage(["blocks"])
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
    if force or acct_changed or now - st.get("creditsAt", 0) > MONTH_TTL:
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


def account_windows(lastgood):
    """Yield (label, window_dict) for every rate-limit window an account has."""
    if not lastgood:
        return
    if lastgood.get("five_hour"):
        yield "5h", lastgood["five_hour"]
    if lastgood.get("seven_day"):
        yield "7d", lastgood["seven_day"]
    for scoped in lastgood.get("scoped") or []:
        yield scoped.get("name", "model")[:5], scoped


def main():
    ensure_tui_command()
    seq = load_json(os.path.join(CSWAP_ROOT, "sequence.json")) or {}
    usage = (load_json(os.path.join(CSWAP_ROOT, "cache", "usage.json")) or {}).get("accounts", {})
    accounts = seq.get("accounts", {})
    active = seq.get("activeAccountNumber")
    order = seq.get("sequence") or sorted(int(k) for k in accounts)
    daemon = daemon_running()
    hidden = os.path.exists(HIDE_EMAILS_FLAG)
    stats = refresh_stats(active_num=active)

    # ---- menu bar title ----
    act_usage = (usage.get(str(active)) or {}).get("lastGood") or {}
    p5 = (act_usage.get("five_hour") or {}).get("pct")
    p7 = (act_usage.get("seven_day") or {}).get("pct")
    codex_usage = stats.get("codex_usage")
    codex_pcts = [w["pct"] for _, w in (codex_usage or {}).get("windows") or []]
    # the warning colour tracks whichever agent is closest to its wall
    all_pcts = [w["pct"] for _, w in account_windows(act_usage)] + codex_pcts
    worst = max(all_pcts) if all_pcts else 0
    prefix = ""
    if not daemon:
        prefix = "⚠️ "
    elif worst >= 90:
        prefix = "🔴 "
    elif worst >= 75:
        prefix = "🟠 "
    circ = CIRCLED[active - 1] if active and active <= 10 else "•"
    claude_bit = f"{circ} {p5:.0f}%·{p7:.0f}%" if p5 is not None else circ
    # both agents share one title line so neither is hidden behind a cycle
    codex_bit = f"  {CODEX_MARK} {max(codex_pcts):.0f}%" if codex_pcts else ""
    print(f"{prefix}{claude_bit}{codex_bit} | image={ICON}")
    today = stats.get("today")
    block = stats.get("block")
    codex_today = ((stats.get("codex_spend") or {}).get("today") or {}).get("cost") or 0
    if today or codex_today:
        # second title line -> SwiftBar cycles them (usage <-> live spend)
        spend = f"${(today or {}).get('cost', 0) + codex_today:,.0f} today"
        if block and block.get("perHour"):
            spend += f" · ${block['perHour']:,.0f}/hr"
        print(f"{prefix}{spend} | image={ICON}")
    print("---")

    # ---- accounts ----
    print(f"Claude Max accounts · active window used (5h·7d) | size=11 color={GRAY}")
    for num in order:
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
        print_gauges(codex_usage.get("windows") or [])
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

    if month and codex_spend.get("month"):
        both = (month.get("cost") or 0) + (codex_spend["month"].get("cost") or 0)
        print(
            f"Both   {money(both)} this month, both agents "
            f"| font=Menlo size=12 color={RUST} trim=false"
        )

    # ---- credits & billing lanes ----
    print("---")
    active_email = display_email(accounts.get(str(active), {}).get("email", "?"), hidden)
    print(f"Credits & billing · {active_email} | size=11 color={GRAY}")
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
    print("---")
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

    # ---- actions ----
    print("---")
    toggle_label = "Show emails" if hidden else "Hide emails"
    print(
        f"{toggle_label} | bash={PLUGIN} param1=toggle-emails terminal=false refresh=true"
    )
    print(f"Open cswap dashboard (TUI) | bash=/usr/bin/open param1={TUI_CMD} terminal=false")
    print(
        f"Open Codex usage settings | bash=/usr/bin/open "
        f"param1=https://chatgpt.com/codex/settings/usage terminal=false"
    )
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
