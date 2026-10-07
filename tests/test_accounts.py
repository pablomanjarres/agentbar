#!/usr/bin/env python3
"""Account summaries must fit while keeping switching and stale details reachable."""
import json
import os
import tempfile

from test_degraded import load, render


def test_five_accounts():
    with tempfile.TemporaryDirectory() as tmp:
        ab = load(tmp, codex=False)
        sequence = {
            "activeAccountNumber": 1,
            "sequence": [1, 2, 3, 4, 5, 6],
            "accounts": {
                str(n): {"email": f"account{n}@example.com"}
                for n in [1, 2, 4, 5, 6]
            },
        }
        sequence["accounts"]["3"] = {"email": "apikey@token.local"}
        with open(os.path.join(ab.CSWAP_ROOT, "sequence.json"), "w") as f:
            json.dump(sequence, f)
        cached = ab.load_json(os.path.join(ab.CSWAP_ROOT, "cache", "usage.json"))
        cached["accounts"]["2"] = {"lastError": "http-429"}
        with open(os.path.join(ab.CSWAP_ROOT, "cache", "usage.json"), "w") as f:
            json.dump(cached, f)
        output = render(ab)
    section = output.split("---")[1].splitlines()
    top = [line for line in section if line and not line.startswith("--")]
    assert len(top) == 7, f"five logins and an API key use {len(top)} top-level rows"
    assert "5h 40%" in top[1] and "7d 55%" in top[1], top[1]
    assert "http-429" in top[2], "failed account looks healthy in summary"
    assert "manual" in top[3], "API entry must stay manual-only"
    switches = [line for line in section if "param1=switch" in line]
    assert len(switches) == 5, switches
    assert all(line.startswith("--") for line in switches), switches
    assert not any("param2=1 " in line for line in switches), "active account is switchable"
    assert any("param2=3 " in line for line in switches), "manual API switching was removed"
    assert any(line.startswith("--") and "40% used" in line for line in section)
    print("ok   five logins fit; details and manual switching remain reachable")


def test_stale_summary():
    with tempfile.TemporaryDirectory() as tmp:
        ab = load(tmp, codex=False, stale=True)
        path = os.path.join(ab.CSWAP_ROOT, "cache", "usage.json")
        usage = ab.load_json(path)
        usage["accounts"]["1"]["lastGood"]["scoped"] = [{"name": "Fable", "pct": 0}]
        with open(path, "w") as f:
            json.dump(usage, f)
        output = render(ab)
    summary = output.split("---")[1].splitlines()[2]
    assert "5h stale" in summary and "7d stale" in summary, summary
    assert "40%" not in summary and "55%" not in summary, summary
    assert "stale reading" in output and "14d" in output
    fable = next(line for line in output.splitlines() if "Fable" in line)
    assert "stale reading" in fable, "undated model gauge from expired fetch looks live"
    print("ok   stale summary never displays an expired percentage")


def test_unmanaged_login():
    with tempfile.TemporaryDirectory() as tmp:
        ab = load(tmp, codex=False)
        ab.daemon_running = lambda: True
        ab.last_log_events = lambda: ({
            "ts": "2026-10-07T23:47:18Z",
            "event": "no-switch",
            "reason": "unmanaged-active-account",
        }, None)
        output = render(ab)
    assert "login not connected" in output, "unmanaged login shown as healthy running"
    assert "← active" not in output, "saved account falsely labelled current"
    assert "Auto-switch: running ·" not in output
    print("ok   unregistered current login cannot appear healthy or active")


if __name__ == "__main__":
    test_five_accounts()
    test_stale_summary()
    test_unmanaged_login()
    print("\nall checks passed")
