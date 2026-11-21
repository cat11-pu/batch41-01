import pytest

import glyph_distance_field as gdf


FONTS = {
    "latin": {
        "glyphs": {"A": 10.0, "B": 10.0, "i": 5.3},
        "kerning": {("A", "B"): -2.0},
    },
    "cjk": {
        "glyphs": {"中": 20.0, "文": 20.0, "X": 9.0},
        "kerning": {("中", "文"): -4.0},
    },
    "arabic": {
        "glyphs": {"م": 10.0, "ر": 10.0, "ح": 10.0, "ب": 10.0, "ا": 10.0,
                   "ي": 10.0, "X": 11.0, " ": 5.0},
        "kerning": {},
    },
}

FALLBACK = {
    "ja": ["cjk", "latin"],
    "ar": ["arabic", "latin"],
    "en": ["latin"],
}


def field():
    return gdf.GlyphDistanceField(FONTS, fallback=FALLBACK)


def test_cache_key_includes_font_weight_and_size():
    layout = field()
    base = layout.glyph_key("latin", "regular", 16.0, "A")
    assert base == layout.glyph_key("latin", "regular", 16.0, "A")
    assert base != layout.glyph_key("latin", "bold", 16.0, "A")
    assert base != layout.glyph_key("latin", "regular", 24.0, "A")
    assert base != layout.glyph_key("cjk", "regular", 16.0, "A")


def test_size_change_creates_separate_entries_and_positions():
    layout = field()
    small = layout.layout("AA", language="en", size=16.0)
    large = layout.layout("AA", language="en", size=32.0)
    assert small["glyphs"][1]["x"] == pytest.approx(10.0)
    assert large["glyphs"][1]["x"] == pytest.approx(20.0)
    assert small["time_ms"] >= 0.0
    assert layout.cache_stats()["entries"] == 2


def test_fallback_chain_picks_first_font_with_glyph():
    result = field().layout("中A", font="latin", language="ja", size=16.0)
    fonts = [glyph["font"] for glyph in result["glyphs"]]
    assert fonts == ["cjk", "latin"]


def test_language_order_controls_fallback_choice():
    layout = field()
    japanese = layout.layout("X", font="latin", language="ja", size=16.0)
    arabic = layout.layout("X", font="latin", language="ar", size=16.0)
    assert japanese["glyphs"][0]["font"] == "cjk"
    assert arabic["glyphs"][0]["font"] == "arabic"


def test_missing_glyph_is_reported():
    result = field().layout("😀", font="latin", language="en", size=16.0)
    assert result["missing"] == ["😀"]


def test_bidi_reverses_arabic_run():
    assert field().reorder_bidi("مرحبا") == "ابحرم"


def test_bidi_keeps_latin_runs_in_order():
    assert field().reorder_bidi("ab مرح") == "ab حرم"


def test_bidi_is_applied_before_line_breaking():
    result = field().layout("مرحبا يا", font="arabic", language="ar",
                            size=16.0, max_width=50.0)
    assert result["lines"] == ["اي", "ابحرم"]


def test_fallback_glyph_uses_its_own_kerning():
    layout = gdf.GlyphDistanceField(
        FONTS,
        fallback={"ja": ["latin", "cjk"]},
    )
    result = layout.layout("中文", font="latin", language="ja", size=16.0)
    assert result["glyphs"][0]["font"] == "cjk"
    assert result["glyphs"][1]["font"] == "cjk"
    assert result["glyphs"][1]["x"] == pytest.approx(16.0)


def test_subpixel_positions_are_preserved():
    result = field().layout("iii", language="en", size=16.0)
    positions = [glyph["x"] for glyph in result["glyphs"]]
    assert positions == pytest.approx([0.0, 5.3, 10.6])


def test_full_atlas_opens_a_second_page():
    chars = [chr(ord("A") + index) for index in range(17)]
    fonts = {"latin": {"glyphs": {char: 5.0 for char in chars}, "kerning": {}}}
    layout = gdf.GlyphDistanceField(fonts, page_size=(64, 64), cell_size=16,
                                    padding=1)
    result = layout.layout("".join(chars), language="en", size=16.0)
    layout.commit(layout.stage(result))
    usage = layout.page_usage()
    assert len(usage) == 2
    assert usage[0]["used"] == 16
    assert usage[0]["capacity"] == 16
    assert usage[1]["used"] == 1
    assert result["glyphs"][-1]["page"] == 1


def test_atlas_uv_leaves_sampling_margin():
    result = field().layout("A", language="en", size=16.0)
    glyph = result["glyphs"][0]
    expected = 14.0 / 64.0
    assert glyph["u1"] - glyph["u0"] == pytest.approx(expected)
    assert glyph["v1"] - glyph["v0"] == pytest.approx(expected)
    assert glyph["u0"] >= 1.0 / 64.0 - 1e-9
    assert glyph["u1"] <= 63.0 / 64.0 + 1e-9


def test_staged_upload_is_atomic():
    layout = field()
    result = layout.layout("AB", language="en", size=16.0)
    assert layout.revision() == 0
    token = layout.stage(result)
    assert layout.revision() == 0
    assert layout.commit(token) is True
    assert layout.revision() == 1
    assert layout.live_glyph_count() == 2
    assert layout.commit(token) is False
    assert layout.revision() == 1


def test_empty_text_and_invalid_sizes_are_rejected():
    layout = field()
    with pytest.raises(ValueError):
        layout.layout("", language="en")
    with pytest.raises(ValueError):
        layout.layout("A", language="en", size=0.0)
    with pytest.raises(ValueError):
        layout.layout("A", language="en", size=-1.0)


def test_sdf_spread_scales_with_font_size():
    layout = field()
    small = layout.sdf_spread(12.0)
    large = layout.sdf_spread(48.0)
    assert small > 0.0
    assert large > small
    assert large == pytest.approx(4.0 * small, rel=0.05)
