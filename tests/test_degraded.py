#!/usr/bin/env python3
"""Each lane must stand alone. Run: python3 tests/test_degraded.py

Renders the menu with one agent's data missing and asserts the other still
draws, with no false alarm about tooling the user never installed. Nothing here
touches the network or the real cache: every path is pointed at a temp dir.
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile

PLUGIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agentbar.1m.py")


def load(tmp, *, cswap=True, codex=True):
    """Fresh module with every filesystem and network dependency stubbed out."""
    spec = importlib.util.spec_from_file_location("agentbar_" + str(id(tmp)), PLUGIN)
    ab = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ab)

    ab.CACHE_DIR = os.path.join(tmp, "cache")
    ab.STATS_PATH = os.path.join(ab.CACHE_DIR, "stats.json")
    ab.LEDGER_PATH = os.path.join(ab.CACHE_DIR, "ledger.json")
    ab.TUI_CMD = os.path.join(ab.CACHE_DIR, "tui.command")
    ab.SECRETS_DIR = os.path.join(tmp, "secrets")
    ab.ADMIN_KEY_PATH = os.path.join(ab.SECRETS_DIR, "none")
    ab.HIDE_EMAILS_FLAG = os.path.join(ab.CACHE_DIR, "hide")
    ab.PAUSE_FLAG = os.path.join(ab.CACHE_DIR, "paused")
    ab.CSWAP_ROOT = os.path.join(tmp, "cswap") if cswap else os.path.join(tmp, "gone")
    ab.CODEX_AUTH = os.path.join(tmp, "codex-auth.json") if codex else os.path.join(tmp, "gone.json")

    ab.daemon_running = lambda: False
    ab.ccusage = lambda args: None
    ab.fetch_credit_state = lambda: None
    ab.fetch_api_month_cost = lambda: None
    ab.codex_summary = lambda: {
        "today": {"cost": 0.0, "tokens": 0},
        "month": {"cost": 1.5, "tokens": 1000},
        "alltime": {"cost": 9.0, "tokens": 5000, "days": 3},
        "unpriced": [],
        "last_day": "2026-08-30",
    }
    if codex:
        ab.fetch_codex_usage = lambda: {
            "plan": "pro",
            "email": "someone@example.com",
            "windows": [
                ("7d", {"pct": 12.0, "clock": "16:05", "countdown": "6d 2h", "minutes": 10080, "at": 1}),
            ],
            "limit_reached": False,
            "credits": {"has": False, "unlimited": False, "balance": "0"},
            "at": 1788209794,
        }
    else:
        ab.fetch_codex_usage = lambda: None
        ab.codex_summary = lambda: {
            "today": {"cost": 0.0, "tokens": 0},
            "month": {"cost": 0.0, "tokens": 0},
            "alltime": {"cost": 0.0, "tokens": 0, "days": 0},
            "unpriced": [],
            "last_day": None,
        }

    if cswap:
        os.makedirs(os.path.join(ab.CSWAP_ROOT, "cache"), exist_ok=True)
        with open(os.path.join(ab.CSWAP_ROOT, "sequence.json"), "w") as f:
            f.write('{"activeAccountNumber":1,"sequence":[1],'
                    '"accounts":{"1":{"email":"me@example.com"}}}')
        with open(os.path.join(ab.CSWAP_ROOT, "cache", "usage.json"), "w") as f:
            f.write('{"accounts":{"1":{"lastGood":{"five_hour":{"pct":40,"clock":"18:00",'
                    '"countdown":"1h 0m"},"seven_day":{"pct":55,"clock":"Sep 3","countdown":"3d 0h"}}}}}')
    return ab


def render(ab):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ab.main()
    return buf.getvalue()


def test_codex_only():
    with tempfile.TemporaryDirectory() as tmp:
        out = render(load(tmp, cswap=False, codex=True))
    title = out.splitlines()[0]
    assert "⚠️" not in title, f"false daemon alarm for a Codex-only user: {title}"
    assert "✳" in title, title
    assert "Auto-switch" not in out, "cswap daemon lane drawn without cswap"
    assert "cswap dashboard" not in out, "cswap action drawn without cswap"
    assert "claude-swap not set up" in out, "no explanation for the missing Claude rows"
    assert "Codex · ChatGPT Pro" in out, "Codex lane missing"
    assert "12% used" in out, "Codex gauge missing"
    print("ok   Codex-only: Codex draws, no false claude-swap alarm")


def test_claude_only():
    with tempfile.TemporaryDirectory() as tmp:
        out = render(load(tmp, cswap=True, codex=False))
    title = out.splitlines()[0]
    assert "✳" not in title, f"Codex shown in title with no Codex: {title}"
    assert "40%·55%" in title, title
    assert "Codex · not signed in" in out, "no hint about signing in to Codex"
    assert "Auto-switch" in out, "cswap lane missing when cswap is present"
    assert "no Codex sessions on this Mac" in out
    print("ok   Claude-only: Claude draws, Codex lane says how to enable it")


def test_neither():
    with tempfile.TemporaryDirectory() as tmp:
        out = render(load(tmp, cswap=False, codex=False))
    assert out.splitlines()[0].strip().startswith("•"), out.splitlines()[0]
    assert "⚠️" not in out.splitlines()[0]
    assert "---" in out, "menu body missing entirely"
    print("ok   Neither configured: still renders a sane, quiet menu")


if __name__ == "__main__":
    test_codex_only()
    test_claude_only()
    test_neither()
    print("\nall checks passed")
