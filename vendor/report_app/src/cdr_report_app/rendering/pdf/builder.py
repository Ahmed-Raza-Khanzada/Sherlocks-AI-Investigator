"""Reusable PDF builder with dynamic tables and styling."""

from __future__ import annotations

import math
import os
import re

from fpdf import FPDF

from cdr_report_app.rendering.theme import CLR_BG_LIGHT, CLR_MUTED, CLR_PRIMARY, CLR_SECONDARY, CLR_TEXT

ARABIC_GLYPH_FORMS: dict[str, tuple[str, str, str, str] | tuple[str, str]] = {
    "ا": ("\uFE8D", "\uFE8E"),
    "آ": ("\uFE81", "\uFE82"),
    "أ": ("\uFE83", "\uFE84"),
    "إ": ("\uFE87", "\uFE88"),
    "ء": ("\uFE80", "\uFE80"),
    "ئ": ("\uFE89", "\uFE8A", "\uFE8B", "\uFE8C"),
    "ؤ": ("\uFE85", "\uFE86"),
    "ب": ("\uFE8F", "\uFE90", "\uFE91", "\uFE92"),
    "پ": ("\uFB56", "\uFB57", "\uFB58", "\uFB59"),
    "ت": ("\uFE95", "\uFE96", "\uFE97", "\uFE98"),
    "ٹ": ("\uFB66", "\uFB67", "\uFB68", "\uFB69"),
    "ث": ("\uFE99", "\uFE9A", "\uFE9B", "\uFE9C"),
    "ج": ("\uFE9D", "\uFE9E", "\uFE9F", "\uFEA0"),
    "چ": ("\uFB7A", "\uFB7B", "\uFB7C", "\uFB7D"),
    "ح": ("\uFEA1", "\uFEA2", "\uFEA3", "\uFEA4"),
    "خ": ("\uFEA5", "\uFEA6", "\uFEA7", "\uFEA8"),
    "د": ("\uFEA9", "\uFEAA"),
    "ڈ": ("\uFB88", "\uFB89"),
    "ذ": ("\uFEAB", "\uFEAC"),
    "ر": ("\uFEAD", "\uFEAE"),
    "ڑ": ("\uFB8C", "\uFB8D"),
    "ز": ("\uFEAF", "\uFEB0"),
    "ژ": ("\uFB8A", "\uFB8B"),
    "س": ("\uFEB1", "\uFEB2", "\uFEB3", "\uFEB4"),
    "ش": ("\uFEB5", "\uFEB6", "\uFEB7", "\uFEB8"),
    "ص": ("\uFEB9", "\uFEBA", "\uFEBB", "\uFEBC"),
    "ض": ("\uFEBD", "\uFEBE", "\uFEBF", "\uFEC0"),
    "ط": ("\uFEC1", "\uFEC2", "\uFEC3", "\uFEC4"),
    "ظ": ("\uFEC5", "\uFEC6", "\uFEC7", "\uFEC8"),
    "ع": ("\uFEC9", "\uFECA", "\uFECB", "\uFECC"),
    "غ": ("\uFECD", "\uFECE", "\uFECF", "\uFED0"),
    "ف": ("\uFED1", "\uFED2", "\uFED3", "\uFED4"),
    "ق": ("\uFED5", "\uFED6", "\uFED7", "\uFED8"),
    "ك": ("\uFED9", "\uFEDA", "\uFEDB", "\uFEDC"),
    "ک": ("\uFB8E", "\uFB8F", "\uFB90", "\uFB91"),
    "گ": ("\uFB92", "\uFB93", "\uFB94", "\uFB95"),
    "ل": ("\uFEDD", "\uFEDE", "\uFEDF", "\uFEE0"),
    "م": ("\uFEE1", "\uFEE2", "\uFEE3", "\uFEE4"),
    "ن": ("\uFEE5", "\uFEE6", "\uFEE7", "\uFEE8"),
    "ں": ("\uFB9E", "\uFB9F"),
    "و": ("\uFEED", "\uFEEE"),
    "ہ": ("\uFBA6", "\uFBA7", "\uFBA8", "\uFBA9"),
    "ھ": ("\uFBAA", "\uFBAB", "\uFBAC", "\uFBAD"),
    "ه": ("\uFEE9", "\uFEEA", "\uFEEB", "\uFEEC"),
    "ی": ("\uFBFC", "\uFBFD", "\uFBFE", "\uFBFF"),
    "ے": ("\uFEEF", "\uFEF0"),
}

NO_DATA_TEXT = "کوئی ڈیٹا نہیں ملا۔"

RIGHT_JOIN_ONLY = {char for char, forms in ARABIC_GLYPH_FORMS.items() if len(forms) == 2}
DUAL_JOINING = {char for char, forms in ARABIC_GLYPH_FORMS.items() if len(forms) == 4}


class ReportPdf(FPDF):
    def __init__(self):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_auto_page_break(auto=True, margin=15)
        self.dfont = "Helvetica"
        self.afont = "Helvetica"
        self.text_shaping_enabled = False
        self._chart_files: list[str] = []
        self._load_fonts()
        self._enable_text_shaping()
        self._register_fallback_fonts()
        self.set_font(self.dfont, "", 10)
        self.c_margin = 1.0  # Ensure enough padding for text

    def _load_fonts(self) -> None:
        loaded_styles: dict[str, set[str]] = {}
        # Resolve the bundled assets/fonts directory relative to this file
        _this_dir = os.path.dirname(os.path.abspath(__file__))
        _assets_fonts = os.path.normpath(os.path.join(_this_dir, "..", "..", "..", "..", "assets", "fonts"))

        # ---------- 1. Bundled fonts (always ship with the project, work on ALL OSes) ----------
        bundled_font_paths = [
            # Latin fonts – LiberationSans (Arial-compatible, full style set)
            (os.path.join(_assets_fonts, "LiberationSans-Regular.ttf"), "LiberationSans"),
            (os.path.join(_assets_fonts, "LiberationSans-Bold.ttf"), "LiberationSans", "B"),
            (os.path.join(_assets_fonts, "LiberationSans-Italic.ttf"), "LiberationSans", "I"),
            (os.path.join(_assets_fonts, "LiberationSans-BoldItalic.ttf"), "LiberationSans", "BI"),
            # Latin fallback – DejaVuSans (broad Unicode support)
            (os.path.join(_assets_fonts, "DejaVuSans.ttf"), "DejaVuSans"),
            (os.path.join(_assets_fonts, "DejaVuSans-Bold.ttf"), "DejaVuSans", "B"),
            (os.path.join(_assets_fonts, "DejaVuSans-Oblique.ttf"), "DejaVuSans", "I"),
            (os.path.join(_assets_fonts, "DejaVuSans-BoldOblique.ttf"), "DejaVuSans", "BI"),
            # Arabic/Urdu fonts
            (os.path.join(_assets_fonts, "NotoNaskhArabic-Regular.ttf"), "NotoNaskhArabic"),
            (os.path.join(_assets_fonts, "NotoNaskhArabic-Bold.ttf"), "NotoNaskhArabic", "B"),
            (os.path.join(_assets_fonts, "NotoSansArabic-Regular.ttf"), "NotoArabic"),
            (os.path.join(_assets_fonts, "NotoSansArabic-Bold.ttf"), "NotoArabic", "B"),
        ]

        # ---------- 2. System font fallbacks (only used if bundled fonts are missing) ----------
        system_font_paths = [
            # Windows
            ("C:/Windows/Fonts/arial.ttf", "Arial"),
            ("C:/Windows/Fonts/arialbd.ttf", "Arial", "B"),
            ("C:/Windows/Fonts/ariali.ttf", "Arial", "I"),
            ("C:/Windows/Fonts/arialbi.ttf", "Arial", "BI"),
            ("C:/Windows/Fonts/tahoma.ttf", "Tahoma"),
            ("C:/Windows/Fonts/tahomabd.ttf", "Tahoma", "B"),
            # Linux
            ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "LiberationSans"),
            ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", "LiberationSans", "B"),
            ("/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf", "LiberationSans", "I"),
            ("/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf", "LiberationSans", "BI"),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans"),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans", "B"),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf", "DejaVuSans", "I"),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf", "DejaVuSans", "BI"),
            ("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf", "NotoNaskhArabic"),
            ("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf", "NotoNaskhArabic", "B"),
            ("/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf", "NotoArabic"),
            ("/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf", "NotoArabic", "B"),
            # macOS
            ("/System/Library/Fonts/Supplemental/Arial.ttf", "Arial"),
            ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "Arial", "B"),
            ("/System/Library/Fonts/Supplemental/Arial Italic.ttf", "Arial", "I"),
            ("/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf", "Arial", "BI"),
            ("/System/Library/Fonts/Supplemental/Geeza Pro.ttf", "GeezaPro"),
            ("/System/Library/Fonts/Supplemental/Geeza Pro Bold.ttf", "GeezaPro", "B"),
            ("/System/Library/Fonts/Supplemental/Tahoma.ttf", "Tahoma"),
            ("/System/Library/Fonts/Supplemental/Tahoma Bold.ttf", "Tahoma", "B"),
        ]

        # Load bundled fonts first, then system fonts
        for path, name, *style in bundled_font_paths + system_font_paths:
            if not os.path.exists(path):
                continue
            s = style[0] if style else ""
            # Skip if this font+style was already loaded (bundled takes precedence)
            if s in loaded_styles.get(name, set()):
                continue
            try:
                self.add_font(name, s, path)
                loaded_styles.setdefault(name, set()).add(s)
            except Exception:
                pass

        # ---------- 3. Select the best Arabic font (afont) ----------
        for candidate in ("NotoNaskhArabic", "NotoArabic", "GeezaPro", "Tahoma", "Arial", "DejaVuSans", "LiberationSans"):
            if "" in loaded_styles.get(candidate, set()):
                self.afont = candidate
                break

        # ---------- 4. Select the best default/Latin font (dfont) ----------
        # Prefer a font with full style set (Regular + Bold + Italic)
        for candidate in ("LiberationSans", "Arial", "DejaVuSans"):
            styles = loaded_styles.get(candidate, set())
            if "" in styles and "B" in styles and "I" in styles:
                self.dfont = candidate
                break

        # If no Latin font with full styles was found, try fonts with at least Regular + Bold
        if self.dfont == "Helvetica":
            for candidate in ("LiberationSans", "Arial", "DejaVuSans"):
                styles = loaded_styles.get(candidate, set())
                if "" in styles and "B" in styles:
                    self.dfont = candidate
                    break

    def _register_fallback_fonts(self) -> None:
        """Urdu font has no Latin glyphs: without a fallback, English words
        inside an Urdu string are silently dropped from the PDF."""
        try:
            self.set_fallback_fonts([self.dfont])
        except Exception:
            pass

    def _enable_text_shaping(self) -> None:
        if hasattr(self, "set_text_shaping"):
            try:
                self.set_text_shaping(True)
                self.text_shaping_enabled = True
            except Exception:
                self.text_shaping_enabled = False

    def cleanup(self) -> None:
        for path in self._chart_files:
            try:
                os.remove(path)
            except OSError:
                pass
        self._chart_files.clear()

    def _safe_text(self, value: object) -> str:
        if value is None:
            return ""
        try:
            text = str(value)
            # For Arabic/Urdu text, always apply manual shaping if automatic shaping is not enabled
            if not self.text_shaping_enabled and self._contains_arabic(text):
                return self._manually_shape_arabic(text)
            # Don't encode to latin-1 for Arabic text - keep it as UTF-8
            if self._contains_arabic(text):
                return text
            # For non-Arabic text with Helvetica, use safe encoding
            if self.dfont == "Helvetica":
                return text.encode("latin-1", errors="replace").decode("latin-1")
            return text
        except Exception:
            return str(value)

    def _contains_arabic(self, value: object) -> bool:
        text = str(value or "")
        return bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text))

    def _manually_shape_arabic(self, text: str) -> str:
        if not text or not self._contains_arabic(text):
            return text
        shaped_lines = []
        for line in text.split("\n"):
            segments = [self._shape_segment(segment) if self._segment_is_arabic_rich(segment) else segment for segment in re.split(r"(\s*\|\s*)", line)]
            shaped_lines.append("".join(segments))
        return "\n".join(shaped_lines)

    def _segment_is_arabic_rich(self, text: str) -> bool:
        letters = [char for char in text if char.isalpha()]
        if not letters:
            return False
        arabic_count = sum(1 for char in letters if self._contains_arabic(char))
        return arabic_count >= max(1, int(len(letters) * 0.5))

    def _shape_segment(self, segment: str) -> str:
        tokens = re.split(r"(\s+)", segment)
        shaped_tokens = [self._shape_word(token) if self._contains_arabic(token) else token for token in tokens]
        words_only = [token for token in shaped_tokens if token and not token.isspace()]
        spaces = [token for token in shaped_tokens if token.isspace()]
        if not words_only:
            return segment
        result: list[str] = []
        words_iter = list(reversed(words_only))
        word_index = 0
        for token in shaped_tokens:
            if token.isspace():
                result.append(token)
            else:
                result.append(words_iter[word_index])
                word_index += 1
        return "".join(result)

    def _shape_word(self, word: str) -> str:
        chars = list(word)
        shaped: list[str] = []
        arabic_indices = [index for index, char in enumerate(chars) if char in ARABIC_GLYPH_FORMS]
        if not arabic_indices:
            return word
        for index, char in enumerate(chars):
            forms = ARABIC_GLYPH_FORMS.get(char)
            if not forms:
                shaped.append(char)
                continue
            prev_char = chars[index - 1] if index > 0 else ""
            next_char = chars[index + 1] if index + 1 < len(chars) else ""
            joins_prev = prev_char in DUAL_JOINING and char in DUAL_JOINING.union(RIGHT_JOIN_ONLY)
            joins_next = char in DUAL_JOINING and next_char in DUAL_JOINING.union(RIGHT_JOIN_ONLY)
            if len(forms) == 2:
                shaped.append(forms[1] if joins_prev else forms[0])
            else:
                if joins_prev and joins_next:
                    shaped.append(forms[3])
                elif joins_prev:
                    shaped.append(forms[1])
                elif joins_next:
                    shaped.append(forms[2])
                else:
                    shaped.append(forms[0])
        return "".join(reversed(shaped))

    def _font_with_style(self, family: str, style: str) -> tuple[str, str]:
        if family == "Helvetica":
            return family, style
        if family == self.afont:
            if style in {"BI", "IB"}:
                try:
                    self.set_font(family, "BI", 10)
                    return family, "BI"
                except Exception:
                    if style in {"B", "BI", "IB"}:
                        try:
                            self.set_font(family, "B", 10)
                            return family, "B"
                        except Exception:
                            return family, ""
            if style == "I":
                try:
                    self.set_font(family, "I", 10)
                    return family, "I"
                except Exception:
                    return family, ""
            if style == "B":
                try:
                    self.set_font(family, "B", 10)
                    return family, "B"
                except Exception:
                    return family, ""
        return family, style

    def set_text_font(self, text: object, style: str = "", size: float = 10) -> None:
        is_arabic = self._contains_arabic(text)
        family = self.afont if is_arabic else self.dfont
        # Increase size for Arabic/Urdu text for better readability
        effective_size = size + 2.0 if is_arabic else size
        family, resolved_style = self._font_with_style(family, style)
        self.set_font(family, resolved_style, effective_size)

    def section_heading(self, text: str, min_height: float = 40, align: str = "C") -> None:
        if self.get_y() + min_height > self.page_break_trigger:
            self.add_page()
        self.set_text_font(text, "B", 11)
        self.set_fill_color(*CLR_SECONDARY)
        self.set_text_color(255, 255, 255)
        self.cell(0, 8, self._safe_text(text), ln=True, fill=True, align=align)
        self.set_text_color(*CLR_TEXT)
        self.ln(2)

    def sub_heading(self, text: str, align: str = "L", size: float = 9) -> None:
        self.set_text_font(text, "B", size)
        self.set_text_color(*CLR_PRIMARY)
        self.cell(0, 6, self._safe_text(text), ln=True, align=align)
        self.set_text_color(*CLR_TEXT)
        self.ln(1)

    def key_value(self, key: str, value: object, bold_value: bool = False, align: str = "L") -> None:
        x_start = self.l_margin
        y_start = self.get_y()
        key_width = 45
        value_width = self.w - self.l_margin - self.r_margin - key_width

        self.set_xy(x_start, y_start)
        self.set_text_font(key, "B", 9)
        self.cell(key_width, 5, self._safe_text(f"  {key}:"))
        self.set_xy(x_start + key_width, y_start)
        self.set_text_font(value[0] if isinstance(value, tuple) and len(value) == 2 else value, "B" if bold_value else "", 9)
        if isinstance(value, tuple) and len(value) == 2:
            self.multi_cell(value_width, 5, self._safe_text(value[0]), link=str(value[1]), align=align)
        else:
            self.multi_cell(value_width, 5, self._safe_text(value), align=align)
        self.set_x(self.l_margin)

    def stat_box(self, label_en: str, value: object, x: float, y: float, w: float = 35, h: float = 18, sub_value: object | None = None, label_ur: str | None = None) -> None:
        self.set_xy(x, y)
        self.set_fill_color(*CLR_BG_LIGHT)
        self.rect(x, y, w, h, style="F")
        self.set_draw_color(*CLR_SECONDARY)
        self.rect(x, y, w, h, style="D")
        self.set_xy(x, y + 2)
        self.set_text_font(label_en, "B", 7)
        self.set_text_color(*CLR_TEXT)
        self.cell(w, 3.5, self._safe_text(label_en), align="C", ln=True)
        if label_ur:
            self.set_text_font(label_ur, "", 6)
            self.set_text_color(*CLR_MUTED)
            self.set_x(x)
            self.cell(w, 3, self._safe_text(label_ur), align="C", ln=True)
            self.set_xy(x, y + 10)
        else:
            self.set_xy(x, y + 8)
        value_str = self._safe_text(value)
        font_size = 7 if len(value_str) > 20 else 8 if len(value_str) > 15 else 11
        self.set_text_font(value_str, "B", font_size)
        self.set_text_color(*CLR_PRIMARY)
        self.multi_cell(w, 3, value_str, align="C")
        if sub_value:
            self.set_xy(x, self.get_y() + 1)
            self.set_text_font(sub_value, "B", 9)
            self.set_text_color(*CLR_SECONDARY)
            self.cell(w, 4, self._safe_text(sub_value), align="C")
        self.set_text_color(*CLR_TEXT)

    def dynamic_widths(self, headers: list[str], rows: list[list[object]], min_widths: list[float] | None = None) -> list[float]:
        usable = self.w - self.l_margin - self.r_margin
        count = len(headers)
        if count == 0:
            return []
        if min_widths is None:
            min_widths = [max(18.0, usable / count * 0.65)] * count
        weights = []
        for idx, header in enumerate(headers):
            max_len = len(str(header))
            for row in rows:
                if idx < len(row):
                    value = row[idx][0] if isinstance(row[idx], tuple) else row[idx]
                    for line in str(value).split("\n"):
                        max_len = max(max_len, len(line))
            weights.append(max(max_len, 8))
        total_weight = sum(weights) or 1
        widths = [max(min_widths[i], usable * (weights[i] / total_weight)) for i in range(count)]
        scale = usable / sum(widths)
        # Ensure a minimum absolute width per column (e.g., 12mm) to avoid FPDF 'Not enough horizontal space' error
        return [max(12.0, round(width * scale, 2)) for width in widths]

    def _line_count(self, text: object, width: float) -> int:
        safe_text = self._safe_text(text).replace("\\n", "\n")
        lines = 0
        for line in safe_text.split("\n"):
            # FPDF multicell has 1mm padding on L/R: 2mm total lost.
            # We also lose space due to word wrapping not splitting words perfectly.
            # Using 0.85 factor compensates for wrapping dead space tightly for Urdu logic.
            cell_width = max(1.0, (width - 2.0) * 0.85)
            text_width = self.get_string_width(f" {line}")
            lines += max(1, math.ceil(text_width / cell_width))
        return max(lines, 1)

    def table(self, headers: list[str], rows: list[list[object]], col_widths: list[float] | None = None, severity_rows: list[tuple[int, tuple[int, int, int]]] | None = None, align: str | list[str] = "L") -> None:
        if not rows:
            self.set_text_font(NO_DATA_TEXT, "I", 8)
            self.cell(0, 5, self._safe_text(NO_DATA_TEXT), ln=True, align="R")
            return
        max_cols = max([len(headers)] + [len(row) for row in rows])
        if max_cols == 0:
            self.set_text_font(NO_DATA_TEXT, "I", 8)
            self.cell(0, 5, self._safe_text(NO_DATA_TEXT), ln=True, align="R")
            return
        if len(headers) < max_cols:
            headers = headers + [f"Detail {index}" for index in range(len(headers) + 1, max_cols + 1)]
        rows = [list(row) + [""] * (max_cols - len(row)) for row in rows]
        if col_widths is None:
            col_widths = self.dynamic_widths(headers, rows)
        elif len(col_widths) < max_cols:
            extra_width = max(12.0, (self.w - self.l_margin - self.r_margin - sum(col_widths)) / max(1, max_cols - len(col_widths)))
            col_widths = col_widths + [extra_width] * (max_cols - len(col_widths))

        severity_map = {idx: color for idx, color in severity_rows or []}

        def _get_configured_align(idx: int) -> str:
            return align[idx] if isinstance(align, list) and idx < len(align) else align if isinstance(align, str) else "L"

        def _smart_align(text: str, default_align: str) -> str:
            if default_align in ("L", "None") and self._contains_arabic(str(text)):
                return "R"
            return default_align or "L"

        def draw_header() -> None:
            self.set_fill_color(*CLR_PRIMARY)
            self.set_text_color(255, 255, 255)
            for i, header in enumerate(headers):
                cell_align = _smart_align(header, _get_configured_align(i))
                self.set_text_font(header, "B", 7)
                self.cell(col_widths[i], 6, f" {self._safe_text(header)}" if cell_align == "L" else self._safe_text(header), border=1, fill=True, align=cell_align)
            self.ln()
            self.set_text_color(*CLR_TEXT)

        draw_header()
        alternate_fill = False

        for row_index, row in enumerate(rows):
            max_lines = max(self._line_count((cell[0] if isinstance(cell, tuple) else cell), col_widths[i]) for i, cell in enumerate(row))
            row_height = max(5 * max_lines, 5)
            if self.get_y() + row_height > self.page_break_trigger:
                self.add_page()
                draw_header()

            x_start = self.l_margin
            y_start = self.get_y()
            fill_color = severity_map.get(row_index, CLR_BG_LIGHT if alternate_fill else None)
            if fill_color:
                self.set_fill_color(*fill_color)

            for i, cell in enumerate(row):
                width = col_widths[i]
                display_text = cell[0] if isinstance(cell, tuple) and len(cell) == 2 else cell
                cell_align = _smart_align(str(display_text), _get_configured_align(i))
                self.rect(x_start, y_start, width, row_height, style="DF" if fill_color else "D")
                self.set_xy(x_start, y_start)
                self.set_text_font(display_text, "", 7)
                if width < 5:  # Absolute safety floor
                    continue
                if isinstance(cell, tuple) and len(cell) == 2:
                    text = self._safe_text(cell[0]).replace("\\n", "\n")
                    self.multi_cell(width, 5, f" {text}" if cell_align == "L" else text, border=0, align=cell_align, link=str(cell[1]))
                else:
                    text = self._safe_text(cell).replace("\\n", "\n")
                    self.multi_cell(width, 5, f" {text}" if cell_align == "L" else text, border=0, align=cell_align)
                x_start += width

            self.set_xy(self.l_margin, y_start + row_height)
            alternate_fill = not alternate_fill

    def note(self, text: str, italic: bool = True, color: tuple[int, int, int] = CLR_MUTED) -> None:
        is_arabic = self._contains_arabic(text)
        align = "R" if is_arabic else "L"
        self.set_text_font(text, "I" if italic else "", 8)
        self.set_text_color(*color)
        self.multi_cell(0, 5, self._safe_text(text), align=align)
        self.set_text_color(*CLR_TEXT)
        self.ln(1)

    def bullet_item(self, text: str, bullet_color: tuple[int, int, int] = CLR_PRIMARY) -> None:
        """Render a bullet point with a rounded circle. Supports RTL Urdu."""
        is_arabic = self._contains_arabic(text)
        y_start = self.get_y()
        
        # Bullet settings
        bullet_size = 1.5
        bullet_margin = 4
        
        if is_arabic:
            # Right-aligned bullet for Urdu
            x_bullet = self.w - self.r_margin - bullet_size - 1
            self.set_fill_color(*bullet_color)
            self.set_draw_color(*bullet_color)
            self.ellipse(x_bullet, y_start + 2, bullet_size, bullet_size, style="FD")
            
            # Text alignment
            orig_r_margin = self.r_margin
            self.set_right_margin(orig_r_margin + bullet_margin)
            self.set_text_font(text, "", 11)
            
            self.multi_cell(0, 6.5, self._safe_text(text), align="R")

            self.set_right_margin(orig_r_margin)
        else:
            # Left-aligned bullet for English
            x_bullet = self.l_margin + 1
            self.set_fill_color(*bullet_color)
            self.set_draw_color(*bullet_color)
            self.ellipse(x_bullet, y_start + 2, bullet_size, bullet_size, style="FD")
            
            # Text alignment
            orig_l_margin = self.l_margin
            self.set_left_margin(orig_l_margin + bullet_margin)
            self.set_text_font(text, "", 10)
            self.multi_cell(0, 6, self._safe_text(text), align="L")
            self.set_left_margin(orig_l_margin)
            
        self.ln(1)