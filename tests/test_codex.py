#!/usr/bin/env python3
"""Checks for the Codex lane. Run: python3 tests/test_codex.py [--live]

The pure-arithmetic checks always run. The scan checks read whatever is in
~/.codex and skip themselves when Codex was never used on this machine. --live
adds one call to the ChatGPT usage endpoint.
"""
import importlib.util
import os
import sys
import time

PLUGIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agentbar.1m.py")


def load():
    spec = importlib.util.spec_from_file_location("agentbar", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_labels(ab):
    assert ab.win_label(300) == "5h"
    assert ab.win_label(10080) == "7d"
    assert ab.win_label(60) == "1h"
    assert ab.win_label(90) == "90m"
    assert ab.win_label(0) == "win"
    assert ab.human_dur(0) == "0m"
    assert ab.human_dur(3600 * 5 + 60) == "5h 1m"
    assert ab.human_dur(604800) == "7d 0h"
    assert ab.human_dur(None) == "?"
    assert ab.local_clock("2026-04-23T22:42:07.882Z") != "?"
    assert ab.local_clock("not a date") == "?"
    assert ab.local_clock_ts(1788227814) != "?"
    print("ok   window labels, countdowns and clocks")


def test_cost(ab):
    prices = ab.codex_prices()
    assert "gpt-5.5" in prices
    # gpt-5.3-codex-spark has no API price at all, so it must stay absent and
    # report None rather than inheriting its parent model's rate.
    assert "gpt-5.3-codex-spark" not in prices
    assert ab.codex_cost("gpt-5.3-codex-spark", {"input_tokens": 999}, prices) is None

    # input_tokens already CONTAINS cached_input_tokens: 1M in, half of it cached,
    # 100k out on gpt-5.5 (5.00 / 0.50 / 30.00) is 0.5M*5 + 0.5M*0.5 + 0.1M*30.
    got = ab.codex_cost(
        "gpt-5.5",
        {"input_tokens": 1_000_000, "cached_input_tokens": 500_000, "output_tokens": 100_000},
        prices,
    )
    assert abs(got - 5.75) < 1e-9, got

    # all-cached input must never be billed twice
    allcached = ab.codex_cost(
        "gpt-5.5",
        {"input_tokens": 1_000_000, "cached_input_tokens": 1_000_000, "output_tokens": 0},
        prices,
    )
    assert abs(allcached - 0.5) < 1e-9, allcached

    # gpt-5.6-sol is what Codex defaults to since 0.151; 1M fresh in + 100k out
    # at 4.00 / 0.40 / 20.00 is 4 + 2.
    sol = ab.codex_cost(
        "gpt-5.6-sol",
        {"input_tokens": 1_000_000, "cached_input_tokens": 0, "output_tokens": 100_000},
        prices,
    )
    assert sol is not None, "gpt-5.6-sol has no price"
    assert abs(sol - 6.0) < 1e-9, sol
    print("ok   cached input billed as a slice, unpriced models return None")


def test_rollout_deltas(ab):
    """The parser must reproduce a session's final cumulative reading exactly."""
    import glob
    import json

    files = glob.glob(os.path.join(ab.CODEX_SESSIONS, "**", "*.jsonl"), recursive=True)
    files = [f for f in files if os.path.getsize(f) > 0]
    if not files:
        print("skip Codex rollout checks (no sessions in ~/.codex)")
        return
    biggest = max(files, key=os.path.getsize)

    final = None
    for line in open(biggest, errors="replace"):
        if '"token_count"' not in line:
            continue
        try:
            info = (json.loads(line).get("payload") or {}).get("info") or {}
        except Exception:
            continue
        if info.get("total_token_usage"):
            final = info["total_token_usage"].get("total_tokens")
    if not final:
        print("skip delta check (largest session records no token_count)")
        return

    summed = sum(
        counts["total_tokens"]
        for models in ab.parse_rollout(biggest).values()
        for counts in models.values()
    )
    assert summed == final, f"delta sum {summed:,} != final cumulative {final:,}"
    print(f"ok   delta walk matches final cumulative exactly ({final:,} tokens)")


def test_history_imports_are_not_usage(ab):
    """Codex Desktop's imported legacy threads are not API usage.

    Each carries a single token_count with only total_tokens set: no input, no
    output, and no turn_context, so there is no model to attribute it to. They
    used to land in an "unknown" bucket and trip the unpriced warning for spend
    that never happened.
    """
    import json
    import tempfile

    stamp = "2026-09-01T12:00:00.000Z"
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 640274,
    }
    rows = [
        {
            "timestamp": stamp,
            "type": "session_meta",
            "payload": {"id": "x", "history_mode": "legacy", "cli_version": "0.151.0-alpha.7.2"},
        },
        {
            "timestamp": stamp,
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": usage}},
        },
    ]

    def parse(rows):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        try:
            return ab.parse_rollout(fh.name)
        finally:
            os.unlink(fh.name)

    assert parse(rows) == {}, parse(rows)

    # the same shape with a real turn behind it still counts, under that model
    rows.insert(1, {"timestamp": stamp, "type": "turn_context", "payload": {"model": "gpt-5.6-sol"}})
    usage.update({"input_tokens": 600_000, "output_tokens": 40_274})
    days = parse(rows)
    assert list(days) == ["2026-09-01"], days
    assert list(days["2026-09-01"]) == ["gpt-5.6-sol"], days
    assert days["2026-09-01"]["gpt-5.6-sol"]["total_tokens"] == 640274
    print("ok   imported legacy threads are skipped, real turns still counted")


def test_scan_cache(ab):
    t0 = time.time()
    first = ab.codex_summary()
    cold = time.time() - t0
    t0 = time.time()
    second = ab.codex_summary()
    warm = time.time() - t0
    assert first == second, "cached scan disagreed with the cold scan"
    for scope in ("today", "month", "alltime"):
        assert first[scope]["cost"] >= 0
        assert first[scope]["tokens"] >= 0
    assert first["alltime"]["tokens"] >= first["month"]["tokens"] >= first["today"]["tokens"]
    assert first["alltime"]["cost"] + 1e-9 >= first["month"]["cost"]
    print(f"ok   scan cache is stable and consistent (cold {cold:.2f}s, warm {warm:.2f}s)")


def test_window_dedupe(ab):
    """A per-model bucket restating an account window must not be drawn twice."""
    shared = {"used_percent": 0, "limit_window_seconds": 604800, "reset_at": 1788814614}
    data = {
        "rate_limit": {"primary_window": shared, "secondary_window": None},
        "additional_rate_limits": [
            {
                "limit_name": "GPT-5.3-Codex-Spark",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 4,
                        "limit_window_seconds": 18000,
                        "reset_at": 1788227814,
                    },
                    "secondary_window": shared,
                },
            }
        ],
    }
    windows = ab.codex_windows(data)
    labels = [label for label, _ in windows]
    assert labels == ["7d", "5.3-Codex-Spark 5h"], labels
    assert ab.codex_windows({}) == []
    print("ok   duplicate account window dropped, model bucket kept")


def test_distinct_models_survive(ab):
    """Two models can share a length, reset and usage without being the same limit.

    Any two buckets sitting at 0% just after a reset look identical on those three
    fields. Deduping on them alone made a real limit disappear from the menu.
    """
    same = {"used_percent": 0, "limit_window_seconds": 18000, "reset_at": 1788227814}
    data = {
        "rate_limit": {"primary_window": None, "secondary_window": None},
        "additional_rate_limits": [
            {"limit_name": "GPT-5.3-Codex-Spark", "rate_limit": {"primary_window": same}},
            {"limit_name": "GPT-5.5-Pro", "rate_limit": {"primary_window": same}},
        ],
    }
    labels = [label for label, _ in ab.codex_windows(data)]
    assert labels == ["5.3-Codex-Spark 5h", "5.5-Pro 5h"], labels
    print("ok   two different models at the same usage both survive")


def test_fetch_guards_shape_drift(ab):
    """wham/usage is undocumented; a shape change must not escape into main()."""
    original_http, original_auth = ab.http_json, ab.codex_auth
    try:
        ab.codex_auth = lambda: ("token", "account")
        ab.http_json = lambda *a, **k: {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 5,
                    "limit_window_seconds": 300,
                    "reset_at": "2026-09-01T00:00:00Z",  # epoch turned into a string
                }
            },
        }
        got = ab.fetch_codex_usage()
        assert got and got.get("error"), got
    finally:
        ab.http_json, ab.codex_auth = original_http, original_auth
    print("ok   endpoint shape drift returns an error, does not raise")


def test_fresh_windows(ab):
    """A cached window past its own reset says nothing about now."""
    future = time.time() + 3600
    windows = [
        ("5h", {"pct": 95, "at": 1000, "minutes": 300}),
        ("7d", {"pct": 10, "at": future, "minutes": 10080}),
        ("no-reset", {"pct": 3, "at": None, "minutes": 60}),
    ]
    kept = [label for label, _ in ab.fresh_windows(windows)]
    assert kept == ["7d", "no-reset"], kept
    assert ab.fresh_windows([]) == []
    print("ok   expired windows dropped, undated ones kept")


def test_live(ab):
    usage = ab.fetch_codex_usage()
    if usage is None:
        print("skip live usage (Codex not logged in)")
        return
    assert not usage.get("error"), usage
    assert usage["windows"], "endpoint returned no rate-limit windows"
    for _label, w in usage["windows"]:
        assert 0 <= w["pct"] <= 100
    print(f"ok   live usage: plan={usage['plan']}, {len(usage['windows'])} window(s)")


def main():
    ab = load()
    test_labels(ab)
    test_cost(ab)
    test_window_dedupe(ab)
    test_distinct_models_survive(ab)
    test_fetch_guards_shape_drift(ab)
    test_fresh_windows(ab)
    test_rollout_deltas(ab)
    test_history_imports_are_not_usage(ab)
    test_scan_cache(ab)
    if "--live" in sys.argv:
        test_live(ab)
    else:
        print("skip live usage (pass --live to hit the network)")
    print("\nall checks passed")


if __name__ == "__main__":
    main()
