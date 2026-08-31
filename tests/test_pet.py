#!/usr/bin/env python3
"""Checks for the Codex pet. Run: python3 tests/test_pet.py

The pet art is OpenAI's and is deliberately NOT in this repository: it is read
out of the local Codex install at runtime. So the asset-backed checks skip
themselves when Codex.app is absent or Pillow is missing, and one check asserts
the repo stays free of sprite art.
"""
import base64
import importlib.util
import os
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "agentbar.1m.py")


def load():
    spec = importlib.util.spec_from_file_location("agentbar_pet", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def png_size(data):
    """(width, height) straight out of a PNG IHDR, no image library needed."""
    raw = base64.b64decode(data)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", raw[16:24])


def test_moods(ab):
    far = ab.pet_mood(10, False, False)
    busy = ab.pet_mood(10, True, False)
    low = ab.pet_mood(85, True, False)
    gone = ab.pet_mood(99, False, False)
    reached = ab.pet_mood(2, False, True)
    assert (far, busy, low, gone, reached) == (
        "calm", "working", "strained", "spent", "spent"
    ), (far, busy, low, gone, reached)
    # a reported limit beats a low percentage: the account is the source of truth
    assert ab.pet_mood(0, True, True) == "spent"
    print("ok   mood follows the worst window, and a reached limit wins")


def test_no_art_in_repo():
    """The sprite must never be committed. It is OpenAI's asset, not ours.

    Asks git what is TRACKED rather than walking the working tree: an untracked
    debug screenshot or a .venv full of Pillow's own test images is not something
    this repo ships, and failing on those would train people to ignore the check.
    """
    try:
        tracked = subprocess.run(
            ["git", "-C", ROOT, "ls-files"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        print(f"skip no-art guard (git unavailable: {exc})")
        return
    if tracked.returncode != 0:
        print("skip no-art guard (not a git checkout)")
        return
    stray = [
        line for line in tracked.stdout.splitlines()
        if line.lower().endswith((".webp", ".png", ".gif", ".apng", ".jpg", ".jpeg"))
    ]
    assert not stray, f"sprite art must not be committed: {stray}"
    print("ok   no pet art tracked in the repo")


def test_asar_lookup(ab):
    if not os.path.exists(ab.CODEX_ASAR):
        print("skip asar lookup (Codex.app not installed)")
        return
    blob = ab.asar_lookup(ab.CODEX_ASAR, ab.PET_NAME + "-spritesheet")
    assert blob, "sprite sheet not found in the Codex asar"
    assert blob[:4] == b"RIFF" and blob[8:12] == b"WEBP", blob[:12]
    assert len(blob) > 100_000, len(blob)
    assert ab.asar_lookup(ab.CODEX_ASAR, "definitely-not-a-pet-xyz") is None
    print(f"ok   asar lookup pulled a {len(blob):,} byte WebP out of Codex.app")


def png_dpi(data):
    """Pixels-per-metre from a PNG's pHYs chunk, or None when it has none.

    SwiftBar sizes menu bar art by physical size, not pixels, so a frame with no
    pHYs is read as 72 dpi and drawn at double size beside the 144 dpi glyph.
    """
    raw = base64.b64decode(data)
    at = 8
    while at + 8 <= len(raw):
        length = struct.unpack(">I", raw[at:at + 4])[0]
        kind = raw[at + 4:at + 8]
        if kind == b"pHYs":
            x, y, unit = struct.unpack(">IIB", raw[at + 8:at + 17])
            return x, y, unit
        if kind == b"IDAT":
            return None
        at += 12 + length
    return None


def test_icons(ab):
    if not os.path.exists(ab.CODEX_ASAR):
        print("skip pet icons (Codex.app not installed)")
        return
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("skip pet icons (Pillow not installed, pet is optional)")
        return

    with tempfile.TemporaryDirectory() as tmp:
        # never touch the real cache: a stale crop there would let this pass
        # while asserting on artifacts instead of the crop path
        ab.PET_DIR = os.path.join(tmp, "pet")
        ab.HIDE_PET_FLAG = os.path.join(tmp, "hide-pet")
        seen = {}
        for mood in ab.PET_MOODS:
            data = ab.pet_icon(mood)
            assert data, f"no icon for {mood}"
            w, h = png_size(data)
            assert h == ab.PET_BAR_PX, f"{mood} is {h}px tall, want {ab.PET_BAR_PX}"
            assert 0 < w <= 3 * ab.PET_BAR_PX, f"{mood} is {w}px wide"
            dpi = png_dpi(data)
            assert dpi, f"{mood} has no pHYs chunk, it will draw at double size"
            assert dpi == (5669, 5669, 1), f"{mood} pHYs is {dpi}, want 144 dpi"
            seen[mood] = data
    # if the row/col mapping were wrong every mood would return the same frame
    assert len(set(seen.values())) == len(seen), "moods are not distinct frames"
    print(f"ok   {len(seen)} distinct frames, {ab.PET_BAR_PX}px at 144 dpi")


def test_cache_and_optout(ab):
    if not os.path.exists(ab.CODEX_ASAR):
        print("skip pet cache (Codex.app not installed)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        ab.PET_DIR = os.path.join(tmp, "pet")
        ab.HIDE_PET_FLAG = os.path.join(tmp, "hide-pet")
        first = ab.pet_icon("calm")
        if first is None:
            print("skip pet cache (Pillow not installed)")
            return
        written = os.listdir(ab.PET_DIR)
        assert len(written) == 1, written
        # second call must come off disk, so a broken Pillow cannot matter
        ab.asar_lookup = lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-read"))
        assert ab.pet_icon("calm") == first
        open(ab.HIDE_PET_FLAG, "w").close()
        assert ab.pet_icon("calm") is None, "hide flag ignored"
    print("ok   frames cached per sheet version, hide flag respected")


def test_missing_codex(ab):
    ab.CODEX_ASAR = "/nope/Codex.app/Contents/Resources/app.asar"
    ab.HIDE_PET_FLAG = "/nope/hide"
    assert ab.pet_icon("calm") is None
    print("ok   no Codex install means no pet, not an error")


if __name__ == "__main__":
    ab = load()
    test_moods(ab)
    test_no_art_in_repo()
    test_asar_lookup(ab)
    test_icons(ab)
    test_cache_and_optout(ab)
    test_missing_codex(ab)  # mutates paths, so it runs last
    print("\nall checks passed")
