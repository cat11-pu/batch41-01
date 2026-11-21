"""Distance-field glyph layout and atlas upload (initial stub).

Public API::

    GlyphDistanceField(fonts, fallback=None, page_size=(64, 64), cell_size=16,
                       padding=1, budget_ms=2.0, reference_size=16.0)
        .glyph_key(font, weight, size, ch) -> tuple
        .sdf_spread(size) -> float
        .reorder_bidi(text) -> str
        .layout(text, font="latin", weight="regular", size=16.0,
                language="en", max_width=None) -> dict
        .stage(layout_result) -> token
        .commit(token) -> bool
        .revision() -> int
        .live_glyph_count() -> int
        .page_usage() -> [{"page", "used", "capacity"}, ...]
        .cache_stats() -> {"entries": int}

`fonts` is a mapping of font name to::

    {"glyphs": {char: advance_at_reference_size},
     "kerning": {(left_char, right_char): delta}}

`layout` returns::

    {
        "glyphs": [{"ch", "font", "key", "x", "y", "line", "page",
                    "u0", "v0", "u1", "v1", "advance"}, ...],
        "lines": [str, ...],       # visual order, one entry per line
        "missing": [char, ...],
        "size": float,
        "language": str,
        "time_ms": float,
    }

Behaviour enforced by the checks:

* cache keys include font name, weight and size;
* fallback follows the per-language chain and picks the first font that has the
  character; kerning comes from the font that owns the glyph, not the primary
  font;
* bidirectional text is reordered before line breaking; Arabic runs are
  reversed while Latin runs keep their order; line strings carry neither
  leading nor trailing whitespace;
* positions are fractional (sub-pixel), never rounded to integers;
* the atlas is paged: a full page opens a new page instead of overwriting live
  glyphs; every glyph reserves `padding` on all four sides of its cell;
* staging a layout never changes the live atlas; commit atomically publishes
  it and a stale token is refused;
* empty text, zero/negative sizes raise ValueError;
* the SDF spread grows linearly with the requested font size.
"""

import threading
import time


class GlyphDistanceField:
    def __init__(self, fonts, fallback=None, page_size=(64, 64), cell_size=16,
                 padding=1, budget_ms=2.0, reference_size=16.0):
        self.fonts = fonts
        self.fallback = dict(fallback or {})
        self.page_size = page_size
        self.cell_size = cell_size
        self.padding = padding
        self.budget_ms = budget_ms
        self.reference_size = reference_size
        self._cache = {}
        self._pages = []
        self._live_keys = []
        self._revision = 0
        self._staged_epoch = 0
        self._staged_committed = True
        self._staged_keys = ()
        self._lock = threading.RLock()

    def glyph_key(self, font, weight, size, ch):
        return (font, weight, float(size), ch)

    def sdf_spread(self, size):
        base_spread = self.reference_size / 8.0
        return base_spread * float(size) / self.reference_size

    @staticmethod
    def _is_arabic(ch):
        code = ord(ch)
        return (
            0x0600 <= code <= 0x06FF
            or 0x0750 <= code <= 0x077F
            or 0x08A0 <= code <= 0x08FF
            or 0xFB50 <= code <= 0xFDFF
            or 0xFE70 <= code <= 0xFEFF
        )

    def _is_rtl(self, text):
        for ch in text:
            if ch.isspace():
                continue
            return self._is_arabic(ch)
        return False

    def reorder_bidi(self, text):
        result = []
        run = []
        run_arabic = None
        for ch in text:
            arabic = self._is_arabic(ch)
            if run_arabic is None or arabic == run_arabic:
                run.append(ch)
            else:
                result.extend(reversed(run) if run_arabic else run)
                run = [ch]
            run_arabic = arabic
        if run:
            result.extend(reversed(run) if run_arabic else run)
        return "".join(result)

    def _resolve_font(self, ch, chain):
        for font_name in chain:
            if ch in self.fonts.get(font_name, {}).get("glyphs", {}):
                return font_name
        return None

    def _page_capacity(self):
        columns = self.page_size[0] // self.cell_size
        rows = self.page_size[1] // self.cell_size
        return columns * rows

    def _allocate_cell(self):
        capacity = self._page_capacity()
        if not self._pages or self._pages[-1] == capacity:
            self._pages.append(0)
        page = len(self._pages) - 1
        index = self._pages[page]
        self._pages[page] = index + 1
        columns = self.page_size[0] // self.cell_size
        col = index % columns
        row = index // columns
        pixel_x = col * self.cell_size
        pixel_y = row * self.cell_size
        page_w = float(self.page_size[0])
        page_h = float(self.page_size[1])
        return {
            "page": page,
            "u0": (pixel_x + self.padding) / page_w,
            "v0": (pixel_y + self.padding) / page_h,
            "u1": (pixel_x + self.cell_size - self.padding) / page_w,
            "v1": (pixel_y + self.cell_size - self.padding) / page_h,
        }

    def _entry_for(self, key):
        entry = self._cache.get(key)
        if entry is None:
            cell = self._allocate_cell()
            entry = dict(key=key, **cell)
            self._cache[key] = entry
        return entry

    def layout(self, text, font="latin", weight="regular", size=16.0,
               language="en", max_width=None):
        if not text:
            raise ValueError("text must not be empty")
        if size <= 0:
            raise ValueError("font size must be positive")
        with self._lock:
            started = time.perf_counter()
            chain = list(self.fallback.get(language, ())) or [font]
            reordered = self.reorder_bidi(text)
            scale = float(size) / self.reference_size

            items = []
            missing = []
            seen_missing = set()
            for ch in reordered:
                if ch.isspace():
                    items.append((ch, None))
                    continue
                owner = self._resolve_font(ch, chain)
                if owner is None:
                    if ch not in seen_missing:
                        missing.append(ch)
                        seen_missing.add(ch)
                    items.append((ch, None))
                    continue
                items.append((ch, owner))

            words = []
            current = []
            for ch, owner in items:
                if ch.isspace():
                    if current:
                        words.append(current)
                        current = []
                    continue
                current.append((ch, owner))
            if current:
                words.append(current)

            def word_width(word):
                total = 0.0
                for ch, owner in word:
                    if owner is not None:
                        total += self.fonts[owner]["glyphs"][ch] * scale
                return total

            line_words = []
            if max_width is None:
                line_words = [list(words)]
            else:
                pending = []
                pending_width = 0.0
                for word in words:
                    width = word_width(word)
                    if pending and pending_width + width > max_width:
                        line_words.append(pending)
                        pending = []
                        pending_width = 0.0
                    pending.append(word)
                    pending_width += width
                if pending:
                    line_words.append(pending)
            if self._is_rtl(text):
                line_words.reverse()

            lines = [
                " ".join("".join(ch for ch, _ in word) for word in grouped)
                for grouped in line_words
            ]

            glyphs = []
            line_index = 0
            for grouped in line_words:
                pen_x = 0.0
                previous = None
                for word in grouped:
                    for ch, owner in word:
                        if owner is None:
                            continue
                        key = self.glyph_key(owner, weight, size, ch)
                        entry = self._entry_for(key)
                        if previous is not None:
                            prev_ch, prev_font = previous
                            kerning = self.fonts[prev_font]["kerning"].get(
                                (prev_ch, ch), 0.0
                            )
                            pen_x += kerning * scale
                        advance = self.fonts[owner]["glyphs"][ch] * scale
                        glyphs.append({
                            "ch": ch,
                            "font": owner,
                            "key": key,
                            "x": pen_x,
                            "y": line_index * size * 1.2,
                            "line": line_index,
                            "page": entry["page"],
                            "u0": entry["u0"],
                            "v0": entry["v0"],
                            "u1": entry["u1"],
                            "v1": entry["v1"],
                            "advance": advance,
                        })
                        pen_x += advance
                        previous = (ch, owner)
                line_index += 1

            return {
                "glyphs": glyphs,
                "lines": lines,
                "missing": missing,
                "size": float(size),
                "language": language,
                "time_ms": (time.perf_counter() - started) * 1000.0,
            }

    def stage(self, layout_result):
        with self._lock:
            self._staged_epoch += 1
            self._staged_keys = tuple(
                glyph["key"] for glyph in layout_result["glyphs"]
            )
            self._staged_committed = False
            return {
                "epoch": self._staged_epoch,
                "keys": self._staged_keys,
            }

    def commit(self, token):
        with self._lock:
            if not isinstance(token, dict):
                return False
            if token.get("epoch") != self._staged_epoch:
                return False
            if self._staged_committed:
                return False
            self._live_keys = list(self._staged_keys)
            self._staged_committed = True
            self._revision += 1
            return True

    def revision(self):
        return self._revision

    def live_glyph_count(self):
        return len(self._live_keys)

    def page_usage(self):
        capacity = self._page_capacity()
        return [
            {"page": index, "used": used, "capacity": capacity}
            for index, used in enumerate(self._pages)
        ]

    def cache_stats(self):
        return {"entries": len(self._cache)}
