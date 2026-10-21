"""Render the case report (``case_report.assemble``) as a PDF for senior officers.

Layout: a cover with the targets' photographs; then numbered sections - executive
summary, targets, linkages between them, the network diagram, key findings,
Sherlock's assessment (every line with its basis), timeline, evidence register and
digest (quoted facts, CRO poses, fingerprints, lab pages), open questions and
recommendations, and the references every citation resolves to.

Urdu (FIR text, names) is shaped with HarfBuzz (``uharfbuzz``) and set in Noto Naskh;
without HarfBuzz it falls back to arabic-reshaper + bidi so it still reads right.
"""

from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path
from typing import Any

from fpdf import FPDF
from fpdf.enums import TextDirection
from fpdf.fonts import FontFace

from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph.normalize import dashed_cnic
from sherlocks.linkgraph.systems import system_label
from sherlocks.settings import PROJECT_ROOT

logger = logging.getLogger(__name__)

FONT_DIR = PROJECT_ROOT / "vendor" / "report_app" / "assets" / "fonts"
_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")

NAVY = (17, 38, 66)
NAVY_SOFT = (232, 237, 245)
GOLD = (196, 154, 40)
RED = (176, 32, 44)
GREY = (110, 117, 128)
LINE = (210, 215, 222)
INK = (28, 31, 36)
TIER = {"stated": (34, 120, 72), "corroborated": (32, 96, 168), "inferred": (196, 120, 20),
        "speculative": (120, 120, 120)}
CONF = {"high": (34, 120, 72), "medium": (196, 120, 20), "low": (120, 120, 120)}
ROLE = {"accused": (176, 32, 44), "complainant": (222, 120, 30), "witness": (190, 160, 20),
        "officer": (40, 90, 170), "victim": (120, 70, 160)}


def _role_colour(data: dict[str, Any]) -> tuple[int, int, int]:
    flags = " ".join(data.get("flags") or []).lower()
    roles = " ".join(str(f.get("role") or "") for f in data.get("firs") or []).lower()
    if "criminal" in flags or "accused" in roles or "suspect" in roles or "watchlist" in flags:
        return ROLE["accused"]
    if "complainant" in roles:
        return ROLE["complainant"]
    if "police_officer" in flags or "officer" in roles:
        return ROLE["officer"]
    if "witness" in roles:
        return ROLE["witness"]
    return (90, 100, 115)


class _Pdf(FPDF):
    def __init__(self, subject: str) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.subject_line = subject
        self.set_margins(16, 18, 16)
        self.set_auto_page_break(True, margin=18)
        self.shaped = False
        self.sans = "Helvetica"
        self.footnote = "Every statement cites its source. Inferred links are leads, not findings of fact."
        self._fonts()

    def _fonts(self) -> None:
        def add(family: str, style: str, file: str) -> bool:
            path = FONT_DIR / file
            if not path.exists():
                return False
            self.add_font(family, style, str(path))
            return True

        if add("Sans", "", "LiberationSans-Regular.ttf"):
            add("Sans", "B", "LiberationSans-Bold.ttf")
            add("Sans", "I", "LiberationSans-Italic.ttf")
            add("Sans", "BI", "LiberationSans-BoldItalic.ttf")
            self.sans = "Sans"
        fallbacks = []
        if add("Naskh", "", "NotoNaskhArabic-Regular.ttf"):
            add("Naskh", "B", "NotoNaskhArabic-Bold.ttf")
            fallbacks.append("Naskh")
        if add("DejaVu", "", "DejaVuSans.ttf"):
            add("DejaVu", "B", "DejaVuSans-Bold.ttf")
            fallbacks.append("DejaVu")
        if fallbacks:
            self.set_fallback_fonts(fallbacks, exact_match=False)
        try:
            self.set_text_shaping(True)
            self.shaped = True
        except Exception:  # noqa: BLE001 - no uharfbuzz: manual shaping below
            self.shaped = False

    def t(self, value: Any) -> str:
        """Text as the PDF should receive it."""
        text = str(value if value is not None else "")
        text = text.replace("\u200f", "").replace("\u200e", "")
        if self.sans == "Helvetica":
            return text.encode("latin-1", "replace").decode("latin-1")
        if not self.shaped and _ARABIC.search(text):
            try:
                import arabic_reshaper
                from bidi.algorithm import get_display

                return get_display(arabic_reshaper.reshape(text))
            except Exception:  # noqa: BLE001
                return text
        return text

    def _reads(self, text: Any) -> None:
        """An English sentence with Urdu names in it reads left to right - also when a name
        comes first (the library would otherwise lay the whole line out right to left, with
        the full stop in front and the words in the wrong order). A line that is mostly Urdu
        keeps its own direction."""
        if not self.shaped or not self.text_shaping:
            return
        text = str(text or "")
        urdu = len(_ARABIC.findall(text))
        latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
        self.text_shaping["direction"] = TextDirection.LTR if urdu and latin >= urdu else None

    def multi_cell(self, *args: Any, **kwargs: Any) -> Any:
        self._reads(kwargs.get("text") if "text" in kwargs else (args[2] if len(args) > 2 else ""))
        return super().multi_cell(*args, **kwargs)

    def cell(self, *args: Any, **kwargs: Any) -> Any:
        self._reads(kwargs.get("text") if "text" in kwargs else (args[2] if len(args) > 2 else ""))
        return super().cell(*args, **kwargs)

    def font(self, size: float = 9.5, style: str = "", colour: tuple[int, int, int] = INK) -> None:
        self.set_font(self.sans, style, size)
        self.set_text_color(*colour)

    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_fill_color(*NAVY)
        self.rect(0, 0, self.w, 4, style="F")
        self.set_xy(16, 7)
        self.font(7.5, "B", NAVY)
        self.cell(0, 4, self.t(f"SHERLOCKS · CASE INTELLIGENCE REPORT · {self.subject_line}")[:110])
        self.set_xy(16, 7)
        self.font(7.5, "B", RED)
        self.cell(0, 4, "CONFIDENTIAL", align="R")
        self.set_draw_color(*LINE)
        self.line(16, 12.5, self.w - 16, 12.5)
        self.set_y(18)

    def footer(self) -> None:
        if self.page_no() == 1:
            return
        self.set_y(-12)
        self.font(7, "", GREY)
        self.cell(0, 4, self.t(self.footnote))
        self.set_y(-12)
        self.cell(0, 4, f"Page {self.page_no()} / {{nb}}", align="R")

    # -- building blocks -----------------------------------------------------------

    def section(self, number: int, title: str) -> None:
        if self.get_y() > self.h - 50:
            self.add_page()
        self.ln(3)
        y = self.get_y()
        self.set_fill_color(*NAVY)
        self.rect(16, y, 9, 8, style="F")
        self.set_xy(16, y + 1.2)
        self.font(10, "B", (255, 255, 255))
        self.cell(9, 5.6, str(number), align="C")
        self.set_xy(28, y + 0.6)
        self.font(13, "B", NAVY)
        self.cell(0, 7, self.t(title))
        self.set_draw_color(*GOLD)
        self.set_line_width(0.6)
        self.line(28, y + 8.6, self.w - 16, y + 8.6)
        self.set_line_width(0.2)
        self.set_y(y + 12)

    def para(self, text: str, size: float = 9.5, colour: tuple[int, int, int] = INK, style: str = "",
             h: float = 5.0) -> None:
        self.font(size, style, colour)
        self.multi_cell(0, h, self.t(text), new_x="LMARGIN", new_y="NEXT")

    def badge(self, text: str, colour: tuple[int, int, int], x: float | None = None, y: float | None = None) -> float:
        self.font(7, "B", (255, 255, 255))
        w = self.get_string_width(text) + 4
        x = self.get_x() if x is None else x
        y = self.get_y() if y is None else y
        self.set_fill_color(*colour)
        self.rect(x, y + 0.6, w, 4.2, style="F", round_corners=True, corner_radius=1.2)
        self.set_xy(x, y + 0.6)
        self.cell(w, 4.2, text, align="C")
        return w

    def label_value(self, label: str, value: Any, w_label: float = 30) -> None:
        if value in (None, "", []):
            return
        x = self.get_x()
        self.font(8.5, "B", GREY)
        self.cell(w_label, 5, self.t(label))
        self.font(9, "", INK)
        self.multi_cell(self.w - x - 16 - w_label, 5, self.t(value), new_x="LMARGIN", new_y="NEXT")
        self.set_x(x)

    def image_bytes(self, data: bytes, x: float, y: float, w: float, h: float) -> bool:
        try:
            self.image(io.BytesIO(data), x=x, y=y, w=w, h=h, keep_aspect_ratio=True)
            return True
        except Exception:  # noqa: BLE001 - an image fpdf cannot decode is skipped
            return False


def _photo(images: Any, ids: list[str]) -> bytes | None:
    if images is None:
        return None
    for image_id in ids:
        item = images.get(image_id)
        if item:
            return item[0]
    return None


def _initials_box(pdf: _Pdf, name: str, x: float, y: float, w: float, h: float, colour: tuple[int, int, int]) -> None:
    pdf.set_fill_color(*NAVY_SOFT)
    pdf.rect(x, y, w, h, style="F")
    pdf.set_xy(x, y + h / 2 - 5)
    pdf.font(18, "B", colour)
    initials = "".join(p[0] for p in str(name).split()[:2] if p) or "?"
    pdf.cell(w, 10, pdf.t(initials.upper()), align="C")


# --------------------------------------------------------------------------------------
# The network diagram
# --------------------------------------------------------------------------------------


def network_png(graph: dict[str, Any]) -> bytes | None:
    """People and how they connect; record nodes collapsed into person-person links."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import networkx as nx
        from matplotlib import font_manager

        from sherlocks.linkgraph.network import PersonNetwork
    except Exception:  # noqa: BLE001
        return None
    net = PersonNetwork(graph)
    G = net.G
    if G.number_of_nodes() == 0:
        return None
    if G.number_of_nodes() > 70:
        keep = {k["id"] for k in net.key_people(top=45)} | set(net.seeds())
        G = G.subgraph([n for n in G.nodes if n in keep]).copy()
    for file in ("NotoNaskhArabic-Regular.ttf", "DejaVuSans.ttf"):
        if (FONT_DIR / file).exists():
            font_manager.fontManager.addfont(str(FONT_DIR / file))
    try:
        import arabic_reshaper
        import matplotlib
        from bidi.algorithm import get_display

        # From 3.11 the chart library joins and orders Urdu letters itself: doing it here as well
        # would turn every name around.
        own_shaping = tuple(int(x) for x in matplotlib.__version__.split(".")[:2]) >= (3, 11)

        def label(text: str) -> str:
            if own_shaping or not _ARABIC.search(text):
                return text
            return get_display(arabic_reshaper.reshape(text))
    except Exception:  # noqa: BLE001
        def label(text: str) -> str:
            return text

    pos = nx.spring_layout(G, seed=7, k=1.6 / max(1, G.number_of_nodes()) ** 0.5, iterations=120)
    fig, ax = plt.subplots(figsize=(10, 7), dpi=150)
    stated = [(u, v) for u, v, d in G.edges(data=True) if d.get("stated")]
    inferred = [(u, v) for u, v, d in G.edges(data=True) if not d.get("stated")]
    nx.draw_networkx_edges(G, pos, edgelist=stated, ax=ax, edge_color="#5b6b82", width=1.1, alpha=0.75)
    nx.draw_networkx_edges(G, pos, edgelist=inferred, ax=ax, edge_color="#c47814", width=0.9, style="dashed", alpha=0.7)
    seeds = set(net.seeds())
    colours = ["#{:02x}{:02x}{:02x}".format(*_role_colour(net.data(n))) for n in G.nodes]
    sizes = [620 if n in seeds else 260 for n in G.nodes]
    nx.draw_networkx_nodes(G, pos, ax=ax, node_color=colours, node_size=sizes, linewidths=[3 if n in seeds else 0.6 for n in G.nodes],
                           edgecolors=["#c49a28" if n in seeds else "#33404f" for n in G.nodes])
    important = seeds | {k["id"] for k in net.key_people(top=14)}
    labels = {n: label(net.name(n))[:26] for n in G.nodes if n in important}
    nx.draw_networkx_labels(G, pos, labels=labels, ax=ax, font_size=7.5,
                            font_family=["DejaVu Sans", "Noto Naskh Arabic"],
                            bbox={"boxstyle": "round,pad=0.2", "fc": "white", "ec": "none", "alpha": 0.8})
    ax.set_axis_off()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


# --------------------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------------------


def render(report: dict[str, Any], case: CaseFile, graph: dict[str, Any], images: Any = None) -> bytes:
    pdf = _Pdf(report.get("subject") or "")
    pdf.set_title(f"Case report - {report.get('subject')}")
    pdf.set_author("Sherlocks")
    _cover(pdf, report, images)
    if ((report.get("parts") or {}).get("case_story")) is not None:
        # The brief for the investigating officer (report_graph.STRUCTURE): the case and the target only.
        _brief_report(pdf, report, case)
        return bytes(pdf.output())
    pdf.add_page()
    n = 1
    pdf.section(n, "Case summary")
    parts = report.get("summary_parts")
    if parts:
        # P5's seven parts, in their fixed order: facts, disputes, leads, where it stands, gaps, actions.
        titles = report.get("summary_titles") or {}
        # Headings in the report's own order; a report saved before a heading existed simply lacks it.
        for name in report.get("summary_order") or ("brief", "target", "connections", "past_cases", "established",
                                                     "disputed", "leads", "stands", "unclear", "next_steps"):
            value = parts.get(name)
            if not value:
                continue
            pdf.para(titles.get(name, name), size=9.5, style="B", colour=NAVY)
            for line in ([value] if isinstance(value, str) else value):
                pdf.para(line if isinstance(value, str) else f"•  {line}", size=9.8, h=5.2)
            pdf.ln(1.5)
        if report.get("built_without_model"):
            pdf.para("Built from the case board with templates (the model was not used for this summary).", size=7.5,
                     colour=GREY, style="I")
    else:
        for block in (report.get("executive_summary") or "").split("\n\n"):
            pdf.para(block.strip(), size=10, h=5.4)
            pdf.ln(1.5)
    if report.get("model"):
        pdf.para(f"Written by Sherlock ({report['model']}) from the evidence below; ids in [brackets] are listed "
                 "under References.", size=7.5, colour=GREY, style="I")
    inc = report.get("incident") or {}
    if True:
        n += 1
        pdf.section(n, "The case, as the officer gave it")
        basis = (case.dialog or {}).get("case_basis")
        told = [k for k in ("offence", "date", "place", "fir") if inc.get(k)]
        if not told and not basis:
            pdf.para("The officer has not yet said what case is being investigated. Nothing in this report is about "
                     "that case: it sets out what the records hold about the target, their connections and their "
                     "earlier cases.", size=9.5)
        pdf.label_value("Which case", {"new_matter": "A new matter - the FIRs on record are earlier cases.",
                                       "fir_on_record": f"FIR {inc.get('fir') or ''}, already on record - that FIR "
                                                        "describes this case."}.get(basis), 34)
        pdf.label_value("Offence", inc.get("offence"), 34)
        pdf.label_value("Target", inc.get("target_status"), 34)
        if told or basis:
            missing = [w for k, w in (("offence", "what happened"), ("date", "when"), ("place", "where")) if not inc.get(k)]
            pdf.label_value("Not said yet", ", ".join(missing) or None, 34)
        where = inc.get("place") or ""
        if inc.get("lat") is not None:
            where += f" ({float(inc['lat']):.5f}, {float(inc['lon']):.5f})"
        pdf.label_value("Place", where.strip() or None, 34)
        pdf.label_value("Date and time", " ".join(x for x in (inc.get("date"), inc.get("time")) if x) or None, 34)
        pdf.label_value("FIR", inc.get("fir"), 34)
        pdf.label_value("Nearest police station", inc.get("nearest_ps"), 34)
        pdf.label_value("Roles stated", "; ".join(f"{k}: {v}" for k, v in (report.get("roles") or {}).items()) or None, 34)
        if inc.get("lat") is not None:
            pdf.para(f"Map: https://www.openstreetmap.org/?mlat={inc['lat']}&mlon={inc['lon']}#map=16/{inc['lat']}/{inc['lon']}",
                     size=7.5, colour=GREY)
        pdf.ln(2)
    n += 1
    pdf.section(n, "Targets")
    for t in report.get("targets") or []:
        _target_card(pdf, t, images)
    n += 1
    _linkages(pdf, n, report)
    png = network_png(graph)
    if png:
        n += 1
        if pdf.get_y() > pdf.h - 150:
            pdf.add_page()
        pdf.section(n, "Network")
        y = pdf.get_y()
        pdf.image_bytes(png, 16, y, pdf.w - 32, 125)
        pdf.set_y(y + 127)
        x = 16
        for name, colour in (("Accused / criminal record", ROLE["accused"]), ("Complainant", ROLE["complainant"]),
                             ("Witness", ROLE["witness"]), ("Police officer", ROLE["officer"]), ("Other", (90, 100, 115))):
            pdf.set_fill_color(*colour)
            pdf.ellipse(x, pdf.get_y() + 1, 3, 3, style="F")
            pdf.set_xy(x + 4, pdf.get_y())
            pdf.font(7.5, "", GREY)
            pdf.cell(36, 5, name)
            x += 36
        pdf.ln(6)
        pdf.para("Gold ring: target. Solid line: stated by a record. Dashed: inferred (a lead).", size=7.5, colour=GREY)
    n += 1
    _findings(pdf, n, report)
    if report.get("kind") == "file":
        # The report for court and the case file: the records and what they show - no opinion.
        pdf.para("Case file version: Sherlock's suspicions, the ranking of hypotheses, his views on persons and "
                 "the leads are left out. They are in the internal report of the same board version.", size=8.5,
                 colour=GREY, style="I")
    else:
        n += 1
        _assessment(pdf, n, report)
    n = _parts_sections(pdf, n, report)
    n = _board_sections(pdf, n, report)
    part_e = (report.get("parts") or {}).get("E") or {}
    timeline = part_e.get("timeline") or report.get("timeline")
    if timeline:
        n += 1
        pdf.section(n, "Timeline")
        # One timeline from every source; where sources give different times, both are shown.
        for c in part_e.get("conflicting_times") or []:
            pdf.para(f"Conflicting times [{c['id']}]: {c['first']} ({c['sources'][0]}) against {c['second']} "
                     f"({c['sources'][1]}).", size=8.5, colour=RED)
        _table(pdf, ["When", "What", "Ref"], [[r["when"], r["what"], r["ref"]] for r in timeline[:120]], (32, 124, 22))
    n += 1
    _evidence(pdf, n, case, images)
    n += 1
    pdf.section(n, "Open questions and next steps")
    for q in report.get("open_questions") or ["None recorded."]:
        pdf.para(f"?  {q}", size=9.5)
    pdf.ln(2)
    for r in report.get("recommendations") or ["None recorded."]:
        pdf.para(f"→  {r}", size=9.5)
    n += 1
    _references(pdf, n, report, case)
    if report.get("checked"):
        # What the checker left out of the summary and why: kept at the very end, for whoever audits the report.
        pdf.ln(3)
        pdf.para("Report checker's notes", size=8.5, style="B", colour=GREY)
        for warning in (report.get("checked") or [])[:20]:
            pdf.para(f"-  {warning}", size=7.5, colour=GREY)
    _audit(pdf, n, report)
    return bytes(pdf.output())


def _sub(pdf: _Pdf, title: str) -> None:
    """A heading inside a section."""
    if pdf.get_y() > pdf.h - 30:
        pdf.add_page()
    pdf.ln(0.8)
    pdf.para(title, size=9.5, style="B", colour=NAVY)


def _points(pdf: _Pdf, lines: list[str], indent: float = 20, size: float = 9.2, colour: tuple[int, int, int] = INK) -> None:
    """Short points, one a line."""
    for line in lines:
        if not str(line or "").strip():
            continue
        pdf.set_x(indent)
        pdf.font(size, "", colour)
        pdf.multi_cell(pdf.w - indent - 16, 4.9, pdf.t(f"•  {line}"), new_x="LMARGIN", new_y="NEXT")


_REF = re.compile(r"\b[DFWXKH]\d{1,4}\b")


def _numbered(pdf: _Pdf, lines: list[str]) -> None:
    for i, line in enumerate(lines, 1):
        pdf.set_x(20)
        pdf.font(9.4, "", INK)
        pdf.multi_cell(pdf.w - 36, 5, pdf.t(f"{i}.  {line}"), new_x="LMARGIN", new_y="NEXT")


_CITE = re.compile(r"\s*\[([^\[\]]{1,160})\]")
_BARE = re.compile(r"(?<![\w/#-])[DFWXKHGLQ]\d{1,4}(?![\w/-])")
_PREMISES = re.compile(r"\b(sector|block|street|st|house|plot|phase|flat|no|room|ward)\.?\s*$", re.IGNORECASE)


class _Sources:
    """The report never shows an internal id (D1, W2, X3): a record is named by what it is -
    "FIR 211/24 · PS Saddar", "statement of Asif, 06-03-2023" - and the analysts' own entries
    (contradictions, leads, lines of inquiry) are not cited at all."""

    def __init__(self, case: CaseFile, extra: set[str] | None = None) -> None:
        self.case = case
        self.known = case.known_ids() | set(extra or ())

    def label(self, ref: Any) -> str | None:
        ref = str(ref or "").strip("[] ")
        case = self.case
        if ref in case.documents:
            return str(case.documents[ref].get("title") or "a case document")
        if ref in case.statements:
            w = case.statements[ref]
            return f"statement of {w['speaker']}" + (f", {w['date']}" if w.get("date") else "")
        if ref in case.facts:
            return self.label(case.facts[ref]["doc"])
        return None

    def labels(self, refs: Any) -> str:
        return "; ".join(dict.fromkeys(x for x in (self.label(r) for r in refs or []) if x))

    def text(self, value: Any, bare: bool = True) -> str:
        """Text with every [id] turned into the record's name, and - in written lines - every
        bare id too (an id that is not a record, such as a contradiction's, is left out)."""
        def bracket(m: re.Match[str]) -> str:
            out = []
            for part in (p.strip() for p in m.group(1).split(",")):
                head = part.split(" ")[0]
                if re.fullmatch(r"[DFWXKHGLQ]\d{1,4}", head):
                    out.append(self.label(head) or "")
                elif part:
                    out.append(part)
            out = [x for x in dict.fromkeys(out) if x]
            return f" ({'; '.join(out)})" if out else ""

        text = _CITE.sub(bracket, str(value if value is not None else ""))
        if bare:
            def one(m: re.Match[str]) -> str:
                ref = m.group(0)
                if ref not in self.known or _PREMISES.search(text[max(0, m.start() - 12):m.start()]):
                    return ref                          # "Block F7" is a place, not a record
                return self.label(ref) or ""

            text = _BARE.sub(one, text)
        text = re.sub(r"\(\s*[;,]?\s*\)", "", text)
        text = re.sub(r"\s+([,.;:)])", r"\1", re.sub(r"\s{2,}", " ", text)).strip(" :;,")
        return text[:1].upper() + text[1:] if text else text

    def all(self, value: Any) -> Any:
        """Every written line of a part, in plain words."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.all(x) for x in value]
        if isinstance(value, dict):
            return {k: self.all(v) for k, v in value.items()}
        return value


def _brief_report(pdf: _Pdf, report: dict[str, Any], case: CaseFile) -> None:
    """The report as a brief for the investigating officer, about the case and the target only:
    the case at a glance, Sherlock's brief, the story of the case, the target, the people in the
    case, statements and what does not fit, the reports, leads, next steps - then a short annex.
    Before the officer has described the case it is the target's record instead: their earlier
    FIRs, the people the records tie to them, their hotel stays and the earlier reports."""
    parts = report.get("parts") or {}
    file = report.get("kind") == "file"
    src = _Sources(case, {g["id"] for g in report.get("graph_evidence") or []})
    story = parts.get("case_story") or {}
    mine = parts.get("in_case") or {}
    target = parts.get("target") or {}
    record = parts.get("record") or {}
    summary = report.get("summary_parts") or {}
    titles = report.get("summary_titles") or {}
    pdf.footnote = "Every point names the record it rests on. A connection between people is not a finding against anyone."
    pdf.add_page()
    n = 0
    # 1 - the case at a glance
    n += 1
    glance = parts.get("glance")
    if not glance:
        pdf.section(n, "About this report")
        pdf.para("The officer has not yet said what case is being investigated, so nothing in this report is about "
                 "that case. It sets out what the police records hold about the target: their earlier FIRs and "
                 "where each stands, the people the records connect them to, their hotel stays and the earlier "
                 "reports. Tell Sherlock what the case is to get the case report.", size=9.5)
    else:
        pdf.section(n, "The case at a glance")
        for row in glance["rows"]:
            pdf.label_value(row["label"], src.text(row["value"], bare=False), 38)
    pdf.ln(2)
    # 2 - Sherlock's brief
    n += 1
    pdf.section(n, "Sherlock's brief")
    for name in report.get("summary_order") or []:
        value = summary.get("stands_file" if file and name == "stands" else name)
        if not value:
            continue
        pdf.para("Lines of inquiry" if file and name == "stands" else titles.get(name, name), size=9.5, style="B",
                 colour=NAVY)
        _points(pdf, [src.text(x) for x in ([value] if isinstance(value, str) else value)], indent=18, size=9.6)
        pdf.ln(1.2)
    if report.get("built_without_model"):
        pdf.para("Built from the case board with templates (the model was not used for this brief).", size=7.5,
                 colour=GREY, style="I")
    elif report.get("model"):
        pdf.para(f"Written by Sherlock ({report['model']}) from the sections below; the record each point rests on "
                 "is named in (brackets).", size=7.5, colour=GREY, style="I")
    # 3 - the story of the case
    if glance:
        n += 1
        pdf.section(n, "The story of the case")
        pdf.para("What happened, in the order it happened, as this case's own records tell it.", size=8.5, colour=GREY)
        if story.get("events"):
            _table(pdf, ["When", "What happened", "Source"],
                   [[e["when"], src.text(e["what"]), src.labels(e["sources"])] for e in story["events"]], (30, 108, 40))
        else:
            _points(pdf, [("No record of this case is on the board yet: upload the FIR, the case diaries and the "
                           "statements, or tell Sherlock what happened.")], colour=GREY)
    # 4 - the target
    n += 1
    pdf.section(n, "The target")
    dated = {t["pid"]: {c["fir"]: c for c in t["past_cases"]} for t in record.get("targets") or []}
    for t in target.get("targets") or []:
        _sub(pdf, t["name"])
        for label, value in (
                ("Part in this case", " - ".join(x for x in (t.get("role_in_case"), t.get("status_in_case")) if x) or None),
                ("CNIC", dashed_cnic(t["cnic"]) if t.get("cnic") else None), ("Father", t.get("father")),
                ("Also recorded as", ", ".join(t.get("also_recorded_as") or []) or None),
                ("Phones", "; ".join(p["number"] + (f" (SIM registered to {p['registered_to']})" if p.get("registered_to")
                                                    else "") for p in t.get("phones") or []) or None),
                ("Addresses", "; ".join(t.get("addresses") or []) or None),
                ("Vehicles", "; ".join(t.get("vehicles") or []) or None), ("Work", "; ".join(t.get("work") or []) or None)):
            pdf.set_x(20)
            pdf.label_value(label, value, 36)
        if any(p.get("registered_to") for p in t.get("phones") or []):
            pdf.para("A SIM registered on another person's CNIC is common (a relative's or a friend's) and is not a lead "
                     "by itself.", size=7.5, colour=GREY, style="I")
        if not glance:
            continue                                   # the record sections below tell the rest
        window = t.get("window") or {}
        _sub(pdf, "Where the records put them at the time" + (
            f" ({window.get('from', '')[:16].replace('T', ' ')} to {window.get('to', '')[:16].replace('T', ' ')})"
            if isinstance(window, dict) and window.get("from") else ""))
        _points(pdf, t.get("presence") or [("Nothing on record places them anywhere in the incident window. A phone "
                                            "on a tower or a hotel record is not the person.")],
                colour=INK if t.get("presence") else GREY)
        _sub(pdf, "Their own earlier cases")
        if t.get("firs"):
            mine_dated = dated.get(t["pid"]) or {}
            _table(pdf, ["FIR", "Police station", "Offence", "Incident", "Their role", "Where it stands"],
                   [[r["fir"], r["police_station"], r["offence"], (mine_dated.get(_fir_no(r["fir"])) or {}).get("occurred") or "-",
                     r["role"], _stands(mine_dated.get(_fir_no(r["fir"])), r["status"])] for r in t["firs"]],
                   (18, 28, 40, 28, 28, 36))
        else:
            _points(pdf, ["No earlier FIR names them in the systems searched."], colour=GREY)
    circle = target.get("circle") or []
    if glance and circle:
        _sub(pdf, "Close circle")
        pdf.para("Family, co-accused, the same vehicle, travelling together, phone contact - as the records state. "
                 "Tenancy witnesses, landlords, guarantors and SIM owners are not listed.", size=8, colour=GREY)
        kinds = {"crime": "crime", "family": "family", "vehicle": "vehicle", "travel": "travel", "contact": "phone contact"}
        _table(pdf, ["Person", "Tie", "How the record states it", "Source", "Criminal record"],
               [[c["name"], kinds.get(c["kind"], c["kind"]), _plainly(c["how"]), c["source"], "yes" if c["criminal"] else "-"]
                for c in circle], (34, 20, 78, 26, 20))
    if not glance:
        n = _record_sections(pdf, n, record)
    # 5 - people in this case
    if glance:
        n += 1
        pdf.section(n, "People in this case")
        people = story.get("people") or []
        if people:
            _table(pdf, ["Name", "Part", "Status", "What they did or said", "Source"],
                   [[p["name"], p["part"], p["status"], src.text(p["did"]), src.labels(p["sources"])] for p in people],
                   (30, 24, 24, 66, 34))
        else:
            _points(pdf, ["No one is named in this case's records yet."], colour=GREY)
    # 6 - statements and what does not fit
    if glance:
        n += 1
        pdf.section(n, "Statements and what does not fit")
        pdf.para("A statement shows what the person said, not that it is true. \"Changed\" never means \"lied\": a "
                 "change can have an innocent cause.", size=8.5, colour=GREY)
        for person in mine.get("said") or []:
            _sub(pdf, f"{person['name']}" + (f" ({person['role']})" if person.get("role") else ""))
            if person.get("summary"):
                _points(pdf, [src.text(person["summary"])], colour=GREY)
            _table(pdf, ["Date", "Kind", "Recorded by", "What was said (opening words)"],
                   [[w["date"], w["kind"], w["recorded_by"], w["text"][:240]] for w in person["statements"]],
                   (22, 18, 30, 108))
            _points(pdf, [f"Changed between the {src.label(c['earlier']) or 'earlier statement'} and the "
                          f"{src.label(c['later']) or 'later statement'}: {src.text(c['what'])}"
                          for c in person.get("changes") or []]
                    + [f"The {src.label(c['statement']) or 'statement'} does not fit "
                       f"{src.label(c['against']) or 'the record'}: {src.text(c['what'])}" for c in person.get("clashes") or []])
        if not mine.get("said"):
            _points(pdf, ["No statement of this case is on the board yet."], colour=GREY)
        found = mine.get("contradictions") or []
        if found:
            _sub(pdf, "Contradictions with the evidence")
            _table(pdf, ["How serious", "One source says", "Another says", "What would settle it", "Status"],
                   [[x["severity"], src.text(x["first"]), src.text(x["second"]), src.text(x["settle"]), x["status"]]
                    for x in found], (18, 54, 54, 34, 18))
    # 7 - reports
    if glance:
        n += 1
        pdf.section(n, "Medical, forensic and laboratory reports")
        reports = mine.get("reports") or []
        if reports:
            _table(pdf, ["Report", "What it records"],
                   [[f"{r['kind']}: {r['title']}", "\n".join(r["found"]) or "-"] for r in reports], (70, 108))
        else:
            _points(pdf, ["No medical, forensic or laboratory report of this case is on the board."], colour=GREY)
        gaps = [f"{c['exhibit']}: reached {g['to']} {g['days']} day(s) after {g['from']} ({src.labels(g['refs'])})"
                for c in mine.get("custody") or [] for g in c.get("gaps") or []]
        if gaps:
            _sub(pdf, "Gaps in custody")
            _points(pdf, gaps)
        if mine.get("pending"):
            _sub(pdf, "Reports awaited")
            _points(pdf, [f"{x['what']} ({x['record']})" + (f", requested {x['requested_on']}" if x.get("requested_on") else "")
                          + (f" - {src.label(x['doc'])}" if src.label(x["doc"]) else "") for x in mine["pending"]])
    # 8 - leads
    if glance and not file and mine.get("leads"):
        n += 1
        pdf.section(n, "Leads worth checking")
        pdf.para("A lead is a possibility to check - not evidence and not a finding against anyone.", size=8.5, colour=GREY)
        _table(pdf, ["What was noticed", "Why it could matter", "Next step", "Status"],
               [[src.text(k["hint"])[:220], src.text(k["why"]), src.text(k["next_step"]), k["status"]] for k in mine["leads"]],
               (66, 48, 46, 18))
    # 9 - next steps
    n += 1
    pdf.section(n, "Next steps for the investigating officer")
    steps = summary.get("next_steps") or (parts.get("F") or {}).get("next_steps") or []
    if steps:
        _numbered(pdf, [src.text(s) for s in steps[:8]])
    else:
        _points(pdf, ["None recorded."], colour=GREY)
    waiting, seen = [], set()
    for q in report.get("open_questions") or []:
        key = re.sub(r"\W+", " ", str(q)).strip().lower()
        if key not in seen:
            seen.add(key)
            waiting.append(src.text(q))
    if waiting:
        _sub(pdf, "Questions waiting for the officer")
        _points(pdf, waiting[:6])
    # Annex: the police systems checked for the target, and the records the report rests on.
    pdf.add_page()
    pdf.para("ANNEX", size=14, style="B", colour=NAVY)
    n += 1
    pdf.section(n, "Police systems checked for the target")
    for t in target.get("targets") or []:
        if len(target.get("targets") or []) > 1:
            _sub(pdf, t["name"])
        if t.get("systems"):
            _table(pdf, ["System", "What it holds"], [[x["system"], x["found"]] for x in t["systems"]], (40, 138))
        if t.get("nothing_in"):
            _points(pdf, [f"Checked, nothing found: {', '.join(t['nothing_in'])}."], colour=GREY)
    n += 1
    shown = {"summary": summary, "glance": parts.get("glance"), "story": story,
             "in_case": {k: v for k, v in mine.items() if not (file and k == "leads")},
             "record": [c["doc"] for t in record.get("targets") or [] for c in t["past_cases"] if c.get("doc")]
             + [r["doc"] for t in record.get("targets") or [] for r in t["reports"]]}
    cited = sorted(set(_REF.findall(json.dumps(shown, ensure_ascii=False, default=str))), key=lambda x: (x[0], int(x[1:])))
    pdf.section(n, "Records this report rests on")
    docs = list(dict.fromkeys(d for d in (ref if ref in case.documents else (case.statements.get(ref) or case.facts.get(ref)
                                                                             or {}).get("doc") for ref in cited) if d))
    if docs:
        _table(pdf, ["Record", "From"], [[case.documents[d]["title"], case.documents[d].get("source") or "-"] for d in docs],
               (120, 58))
    else:
        pdf.para("No case document was read.", colour=GREY)


def _fir_no(label: Any) -> str:
    m = re.search(r"(\d{1,5})\s*/\s*(\d{2,4})", str(label or ""))
    return f"{m.group(1).lstrip('0')}/{m.group(2)[-2:]}" if m else ""


def _stands(row: dict[str, Any] | None, status: Any = None) -> str:
    """Where an earlier case stands: the outcome, and the record's own words beside it."""
    if not row:
        return str(status or "not on record")
    detail = row.get("outcome_detail") or ""
    return row["outcome"] + (f" ({detail})" if detail and detail.lower() != row["outcome"] else "")


def _plainly(text: Any) -> str:
    from sherlocks.team.record_writer import plainly

    return plainly(text)


def _record_sections(pdf: _Pdf, n: int, record: dict[str, Any]) -> int:
    """The target's record, for a report written before the case is described: earlier FIRs,
    the people the records tie to them, hotel stays, earlier reports."""
    kinds = record.get("kinds") or {}
    targets = record.get("targets") or []
    many = len(targets) > 1
    # Earlier FIRs
    n += 1
    pdf.section(n, "Earlier cases (criminal record)")
    pdf.para("Each FIR the police systems give for the target, newest first. Where the FIR file was read, what the "
             "complainant said and what was found against the target are set out below it.", size=8.5, colour=GREY)
    for t in targets:
        if many:
            _sub(pdf, t["name"])
        cases = t["past_cases"]
        if not cases:
            _points(pdf, [f"No earlier FIR names {t['name']} in the police systems searched."], colour=GREY)
            continue
        _table(pdf, ["FIR", "Police station", "Offence", "Incident (date, time)", "Their role", "Where it stands"],
               [[c["fir"], c["police_station"], c["offence"], c["occurred"] or "not on record", c["role"],
                 _stands(c)] for c in cases], (16, 28, 40, 30, 26, 38))
        for c in cases:
            _sub(pdf, f"FIR {c['fir']} · PS {c['police_station']} - {c['offence']}")
            for label, value in (("Incident", c["occurred"]), ("FIR registered", c["registered"]), ("Place", c["place"]),
                                 ("Complainant", c["complainant"]), ("Their role", c["role"]),
                                 ("Where it stands", _stands(c))):
                pdf.set_x(20)
                pdf.label_value(label, value or None, 34)
            if not c["file_read"]:
                _points(pdf, [("The FIR file was not read in this search: the date, the complainant's statement and "
                           "the evidence are not on record here. Status as the police systems give it.")], colour=GREY)
                continue
            if c["what_happened"]:
                pdf.set_x(20)
                pdf.label_value("What happened", c["what_happened"], 34)
            if c["complainant_said"]:
                pdf.set_x(20)
                pdf.font(8.5, "B", GREY)
                pdf.cell(0, 5, "What the complainant said", new_x="LMARGIN", new_y="NEXT")
                _points(pdf, c["complainant_said"], indent=24)
            pdf.set_x(20)
            pdf.font(8.5, "B", GREY)
            pdf.cell(0, 5, "What was found against them", new_x="LMARGIN", new_y="NEXT")
            _points(pdf, c["found_against"] or ["Nothing in the records read shows what was found against them."],
                    indent=24, colour=INK if c["found_against"] else GREY)
    # Relations
    n += 1
    pdf.section(n, "People connected to the target")
    pdf.para("How the police records connect each person to the target. A connection is not evidence of a crime; a "
             "SIM registered on a relative's or a friend's CNIC is common. The same address means the same house or "
             "the same area - never only the same city.", size=8.5, colour=GREY)
    for t in targets:
        if many:
            _sub(pdf, t["name"])
        if not t["relations"]:
            _points(pdf, ["The records searched connect no one to them."], colour=GREY)
            continue
        for kind, title in kinds.items():
            rows = [r for r in t["relations"] if r["kind"] == kind]
            if not rows:
                continue
            _sub(pdf, f"{title} ({len(rows)})")
            _table(pdf, ["Person", "How they are connected", "Source"],
                   [[r["name"] + (" (police officer)" if r["police_officer"] else "")
                     + (" - has a criminal record" if r["criminal"] and not r["police_officer"] else ""),
                     "\n".join(x["how"] for x in r["ties"]), ", ".join(dict.fromkeys(x["source"] for x in r["ties"]))]
                    for r in rows], (40, 110, 28))
    # Hotel stays
    n += 1
    pdf.section(n, "Hotel history")
    for t in targets:
        if many:
            _sub(pdf, t["name"])
        if t["hotels"]:
            _table(pdf, ["Hotel", "District", "Room", "Check-in", "Check-out", "Stayed at the same time"],
                   [[s["hotel"], s["district"], s["room"], s["check_in"], s["check_out"], ", ".join(s["with"]) or "-"]
                    for s in t["hotels"]], (40, 24, 14, 30, 30, 40))
        else:
            _points(pdf, ["No hotel stay on record (Hotel Eye)."], colour=GREY)
    # Earlier reports
    n += 1
    pdf.section(n, "What the earlier reports say")
    for t in targets:
        if many:
            _sub(pdf, t["name"])
        if not t["reports"]:
            _points(pdf, ["No medical, forensic or laboratory report of their earlier FIRs is on the board."], colour=GREY)
            continue
        for r in t["reports"]:
            _sub(pdf, f"FIR {r['fir']} ({r['case']}) - {r['heading']}")
            pdf.set_x(20)
            pdf.label_value("From", " · ".join(x for x in (r["from"], f"case received {r['received']}" if r["received"] else "")
                                                if x) or None, 30)
            _points(pdf, ([r["says"]] if r["says"] else []) + r["found"], indent=24)
    return n


def _status_line(report: dict[str, Any]) -> str:
    """Whether the assessment covers the board this report was built from."""
    version = report.get("board_version")
    if report.get("status") == "current":
        line = f"Assessment current (board v{version})"
    elif report.get("as_of"):
        line = (f"Assessment as of {str(report['as_of'])[:16].replace('T', ' ')} (board v{report.get('assessment_version')}); "
                f"{len(report.get('not_covered') or [])} newer entr(ies) not yet assessed - see the appendix")
    else:
        line = "Sherlock has not assessed this case yet - sections from the records"
    if not report.get("model"):
        line += " · built without the AI model"
    return line


def _cover(pdf: _Pdf, report: dict[str, Any], images: Any) -> None:
    pdf.add_page()
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, pdf.w, 112, style="F")
    pdf.set_fill_color(*GOLD)
    pdf.rect(0, 112, pdf.w, 1.6, style="F")
    pdf.set_xy(16, 18)
    pdf.font(9, "B", GOLD)
    pdf.cell(0, 5, "SINDH POLICE  ·  SHERLOCKS")
    pdf.set_xy(16, 30)
    pdf.font(28, "B", (255, 255, 255))
    pdf.cell(0, 12, pdf.t(report.get("title") or "Case Intelligence Report"))
    pdf.set_xy(16, 45)
    pdf.font(13, "", (214, 222, 235))
    pdf.multi_cell(pdf.w - 32, 7, pdf.t(report.get("subject") or ""), new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(16, 92)
    pdf.badge("CONFIDENTIAL — FOR OFFICIAL USE ONLY", RED, x=16, y=92)
    pdf.set_xy(16, 100)
    pdf.font(8.5, "", (214, 222, 235))
    pdf.cell(0, 5, pdf.t(f"Generated {report.get('generated_at')}  ·  {_status_line(report)}"))

    targets = report.get("targets") or []
    y = 124
    w, h, gap = 34, 42, 8
    for i, t in enumerate(targets[:4]):
        x = 16 + i * (w + gap + 6)
        data = _photo(images, t.get("images") or [])
        colour = ROLE["accused"] if t.get("criminal") else NAVY
        if not (data and pdf.image_bytes(data, x, y, w, h)):
            _initials_box(pdf, t["name"], x, y, w, h, colour)
        pdf.set_draw_color(*colour)
        pdf.set_line_width(0.8)
        pdf.rect(x, y, w, h)
        pdf.set_line_width(0.2)
        pdf.set_xy(x - 3, y + h + 2)
        pdf.font(9, "B", INK)
        pdf.multi_cell(w + 6, 4.6, pdf.t(t["name"]), align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(x - 3)
        pdf.font(7.5, "", GREY)
        pdf.cell(w + 6, 4, pdf.t(dashed_cnic(t["cnic"]) if t.get("cnic") else (t.get("phones") or [""])[0]), align="C")
    stats = report.get("stats") or {}
    run = report.get("run") or {}
    pdf.set_xy(16, 205)
    rows = [("People in the network", stats.get("people")), ("Links", stats.get("links")),
            ("Linkage patterns found", stats.get("findings")), ("Case documents read", stats.get("documents")),
            ("Quoted facts", stats.get("facts")), ("Data source", (run.get("backend") or "").upper() or "-"),
            ("Search", run.get("seed_label") or "-"), ("Run", (run.get("id") or "-")[:8])]
    parts = report.get("parts") or {}
    if parts.get("case_story") is not None:
        # The brief's cover: the case, not the size of the search.
        glance = {r["label"]: r["value"] for r in (parts.get("glance") or {}).get("rows") or []}
        target = ((parts.get("target") or {}).get("targets") or [{}])[0]
        rows = [("FIR", re.sub(r"\s*\[[^\]]*\]", "", glance.get("FIR") or "not given yet")),
                ("Offence", glance.get("Offence") or "not described yet"),
                ("Target", " - ".join(x for x in (target.get("role_in_case"), target.get("status_in_case")) if x) or "-"),
                ("Case position", glance.get("Case position") or "-"),
                ("Investigating officer", glance.get("Investigating officer") or "-"),
                ("Reports awaited", str(len((parts.get("in_case") or {}).get("pending") or [])) or "0"),
                ("Board version", report.get("board_version")), ("Run", (run.get("id") or "-")[:8])]
        record = ((parts.get("record") or {}).get("targets") or [{}])[0]
        if not glance and record:
            # Before the case is described the cover gives the target's record.
            cases = record.get("past_cases") or []
            rows = [("Case", "not described yet"), ("Target", target.get("name") or record.get("name") or "-"),
                    ("Earlier FIRs", len(cases)),
                    ("Convicted / acquitted", (f"{sum(c['outcome'] == 'convicted' for c in cases)} / "
                                               f"{sum(c['outcome'] == 'acquitted' for c in cases)}")),
                    ("People connected", len(record.get("relations") or [])), ("Hotel stays", len(record.get("hotels") or [])),
                    ("Board version", report.get("board_version")), ("Run", (run.get("id") or "-")[:8])]
    for i, (label, value) in enumerate(rows):
        col, row = i % 2, i // 2
        x, yy = 16 + col * 90, 205 + row * 13
        pdf.set_fill_color(*NAVY_SOFT)
        pdf.rect(x, yy, 86, 11, style="F")
        pdf.set_xy(x + 3, yy + 1.2)
        pdf.font(7, "B", GREY)
        pdf.cell(80, 3.5, label.upper())
        pdf.set_xy(x + 3, yy + 5)
        pdf.font(10, "B", NAVY)
        pdf.cell(80, 5, pdf.t(str(value if value is not None else "-"))[:60])
    pdf.set_xy(16, 262)
    pdf.font(7.5, "I", GREY)
    pdf.multi_cell(pdf.w - 32, 4, pdf.t(
        "Compiled by Sherlocks from Sindh Police and government records and the case documents read during the "
        "search. Every point names the record it rests on. A connection between two people is how the records "
        "link them - check it before acting on it." if parts.get("case_story") is not None else
        "Compiled by Sherlocks from Sindh Police and government records and the case documents read during the "
        "search. Every assessment cites the record, document or quoted fact it rests on. Links marked inferred "
        "are leads for investigation and must be verified before action."), new_x="LMARGIN", new_y="NEXT")


def _target_card(pdf: _Pdf, t: dict[str, Any], images: Any) -> None:
    if pdf.get_y() > pdf.h - 75:
        pdf.add_page()
    y = pdf.get_y()
    colour = ROLE["accused"] if t.get("criminal") else NAVY
    pdf.set_fill_color(*colour)
    pdf.rect(16, y, 1.6, 50, style="F")
    data = _photo(images, t.get("images") or [])
    if not (data and pdf.image_bytes(data, 20, y, 30, 38)):
        _initials_box(pdf, t["name"], 20, y, 30, 38, colour)
    pdf.set_xy(55, y)
    pdf.font(12, "B", NAVY)
    pdf.cell(0, 6, pdf.t(t["name"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(55)
    x = 55
    if t.get("criminal"):
        x += pdf.badge("CRIMINAL RECORD", RED, x=x) + 2
    for flag in [f for f in t.get("flags") or [] if f not in ("criminal_record",)][:4]:
        x += pdf.badge(flag.replace("_", " ").upper(), GREY, x=x) + 2
    pdf.set_xy(55, pdf.get_y() + 6)
    pdf.set_left_margin(55)
    pdf.label_value("Role", (t.get("role") or "").title() or None)
    pdf.label_value("Father", t.get("father"))
    pdf.label_value("CNIC", dashed_cnic(t["cnic"]) if t.get("cnic") else None)
    pdf.label_value("Phones", ", ".join(t.get("phones") or []))
    pdf.label_value("Address", "; ".join(t.get("addresses") or []))
    pdf.label_value("Vehicles", ", ".join(t.get("vehicles") or [])[:200])
    pdf.label_value("Network", f"{t.get('links')} direct link(s), {t.get('records')} record(s)")
    near = t.get("nearest_criminal")
    if near:
        pdf.label_value("Closest person with a criminal record",
                        f"{near['name']} - {near['hops']} step(s) away (see the connections below)", w_label=62)
    pdf.set_left_margin(16)
    pdf.set_y(max(pdf.get_y(), y + 40) + 2)
    if t.get("firs"):
        _table(pdf, ["FIR", "Police station", "Role", "Offence", "Source"],
               [[f.get("label"), f.get("ps"), f.get("role"), f.get("offence"), (f.get("system") or "").upper()]
                for f in t["firs"][:12]], (22, 46, 36, 50, 24))
    if t.get("stays"):
        _table(pdf, ["Hotel", "District", "Check-in", "Check-out"],
               [[s.get("hotel"), s.get("district"), s.get("check_in"), s.get("check_out")] for s in t["stays"][:8]],
               (64, 34, 40, 40))
    ev = t.get("evidence") or {}
    if ev.get("documents"):
        pdf.para("Case documents: " + "; ".join(f"[{d['id']}] {d['title']}" for d in ev["documents"][:8]), size=8.5,
                 colour=NAVY)
    pdf.ln(4)


def _linkages(pdf: _Pdf, n: int, report: dict[str, Any]) -> None:
    gitems = report.get("graph_evidence") or []
    routes = [g for g in gitems if g["kind"] in ("route", "no_route")]
    links = [g for g in gitems if g["kind"] == "link"]
    title = "Linkages between the targets" if len(report.get("targets") or []) > 1 else "The target's connections"
    pdf.section(n, title)
    for g in routes:
        if pdf.get_y() > pdf.h - 45:
            pdf.add_page()
        y = pdf.get_y()
        pdf.badge(g["id"], NAVY, x=16, y=y)
        if g["kind"] == "no_route":
            pdf.set_xy(30, y)
            pdf.para(g["text"], size=9.5, colour=GREY)
            continue
        pdf.badge("INFERRED" if g.get("inferred") else "STATED", TIER["inferred"] if g.get("inferred") else TIER["stated"],
                  x=30, y=y)
        pdf.set_xy(52, y)
        pdf.font(9.5, "B", INK)
        pdf.cell(0, 5.4, pdf.t(" → ".join(_names(g))), new_x="LMARGIN", new_y="NEXT")
        for i, hop in enumerate(g.get("hops") or [], 1):
            pdf.set_x(30)
            pdf.font(9, "", INK)
            kind = " (inferred)" if hop.get("kind") == "weak" else ""
            pdf.multi_cell(0, 4.8, pdf.t(f"{i}. {hop['relation']}{kind}   — {hop.get('via') or 'record'}"),
                           new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2.5)
    if links:
        pdf.ln(1)
        pdf.para("Direct connections of the target(s):", size=9, style="B", colour=NAVY)
        _table(pdf, ["Id", "Connection", "Source"],
               [[g["id"], g["text"], ", ".join(g["sources"])] for g in links[:30]], (12, 136, 30))


def _names(g: dict[str, Any]) -> list[str]:
    hops = g.get("hops") or []
    if not hops:
        return []
    return [hops[0]["from_name"]] + [h["to_name"] for h in hops]


def _findings(pdf: _Pdf, n: int, report: dict[str, Any]) -> None:
    pdf.section(n, "Patterns found in the records")
    pdf.para("What the link analysis noticed across the people and records. A pattern marked INFERRED is something "
             "to check, not a fact.", size=8.5, colour=GREY)
    # A SIM registered in another person's name is common: noted in the records below, not listed as a finding.
    items = [g for g in report.get("graph_evidence") or [] if g["kind"] == "finding"
             and not (g.get("title") or "").startswith("SIMs in someone else")]
    if not items:
        pdf.para("No linkage pattern was found by the rules.", colour=GREY)
    for g in items:
        if pdf.get_y() > pdf.h - 35:
            pdf.add_page()
        y = pdf.get_y()
        pdf.badge(g["id"], NAVY, x=16, y=y)
        pdf.badge((g.get("tier") or "").upper(), TIER.get(g.get("tier") or "", GREY), x=30, y=y)
        pdf.set_xy(32 + pdf.get_string_width((g.get("tier") or "").upper()) + 6, y)
        pdf.font(9.5, "B", INK)
        pdf.multi_cell(0, 5.2, pdf.t(g.get("title") or g["text"]), new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(30)
        pdf.font(9, "", INK)
        pdf.multi_cell(0, 4.8, pdf.t(g.get("summary") or ""), new_x="LMARGIN", new_y="NEXT")
        for e in g.get("evidence") or []:
            pdf.set_x(34)
            pdf.font(8, "", GREY)
            pdf.multi_cell(0, 4.2, pdf.t(f"• {e}"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)


def _assessment(pdf: _Pdf, n: int, report: dict[str, Any]) -> None:
    pdf.section(n, "Sherlock's assessment")
    if report.get("assessment_note"):
        pdf.para(report["assessment_note"], size=9, colour=GREY, style="I")
        pdf.ln(1.5)
    if report.get("conclusion"):
        if report.get("conclusion_about_past"):
            pdf.para("About the earlier cases on record - not a view on the case under investigation:", size=9,
                     style="B", colour=NAVY)
        pdf.para(report["conclusion"], size=10, h=5.4)
        pdf.ln(2)
    items = report.get("assessments") or []
    if not items and not report.get("assessment_note"):
        pdf.para("No assessment could be supported by the evidence gathered.", colour=GREY)
    against = {h["id"]: h.get("against") or [] for h in report.get("hypotheses") or []}
    for i, a in enumerate(items, 1):
        if pdf.get_y() > pdf.h - 30:
            pdf.add_page()
        y = pdf.get_y()
        pdf.set_fill_color(*NAVY_SOFT)
        pdf.set_xy(16, y)
        pdf.font(10, "B", NAVY)
        pdf.cell(8, 5.4, f"{i}.")
        pdf.set_xy(24, y)
        pdf.font(10, "", INK)
        pdf.multi_cell(pdf.w - 16 - 24 - 44, 5.4, pdf.t(a["statement"]), new_x="LMARGIN", new_y="NEXT")
        end = pdf.get_y()
        pdf.badge(f"{a['confidence'].upper()} CONFIDENCE", CONF.get(a["confidence"], GREY), x=pdf.w - 16 - 40, y=y)
        pdf.set_xy(24, end)
        pdf.font(8, "B", NAVY)
        status = f"{a['id']} · {a['status']} · " if a.get("id") else ""
        pdf.cell(0, 4.6, pdf.t(status + "For: " + "  ".join(f"[{b}]" for b in a["basis"])), new_x="LMARGIN", new_y="NEXT")
        if against.get(a.get("id")):
            pdf.set_x(24)
            pdf.font(8, "B", RED)
            pdf.cell(0, 4.6, "Against: " + "  ".join(f"[{b}]" for b in against[a["id"]]), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2.5)
    if report.get("concerns"):
        pdf.para("Concerns: " + "; ".join(report["concerns"]), size=9, colour=GREY)


def _board_sections(pdf: _Pdf, n: int, report: dict[str, Any]) -> int:
    """The sections laid out from the Sherlock team's work on the case board."""
    if report.get("contradictions"):
        n += 1
        pdf.section(n, "Contradictions and conflicts")
        _table(pdf, ["Topic", "Sources say", "Leads (order of trust)", "What would settle it"],
               [[c["topic"] or "-", "; ".join(c["values"]) or "-", c["leads"] or "-", c["settle"]]
                for c in report["contradictions"]], (24, 62, 34, 58))
    n = _casework_sections(pdf, n, report)
    if report.get("suspicions"):
        n += 1
        pdf.section(n, "Suspicions (not evidence)")
        pdf.para("Sherlock's suspicions: leads to check, never evidence. None of them is a finding.", size=8.5,
                 colour=RED, style="I")
        for sp in report["suspicions"]:
            pdf.para(f"?  {sp['statement']}" + (f"  [{', '.join(sp['for'])}]" if sp.get("for") else ""), size=9.5)
        pdf.ln(2)
    if report.get("history") or report.get("replaced"):
        n += 1
        pdf.section(n, "How the assessment changed")
        _table(pdf, ["Id", "Hypothesis", "Before", "After", "Because of"],
               [[h["id"], h.get("statement", "")[:120], h.get("before") or "-", h.get("after") or "-", h.get("cause") or "-"]
                for h in report.get("history") or []][-60:], (12, 70, 26, 26, 44))
        if report.get("replaced"):
            pdf.para("Corrected by the officer (kept, not used):", size=9, style="B")
            _table(pdf, ["Id", "Statement", "Replaced by"],
                   [[r["id"], r["statement"][:160], r["replaced_by"]] for r in report["replaced"]], (14, 140, 24))
    if report.get("cdrs"):
        n += 1
        pdf.section(n, "CDR analysis")
        for c in report["cdrs"]:
            if pdf.get_y() > pdf.h - 50:
                pdf.add_page()
            pdf.para(f"{c['title']} [{c['id']}] - subscriber {c.get('subject') or 'unknown'}"
                     + (f", owner {c['owner']}" if c.get("owner") else ", owner not confirmed: filed under the number"),
                     size=9.5, style="B")
            if c.get("summary"):
                pdf.para(c["summary"], size=8.5, colour=GREY)
            _table(pdf, ["Id", "Finding", "Tier", "Rows"],
                   [[f["id"], f["text"][:220], f["tier"], f.get("rows") or "-"] for f in c["findings"]], (12, 116, 18, 32))
    if report.get("statements") or report.get("qa"):
        n += 1
        pdf.section(n, "The officer's statements and questions")
        for st in report.get("statements") or []:
            pdf.para(f"•  {st['statement']}  [{st['id']}]", size=9)
        pdf.ln(1)
        _table(pdf, ["Id", "Question", "Status", "Answer", "Asked in turns"],
               [[q["id"], q["question"][:140], q["status"], (q.get("answer") or "-")[:100],
                 ", ".join(map(str, q.get("turns") or [])) or "-"] for q in report.get("qa") or []], (12, 70, 20, 50, 26))
    if report.get("views"):
        n += 1
        pdf.section(n, "Asked of Sherlock in the chat")
        _table(pdf, ["Turn", "Question", "Sherlock's view then", "Later"],
               [[v.get("turn") or "-", v["question"][:120], v["view"][:200],
                 v["status"] + (f": {v['note']}" if v.get("note") else "")] for v in report["views"]], (12, 50, 80, 36))
    if report.get("nothing_found"):
        n += 1
        pdf.section(n, "Checked, nothing found")
        for line in report["nothing_found"]:
            pdf.para(f"–  {line}", size=8.5, colour=GREY)
        pdf.ln(2)
    return n


def _parts_sections(pdf: _Pdf, n: int, report: dict[str, Any]) -> int:
    """What the Report team's parts add: the people table (A), every other FIR of the people
    on the case (B1), police system records (B5), the evidence by topic (B7), the open
    points with the question that would clear each (F), and the index by person (annex)."""
    parts = report.get("parts") or {}
    a, b, f = parts.get("A") or {}, parts.get("B") or {}, parts.get("F") or {}
    if a.get("people"):
        n += 1
        pdf.section(n, "People on the case")
        _table(pdf, ["Name", "Role", "CNIC", "Phones", "Vehicles", "FIRs", "Links"],
               [[p["name"], p["role"], p["cnic"], ", ".join(p["phones"]) or "-", ", ".join(p["vehicles"]) or "-", p["firs"],
                 p["links"]] for p in a["people"]], (40, 24, 30, 34, 26, 12, 12))
    firs = (b.get("B1") or {}).get("other_firs") or []
    if firs:
        n += 1
        pdf.section(n, "FIRs of the people on the case")
        _table(pdf, ["Person", "FIR", "Police station", "Offence", "Role", "Status", "Source"],
               [[x["person"], x["fir"], x["police_station"], x["offence"], x["role"], x["status"], x["source"]]
                for x in firs[:80]], (34, 20, 30, 38, 20, 20, 16))
    systems = [x for x in (b.get("B5") or {}).get("systems") or [] if x["lines"]]
    if systems:
        n += 1
        pdf.section(n, "Police system records")
        pdf.para("Every system checked for each person. \"Nothing found\" is listed too: it shows what was checked.",
                 size=8.5, colour=GREY)
        _table(pdf, ["Person", "System", "Found", "Call / checked"],
               [[x["person"], system_label(line["system"]), line["found"] + (" (background, not evidence)" if line.get("background") else ""),
                 line.get("source") or "-"] for x in systems for line in x["lines"]][:160], (36, 24, 88, 30))
    topics = (b.get("B7") or {}).get("by_topic") or {}
    if topics:
        n += 1
        pdf.section(n, "Evidence by topic")
        pdf.para("Everything the records say on one question, together. An earlier FIR is background, not evidence "
                 "about the case under investigation.",
                 size=8.5, colour=GREY)
        _table(pdf, ["Topic", "Record", "What it says", "About", "Source"],
               [[topic, ("earlier " + r["record"] if r.get("background") else r["record"]).replace("fir", "FIR"),
                 f"{str(r['name']).replace('_', ' ').strip().capitalize()}: {r['value']}",
                 "-" if str(r.get("person") or "").lower() in ("", "none") else r["person"], r["ref"]]
                for topic, rows in sorted(topics.items()) for r in rows][:200],
               (20, 26, 84, 28, 20))
    if f.get("open_points"):
        n += 1
        pdf.section(n, "Open points")
        _table(pdf, ["Ref", "Not clear", "Why it matters", "The question that would clear it", "Who"],
               [[p["ref"], p["unclear"], p["why"], p["question"], p["who"]] for p in f["open_points"]], (12, 60, 30, 52, 24))
        for t in f.get("tasks") or []:
            pdf.para(f"Open task [{t['id']}]: {t['text']}", size=9)
    index = (parts.get("annex") or {}).get("index_by_person") or []
    if index:
        n += 1
        pdf.section(n, "Index by person")
        _table(pdf, ["Person", "Role", "Documents", "Statements", "Contradictions", "Leads / hypotheses"],
               [[r["name"], r["role"], ", ".join(r.get("documents") or []) or "-", ", ".join(r.get("statements") or []) or "-",
                 ", ".join(r.get("contradictions") or []) or "-",
                 ", ".join((r.get("leads") or []) + (r.get("hypotheses") or [])) or "-"] for r in index], (36, 22, 40, 26, 26, 28))
    return n


def _casework_sections(pdf: _Pdf, n: int, report: dict[str, Any]) -> int:
    """Plan v5: the hypothesis matrix, contradictions found between sources, recorded
    statements, presence in the incident window, leads and the Skeptic's challenges."""
    m = report.get("matrix")
    if m and m.get("rows"):
        n += 1
        pdf.section(n, "Hypothesis matrix")
        pdf.para("Each hypothesis against each piece of evidence: ++ strongly for, + for, 0 neutral, - against, "
                 "-- strongly against. Ranked by the evidence AGAINST each one, not by how much seems to fit. "
                 "Leads are not evidence and are not in the matrix.", size=8.5, colour=GREY)
        for h in m["hypotheses"]:
            score = (m.get("score") or {}).get(h["id"]) or {}
            lead = "  <- fewest against" if h["id"] == m.get("leader") else ""
            pdf.para(f"{h['id']}  {h['statement']}  ({h['status']}; against {score.get('against', 0)}, "
                     f"for {score.get('for', 0)}){lead}", size=9)
        ids = [h["id"] for h in m["hypotheses"]][:6]
        width = max(10.0, 60.0 / max(1, len(ids)))
        _table(pdf, ["Evidence", *ids], [[f"{r['id']}  {r['text']}", *[r["marks"].get(i, "0") for i in ids]]
                                         for r in m["rows"]], (178 - width * len(ids), *([width] * len(ids))))
        reasons = [f"{r['id']} / {hid}: {why}" for r in m["rows"] for hid, why in (r.get("reasons") or {}).items() if why]
        for line in reasons[:40]:
            pdf.para(f"–  {line}", size=8, colour=GREY)
        pdf.ln(2)
    if report.get("contradictions_found"):
        n += 1
        pdf.section(n, "Contradictions between sources")
        pdf.para("Both sides as the records have them. A contradiction is a question to settle, not a finding "
                 "against anyone.", size=8.5, colour=GREY)
        _table(pdf, ["Id", "Severity", "First source", "Second source", "What would settle it", "Status"],
               [[x["id"], x["severity"], x["first"], x["second"], x["settle"],
                 x["status"] + (f": {x['reason']}" if x.get("reason") and x["status"] != "open" else "")]
                for x in report["contradictions_found"]], (10, 16, 52, 52, 30, 18))
    if report.get("recorded"):
        n += 1
        pdf.section(n, "Recorded statements")
        pdf.para("A recorded statement shows what the person said, not that it is true. \"Changed\" never means "
                 "\"lied\": a change can have an innocent cause.", size=8.5, colour=GREY)
        for person in report["recorded"]:
            if pdf.get_y() > pdf.h - 50:
                pdf.add_page()
            pdf.para(f"{person['name']}" + (f" ({person['role']})" if person.get("role") else ""), size=9.5, style="B")
            if person.get("summary"):
                pdf.para(person["summary"], size=8.5, colour=GREY)
            _table(pdf, ["Id", "Kind", "Date", "Recorded by", "Statement"],
                   [[w["id"], w["kind"], w["date"], w["recorded_by"], w["text"]] for w in person["statements"]],
                   (10, 20, 20, 28, 100))
            for ch in person.get("changes") or []:
                pdf.para(f"Changed between {ch['earlier']} and {ch['later']}: {ch['what']}", size=9)
            for cl in person.get("clashes") or []:
                pdf.para(f"{cl['statement']} does not fit {cl['against']}: {cl['what']}", size=9)
            for rel in person.get("relations") or []:
                pdf.para(f"Relation: {rel}", size=8.5, colour=GREY)
            for note in person.get("notes") or []:
                pdf.para(f"The officer on this [{note.get('ref')}]: {note.get('answer')}", size=8.5, colour=GREY)
            pdf.ln(1)
    presence = report.get("presence")
    if presence and presence.get("rows"):
        n += 1
        pdf.section(n, "Presence in the incident window")
        win = presence.get("window") or {}
        pdf.para(f"Window: {win.get('from')} to {win.get('to')}. Each line says which kind of evidence it is: a phone "
                 "on a tower or a hotel record is not the person.", size=8.5, colour=GREY)
        _table(pdf, ["Person", "Role", "What says where they were"],
               [[r["name"], r["role"], "\n".join(r["cells"]) or "nothing on record for the window"]
                for r in presence["rows"]], (40, 24, 114))
    if report.get("leads"):
        n += 1
        pdf.section(n, "Leads (not evidence)")
        _table(pdf, ["Id", "Hint", "Why it could matter", "Next step", "Bears on", "Status"],
               [[k["id"], k["hint"][:200], k["why"], k["next_step"], f"{k['hypothesis']} ({k['bears']})",
                 k["status"] + (f": {k['reason']}" if k.get("reason") and k["status"] in ("confirmed", "dead end") else "")]
                for k in report["leads"]], (10, 52, 40, 36, 18, 22))
    if report.get("challenges"):
        n += 1
        pdf.section(n, "The Skeptic's challenges")
        _table(pdf, ["Against", "Kind", "Challenge", "Sherlock's reply"],
               [[c.get("hypothesis") or "-", c["kind"], c["text"],
                 c["verdict"] + (f": {c['note']}" if c.get("note") else "")] for c in report["challenges"]],
               (16, 28, 78, 56))
    if report.get("custody") or report.get("pending_reports"):
        n += 1
        pdf.section(n, "Chain of custody and pending reports")
        for chain in report.get("custody") or []:
            steps = "; ".join(f"{st['step']} {st.get('when') or '?'} [{st['ref']}]" for st in chain["steps"])
            gaps = "; ".join(f"{g['to']} {g['days']} days after {g['from']}" for g in chain.get("gaps") or [])
            pdf.para(f"{chain['exhibit']}: {steps}" + (f"  -  GAP: {gaps}" if gaps else ""), size=9)
        for p in report.get("pending_reports") or []:
            pdf.para(f"Awaited: {p['what']} ({p['record']})" + (f", requested {p['requested_on']}" if p.get("requested_on") else "")
                     + f" [{p['doc']}]", size=9)
        pdf.ln(2)
    return n


def _audit(pdf: _Pdf, n: int, report: dict[str, Any]) -> int:
    audit = report.get("audit") or {}
    n += 1
    pdf.section(n, "Appendix: live calls made for this case")
    pdf.para(f"{audit.get('total', 0)} call(s) made, {audit.get('refused', 0)} refused by the safety rules; "
             f"officer(s): {', '.join(audit.get('officers') or []) or '-'}.", size=9)
    rows = [[k, v] for k, v in sorted((audit.get("by_system") or {}).items(), key=lambda kv: -kv[1])]
    _table(pdf, ["System", "Calls"], rows, (120, 30))
    if report.get("status") == "as_of" and report.get("not_covered"):
        n += 1
        pdf.section(n, "Appendix: newer entries not yet assessed")
        for line in report["not_covered"]:
            pdf.para(f"–  {line}", size=8.5, colour=GREY)
    return n


def _table(pdf: _Pdf, headings: list[str], rows: list[list[Any]], widths: tuple[float, ...]) -> None:
    if not rows:
        return
    pdf.font(8, "", INK)
    pdf.set_draw_color(*LINE)
    pdf.set_fill_color(255, 255, 255)
    with pdf.table(col_widths=widths, width=sum(widths), align="LEFT", line_height=4.4,
                   headings_style=FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=NAVY),
                   borders_layout="HORIZONTAL_LINES", padding=1.2, text_align="LEFT") as table:
        head = table.row()
        for h in headings:
            head.cell(h)
        for i, r in enumerate(rows):
            row = table.row(style=FontFace(color=INK, fill_color=(244, 246, 250) if i % 2 else (255, 255, 255)))
            for value in r:
                row.cell(pdf.t("" if value is None else str(value))[:900])
    pdf.ln(3)


def _evidence(pdf: _Pdf, n: int, case: CaseFile, images: Any) -> None:
    pdf.section(n, "Evidence register")
    docs = list(case.documents.values())
    if not docs:
        pdf.para("No case document was read during this search.", colour=GREY)
        attempts = [a for a in case.attempts if a["status"] != "hit"]
        if attempts:
            _table(pdf, ["Asked", "Answer"], [[a["key"], f"{a['status']}: {a['message']}"] for a in attempts[:20]], (60, 118))
        return
    _table(pdf, ["Id", "Document", "Source", "Read", "Facts"],
           [[d["id"], d["title"], d["source"], d.get("fetched_at", "")[:16].replace("T", " "), len(d.get("facts") or [])]
            for d in docs], (12, 86, 40, 28, 12))
    for d in docs:
        if pdf.get_y() > pdf.h - 60:
            pdf.add_page()
        y = pdf.get_y()
        pdf.set_fill_color(*NAVY_SOFT)
        pdf.rect(16, y, pdf.w - 32, 8, style="F")
        pdf.set_xy(18, y + 1.5)
        pdf.font(9.5, "B", NAVY)
        pdf.cell(0, 5, pdf.t(f"[{d['id']}]  {d['title']}"))
        pdf.set_y(y + 10)
        summary = d.get("ai_summary") or d.get("summary")
        if summary:
            pdf.para(summary, size=8.8)
        if d.get("unread_pages"):
            pdf.para(f"Pages not machine-readable: {', '.join(map(str, d['unread_pages']))} (see original).",
                     size=8, colour=RED)
        for url in d.get("urls") or []:
            pdf.para(f"Original: {url}", size=7.5, colour=GREY)
        facts = case.facts_of(d["id"])
        if facts:
            _table(pdf, ["Id", "Fact", "Quote from the document"],
                   [[f["id"], f["statement"], f"“{f['quote']}”"] for f in facts[:25]], (12, 78, 88))
        pictures = [p for p in d.get("images") or []][:8]
        if pictures and images is not None:
            if pdf.get_y() > pdf.h - 55:
                pdf.add_page()
            y = pdf.get_y()
            x = 16
            for p in pictures:
                item = images.get(p["id"])
                if not item:
                    continue
                if x + 40 > pdf.w - 16:
                    x, y = 16, y + 50
                    if y > pdf.h - 55:
                        pdf.add_page()
                        y = pdf.get_y()
                if pdf.image_bytes(item[0], x, y, 40, 40):
                    pdf.set_xy(x, y + 41)
                    pdf.font(6.5, "", GREY)
                    pdf.cell(40, 3.5, pdf.t(p.get("caption") or ""), align="C")
                    x += 44
            pdf.set_y(y + 47)
        pdf.ln(2)


def _references(pdf: _Pdf, n: int, report: dict[str, Any], case: CaseFile) -> None:
    pdf.section(n, "References")
    gitems = {g["id"]: g for g in report.get("graph_evidence") or []}
    rows = []
    for ref in report.get("cited") or []:
        if ref in gitems:
            g = gitems[ref]
            rows.append([ref, g["text"], ", ".join(g["sources"]) or "graph"])
        else:
            hit = case.cite(ref)
            if hit:
                rows.append([ref, hit["title"] + (f" — “{hit['quote']}”" if hit.get("quote") else ""), hit["source"]])
    for ref, g in gitems.items():
        if ref not in (report.get("cited") or []) and g["kind"] in ("route", "finding"):
            rows.append([ref, g["text"], ", ".join(g["sources"]) or "graph"])
    if not rows:
        pdf.para("No references.", colour=GREY)
        return
    _table(pdf, ["Id", "Evidence", "Source"], rows[:150], (12, 132, 34))


def save(path: str | Path, pdf: bytes) -> Path:
    out = Path(path)
    out.write_bytes(pdf)
    return out
