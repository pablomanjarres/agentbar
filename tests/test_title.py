#!/usr/bin/env python3
"""The menu bar titles. Run: python3 tests/test_title.py

The Claude item shows the pooled use of every account claude-swap rotates
through, so one exhausted account moves it by its share and not to 100%. The
Codex item carries Codex usage and the spend. Neither wears a colored dot.
"""
import json
import os
import tempfile

from test_degraded import load, render


def accounts_with(ab, readings, *, caps=None, api_key=False, seven=None, errors=(), policy=None):
    """Write a claude-swap sequence and usage cache with one 5h reading per account.

    A reading of None leaves that account with no 5h window at all.
    """
    ab.daemon_running = lambda: True  # a down daemon prefixes its own warning
    accounts = {str(n): {"email": f"a{n}@example.com", "uuid": f"uuid-{n}"} for n in readings}
    if api_key:
        accounts["9"] = {"email": "setup-token-9@token.local", "uuid": ""}
    with open(os.path.join(ab.CSWAP_ROOT, "sequence.json"), "w") as f:
        json.dump({"activeAccountNumber": min(readings), "sequence": sorted(int(n) for n in accounts),
                   "accounts": accounts}, f)
    cached = ab.load_json(os.path.join(ab.CSWAP_ROOT, "cache", "usage.json"))
    template = cached["accounts"]["1"]["lastGood"]["five_hour"]
    cached["accounts"] = {}
    for n, pct in readings.items():
        good = {} if pct is None else {"five_hour": dict(template, pct=pct)}
        if seven and n in seven:
            good["seven_day"] = dict(template, pct=seven[n])
        cached["accounts"][str(n)] = {"lastGood": good}
        if n in errors:
            cached["accounts"][str(n)]["lastError"] = "invalid_grant"
    with open(os.path.join(ab.CSWAP_ROOT, "cache", "usage.json"), "w") as f:
        json.dump(cached, f)
    if policy is not None:
        with open(os.path.join(ab.CSWAP_ROOT, "autoswitch-policy.json"), "w") as f:
            json.dump(policy, f)
    elif caps:
        with open(os.path.join(ab.CSWAP_ROOT, "autoswitch-policy.json"), "w") as f:
            json.dump({"schemaVersion": 1, "defaultUsageCap": 100,
                       "accountUsageCaps": {f"uuid-{n}": cap for n, cap in caps.items()}}, f)


def test_one_exhausted_account_is_a_quarter():
    with tempfile.TemporaryDirectory() as tmp:
        ab = load(tmp, codex=False)
        accounts_with(ab, {1: 100, 2: 0, 3: 0, 4: 0}, api_key=True)
        title = render(ab).splitlines()[0]
    assert title.split("|")[0].strip() == "25%", title
    print("ok   one spent account of four reads as 25%, API keys stay out of the pool")


def test_capped_account_counts_up_to_its_cap():
    with tempfile.TemporaryDirectory() as tmp:
        ab = load(tmp, codex=False)
        # account 1 stops at 60%: a 400% + 60% pool, with 1 and 2 both spent
        accounts_with(ab, {1: 75, 2: 100, 3: 0, 4: 0, 5: 0}, caps={1: 60})
        out = render(ab)
    title = out.splitlines()[0]
    assert title.split("|")[0].strip() == f"{160 / 460 * 100:.0f}%", title
    row = next(line for line in out.splitlines() if "a1@example.com" in line)
    assert "reserve · cap 60%" in row, row
    other = next(line for line in out.splitlines() if "a2@example.com" in line)
    assert "reserve" not in other, other
    print("ok   a capped account counts to its cap and is tagged as reserve")


def pool(ab):
    return render(ab).splitlines()[0].split("|")[0].replace("⚠️", "").strip()


def test_pool_follows_the_rotation():
    with tempfile.TemporaryDirectory() as tmp:
        ab = load(tmp, codex=False)
        # 4 is hidden from the menu but still rotated, so it stays in the pool
        accounts_with(ab, {1: 100, 2: 0, 3: 0, 4: 100})
        with open(ab.HIDDEN_ACCOUNTS_PATH, "w") as f:
            json.dump({"accounts": ["4"]}, f)
        assert pool(ab) == "50%", pool(ab)
    print("ok   hidden accounts still count, the pool follows the rotation")


def test_unknown_accounts_stay_out():
    with tempfile.TemporaryDirectory() as tmp:
        ab = load(tmp, codex=False)
        # 2 has no reading, 3 cannot refresh: neither can be called unused
        accounts_with(ab, {1: 50, 2: None, 3: 0}, errors=(3,))
        assert pool(ab) == "50%", pool(ab)
    print("ok   accounts with no reading or a refresh error stay out of the pool")


def test_spent_week_blocks_the_five_hour_window():
    with tempfile.TemporaryDirectory() as tmp:
        ab = load(tmp, codex=False)
        accounts_with(ab, {1: 0, 2: 0, 3: 0, 4: 0}, seven={1: 100, 2: 100, 3: 100, 4: 0})
        assert pool(ab) == "75%·75%", pool(ab)
    print("ok   an account out of its 7d window counts as spent in the 5h pool")


def test_malformed_policy_falls_back_like_the_daemon():
    for policy in (
        {"schemaVersion": 1, "accountUsageCaps": {"uuid-1": None}},
        {"schemaVersion": 1, "defaultUsageCap": "high"},
        {"schemaVersion": 1, "accountUsageCaps": {"uuid-1": 0}},
        {"schemaVersion": 2, "accountUsageCaps": {"uuid-1": 50}},
        {"schemaVersion": 1, "accountUsageCaps": ["uuid-1"]},
        ["not", "a", "policy"],
    ):
        with tempfile.TemporaryDirectory() as tmp:
            ab = load(tmp, codex=False)
            accounts_with(ab, {1: 50, 2: 0}, policy=policy)
            out = render(ab)
        assert out.splitlines()[0].split("|")[0].strip() == "25%", (policy, out.splitlines()[0])
        assert "reserve" not in out, policy
    print("ok   a malformed policy reads as 100% caps, as claude-swap does")


def test_claude_only_install_keeps_spend():
    with tempfile.TemporaryDirectory() as tmp:
        ab = load(tmp, codex=True)
        summary = ab.codex_summary()
        summary["today"] = {"cost": 3.0, "tokens": 100}
        ab.codex_summary = lambda: summary
        ab.SIBLING_CODEX_ITEM = os.path.join(tmp, "agentbar-codex.1m.py")
        alone = render(ab).splitlines()[0]
        open(ab.SIBLING_CODEX_ITEM, "w").close()
        paired = render(ab).splitlines()[0]
    assert "$3" in alone.split("|")[0], alone
    assert "$" not in paired.split("|")[0], paired
    print("ok   the Claude item carries the spend when no Codex item is installed")


def test_no_colored_dot():
    with tempfile.TemporaryDirectory() as tmp:
        ab = load(tmp, codex=True)
        accounts_with(ab, {1: 99})
        claude = render(ab).splitlines()[0]
        ab.ROLE = "codex"
        codex = render(ab).splitlines()[0]
    for title in (claude, codex):
        assert not any(dot in title for dot in ("🟠", "🔴", "🟡")), title
    print("ok   no colored dot on either title")


def test_each_item_wears_its_own_mark():
    with tempfile.TemporaryDirectory() as tmp:
        ab = load(tmp, codex=True)
        summary = ab.codex_summary()
        summary["today"] = {"cost": 3.0, "tokens": 100}
        ab.codex_summary = lambda: summary
        ab.SIBLING_CODEX_ITEM = os.path.join(tmp, "agentbar-codex.1m.py")
        open(ab.SIBLING_CODEX_ITEM, "w").close()
        claude = render(ab).splitlines()[0]
        ab.ROLE = "codex"
        codex = render(ab).splitlines()[0]
    assert f"image={ab.ICON}" in claude, "Claude item lost the Claude glyph"
    assert "✳" not in claude and "✳" not in codex, "Claude's asterisk still marks a title"
    assert ab.ICON not in codex, "Codex item is wearing the Claude glyph"
    assert "image=" in codex or "sfimage=" in codex, codex
    assert "12%" in codex and "$" in codex, codex
    assert "$" not in claude.split("|")[0], claude
    print("ok   Claude glyph on the Claude item, the pet or a Codex mark on the Codex item")


def test_paired_items_refresh_once():
    """Both items tick together; the second must reuse the first's fresh stats."""
    import threading
    import time
    with tempfile.TemporaryDirectory() as tmp:
        ab = load(tmp)
        calls = []

        def slow(args):
            calls.append(args[-1])
            time.sleep(0.3)
            return {"daily": []} if args[-1] == "daily" else {"blocks": []}

        ab.ccusage = slow
        threads = [threading.Thread(target=ab.refresh_stats) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert calls.count("daily") == 1, calls
    print("ok   two items starting together run ccusage once")


def test_role_follows_the_file_name():
    with tempfile.TemporaryDirectory() as tmp:
        ab = load(tmp)
    assert ab.role_for("/x/agentbar.1m.py") == "claude"
    assert ab.role_for("/x/agentbar-codex.1m.py") == "codex"
    print("ok   the -codex symlink renders the Codex item")


if __name__ == "__main__":
    test_one_exhausted_account_is_a_quarter()
    test_capped_account_counts_up_to_its_cap()
    test_pool_follows_the_rotation()
    test_unknown_accounts_stay_out()
    test_spent_week_blocks_the_five_hour_window()
    test_malformed_policy_falls_back_like_the_daemon()
    test_claude_only_install_keeps_spend()
    test_no_colored_dot()
    test_each_item_wears_its_own_mark()
    test_paired_items_refresh_once()
    test_role_follows_the_file_name()
    print("\nall checks passed")
