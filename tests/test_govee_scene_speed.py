"""Offline unit tests for the H617A scene-speed byte codec.

These exercise ``govee_scene_speed`` with no Home Assistant, no Bluetooth, and no
hardware — pure byte-twiddling against the bundled ``H617A.json`` catalogue plus a
hand-built blob whose layout is known exactly. They guard the two things most
likely to break silently in a refactor:

  1. the absolute byte offsets the segment walker computes (a one-off here
     corrupts every scene upload), and
  2. the safety contract that an unexpected blob/config is uploaded *unchanged*
     rather than mangled.

Run from the repo root with ``pytest``.
"""
from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

# --- locate and import the module under test (the package dir name has a hyphen,
# so it cannot be a normal import; load it straight from its file) -------------

_COMPONENT_DIR = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "govee-ble-lights"
)
_MODULE_PATH = _COMPONENT_DIR / "govee_scene_speed.py"
_CATALOGUE_PATH = _COMPONENT_DIR / "jsons" / "H617A.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("govee_scene_speed", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gss = _load_module()


# --- catalogue fixture --------------------------------------------------------


def _catalogue_scenes():
    """Every H617A scene that ships a speed table: (label, blob, config_str)."""
    data = json.loads(_CATALOGUE_PATH.read_text())
    out = []
    for category in data["data"]["categories"]:
        cat_name = category.get("categoryName", "")
        for scene in category.get("scenes", []):
            scene_name = scene.get("sceneName", "")
            for light_effect in scene.get("lightEffects", []):
                sub = light_effect.get("scenceName") or ""
                for special in light_effect.get("specialEffect") or []:
                    param = special.get("scenceParam")
                    speed_info = special.get("speedInfo") or {}
                    if param and speed_info.get("supSpeed") and speed_info.get("config"):
                        label = f"{cat_name}/{scene_name}/{sub}"
                        out.append((label, base64.b64decode(param), speed_info["config"]))
    return out


CATALOGUE = _catalogue_scenes()


def test_catalogue_loaded():
    # Sanity: the bundled catalogue is present and has the expected scale. If this
    # drops sharply, the JSON was truncated or the shape changed.
    assert len(CATALOGUE) >= 250


# --- the layout invariants over real data ------------------------------------


def test_segment_offsets_parses_every_catalogue_blob():
    """Every shipped H617A blob must match the sceneType-2 layout the walker
    assumes. A failure here means the parser and the catalogue have diverged."""
    bad = []
    for label, blob, _config in CATALOGUE:
        try:
            list(gss._segment_offsets(blob))
        except ValueError as err:
            bad.append((label, str(err)))
    assert not bad, f"{len(bad)} blob(s) failed to parse, e.g. {bad[:3]}"


@pytest.mark.parametrize("override", [None, 0, 3, 7, 999])
def test_apply_never_changes_length(override):
    """The upload length must be byte-stable for any speed level — the strip
    rejects a malformed-length scenceParam."""
    for label, blob, config in CATALOGUE:
        out = gss.apply_scene_speed(blob, config, override)
        assert len(out) == len(blob), f"{label}: length changed at override={override}"


def test_apply_actually_rewrites_most_scenes():
    """Guard against a no-op regression: under 'auto' (per-page defaultIndex) a
    clear majority of scenes should have at least one byte rewritten."""
    changed = sum(
        1 for _label, blob, config in CATALOGUE
        if gss.apply_scene_speed(blob, config, None) != blob
    )
    assert changed > len(CATALOGUE) // 2


def test_apply_is_idempotent():
    """Re-applying the same level overwrites the same bytes with the same values,
    so a second pass is a no-op. Protects against drift / accumulating writes."""
    for label, blob, config in CATALOGUE:
        once = gss.apply_scene_speed(blob, config, None)
        twice = gss.apply_scene_speed(once, config, None)
        assert once == twice, f"{label}: not idempotent under auto"


def test_carnival_static_scene_fix():
    """Festival - Carnival ships its movement field static at 229; the codec must
    bump it so the scene actually animates (the original motivating bug)."""
    carnivals = [t for t in CATALOGUE if "Carnival" in t[0]]
    assert carnivals, "Carnival scene missing from catalogue"
    for label, blob, config in carnivals:
        out = gss.apply_scene_speed(blob, config, None)
        diffs = [(i, blob[i], out[i]) for i in range(len(blob)) if blob[i] != out[i]]
        assert diffs, f"{label}: Carnival left untouched (still static)"
        for _i, old, new in diffs:
            assert old == 229, f"{label}: unexpected pre-fix byte {old}"
            assert new != 229, f"{label}: byte not un-stuck"


def test_override_index_clamps_high():
    """An out-of-range speed level is clamped to the last table entry, not an
    error — two very-high indices must yield the same (stable) result."""
    for label, blob, config in CATALOGUE:
        a = gss.apply_scene_speed(blob, config, 999)
        b = gss.apply_scene_speed(blob, config, 100000)
        assert a == b, f"{label}: high-index clamp not stable"


# --- graceful-degradation contract -------------------------------------------


def _a_real_config():
    return CATALOGUE[0][2]


def test_none_config_returns_blob_unchanged():
    blob = CATALOGUE[0][1]
    assert gss.apply_scene_speed(blob, None) is blob or gss.apply_scene_speed(blob, None) == blob


def test_invalid_json_config_returns_blob_unchanged():
    blob = CATALOGUE[0][1]
    assert gss.apply_scene_speed(blob, "{not json") == blob


@pytest.mark.parametrize("config", ["", "[]", "null", "{}"])
def test_empty_or_pageless_config_returns_blob_unchanged(config):
    blob = CATALOGUE[0][1]
    assert gss.apply_scene_speed(blob, config) == blob


def test_unknown_blob_layout_returns_unchanged():
    """A blob that does not match sceneType-2 (here: claims 5 segments but is
    truncated) must be returned verbatim, never partially rewritten."""
    garbage = bytes([5, 0, 0, 0])
    assert gss.apply_scene_speed(garbage, _a_real_config()) == garbage


def test_empty_blob_returns_unchanged():
    assert gss.apply_scene_speed(b"", _a_real_config()) == b""


# --- white-box: pin the exact offset math on a hand-built single segment ------
#
# One segment, one brightness block (nB=1), one colour (nC=1). Offsets, with the
# segment starting at byte 1 (s=1):
#   s+10 brightness speed   s+14 colour "b"   s+22 moveIn "d"   s+25 moveAll "d"
# seg_len = 27, so blob[1] = 26; total length = 28.

def _build_single_segment_blob():
    blob = bytearray(28)
    blob[0] = 1          # one segment
    blob[1] = 26         # seg_len - 1  (seg_len = 27)
    blob[7] = 1          # nB = 1 brightness block (segment byte [6], i.e. s+6)
    color_start = 1 + 7 + 6 * 1          # = 14
    blob[color_start + 3] = 1            # nC = 1
    # sentinel values at the four speed offsets so we can see them overwritten
    for off in (11, 15, 23, 26):         # s+10, s+14, s+22, s+25
        blob[off] = 0xEE
    return bytes(blob)


def test_single_segment_offsets_resolved():
    blob = _build_single_segment_blob()
    (seg,) = list(gss._segment_offsets(blob))
    assert seg["idx"] == 0
    assert seg["bright_d"] == [11]
    assert seg["color_b"] == 15
    assert seg["movein_d"] == 23
    assert seg["moveall_d"] == 26


def test_single_segment_speed_bytes_written_at_index():
    blob = _build_single_segment_blob()
    config = json.dumps([{
        "page": 0,
        "defaultIndex": 1,
        "color": [0x10, 0x11],
        "moveIn": [0x20, 0x21],
        "moveAll": [0x30, 0x31],
        "bright": [{"brightValue": [0x40, 0x41]}],
    }])

    out0 = gss.apply_scene_speed(blob, config, 0)
    assert (out0[15], out0[23], out0[26], out0[11]) == (0x10, 0x20, 0x30, 0x40)

    out_auto = gss.apply_scene_speed(blob, config, None)  # defaultIndex = 1
    assert (out_auto[15], out_auto[23], out_auto[26], out_auto[11]) == (0x11, 0x21, 0x31, 0x41)

    # nothing outside the four speed offsets moved
    untouched = {i for i in range(len(blob))} - {11, 15, 23, 26}
    assert all(out0[i] == blob[i] for i in untouched)


def test_page_with_no_matching_segment_is_ignored():
    blob = _build_single_segment_blob()
    # page 5 has no segment 5 -> left alone; the blob is unchanged.
    config = json.dumps([{"page": 5, "color": [1, 2], "moveIn": [1], "moveAll": [1]}])
    assert gss.apply_scene_speed(blob, config, 0) == blob


# --- the _at clamp helper -----------------------------------------------------


@pytest.mark.parametrize("arr,index,expected", [
    ([10, 20, 30], 1, 20),
    ([10, 20, 30], -5, 10),     # negative clamps to first
    ([10, 20, 30], 99, 30),     # past end clamps to last
    ([], 0, None),              # empty -> None
    (None, 0, None),            # missing -> None
])
def test_at_clamps(arr, index, expected):
    assert gss._at(arr, index) == expected


@pytest.mark.parametrize("blob", [b"", bytes([3, 0, 0]), bytes([1, 5])])
def test_segment_offsets_raises_on_bad_blob(blob):
    with pytest.raises(ValueError):
        list(gss._segment_offsets(blob))
