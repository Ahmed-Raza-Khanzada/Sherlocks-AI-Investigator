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
import logging
import re
from pathlib import Path
from typing import Any

from fpdf import FPDF
from fpdf.fonts import FontFace

from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph.normalize import dashed_cnic
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
        self.cell(0, 4, self.t("Every statement cites its source. Inferred links are leads, not findings of fact."))
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
        from bidi.algorithm import get_display

        def label(text: str) -> str:
            return get_display(arabic_reshaper.reshape(text)) if _ARABIC.search(text) else text
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
    pdf.add_page()
    n = 1
    pdf.section(n, "Executive summary")
    for block in (report.get("executive_summary") or "").split("\n\n"):
        pdf.para(block.strip(), size=10, h=5.4)
        pdf.ln(1.5)
    if report.get("model"):
        pdf.para(f"Written by Sherlock ({report['model']}) from the evidence below; ids in [brackets] are listed "
                 "under References.", size=7.5, colour=GREY, style="I")
    inc = report.get("incident") or {}
    if inc or report.get("roles"):
        n += 1
        pdf.section(n, "The incident, as the officer gave it")
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
    n += 1
    _assessment(pdf, n, report)
    n = _board_sections(pdf, n, report)
    if report.get("timeline"):
        n += 1
        pdf.section(n, "Timeline")
        _table(pdf, ["When", "What", "Ref"], [[r["when"], r["what"], r["ref"]] for r in report["timeline"][:60]],
               (32, 128, 18))
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
    _audit(pdf, n, report)
    return bytes(pdf.output())


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
        pdf.label_value("Nearest criminal", f"{near['name']} - {near['hops']} step(s): " + "; ".join(near["route"])[:300])
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
    pdf.section(n, "Key findings")
    items = [g for g in report.get("graph_evidence") or [] if g["kind"] == "finding"]
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
    if report.get("conclusion"):
        pdf.para(report["conclusion"], size=10, h=5.4)
        pdf.ln(2)
    items = report.get("assessments") or []
    if not items:
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
        pdf.multi_cell(pdf.w - 16 - 24 - 26, 5.4, pdf.t(a["statement"]), new_x="LMARGIN", new_y="NEXT")
        end = pdf.get_y()
        pdf.badge(f"{a['confidence'].upper()} CONFIDENCE", CONF.get(a["confidence"], GREY), x=pdf.w - 16 - 34, y=y)
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
