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
    """The sprite must never be committed. It is OpenAI's asset, not ours."""
    stray = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", ".claude")]
        for name in names:
            if name.lower().endswith((".webp", ".png", ".gif", ".apng")):
                stray.append(os.path.relpath(os.path.join(base, name), ROOT))
    assert not stray, f"sprite art must not be committed: {stray}"
    print("ok   no pet art committed to the repo")


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


def test_icons(ab):
    if not os.path.exists(ab.CODEX_ASAR):
        print("skip pet icons (Codex.app not installed)")
        return
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("skip pet icons (Pillow not installed, pet is optional)")
        return

    seen = {}
    for mood in ab.PET_MOODS:
        data = ab.pet_icon(mood)
        assert data, f"no icon for {mood}"
        w, h = png_size(data)
        assert h == ab.PET_BAR_PX, f"{mood} is {h}px tall, want {ab.PET_BAR_PX}"
        assert 0 < w <= 3 * ab.PET_BAR_PX, f"{mood} is {w}px wide"
        seen[mood] = data
    # if the row/col mapping were wrong every mood would return the same frame
    assert len(set(seen.values())) == len(seen), "moods are not distinct frames"
    print(f"ok   {len(seen)} distinct mood frames, each {ab.PET_BAR_PX}px tall")


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
