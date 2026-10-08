#!/usr/bin/env python3
"""Spend summaries stay compact while amounts and warnings remain accessible."""
import contextlib
import importlib.util
import io
import os
import re


PLUGIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agentbar.1m.py")


def load():
    spec = importlib.util.spec_from_file_location("agentbar_spend", PLUGIN)
    ab = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ab)
    ab.CODEX_PRICES_PATH = "/tmp/agentbar-spend-test/codex-prices.json"
    return ab


def stats_fixture():
    return {
        "today": {"cost": 12.50, "tokens": 3_000_000},
        "month": {"cost": 125.75},
        "alltime": {"cost": 1000.25, "tokens": 250_000_000, "days": 110},
        "block": {
            "cost": 3.75, "perHour": 1.25, "projCost": 6.50,
            "end": "2026-10-07T23:30:00Z",
        },
        "fastAt": 1791410000,
        "unpriced": [],
        "codex_spend": {
            "today": {"cost": 2.25, "tokens": 50_000},
            "month": {"cost": 20.25, "tokens": 1_000_000},
            "alltime": {"cost": 75.50, "tokens": 10_000_000, "days": 5},
            "unpriced": [],
            "last_day": "2026-10-07",
        },
    }


def render(ab, stats):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        ab.print_spend(stats)
    return [line.split(" |", 1)[0] for line in output.getvalue().splitlines() if line]


def top_rows(lines):
    return [line for line in lines if not line.startswith("--") and line != "---"]


def provider_row(lines, provider):
    return next(line for line in top_rows(lines) if line.lstrip("⚠ ").startswith(provider))


def details(lines, provider):
    row = provider_row(lines, provider)
    start = lines.index(row) + 1
    children = []
    for line in lines[start:]:
        if not line.startswith("--"):
            break
        children.append(line[2:].strip())
    return "\n".join(children)


def assert_amount(row, period, expected):
    amount = r"\$(\d[\d,]*\.\d{2})"
    match = re.search(rf"\b{period}\s+{amount}", row, re.I)
    if not match:
        match = re.search(rf"{amount}\s+{period}\b", row, re.I)
    assert match and match.group(1) == expected, row


def test_compact_provider_sums_and_details():
    lines = render(load(), stats_fixture())
    top = top_rows(lines)
    assert top[0] == "API-equivalent spend · this Mac", top
    assert len(top) == 4, top
    # Combined amounts are hand-derived: 12.50 + 2.25 and 125.75 + 20.25.
    for provider, today, month in (
        ("Claude", "12.50", "125.75"),
        ("Codex", "2.25", "20.25"),
        ("Both", "14.75", "146.00"),
    ):
        row = provider_row(lines, provider)
        assert_amount(row, "Today", today)
        assert_amount(row, "Month", month)
        assert "⚠" not in row, row
    for extra in ("tok", "Total", "Block", "/hr", "→"):
        assert extra not in "\n".join(top), top
    claude = details(lines, "Claude")
    codex = details(lines, "Codex")
    both = details(lines, "Both")
    for extra in ("3.0M tok", "Total", "$1,000.25", "250.0M tok", "110d",
                  "Block", "$3.75", "$1.25/hr", "$6.50"):
        assert extra in claude, (extra, claude)
    for extra in ("50.0K tok", "1.0M tok", "Total", "$75.50", "10.0M tok", "5d"):
        assert extra in codex, (extra, codex)
    # Lifetime totals also combine once: 1,000.25 + 75.50; 250M + 10M.
    for extra in ("Total", "$1,075.75", "260.0M tok"):
        assert extra in both, (extra, both)
    print("ok   three compact spend rows preserve provider sums and submenu details")


def test_missing_prices_stay_visible_on_affected_provider():
    for affected in ("Claude", "Codex"):
        ab = load()
        stats = stats_fixture()
        if affected == "Claude":
            stats["unpriced"] = ["claude-unpriced"]
            model, path = "claude-unpriced", "~/.claude/ccusage.json"
        else:
            stats["codex_spend"]["unpriced"] = ["codex-auto-review"]
            model, path = "codex-auto-review", ab.CODEX_PRICES_PATH
        lines = render(ab, stats)
        assert "⚠" in provider_row(lines, affected), lines
        other = "Codex" if affected == "Claude" else "Claude"
        assert "⚠" not in provider_row(lines, other), lines
        top = "\n".join(top_rows(lines))
        assert model not in top and path not in top, top
        warning = details(lines, affected)
        assert model in warning and path in warning, warning
        assert "no price" in warning, warning
    print("ok   missing-price marker stays visible; model and setup hint stay in submenu")


def test_unavailable_provider_does_not_fabricate_spend():
    for available in ("Claude", "Codex"):
        fixture = stats_fixture()
        stats = {"codex_spend": fixture["codex_spend"]} if available == "Codex" else {
            key: value for key, value in fixture.items() if key != "codex_spend"
        }
        lines = render(load(), stats)
        missing = "Claude" if available == "Codex" else "Codex"
        row = provider_row(lines, missing)
        assert "$" not in row, row
        assert any(hint in row.lower() for hint in ("not available", "unavailable", "no sessions", "no codex sessions")), row
        expected = ("2.25", "20.25") if available == "Codex" else ("12.50", "125.75")
        assert_amount(provider_row(lines, "Both"), "Today", expected[0])
        assert_amount(provider_row(lines, "Both"), "Month", expected[1])
    empty = render(load(), {})
    assert len(top_rows(empty)) == 4, empty
    assert not any("$" in row for row in top_rows(empty)), empty
    print("ok   unavailable providers explain missing data and do not fabricate amounts")


def test_no_active_block_remains_in_submenu():
    stats = stats_fixture()
    stats["block"] = None
    lines = render(load(), stats)
    assert "no active 5h block" in details(lines, "Claude"), lines
    assert not any("Block" in row for row in top_rows(lines)), lines
    print("ok   inactive block remains reachable without adding a top-level row")


if __name__ == "__main__":
    test_compact_provider_sums_and_details()
    test_missing_prices_stay_visible_on_affected_provider()
    test_unavailable_provider_does_not_fabricate_spend()
    test_no_active_block_remains_in_submenu()
    print("\nall checks passed")
