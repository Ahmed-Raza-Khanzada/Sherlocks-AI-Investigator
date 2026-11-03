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
from collections.abc import Callable
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
        # Never "inferred" or "corroborated": the officer is told what the records show.
        text = re.sub(r"\s*\((?:inferred|corroborated)\)", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\binferred\b", "found by comparing records", text, flags=re.IGNORECASE)
        text = re.sub(r"\bcorroborat\w*", "confirmed by more than one record", text, flags=re.IGNORECASE)
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
        # Digits read left to right too: a cell with an Urdu name over its CNIC and phone laid out right to left
        # turned the numbers round ("7-9897377-42201").
        latin = sum(1 for ch in text if ch.isascii() and ch.isalnum())
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


def two_step_png(network: dict[str, Any]) -> bytes | None:
    """The target's network up to two steps (report part ``network``): the target in the middle,
    the people tied to them directly on the first ring, the people who matter one step further on
    the outer ring - each placed beside those they are reached through. People involved in this
    case are red, people with a criminal record orange; a link a record states is a solid line,
    one found by comparing records a dashed line."""
    import math

    first, second, targets = network.get("first") or [], network.get("second") or [], network.get("targets") or []
    # A number on many people's records stands as ONE node: those people are not ties of the target by it.
    numbers = [{"pid": f"number:{x['number']}", "name": f"{x['number']} ({len(x['people'])} people)",
                "involved": False, "criminal": False, "part": "", "number": True} for x in network.get("shared_numbers") or []]
    first = [*first, *numbers]
    network = {**network, "edges": [*(network.get("edges") or []),
                                    *({"a": targets[0]["pid"], "b": x["pid"], "stated": False} for x in numbers if targets)]}
    if not targets or not first:
        return None
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except Exception:  # noqa: BLE001
        return None
    for file in ("NotoNaskhArabic-Regular.ttf", "DejaVuSans.ttf"):
        if (FONT_DIR / file).exists():
            font_manager.fontManager.addfont(str(FONT_DIR / file))
    own_shaping = tuple(int(x) for x in matplotlib.__version__.split(".")[:2]) >= (3, 11)

    def label(text: str) -> str:
        if own_shaping or not _ARABIC.search(text):
            return text
        try:
            import arabic_reshaper
            from bidi.algorithm import get_display

            return get_display(arabic_reshaper.reshape(text))
        except Exception:  # noqa: BLE001
            return text

    pos: dict[str, tuple[float, float]] = {}
    for i, t in enumerate(targets):                      # one target in the middle; two or three side by side
        pos[t["pid"]] = ((i - (len(targets) - 1) / 2) * 0.35, 0.0)
    angle: dict[str, float] = {}
    inner = list(first)

    def place_inner() -> None:
        for i, r in enumerate(inner):
            a = 2 * math.pi * i / len(inner) + math.pi / 2
            angle[r["name"]] = a
            pos[r["pid"]] = (math.cos(a), math.sin(a))

    place_inner()
    # Fewer crossings: each direct connection moves beside those they lead to (two passes, by mean direction).
    for _ in range(2):
        def pull(r: dict[str, Any]) -> float:
            mates = [angle[m] for x in second if r["name"] in x["through"] for m in x["through"] if m in angle]
            xs, ys = [math.cos(angle[r["name"]]) * 2, *map(math.cos, mates)], [math.sin(angle[r["name"]]) * 2, *map(math.sin, mates)]
            return math.atan2(sum(ys), sum(xs)) % (2 * math.pi)

        inner.sort(key=pull)
        place_inner()
    def base(r: dict[str, Any]) -> float:
        """Where the people a second-step person is reached through stand (their mean direction)."""
        xs = [math.cos(angle[n]) for n in r["through"] if n in angle]
        ys = [math.sin(angle[n]) for n in r["through"] if n in angle]
        return math.atan2(sum(ys), sum(xs)) if xs else 0.0

    # The outer ring in the order of the people they are reached through, spread evenly and turned to sit
    # beside them - nobody overlaps, and each stands near whom they are reached through.
    order = sorted(second, key=lambda r: base(r) % (2 * math.pi))
    if order:
        step = 2 * math.pi / len(order)
        bases = [base(r) for r in order]
        turn = math.atan2(sum(math.sin(b - i * step) for i, b in enumerate(bases)),
                          sum(math.cos(b - i * step) for i, b in enumerate(bases)))
        for i, r in enumerate(order):
            a = bases[i] if len(order) == 1 else turn + i * step
            pos[r["pid"]] = (2.0 * math.cos(a), 2.0 * math.sin(a))
    reach = 2.5 if second else 1.45                   # no one at the second step: the first ring fills the picture
    fig, ax = plt.subplots(figsize=(10, 9.4) if second else (10, 8.2), dpi=150)
    # The rings by depth, each labelled: step 1 - tied to the target directly; step 2 - one step further.
    rings = [(1.0, f"STEP 1 · direct connections ({len(first) - len(numbers)})", "#8fa3c0")]
    if second:
        rings.append((2.0, f"STEP 2 · one step further ({len(second)})", "#c3cad6"))
    for radius, text, colour in rings:
        ax.add_patch(plt.Circle((0, 0), radius, fill=False, ls=(0, (3, 3)), lw=1.1, color=colour, zorder=0))
        a = math.radians(225)                          # lower left: clear of the first person, placed at the top
        ax.text(radius * math.cos(a), radius * math.sin(a), text, ha="center", va="center", fontsize=7.5,
                fontweight="bold", color="#3d4b63", zorder=2, family=["DejaVu Sans"],
                bbox={"boxstyle": "round,pad=0.3", "fc": "#eef2f8", "ec": colour, "lw": 0.8})
    for e in network.get("edges") or []:
        if e["a"] in pos and e["b"] in pos:
            (x1, y1), (x2, y2) = pos[e["a"]], pos[e["b"]]
            ax.plot([x1, x2], [y1, y2], color="#5b6b82" if e["stated"] else "#c47814", lw=1.0 if e["stated"] else 0.8,
                    ls="-" if e["stated"] else (0, (4, 3)), alpha=0.8, zorder=1)

    def dot(pid: str, row: dict[str, Any] | None, size: float, ring: int) -> None:
        colour = ("#1f3a5f" if row is None else "#ffffff" if row.get("number") else "#c0392b" if row["involved"]
                  else "#e08a1e" if row["criminal"] else "#9aa6b8")
        x, y = pos[pid]
        ax.scatter([x], [y], s=size, c=colour, edgecolors="#c49a28" if row is None else "#5b6b82" if row.get("number") else "white",
                   linewidths=3 if row is None else 1.2, zorder=3)
        name = label((row or {}).get("name") or next(t["name"] for t in targets if t["pid"] == pid))[:24]
        tag = f"\n{row['part']}" if row and row.get("part") else ""
        ax.text(x, y - (0.13 if ring == 0 else 0.1), name + tag, ha="center", va="top",
                fontsize=8.5 if ring == 0 else 7, fontweight="bold" if ring < 2 or (row and row["involved"]) else "normal",
                family=["DejaVu Sans", "Noto Naskh Arabic"], zorder=4,
                bbox={"boxstyle": "round,pad=0.15", "fc": "white", "ec": "none", "alpha": 0.85})

    for t in targets:
        dot(t["pid"], None, 900, 0)
    for r in first:
        dot(r["pid"], r, 260, 1)
    for r in second:
        dot(r["pid"], r, 150, 2)
    ax.set_xlim(-reach, reach)
    ax.set_ylim(-reach + 0.05, reach - 0.15)
    ax.set_aspect("equal")
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
    many = len(report.get("targets") or []) > 3
    pdf = _Pdf(f"{len(report['targets'])} targets searched together" if many else report.get("subject") or "")
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
    "FIR 77/24 · PS Saddar", "statement of Asif, 06-03-2023" - and the analysts' own entries
    (contradictions, leads, lines of inquiry) are not cited at all."""

    def __init__(self, case: CaseFile, extra: set[str] | None = None) -> None:
        self.case = case
        self.known = case.known_ids() | set(extra or ())

    def label(self, ref: Any) -> str | None:
        ref = str(ref or "").strip("[] ")
        case = self.case
        if ref in case.documents:
            doc = case.documents[ref]
            # An uploaded document by what it is ("Judicial Commission of Inquiry Report"), not "Upload: file.pdf".
            return str((doc.get("reading") or {}).get("kind") or str(doc.get("title") or "a case document").replace("Upload: ", ""))
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
        # "(Inquiry report, Inquiry report, Inquiry report)": each record named once.
        text = re.sub(r"\(([^()]{3,400})\)", lambda m: "(" + "; ".join(dict.fromkeys(
            x.strip() for x in re.split(r"\s*[;,]\s*(?=[A-Z])", m.group(1)) if x.strip())) + ")", text)
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
        from sherlocks.evidence.questioner import only_the_record

        holds = ("It sets out what the police records hold about the target: their earlier FIRs and where each "
                 "stands, the people the records connect them to, their hotel stays and the earlier reports.")
        pdf.para(f"The officer said there is no case: they are checking the target's record. {holds}"
                 if only_the_record(case) else
                 f"The officer has not yet said what case is being investigated, so nothing in this report is about "
                 f"that case. {holds} Tell Sherlock what the case is to get the case report.", size=9.5)
    else:
        pdf.section(n, "The case at a glance")
        for row in glance["rows"]:
            pdf.label_value(row["label"], src.text(row["value"], bare=False), 38)
        question = ((parts.get("case_analysis") or {}).get("weighing") or {}).get("question")
        if question:
            pdf.label_value("Under inquiry", src.text(question), 38)
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
    analysis = parts.get("case_analysis") or {}
    weighing = analysis.get("weighing") or {}
    said = analysis.get("statements") or []
    reads = {r["doc"]: r for r in analysis.get("reports") or []}
    # 3 - the victim, or the target: who they are, where the records put them, their close circle
    n += 1
    def with_status(p: dict[str, Any]) -> str:
        """Part and status once each: "Victim / Deceased" and "Deceased (found 29/07/2026)" -> "... - found 29/07/2026"."""
        part, status = str(p.get("part") or ""), str(p.get("status") or "")
        if not status or status == "not on record":
            return part
        first = status.split()[0].lower().strip("(),")
        if first in part.lower():
            status = re.sub(r"^\S+\s*", "", status).strip(" ()")
        return part + (f" - {status}" if status else "")

    parts_of = {p["name"]: with_status(p) for p in story.get("people") or []}

    def part_of(name: str) -> str:              # "Kamran Aslam" in the records is the target "Kamran Aslam Khan"
        return parts_of.get(name) or next((v for k, v in parts_of.items() if _same(k, name)), "")

    victim = any(re.search(r"\b(victim|deceased)\b", " ".join((str(t.get("role_in_case") or ""), str(t.get("status_in_case") or ""),
                                                             part_of(t["name"]))), re.IGNORECASE)
                 for t in target.get("targets") or [])
    group = parts.get("group") or {}
    pdf.section(n, "The victim" if glance and victim else ("The targets" if group else "The target"))
    if group:
        _targets_table(pdf, group, target, part_of)
    for t in [] if group else target.get("targets") or []:
        _sub(pdf, t["name"])
        role = " - ".join(x for x in (t.get("role_in_case"), t.get("status_in_case")) if x) or (
            _plainly(part_of(t["name"])) if glance else "")
        for label, value in (
                ("Part in this case", role or None),
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
    if group and glance:
        here = [t for t in target.get("targets") or [] if t.get("presence")]
        _sub(pdf, "Where the records put them at the time")
        if here:
            for t in here:
                _points(pdf, [f"{t['name']}: {x}" for x in t["presence"]])
        else:
            _points(pdf, [("Nothing on record places any target anywhere in the incident window. A phone on a tower "
                           "or a hotel record is not the person.")], colour=GREY)
    if group:
        n = _group_section(pdf, n, group)
    circle = target.get("circle") or []
    if glance and circle and not group:
        _sub(pdf, "Close circle")
        pdf.para("Family, accused together, the same vehicle, travelling together, phone contact - as the records state. "
                 "Tenancy witnesses, landlords, guarantors and SIM owners are not listed.", size=8, colour=GREY)
        kinds = {"crime": "accused together / complaint", "family": "family", "vehicle": "vehicle", "travel": "travel",
                 "contact": "phone contact"}
        _table(pdf, ["Person", "Tie", "How the record states it", "Source", "Criminal record"],
               [[c["name"], kinds.get(c["kind"], c["kind"]), _plainly(c["how"]), c["source"], "yes" if c["criminal"] else "-"]
                for c in circle], (34, 24, 74, 26, 20))
    added = (parts.get("added") or {}).get("sections") or []

    def place(n: int, where: str) -> int:              # the sections the Section writer added for this case
        return _added_sections(pdf, n, [s for s in added if s.get("place") == where], src)

    n = place(n, "targets")
    if not glance:
        n = _network_section(pdf, n, parts.get("network") or {})
        n = place(n, "network")
        n = _record_sections(pdf, n, record, said=said, reads=reads)
        n = place(n, "record")
        n = place(n, "story")
    else:
        # 4 - the story of the case
        n += 1
        pdf.section(n, "The story of the case")
        pdf.para("What happened, in the order it happened, as this case's own records tell it.", size=8.5, colour=GREY)
        if story.get("events"):
            # The source column names the record: the same names are not repeated at the end of each step.
            _table(pdf, ["When", "What happened", "Source"],
                   [[_day(e["when"]), src.text(_uncited(e["what"])), src.labels(e["sources"])]
                    for e in story["events"]], (30, 108, 40))
        else:
            _points(pdf, [("No record of this case is on the board yet: upload the FIR, the case diaries and the "
                           "statements, or tell Sherlock what happened.")], colour=GREY)
        n = place(n, "story")
        # 5 - forensic and medical findings
        filed_twice = {c for r in analysis.get("reports") or [] for c in r.get("copies") or []}
        n = _forensic_section(pdf, n, [reads.get(r["id"]) or {"doc": r["id"], "title": r["title"], "findings": r["found"]}
                                       for r in mine.get("reports") or [] if r["id"] not in filed_twice], mine, src)
        # 5b - the documents the officer uploaded (an inquiry report, a post-mortem, a memo), read in full
        n = _documents_section(pdf, n, analysis.get("documents") or [], src)
        # 6 - weighing the evidence
        n = _weighing_section(pdf, n, weighing, src)
        # 7 - statements, and what does not fit
        n = _statements_section(pdf, n, [x for x in said if x.get("this_case")], mine, src,
                                clashes=weighing.get("clashes"))
        # 8 - people in this case, and the persons of interest
        n += 1
        pdf.section(n, "People in this case")
        people = story.get("people") or []
        police = [p for p in people if _police(p)]
        people = [p for p in people if not _police(p)]
        if people:
            _table(pdf, ["Name", "Part", "Status", "What they did or said", "Source"],
                   [[p["name"], _short_part(src.text(p["part"])), src.text(p["status"]), src.text(_uncited(p["did"])),
                     src.labels(p["sources"])] for p in people], (30, 34, 20, 60, 34))
        elif not police:
            _points(pdf, ["No one is named in this case's records yet."], colour=GREY)
        # The officers who handled the case: who registered it, who investigated, who sent the exhibits - in order.
        for r in analysis.get("reports") or []:
            # "PI Mr. Imran Saleem, Station Investigation Officer, PS Saddar"; "Bilal Aslam (PI) / Naveed Rafiq (PI)"
            for who in re.split(r"\s*/\s*(?=[A-Z][a-z])|\s+and\s+|;", str(r.get("submitted_by") or "")):
                name = re.sub(r"\s*\([^)]*\)", "", who.split(",")[0]).strip()
                if len(_rankless(name)) < 3:
                    continue
                same = next((p for p in police if _same(_rankless(name), _rankless(p["name"]))
                             or _rankless(name) in _rankless(p["name"]) or _rankless(p["name"]) in _rankless(name)), None)
                if same is None:
                    police.append({"name": name, "part": "sent exhibits to the laboratory", "did": f"Sent the exhibits of {r.get('title')}.",
                                   "sources": []})
                elif same.get("part") == "sent exhibits to the laboratory" and r.get("title") not in same["did"]:
                    same["did"] = same["did"].rstrip(".") + f"; {r.get('title')}."
        if police:
            _sub(pdf, "Police and officials on the case")
            pdf.para("Who registered, investigated, examined and handled the evidence of this case, as the records name them - "
                     "the officer holding the case now should be confirmed.", size=8, colour=GREY)
            _table(pdf, ["Officer", "Their part", "What they did", "Source"],
                   [[p["name"], _short_part(src.text(p["part"])), src.text(_uncited(p["did"])), src.labels(p.get("sources") or []) or "-"]
                    for p in police], (36, 40, 70, 32))
        if weighing.get("persons"):
            _sub(pdf, "Persons of interest")
            pdf.para("People the records tie to this case, and what to check about each - a reason to check, not a "
                     "finding against anyone.", size=8, colour=GREY)
            _table(pdf, ["Person", "How the records tie them to this case", "What to check"],
                   [[x["name"], src.text(x["tie"]), src.text(x["check"])] for x in weighing["persons"]], (34, 80, 64))
        # 9 - the network
        n = _network_section(pdf, n, parts.get("network") or {}, many=bool(group))
        n = place(n, "network")
        # 10 - the target's own record: earlier cases, hotel stays, tenancy, other reports and statements on them
        n += 1
        pdf.section(n, "Earlier cases of the " + ("victim" if victim else "targets" if group else "target"))
        for t in record.get("targets") or []:
            if len(record.get("targets") or []) > 1:
                _sub(pdf, t["name"])
            _earlier_cases(pdf, t)
        copies = {c for r in analysis.get("reports") or [] for c in r.get("copies") or []}      # a report filed twice
        n = _more_record(pdf, n, record, skip_reports=(set(reads) & {r["id"] for r in mine.get("reports") or []}) | copies,
                         said=[x for x in said if not x.get("this_case")], reads=reads)
        n = place(n, "record")
        # 11 - leads that rest on this case's records
        if not file and mine.get("leads"):
            n += 1
            pdf.section(n, "Leads worth checking")
            pdf.para("A lead is a possibility to check - not evidence and not a finding against anyone.", size=8.5, colour=GREY)
            _table(pdf, ["What was noticed", "Why it could matter", "Next step", "Status"],
                   [[src.text(k["hint"])[:220], src.text(k["why"]), src.text(k["next_step"]), k["status"]]
                    for k in mine["leads"]], (66, 48, 46, 18))
    n = place(n, "end")
    # 12 - the case papers: which a case of this kind needs, which are on the board
    if glance and (analysis.get("papers") or {}).get("papers"):
        n = _papers_section(pdf, n, analysis["papers"])
    # 13 - questions still open, 14 - the action plan, then Sherlock's own sections
    if glance and (weighing.get("gaps") or weighing.get("answered")):
        n += 1
        pdf.section(n, "Questions still open")
        if weighing.get("gaps"):
            _numbered(pdf, [src.text(g) for g in weighing["gaps"]])
        else:
            _points(pdf, ["Every question the analysis raised is answered by the records below."], colour=GREY)
        if weighing.get("answered"):
            _sub(pdf, "Already answered by the records")
            pdf.para("Questions checked against the records before this report was written: the records answer them, "
                     "so they are not open. Check the answer in the record named.", size=8, colour=GREY)
            _table(pdf, ["Question", "What the records say"],
                   [[src.text(x["question"]), src.text(x["answer"])] for x in weighing["answered"]], (70, 108))
    n += 1
    pdf.section(n, "Action plan for the investigating officer" if glance and weighing.get("actions")
                else "Next steps for the investigating officer")
    if glance and weighing.get("actions"):
        _table(pdf, ["Priority", "What to do", "What it would settle"],
               [[a["priority"], src.text(a["action"]), src.text(a["settles"]) or "-"] for a in weighing["actions"]],
               (18, 92, 68))
    else:
        steps = summary.get("next_steps") or []
        if steps:
            _numbered(pdf, [src.text(s) for s in steps[:8]])
        else:
            _points(pdf, ["None recorded."], colour=GREY)
    for extra in weighing.get("sections") or []:
        n += 1
        pdf.section(n, extra["heading"][:80])
        pdf.para("Added by Sherlock: something the officer should see that the sections above do not cover.", size=8,
                 colour=GREY, style="I")
        _points(pdf, [src.text(x) for x in extra["points"]])
    # Annex: the police systems checked for the target, and the records the report rests on.
    pdf.add_page()
    pdf.para("ANNEX", size=14, style="B", colour=NAVY)
    n += 1
    pdf.section(n, "Police systems checked for the targets" if group else "Police systems checked for the target")
    for t in target.get("targets") or []:
        if len(target.get("targets") or []) > 1:
            _sub(pdf, t["name"])
        if t.get("systems"):
            held: dict[str, list[str]] = {}
            for x in t["systems"]:                  # one row a system: Hotel Eye checked against each date says so once
                found = re.sub(r"^the new case:", "the date of this case:", str(x["found"]))
                if found not in held.setdefault(x["system"], []):
                    held[x["system"]].append(found)
            _table(pdf, ["System", "What it holds"], [[k, "\n".join(v)] for k, v in held.items()], (40, 138))
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


def _added_sections(pdf: _Pdf, n: int, sections: list[dict[str, Any]], src: Any) -> int:
    """Sections the Section writer chose for this case: a heading, why it is there, a table and/or points.
    Column widths follow what the columns hold."""
    for s in sections:
        n += 1
        pdf.section(n, s["heading"][:80])
        pdf.para("Added by Sherlock for this case" + (f": {s['why']}" if s.get("why") else ".")
                 + " Every row is taken from the records named; check it there before acting on it.",
                 size=8, colour=GREY, style="I")
        columns, rows = s.get("columns") or [], s.get("rows") or []
        if columns and rows:
            shown = [[src.text(c) for c in r[:len(columns)]] for r in rows]       # sized by what is printed
            longest = [max([len(str(c))] + [len(str(r[i])) for r in shown if i < len(r)]) for i, c in enumerate(columns)]
            word = [max(len(w) for x in [c, *(r[i] for r in shown if i < len(r))] for w in [*str(x).split(), ""])
                    for i, c in enumerate(columns)]
            share = [max(min(max(x, 12), 60), w + 3) for x, w in zip(longest, word, strict=True)]
            total = pdf.w - 32
            widths = [total * x / sum(share) for x in share]
            _table(pdf, columns, shown, tuple(widths))
        if s.get("points"):
            _points(pdf, [src.text(x) for x in s["points"]])
    return n


def _targets_table(pdf: _Pdf, group: dict[str, Any], target: dict[str, Any], part_of: Callable[[str], str]) -> None:
    """Several targets side by side: who each is, how to reach them, their record."""
    parts = {t["name"]: " - ".join(x for x in (t.get("role_in_case"), t.get("status_in_case")) if x) or _plainly(part_of(t["name"]))
             for t in target.get("targets") or []}
    pdf.para("Each target as the records hold them: identity, every phone and address on record, and their criminal "
             "record. 'Group' is the group the ties below join them into.", size=8.5, colour=GREY)
    _table(pdf, ["Target", "CNIC / father", "Phones", "Addresses", "Criminal record", "Group"],
           [[t["name"] + (f"\n{parts.get(t['name'])}" if parts.get(t["name"]) else ""),
             f"{t['cnic']}\nfather: {t['father']}", "\n".join(t["phones"]) or "-", "\n".join(t["addresses"]) or "-",
             t["record"], str(t["group"]) if t.get("group") else "none"] for t in group["targets"]],
           (30, 28, 26, 52, 30, 12))
    owned = [f"{p['number']} ({t['name']}) is registered to {p['registered_to']}" for t in target.get("targets") or []
             for p in t.get("phones") or [] if p.get("registered_to")]
    if owned:
        pdf.para("SIMs registered on another person's CNIC (common - a relative's or a friend's - and not a lead by "
                 "itself): " + "; ".join(owned) + ".", size=7.5, colour=GREY, style="I")


def _day(when: Any) -> str:
    """2026-07-28T04:00:00 -> 28-07-2026 04:00; anything else as it is."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?", str(when or ""))
    if not m:
        return str(when or "")
    clock = f" {m.group(4)}:{m.group(5)}" if m.group(4) and f"{m.group(4)}:{m.group(5)}" != "00:00" else ""
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}{clock}"


def _nocharges(text: Any) -> str:
    """A tie without its charge list: "Co-accused in FIR 99/25" - the charges are in the record section."""
    return re.sub(r"\s*\(charges:.*?\)", "", str(text or ""))


_TIE_KIND = {"stated": "stated by a record", "matched": "found by comparing records", "common contact": "a common contact",
             "same person?": "if the same person"}


def _group_section(pdf: _Pdf, n: int, group: dict[str, Any]) -> int:
    """Several targets searched together: the groups the records tie them into, each tie, names on a record that
    may be a target, the people tied to two or more targets, and each target's contacts to investigate."""
    n += 1
    pdf.section(n, "How the targets are connected")
    pdf.para(f"{len(group['targets'])} targets were searched together. This is how the records tie them to each other "
             "and who connects them - a reason to investigate, not a finding against anyone.", size=8.5, colour=GREY)
    _sub(pdf, "The groups the records tie together")
    lines = [f"Group {i + 1} - {', '.join(g['members'])}: " + "; ".join(g["why"]) for i, g in enumerate(group["groups"])]
    if group.get("alone"):
        lines.append(f"Nothing on record ties these to the other targets: {', '.join(group['alone'])}. Check their "
                     "call records against the others' numbers, and their addresses and associates.")
    _points(pdf, lines or ["The records tie none of the targets to another."])
    if group.get("bridges"):
        _points(pdf, group["bridges"], colour=(176, 96, 16))
    if group.get("ties"):
        _sub(pdf, "Ties between the targets")
        same: dict[tuple[str, ...], dict[str, Any]] = {}
        for r in group["ties"]:          # the pairs tied the same way, in one row: "co-accused in FIR 99/25" once
            key = (*(_nocharges(h) for h in r["how"] if not h.startswith("the same complainant")), "|", *r["kinds"])
            row = same.setdefault(key, {"who": [], "how": [_nocharges(h) for h in r["how"]], "kinds": r["kinds"]})
            row["who"] += [x for x in (r["a"], r["b"]) if x not in row["who"]]
            row["how"] += [_nocharges(h) for h in r["how"] if _nocharges(h) not in row["how"]]
        _table(pdf, ["Targets", "How the records tie them", "Found as"],
               [["\n".join(r["who"]), "\n".join(r["how"]), ", ".join(_TIE_KIND.get(k, k) for k in r["kinds"])]
                for r in same.values()], (44, 104, 30))
    if group.get("same_person"):
        _sub(pdf, "Names on a record that may be a target")
        pdf.para("A person a record names without a CNIC, whose name sounds the same as a target's. Confirm from the "
                 "record's own particulars (father's name, address, CNIC, photo) before treating them as the same "
                 "person.", size=8, colour=GREY)
        _table(pdf, ["Target", "Name on the record", "CNIC on the record", "Where", "Would tie them to"],
               [[s["target"], s["name_in_record"], s["cnic_in_record"], "; ".join(s["where"]), ", ".join(s["tied_to"])]
                for s in group["same_person"]], (32, 34, 26, 46, 40))
    if group.get("common"):
        _sub(pdf, "Common connections - people tied to two or more targets")
        pdf.para("Directly, or one step on through a contact of the target. Check each: who they are to the targets, "
                 "their numbers against the targets' call records, their address.", size=8, colour=GREY)
        _table(pdf, ["Person", "CNIC", "Phones", "Address", "Tied to, and how", "Criminal record"],
               [[c["name"], c["cnic"], "\n".join(c["phones"]) or "-", c["address"] or "-",
                 "\n".join(f"{x['target']}: {_nocharges(x['how'])}" for x in c["tied_to"]),
                 c["record"] or "-"] for c in group["common"]], (28, 26, 24, 30, 46, 24))
    _sub(pdf, "Contacts of each target to investigate")
    pdf.para("Everyone the records tie to each target directly, with their numbers and address - those tied to other "
             "targets and those with a criminal record first. A SIM owner, a tenancy witness or a guarantor is listed "
             "for contact, not as a lead.", size=8, colour=GREY)
    none = []
    for c in group["contacts"]:
        if not c["rows"]:
            none.append(c["target"])
            continue
        _label(pdf, f"{len(c['rows']) + c['more']} contact(s) of {c['target']}", indent=16)
        _table(pdf, ["Person", "CNIC", "Phones", "Address", "How connected", "Criminal record"],
               [[r["name"], r["cnic"], "\n".join(r["phones"]) or "-", r["address"] or "-",
                 _nocharges(r["how"]) + ("" if r["stated"] else " (found by comparing records)")
                 + (f"\nAlso tied to: {', '.join(r['also_tied_to'])}" if r["also_tied_to"] else ""),
                 r["record"] or "-"] for r in c["rows"]], (30, 26, 24, 34, 40, 24))
        if c["more"]:
            pdf.para(f"{c['more']} more contact(s) of {c['target']} on the case board.", size=7.5, colour=GREY, style="I")
    if none:
        _points(pdf, [f"No contact on record (outside the targets): {', '.join(none)}."], colour=GREY)
    return n


def _network_section(pdf: _Pdf, n: int, network: dict[str, Any], many: bool = False) -> int:
    """The target's network up to two steps: the picture, then who is connected, how and through whom."""
    first, second = network.get("first") or [], network.get("second") or []
    if not first:
        return n
    n += 1
    pdf.add_page()
    whose = "The targets' network" if many else "The target's network"
    pdf.section(n, f"{whose}: step 1 and step 2" if second else f"{whose}: step 1")
    pdf.para(f"Ring 1 - step 1 ({len(first)}): everyone the records tie to the target directly. " + (
        f"Ring 2 - step 2 ({len(second)}): one step further, only the people who matter - involved in this case, with a "
        "criminal record, or tied to two or more of the direct connections. " if second else
        "Step 2: no one one step further is involved in this case, has a criminal record or ties to two of them. ")
        + "A connection is how the records link two people, not a finding against anyone.", size=8.5, colour=GREY)
    png = two_step_png(network)
    if png:
        y = pdf.get_y() + 1
        high = 158 if second else 120
        pdf.image_bytes(png, 16, y, pdf.w - 32, high)
        pdf.set_y(y + high + 2)
        x = 18
        for name, colour in (("Target", NAVY), ("Involved in this case", (192, 57, 43)),
                             ("Criminal record", (224, 138, 30)), ("Other", (154, 166, 184))):
            pdf.set_fill_color(*colour)
            pdf.ellipse(x, pdf.get_y() + 1, 3, 3, style="F")
            pdf.set_xy(x + 4, pdf.get_y())
            pdf.font(7.5, "", GREY)
            pdf.cell(38, 5, name)
            x += 42
        pdf.ln(6)
        pdf.para("Solid line: a record states the link. Dashed line: found by comparing records (the same address, "
                 "phone, hotel stay or father's name) - check it before acting on it.", size=7.5, colour=GREY, style="I")
    def how(row: dict[str, Any]) -> str:
        return (row.get("how") or "-") + ("" if row.get("stated") else " (found by comparing records)")

    for x in network.get("shared_numbers") or []:
        _sub(pdf, f"A number on many people's records: {x['number']}")
        others = x.get("elsewhere") or 0
        pdf.para(f"The target's number {x['number']} is also on the records of {x['holders'] - 1} other people "
                 "(licences, hotel check-ins, FIR files). A number written on many people's forms is usually a family, "
                 "shop or agent's number - it does not tie them to the target by itself. The "
                 f"{len(x['people'])} tied to the target by nothing else are listed here once, and no one is followed "
                 f"through them: {', '.join(x['people'])}."
                 + (f" The other {others} are tied to the target in other ways too, or are named in this case, and "
                    "are shown below." if others > 0 else ""), size=8.5)
    if many:
        # Each target's direct contacts, with their numbers and address, are in "How the targets are connected".
        pdf.para(f"Step 1 - the {len(first)} direct connections drawn above are listed, with their CNIC, phones and "
                 "address, under each target in 'How the targets are connected'.", size=8.5)
    else:
        _sub(pdf, f"Step 1 - direct connections ({len(first)})")
        _table(pdf, ["Person", "Connected to", "How the records connect them", "In this case", "Criminal record"],
               [[r["name"], ", ".join(r["to"]), how(r), r.get("part") or "-", r.get("record") or ("yes" if r["criminal"] else "-")]
                for r in first], (32, 26, 62, 20, 38))
    if second:
        _sub(pdf, f"Step 2 - one step further, the people who matter ({len(second)})")
        _table(pdf, ["Person", "Reached through", "How the records connect them", "In this case", "Criminal record"],
               [[r["name"], ", ".join(r["through"]), how(r), r.get("part") or "-",
                 r.get("record") or ("yes" if r["criminal"] else "-")] for r in second], (32, 30, 58, 20, 38))
    left = network.get("not_shown") or {}
    if left.get("first") or left.get("second"):
        pdf.para(f"Not shown: {left.get('first', 0)} more direct connection(s) and {left.get('second', 0)} more people "
                 "one step further, with no part in this case, no criminal record and only one tie.", size=7.5,
                 colour=GREY, style="I")
    return n


def _fir_no(label: Any) -> str:
    m = re.search(r"(\d{1,5})\s*/\s*(\d{2,4})", str(label or ""))
    return f"{m.group(1).lstrip('0')}/{m.group(2)[-2:]}" if m else ""


def _stands(row: dict[str, Any] | None, status: Any = None) -> str:
    """Where an earlier case stands: the outcome, and the record's own words beside it."""
    if not row:
        return str(status or "not on record")
    detail = row.get("outcome_detail") or ""
    if detail.lower().startswith(row["outcome"]):
        detail = detail[len(row["outcome"]):].strip(" ,;")       # "untraced (Untraced, 30-11-2021)" says it twice
    return row["outcome"] + (f" ({detail})" if detail and detail.lower() != row["outcome"] else "")


def _plainly(text: Any) -> str:
    from sherlocks.team.record_writer import plainly

    return plainly(text)


def _same(a: Any, b: Any) -> bool:
    """The same person's name in two spellings ("Kamran Aslam Khan", "Kamraan Aslam Khan"), or the first words of it
    ("Kamran Aslam" for "Kamran Aslam Khan")."""
    from sherlocks.linkgraph.normalize import sounds_like

    if not (a and b):
        return False
    a, b = (re.sub(r"\s*\([^)]*\)", "", str(x)).strip() for x in (a, b))
    short, long_ = sorted((a.lower().split(), b.lower().split()), key=len)
    return (sounds_like(a, b) and sounds_like(b, a)) or (len(short) >= 2 and long_[:len(short)] == short)


_POLICE = re.compile(r"\b(?:police|officer|inspector|constable|\bS\.?I\.?P?\b|\bA\.?S\.?I\b|\bP\.?I\b|\bI\.?O\.?\b|SHO|DSP|SSP|"
                     r"\bSP\b|\bASP\b|DIG|IGP?|head moharr?ar|investigating)", re.IGNORECASE)


def _uncited(text: Any) -> str:
    """A line without the record ids it cites ("[F404, F407]", "(F404, F407)") - for a table whose Source column
    names them."""
    text = re.sub(r"\s*\[[^\[\]]*\]", "", str(text or ""))
    return re.sub(r"\s*\((?:\s*[DFWXKHGLQ]\d{1,4}\s*[,;]?)+\)", "", text).strip()


def _police(p: dict[str, Any]) -> bool:
    """A police officer of the case - by their part or status, or a rank before the name."""
    from sherlocks.team.case_scope import _RANK

    return bool(_POLICE.search(f"{p.get('part') or ''} {p.get('status') or ''}") or _RANK.match(str(p.get("name") or "")))


def _rankless(name: Any) -> str:
    from sherlocks.team.case_scope import _RANK

    text = str(name or "").strip()
    while True:                                  # "PI/IO Mr. Shabbir Ahmed Laghari": every rank, then the title
        m = _RANK.match(text) or re.match(r"^\W*(?:mr|ms|mrs)\.?(?=\s)", text, re.IGNORECASE)
        if not m or not m.end():
            return text.strip(" ./:-")
        text = text[m.end():].strip(" ./:-")


def _short_part(text: str) -> str:
    """A person's part in a few words: the first clause of what the writer gave ("Complainant and father of ..."),
    without the record it cites, cut at a whole word."""
    first = re.split(r"(?<=[.;])\s|\s-\s|, who\b", str(text or "").strip())[0].strip(" .;")
    first = re.sub(r"\s*\([^()]*\)\s*$", "", first).strip(" ,;")       # "(FIR 12/26 · PS ...)": the source column says it
    if len(first) > 90:
        first = first[:90].rsplit(" ", 1)[0].rstrip(" ,;") + "…"
    return first or "-"


def _label(pdf: _Pdf, text: str, indent: float = 20) -> None:
    pdf.set_x(indent)
    pdf.font(8.5, "B", NAVY)
    pdf.cell(0, 5, pdf.t(text), new_x="LMARGIN", new_y="NEXT")


def _report_block(pdf: _Pdf, r: dict[str, Any], src: Any) -> None:
    """One report read in full: who and when, each exhibit with its result, the findings, what they indicate."""
    _sub(pdf, r.get("title") or r.get("doc") or "Report")
    for label, value in (("Laboratory", r.get("lab")), ("Report no.", r.get("report_no")), ("Submitted by", r.get("submitted_by")),
                         ("Received", r.get("received")), ("Issued", r.get("issued")), ("FIR", r.get("fir")),
                         ("Filed twice", ("the same report is also on file as " + "; ".join(r["also_filed_as"])
                                          + " - read once here") if r.get("also_filed_as") else None)):
        if value:
            pdf.set_x(20)
            pdf.label_value(label, value, 30)
    if r.get("exhibits"):
        _table(pdf, ["Article", "What it is", "Test", "Result"],
               [[e.get("article") or "-", e.get("item") or "-", e.get("test") or "-", e.get("result") or "-"]
                for e in r["exhibits"]], (16, 50, 38, 74))
    if r.get("findings"):
        _label(pdf, "What the report found")
        _points(pdf, [src.text(x) for x in r["findings"]], indent=22)
    if r.get("indicates"):
        _label(pdf, "What it indicates - Sherlock's reading, to be confirmed with the expert")
        _points(pdf, [src.text(x) for x in r["indicates"]], indent=22)
    if r.get("not_stated"):
        _label(pdf, "Not stated in the report")
        _points(pdf, [src.text(x) for x in r["not_stated"]], indent=22, colour=GREY)
    if not any(r.get(k) for k in ("exhibits", "findings", "indicates")):
        _points(pdf, ["The report is on file but what it found has not been read yet."], indent=22, colour=GREY)


def _forensic_section(pdf: _Pdf, n: int, reports: list[dict[str, Any]], mine: dict[str, Any], src: Any) -> int:
    n += 1
    pdf.section(n, "Forensic and medical findings")
    pdf.para("Each report of this case: what it examined, the result for each exhibit, what it found and what that "
             "indicates. The findings are the report's own; what they indicate is Sherlock's reading.", size=8.5, colour=GREY)
    for r in reports:
        _report_block(pdf, r, src)
    if not reports:
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
    return n


def _documents_section(pdf: _Pdf, n: int, docs: list[dict[str, Any]], src: Any) -> int:
    """Each document the officer uploaded, as Sherlock read it in full: what it is, what it finds, the evidence it
    describes, the lines of investigation it raises, what people told it and whom it names."""
    if not docs:
        return n
    n += 1
    pdf.section(n, "Other reports and documents of the case")
    pdf.para("Documents the officer uploaded, read in full. What each finds is the document's own finding - to be "
             "checked against the records, not taken as proof. Its dated steps are in the story of the case above.",
             size=8.5, colour=GREY)

    def page(x: dict[str, Any]) -> str:
        pages = str(x.get("pages") or "")
        return f" (pp. {pages})" if "-" in pages else (f" (p. {pages})" if pages else "")

    for d in docs:
        _sub(pdf, d.get("kind") or d["title"])
        read = (f"all {d['pages']} pages" if d.get("pages") else "in full") + (
            f", in {d['parts']} parts" if (d.get("parts") or 0) > 1 else "") + (
            f" - {d['parts'] - d['parts_read']} part(s) could not be read" if d.get("parts") and d.get("parts_read") is not None
            and d["parts_read"] < d["parts"] else "")
        for label, value in (("Document", d["title"]), ("Issued by", d.get("issued_by")), ("Date", d.get("date")),
                             ("About", d.get("about")), ("Read", read)):
            if value:
                pdf.set_x(20)
                pdf.label_value(label, value, 30)
        if d.get("summary"):
            _label(pdf, "What it establishes")
            pdf.set_x(22)
            pdf.para(src.text(d["summary"]), size=9.2)
        if d.get("top"):
            _label(pdf, "Most important for the investigation")
            _points(pdf, [src.text(x) for x in d["top"]], indent=22)
        if d.get("findings"):
            _label(pdf, f"What it finds ({len(d['findings'])})")
            _points(pdf, [src.text(x["text"]) + page(x) for x in d["findings"][:40]], indent=22)
        if d.get("evidence"):
            _label(pdf, "The evidence it describes, and what happened to it")
            _table(pdf, ["Item", "What happened to it", "Pages"],
                   [[x["item"], src.text(x["what"]), x.get("pages") or "-"] for x in d["evidence"][:40]], (40, 120, 18))
        if d.get("leads"):
            _label(pdf, "Lines of investigation it raises")
            _numbered(pdf, [src.text(x["text"]) + page(x) for x in d["leads"][:25]])
        if d.get("said"):
            _label(pdf, "What people told it (by their account)")
            _table(pdf, ["Who", "What they said", "Pages"],
                   [[x["who"], src.text(x["said"]), x.get("pages") or "-"] for x in d["said"][:30]], (40, 120, 18))
        if d.get("people"):
            _label(pdf, f"People it names ({len(d['people'])})")
            _table(pdf, ["Name", "Their part", "What it says about them"],
                   [[x["name"], x.get("part") or "-", src.text(x.get("says") or "-")] for x in d["people"][:60]], (38, 46, 94))
    return n


def _papers_section(pdf: _Pdf, n: int, papers: dict[str, Any]) -> int:
    """The papers a case of this kind needs: on the board, named in a document but not on the board, or missing."""
    n += 1
    pdf.section(n, "Case papers: on file and still needed")
    kinds = {"death": "a death", "firearm": "a firearm", "kidnap": "a kidnapping or abduction", "property": "a robbery or theft",
             "vehicle": "a vehicle theft", "exhumed": "an exhumation", "injury": "an injury", "sexual": "a sexual offence",
             "fraud": "a fraud or breach of trust", "cheque": "a dishonoured cheque", "forgery": "a forgery",
             "narcotics": "narcotics", "threat": "extortion or threats", "arms": "an illegal weapon", "terrorism": "terrorism"}
    said = ", ".join(kinds.get(k, k) for k in papers.get("kinds") or [])
    pdf.para("The papers a case of this kind" + (f" ({said})" if said else "") + " needs, and whether Sherlock has them. "
             "'Not on the board' means Sherlock has not been given it: get it, or upload it.", size=8.5, colour=GREY)
    _table(pdf, ["Paper", "On the board?", "Where"],
           [[r["paper"], r["status"], r["where"] or "-"] for r in papers["papers"]], (70, 36, 72))
    return n


def _weighing_section(pdf: _Pdf, n: int, w: dict[str, Any], src: Any) -> int:
    if not w or not (w.get("explanations") or w.get("view")):
        return n
    n += 1
    pdf.section(n, "Weighing the evidence")
    if w.get("question"):
        pdf.para(f"The question: {src.text(w['question'])}", size=10, style="B", colour=NAVY)
    pdf.para("Each possible explanation, what in the records supports it, what goes against it and what would settle it. "
             "This is Sherlock's analysis to guide the investigation - not a finding.", size=8.5, colour=GREY)
    for e in w.get("explanations") or []:
        _sub(pdf, e["name"])
        for title, key in (("What supports it", "for"), ("What goes against it", "against"), ("What would settle it", "settle")):
            if e.get(key):
                _label(pdf, title)
                _points(pdf, [src.text(x) for x in e[key]], indent=22)
    if w.get("view"):
        _sub(pdf, "Sherlock's view (not confirmed)")
        pdf.para(src.text(w["view"]), size=9.5)
    return n


def _said_block(pdf: _Pdf, person: dict[str, Any], src: Any) -> None:
    """One statement: who, what they say explained in English, then their own words in full."""
    head = " - ".join(x for x in (person.get("person"), person.get("part"), person.get("date"), person.get("kind")) if x)
    _label(pdf, head, indent=18)
    if person.get("explained"):
        _label(pdf, "What they say (explained in English by Sherlock)", indent=22)
        pdf.set_x(24)
        pdf.para(src.text(person["explained"]), size=9.2)
    _label(pdf, "Their own words (as recorded, in full)", indent=22)
    pdf.set_x(24)
    pdf.para(person.get("said") or "-", size=8.8, colour=(60, 66, 80))
    pdf.ln(1.5)


def _statements_section(pdf: _Pdf, n: int, entries: list[dict[str, Any]], mine: dict[str, Any], src: Any,
                        title: str = "Statements", clashes: list[dict[str, Any]] | None = None) -> int:
    n += 1
    pdf.section(n, title)
    pdf.para("A statement shows what the person said, not that it is true. Each is explained in English, then given in "
             "the person's own words in full.", size=8.5, colour=GREY)
    for fir in entries:
        _sub(pdf, fir["fir"])
        for person in fir["people"]:
            _said_block(pdf, person, src)
    if not entries:
        _points(pdf, ["No statement of this case is on the board yet."], colour=GREY)
    changed = [f"{person['name']}: changed between the {src.label(c['earlier']) or 'earlier statement'} and the "
               f"{src.label(c['later']) or 'later statement'} - {src.text(c['what'])}"
               for person in mine.get("said") or [] for c in person.get("changes") or []]
    found_too = [x for x in clashes or [] if x.get("one")]     # statements against the reports, as Sherlock read them
    clashes = [f"{person['name']}: the {src.label(c['statement']) or 'statement'} does not fit "
               f"{src.label(c['against']) or 'the record'} - {src.text(c['what'])}"
               for person in mine.get("said") or [] for c in person.get("clashes") or []]
    found = mine.get("contradictions") or []
    if changed or clashes or found or found_too:
        _sub(pdf, "What does not fit")
        pdf.para("\"Changed\" never means \"lied\": a change can have an innocent cause.", size=8, colour=GREY)
        _points(pdf, changed + clashes)
        if found_too:
            _table(pdf, ["One source says", "Another source says", "Why it matters"],
                   [[src.text(x["one"]), src.text(x["other"]), src.text(x["why"]) or "-"] for x in found_too], (64, 64, 50))
        if found:
            _table(pdf, ["How serious", "One source says", "Another says", "What would settle it", "Status"],
                   [[x["severity"], src.text(x["first"]), src.text(x["second"]), src.text(x["settle"]), x["status"]]
                    for x in found], (18, 54, 54, 34, 18))
    return n


def _earlier_cases(pdf: _Pdf, t: dict[str, Any]) -> None:
    """The target's earlier FIRs, by their part in each: as complainant, as accused, named otherwise."""
    cases = t.get("past_cases") or []
    if not cases:
        _points(pdf, [f"No earlier FIR names {t['name']} in the police systems searched."], colour=GREY)
        return

    def when(c: dict[str, Any]) -> str:
        return c["occurred"] or (f"registered {c['registered']}" if c["registered"] else "not on record")

    def fir(c: dict[str, Any], charges: bool = True) -> str:
        return "\n".join(x for x in (f"FIR {c['fir']}", c["offence"] if charges else "", _stands(c)) if x)

    from sherlocks.linkgraph.case_queries import role_kind

    by: dict[str, list[dict[str, Any]]] = {"complainant": [], "accused": [], "other": []}
    for c in cases:
        # A row kept from an older report may lack its part and its summary: both come from what it does hold.
        c = {**c, "role_kind": c.get("role_kind") or role_kind([str(c.get("role") or "")]),
             "summary": c.get("summary") or str(c.get("what_happened") or "")[:300] or c.get("offence") or "-"}
        by[c["role_kind"] if c["role_kind"] in ("complainant", "accused") else "other"].append(c)
    if by["complainant"]:
        _sub(pdf, f"As complainant ({len(by['complainant'])})")
        _table(pdf, ["Date and time", "Police station", "What the case is about", "FIR, charges, where it stands"],
               [[when(c), c["police_station"], c.get("summary") or "-", fir(c)] for c in by["complainant"]], (28, 26, 84, 40))
    if by["accused"]:
        _sub(pdf, f"As accused ({len(by['accused'])})")
        _table(pdf, ["Date and time", "Accused by (complainant)", "Complainant's relation to them", "What the case is about",
                     "Police station", "Charges", "FIR, where it stands"],
               [[when(c), c["complainant"] or "not named", c.get("relation") or "-", c.get("summary") or "-",
                 c["police_station"], c["offence"], fir(c, charges=False)] for c in by["accused"]],
               (22, 24, 24, 50, 20, 18, 20))
    if by["other"]:
        _sub(pdf, f"Named in other ways - witness, victim, named in the file ({len(by['other'])})")
        _table(pdf, ["Date and time", "Their part", "What the case is about", "Police station", "FIR, charges, where it stands"],
               [[when(c), c["role"], c.get("summary") or "-", c["police_station"], fir(c)] for c in by["other"]],
               (24, 28, 72, 22, 32))


def _more_record(pdf: _Pdf, n: int, record: dict[str, Any], skip_reports: set[str] | None = None,
                 skip_firs: set[str] | None = None, said: list[dict[str, Any]] | None = None,
                 reads: dict[str, Any] | None = None) -> int:
    """Hotel stays, tenancy records, the reports on the target and what each person states in the FIRs."""
    from sherlocks.team.record_writer import _duration
    targets = record.get("targets") or []
    many = len(targets) > 1
    n += 1
    pdf.section(n, "Hotel stays")
    for t in targets:
        if many:
            _sub(pdf, t["name"])
        if t.get("hotels"):
            _table(pdf, ["Check-in", "Location", "Hotel", "Stayed there at the same time", "Check-out", "Duration"],
                   [[s["check_in"], s.get("location") or s["district"], s["hotel"] + (f" (room {s['room']})" if s["room"] != "-" else ""),
                     ", ".join(s["with"]) or "-", re.sub(r"\s+00:00(?::00)?$", " (time not recorded)", str(s["check_out"])),
                     _duration(s.get("check_in"), s.get("check_out"))] for s in t["hotels"]],
                   (26, 28, 34, 32, 30, 28))
        else:
            _points(pdf, ["No hotel stay on record (Hotel Eye)."], colour=GREY)
    n += 1
    pdf.section(n, "Tenancy records")
    for t in targets:
        if many:
            _sub(pdf, t["name"])
        if t.get("tenancies"):
            _table(pdf, ["Landlord", "Tenant", "Witnesses", "Date and time", "Property and other details",
                         "Their part, record"],
                   [[x["landlord"], x["tenant"], x["witnesses"], x["when"], x["details"], f"{x['part']}\n{x['record']}"]
                    for x in t["tenancies"]], (30, 30, 32, 28, 40, 18))
        else:
            _points(pdf, ["No tenancy record names them as tenant, landlord or witness (matched by CNIC or phone)."],
                    colour=GREY)
            if t.get("family_tenancies"):
                _label(pdf, "The family's tenancy records (the father's - often the home the target lived in)")
                _table(pdf, ["Landlord", "Tenant", "Witnesses", "Date and time", "Property and other details",
                             "Their part, record"],
                       [[x["landlord"], x["tenant"], x["witnesses"], x["when"], x["details"], f"{x['part']}\n{x['record']}"]
                        for x in t["family_tenancies"]], (30, 30, 32, 28, 40, 18))
    reports = [(t, r) for t in targets for r in t.get("reports_on") or [] if r["doc"] not in (skip_reports or set())]
    if reports:
        n += 1
        pdf.section(n, "Reports on the target")
        for t, r in reports:
            if (reads or {}).get(r["doc"]):
                _report_block(pdf, reads[r["doc"]], _Plain())
                continue
            _sub(pdf, (f"{t['name']}: " if many else "") + r["report"])
            pdf.set_x(20)
            pdf.label_value("From", " · ".join(x for x in (r["from"], f"FIR {r['fir']}" if r["fir"] else "",
                                                          f"received {r['received']}" if r["received"] else "") if x) or None, 22)
            _points(pdf, [r["says"]], indent=22)
    if said is not None:
        if said:
            n = _statements_section(pdf, n, said, {}, _Plain(), title="Statements in the earlier FIRs")
        return n
    said = [(t, w) for t in targets for w in t.get("statements") or [] if w["fir"] not in (skip_firs or set())]
    if said:
        n += 1
        pdf.section(n, "What each person states in the FIRs")
        pdf.para("A statement shows what the person said, not that it is true. The record's own words, shortened.",
                 size=8.5, colour=GREY)
        _table(pdf, ["FIR", "Person", "Their part", "What they stated"],
               [[w["fir"], w["person"], w["part"] + (f"\n{w['date']}" if w["date"] else ""), w["said"]] for _, w in said],
               (32, 30, 22, 94))
    return n


class _Plain:
    """The text helpers of ``_Sources`` for sections that carry no record ids."""

    def text(self, value: Any, bare: bool = True) -> str:
        return _plainly(value)

    def label(self, ref: Any) -> str | None:
        return None

    def labels(self, refs: Any) -> str:
        return ""


def _record_sections(pdf: _Pdf, n: int, record: dict[str, Any], said: list[dict[str, Any]] | None = None,
                     reads: dict[str, Any] | None = None) -> int:
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
        _earlier_cases(pdf, t)
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
    return _more_record(pdf, n, record, said=said or None, reads=reads)


def _cover_part(p: dict[str, Any]) -> str:
    """The target's part for the cover, in a word or two: "Victim (deceased)", "Accused (arrested)"."""
    said = f"{p.get('part') or ''} {p.get('status') or ''}"
    status = str(p.get("status") or "")
    status = "" if status in ("", "not on record") or status.lower() in str(p.get("part") or "").lower() else status
    if re.search(r"\b(deceased|dead|killed|murdered)\b", said, re.IGNORECASE):
        return "Victim (deceased)"
    for word in ("victim", "accused", "suspect", "complainant", "witness"):
        if re.search(rf"\b{word}\b", said, re.IGNORECASE):
            return word.capitalize() + (f" ({status})" if status else "")
    return _short_part(str(p.get("part") or "")) + (f" ({status})" if status else "")


def _status_line(report: dict[str, Any]) -> str:
    """Whether the assessment covers the board this report was built from."""
    # Board versions and entry counts are for developers (the trace), not the officer's cover.
    return "" if report.get("model") else "built from the records, without the AI model"


DISCLAIMER = ("This report is generated by Sherlock AI Agent, Sindh Police. It is only an aid to the Investigating "
              "Officer and may contain errors. Anything written in this report - every fact, finding and piece of "
              "evidence - must be verified against the source records it names before any action is taken.")


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
    many = report.get("targets") or []
    pdf.multi_cell(pdf.w - 32, 7, pdf.t(f"{len(many)} targets searched together" if len(many) > 3
                                        else report.get("subject") or ""), new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(16, 92)
    pdf.badge("CONFIDENTIAL — FOR OFFICIAL USE ONLY", RED, x=16, y=92)
    pdf.set_xy(16, 99)
    pdf.font(8, "", (214, 222, 235))
    pdf.multi_cell(pdf.w - 32, 4, pdf.t(" · ".join(x for x in (f"Generated {report.get('generated_at')}", _status_line(report)) if x)),
                   new_x="LMARGIN", new_y="NEXT")

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
            ("Search", run.get("seed_label") or "-")]
    parts = report.get("parts") or {}
    if parts.get("case_story") is not None:
        # The brief's cover: the case, not the size of the search.
        glance = {r["label"]: r["value"] for r in (parts.get("glance") or {}).get("rows") or []}
        target = ((parts.get("target") or {}).get("targets") or [{}])[0]
        rows = [("FIR", re.sub(r"\s*\[[^\]]*\]", "", glance.get("FIR") or "not given yet")),
                ("Offence", glance.get("Offence") or "not described yet"),
                ("Target", " - ".join(x for x in (target.get("role_in_case"), target.get("status_in_case")) if x)
                 or next((_cover_part(p) for p in (parts.get("case_story") or {}).get("people") or []
                          if p["name"] == target.get("name") or _same(p["name"], target.get("name"))), "-")),
                ("Case position", glance.get("Case position") or "-"),
                ("Investigating officer", glance.get("Investigating officer") or "-")]
        group = parts.get("group") or {}
        if group.get("targets"):
            # Several targets: how many, and what the records join them into.
            joined = [f"{len(g['members'])} targets" for g in group.get("groups") or []]
            if group.get("alone"):
                joined.append(f"{len(group['alone'])} with no tie")
            rows[2] = ("Targets", f"{len(group['targets'])} searched together")
            rows.insert(3, ("Groups the records tie together", "; ".join(joined) or "none"))
        record = ((parts.get("record") or {}).get("targets") or [{}])[0]
        if not glance and record:
            # Before the case is described the cover gives the target's record.
            cases = record.get("past_cases") or []
            rows = [("Case", "not described yet"), ("Target", target.get("name") or record.get("name") or "-"),
                    ("Earlier FIRs", len(cases)),
                    ("Convicted / acquitted", (f"{sum(c['outcome'] == 'convicted' for c in cases)} / "
                                               f"{sum(c['outcome'] == 'acquitted' for c in cases)}")),
                    ("People connected", len(record.get("relations") or [])), ("Hotel stays", len(record.get("hotels") or []))]
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
        text = pdf.t(str(value if value is not None else "-"))
        while len(text) > 4 and pdf.get_string_width(text) > 80:
            text = text[:-2].rstrip() + "…" if not text.endswith("…") else text[:-3].rstrip() + "…"
        pdf.cell(80, 5, text)
    # The disclaimer: the report is an aid to the IO, never evidence by itself. The cover's foot sits below the
    # page-break margin, so no break is taken while it is drawn.
    pdf.set_auto_page_break(False)
    pdf.set_draw_color(*GOLD)
    pdf.set_fill_color(255, 249, 230)
    pdf.set_line_width(0.5)
    pdf.rect(16, 249, pdf.w - 32, 22, style="DF")
    pdf.set_line_width(0.2)
    pdf.set_xy(20, 251)
    pdf.font(8.5, "B", NAVY)
    pdf.cell(0, 4.5, "DISCLAIMER", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(20)
    pdf.font(8, "", INK)
    pdf.multi_cell(pdf.w - 40, 4, pdf.t(DISCLAIMER), new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(16, 273)
    pdf.font(7.5, "I", GREY)
    pdf.multi_cell(pdf.w - 32, 4, pdf.t(
        "Compiled by Sherlocks from Sindh Police and government records and the case documents read during the "
        "search. Every point names the record it rests on. A connection between two people is how the records "
        "link them - check it before acting on it." if parts.get("case_story") is not None else
        "Compiled by Sherlocks from Sindh Police and government records and the case documents read during the "
        "search. Every assessment cites the record, document or quoted fact it rests on. Links found by "
        "comparing records are leads for investigation and must be verified before action."), new_x="LMARGIN", new_y="NEXT")
    pdf.set_auto_page_break(True, margin=18)


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
