"""O5 Presenter: the voice the officer reads.

The Officer agent works out *what* to say; the Answer checker makes sure it is right; the
Presenter decides *how* it reads - in the officer's language, laid out so the answer is
clear at a glance:

* **Answer first** - the first line answers the question, in bold.
* **Layout fits the content** - three or more FIRs / items side by side become a small
  table; lists stay bullets; a short answer stays a sentence.
* **Evidence shown, not just cited** - up to two key points carry a short quote from the
  record with its source (document title and page / rows). Quotes stay in the record's
  language, with a translation when the officer's language differs (when a model is
  connected).
* **Fact and opinion apart** - Sherlock's view sits in its own labelled block; the next
  question comes last, on its own line.
* **Nothing changes in transit** - a format guard compares every number, CNIC, phone,
  FIR number and source id of the checked answer with the laid-out reply; if anything
  was lost or changed, the checked answer goes out as it is.

The layout uses plain markers the portal renders (``**bold**``, ``| table |`` rows,
``> quote`` lines, ``VIEW:`` block) and reads fine as text elsewhere. No model is needed:
the layout is code, in all three languages.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

VIEW_LABEL = {"en": "Sherlock's view (not confirmed)", "roman": "Sherlock ka andaza (tasdeeq nahi)",
              "ur": "شرلاک کا اندازہ (تصدیق شدہ نہیں)"}
VIEW_MARK = "VIEW:"
# The Questioner's question, last, in its colour: "ASK:red: ..." (red / orange / green).
ASK_MARK = "ASK:"
_HEADERS = {
    "en": ("FIR", "Police station", "Role", "Crime", "Date", "Source"),
    "roman": ("FIR", "Thana", "Kirdar", "Jurm", "Tareekh", "Source"),
    "ur": ("ایف آئی آر", "تھانہ", "کردار", "جرم", "تاریخ", "ماخذ"),
}
_ROLE_WORDS = {"roman": {"accused": "mulzim", "complainant": "muddai", "witness": "gawah", "victim": "mutasir",
                         "named": "naamzad"},
               "ur": {"accused": "ملزم", "complainant": "مدعی", "witness": "گواہ", "victim": "متاثرہ", "named": "نامزد"}}
_TOKENS = re.compile(r"\b\d{5}-?\d{7}-?\d\b|(?:\+?92|0)3\d{2}[\s-]?\d{7}|\b\d{1,5}\s*/\s*\d{2,4}\b|\[[A-Z]{1,8}\d{0,4}\]|\b\d+\b")


class _Translation(BaseModel):
    text: str = Field(description="The translation.")


def tokens(text: str) -> list[str]:
    """What must survive layout untouched: numbers, CNICs, phones, FIR numbers, source ids."""
    return [re.sub(r"\s+", "", t) for t in _TOKENS.findall(text or "")]


def format_guard(checked: str, shown: str) -> list[str]:
    """Tokens of the checked answer missing from what is shown (empty = safe to send)."""
    have = set(tokens(shown))
    return [t for t in dict.fromkeys(tokens(checked)) if t not in have]


def _first_line(text: str) -> tuple[str, str]:
    text = text.strip()
    if not text:
        return "", ""
    first, _, rest = text.partition("\n")
    if len(first) > 220:
        m = re.search(r"(?<=[.!?؟۔])\s", first)
        if m and m.start() < 220:
            return first[:m.start()], (first[m.end():] + ("\n" + rest if rest else "")).strip()
    return first, rest.strip()


_CO = {"en": "Co-accused", "roman": "Saathi mulzim", "ur": "شریک ملزم"}


def _table(rows: list[dict[str, Any]], language: str) -> list[str]:
    co = any(r.get("co_accused") for r in rows)
    head = (*_HEADERS.get(language, _HEADERS["en"]), *((_CO.get(language, _CO["en"]),) if co else ()))
    words = _ROLE_WORDS.get(language, {})
    out = ["| " + " | ".join(head) + " |"]
    for r in rows[:12]:
        roles = ", ".join(words.get(x, x) for x in r.get("roles") or []) or "-"
        crimes = ", ".join(r.get("crimes") or []) or str(r.get("offence") or "-")
        src = " ".join(f"[{x}]" for x in ([r["doc"]] if r.get("doc") else []) + list(r.get("sources") or [])) or "-"
        out.append("| " + " | ".join(str(x).replace("|", "/") for x in
                                     (f"FIR {r.get('label') or '-'}", r.get("ps") or "-", roles, crimes[:60],
                                      r.get("occurred") or r.get("date") or "-", src,
                                      *((", ".join(r.get("co_accused") or []) or "-",) if co else ()))) + " |")
    return out


# Labels come from the answer itself: a point's own subject ("The charges are ..." ->
# "Charges"), or the label the model wrote ("• Weapon: ..."). No fixed list of labels.
_COPULA = r"is|are|was|were"
_VERBS = (_COPULA + r"|has|have|had|confirms?|confirmed|shows?|showed|found|finds|was filed|were filed|filed|"
          r"occurred|happened|took place|names?|named|lists?|says?|said|states?|stated|records?|recorded|covers?|"
          r"matched|matches|appears?|appeared|remains?|links?|linked")
_SUBJECT = re.compile(rf"^(?:the |his |her |their |this |that |its )?((?:[\w'’()/.-]+ ){{0,3}}?[\w'’()/.-]+?)"
                      rf"\s+(?:\w+ly\s+)?({_VERBS})\b\s*", re.IGNORECASE)
_NOT_A_LABEL = re.compile(r"^(he|she|it|they|there|this|that|which|who|we|i|you|one|both|all|none)$", re.IGNORECASE)
_LABELLED = re.compile(r"^([^:•|>]{2,32}):\s+(?=\S)")


def point_label(sentence: str) -> tuple[str | None, str]:
    """``(label, text)`` for one point, from its own words: "Victim: ..." as written, or
    its subject ("The victim is his daughter" -> ("Victim", "his daughter")); else no label."""
    sentence = sentence.strip()
    m = _LABELLED.match(sentence)
    if m and len(m.group(1).split()) <= 4:
        return m.group(1).strip(), sentence[m.end():]
    m = _SUBJECT.match(sentence)
    if not m:
        return None, sentence
    subject = m.group(1).strip()
    if _NOT_A_LABEL.match(subject) or len(subject.split()) > 4 or re.fullmatch(r"[\d/.-]+", subject):
        return None, sentence
    label = subject[0].upper() + subject[1:]
    if m.group(2).lower() in ("is", "are", "was", "were"):
        rest = sentence[m.end():]          # "The charges are 302, 114" -> "Charges: 302, 114"
        return (label, rest[0].upper() + rest[1:]) if rest else (None, sentence)
    return label, sentence


_FILLER = re.compile(r"^(sure|okay|ok|certainly|of course|absolutely|let'?s|let me|here is|here's|here are|"
                     r"zaroor|ji haan|theek hai|chaliye|جی|ضرور)\b[^.!?؟۔]*[.!?:؟۔]\s*", re.I)
_SENTENCE = re.compile(r"(?<=[.!?؟۔])\s+(?=[A-Z\"“(\[؀-ۿ])")


def pointify(text: str, language: str) -> str:
    """A long paragraph of facts (a model's reply) as one direct line and labelled points:
    one fact per line, the same words - so it reads at a glance."""
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text.strip()):
        lines = para.strip().splitlines()
        if any(ln.lstrip().startswith(("•", "- ")) for ln in lines):
            # The model's own points: bold the label it wrote ("• Weapon: ...").
            fixed = []
            for ln in lines:
                m = re.match(r"^(\s*[•-]\s+)(?!\*\*)([^:•|>\[]{2,32}):\s+(?=\S)", ln)
                fixed.append(f"{m.group(1)}**{m.group(2).strip()}:** {ln[m.end():]}"
                             if m and len(m.group(2).split()) <= 4 else ln)
            out.append("\n".join(fixed))
            continue
        if len(lines) > 1 or any(ln.lstrip().startswith(("*", "|", ">")) for ln in lines) or len(para) < 260:
            out.append(para.strip())
            continue
        body = _FILLER.sub("", para.strip(), count=1)
        sentences = [x.strip() for x in _SENTENCE.split(body) if x.strip()]
        if len(sentences) < 3:
            out.append(body)
            continue
        lead, points = sentences[0], sentences[1:]
        block = [lead]
        for sentence in points:
            label, text = point_label(sentence)
            block.append(f"• **{label}:** {text}" if label else f"• {sentence}")
        out.append("\n".join(block))
    return "\n\n".join(out)


def bracket_ids(text: str, case: Any) -> str:
    """Ids written bare ("... D1", "F16") become sources the portal can open ("[D1]")."""
    if case is None:
        return text
    known = case.known_ids() if hasattr(case, "known_ids") else set()
    return re.sub(r"(?<![\[\w])([DFLH]\d{1,4})(?![\w\]])",
                  lambda m: f"[{m.group(1)}]" if m.group(1) in known else m.group(1), text)


EVIDENCE_LABEL = {"en": "Evidence", "roman": "Saboot", "ur": "ثبوت"}


def _translate(llm: Any, quote: str, language: str) -> str | None:
    target = {"en": "English", "roman": "Roman Urdu", "ur": "Urdu (Urdu script)"}[language]
    try:
        tr, _ = llm.generate_structured(prompt=quote, schema=_Translation,
                                        system=f"Translate this line from a police record into {target}, faithfully, "
                                               "nothing added.", cache_kind="quote_translation", prompt_version="v1",
                                        max_tokens=200)
        return tr.text.strip()[:220] or None
    except Exception as exc:  # noqa: BLE001 - the quote in its own language still stands
        logger.info("Quote translation failed: %s", exc)
        return None


def _evidence(case: Any, text: str, language: str, llm: Any = None, limit: int = 3) -> list[str]:
    """The evidence behind the answer, shown in it: for each record the reply cites (in
    order, up to ``limit``), the document and a short quote from it - the words of the
    record where there are some, else what the document establishes. Our own analysis
    (graph patterns, CDR lines) is cited, not quoted."""
    if case is None:
        return []
    out: list[str] = []
    seen_docs: set[str] = set()
    shown: set[str] = set()
    cited = [ref for group in re.findall(r"\[([^\]]+)\]", text) for ref in re.findall(r"\b([DF]\d{1,4})\b", group)]
    for ref in dict.fromkeys(cited):
        if ref.startswith("F"):
            fact = case.facts.get(ref)
            if not fact or fact.get("by") in ("officer", "graph"):
                continue
            doc = case.documents.get(fact["doc"]) or {}
            if doc.get("kind") in ("graph", "notes"):
                continue
            where = doc.get("title", fact["doc"]) + (f", page {fact['page']}" if fact.get("page") else "") + (
                f", rows {fact['rows']}" if fact.get("rows") else "")
            quote = (fact.get("quote") or fact["statement"])[:180]
            seen_docs.add(fact["doc"])
        else:
            doc = case.documents.get(ref) or {}
            if not doc or doc.get("kind") in ("graph", "notes") or ref in seen_docs:
                continue
            where = doc.get("title", ref)
            # The document's quote that backs the sentence citing it - not just its first fact.
            line = next((ln for ln in text.splitlines() if re.search(rf"\b{ref}\b", ln)), "")
            said = {w for w in re.findall(r"[\w؀-ۿ]{3,}", line.lower())}
            facts = [case.facts[f] for f in doc.get("facts") or [] if f in case.facts and case.facts[f].get("quote")]
            scored = sorted(((len(said & {w for w in re.findall(r"[\w؀-ۿ]{3,}", (f["quote"] + " " + f["statement"]).lower())}), f)
                             for f in facts), key=lambda x: -x[0])
            if not scored or scored[0][0] < 2:
                continue                # no line of the document backs it better than the citation itself
            quote = scored[0][1]["quote"][:180]
            seen_docs.add(ref)
        if quote in shown:
            continue                    # the same words, already shown
        shown.add(quote)
        out.append(f"> \"{quote}\" - {where} [{ref}]")
        letters = [c for c in quote if c.isalpha()]
        urdu_quote = bool(letters) and sum(1 for c in letters if "؀" <= c <= "ۿ") > 0.5 * len(letters)
        if llm is not None and urdu_quote != (language == "ur"):
            translated = _translate(llm, quote, language)
            if translated:
                out.append(f"> {translated}")
        if sum(1 for x in out if x.startswith('> "')) >= limit:
            break
    sources = sources_of(text)
    if not out and not sources:
        return []
    return [f"**{EVIDENCE_LABEL.get(language, EVIDENCE_LABEL['en'])}:**", *out,
            *([SOURCES_MARK + " " + " ".join(f"[{x}]" for x in sources)] if sources else [])]


SOURCES_MARK = "SOURCES:"


def sources_of(text: str) -> list[str]:
    """Every source the answer cites, once each, in order: case ids (D2, F7) and the
    systems or records named in brackets ([PSRMS], [CRO], [FIR Roster])."""
    out: list[str] = []
    for group in re.findall(r"\[([^\]]{1,80})\]", text or ""):
        for part in re.split(r",\s*", group):
            part = part.strip()
            ref = re.search(r"\b([DFLHQ]\d{1,4})\b", part)
            item = ref.group(1) if ref else part
            if item and len(item) <= 40 and item not in out and not item.isdigit():
                out.append(item)
    return out[:12]


def highlight(text: str, terms: list[str]) -> str:
    """Bold what the question asked about - the people, the FIR numbers, the fact asked -
    wherever the answer mentions it (not inside sources, not where already bold)."""
    terms = sorted({t.strip() for t in terms if t and len(t.strip()) >= 3}, key=len, reverse=True)
    if not terms:
        return text
    rx = re.compile(r"(?<![\w*\[])(" + "|".join(re.escape(t) for t in terms) + r")(?![\w*\]])", re.IGNORECASE)
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith(("|", ">")):
            out.append(line)
            continue
        parts = re.split(r"(\*\*.+?\*\*|\[[^\]]+\])", line)      # leave bold spans and sources alone
        out.append("".join(p if p.startswith(("**", "[")) else rx.sub(r"**\1**", p) for p in parts))
    return "\n".join(out)


def asked_terms(ask: dict[str, Any] | None, net: Any = None) -> list[str]:
    """What to highlight for an ask: the people asked about, the FIRs named, the fact asked."""
    if not ask:
        return []
    terms = [net.name(p) for p in ask.get("people") or []] if net is not None else []
    terms += list(ask.get("firs") or [])
    if ask.get("asks_for"):
        terms.append(ask["asks_for"])
    return terms


def present(checked: str, *, language: str, facts: dict[str, Any] | None = None, question: str | None = None,
            view: str | None = None, case: Any = None, llm: Any = None, answer_first: bool = True,
            highlight_terms: list[str] | None = None, question_priority: str = "orange") -> str:
    """The checked answer laid out for the officer. Falls back to ``checked`` itself
    (with the question last) if the layout would change any number, name or source."""
    body = bracket_ids(checked.strip(), case)
    if answer_first:
        body = pointify(body, language)
    q = (question or "").strip()
    if q and q in body:
        body = body.replace(q, "").strip()          # the question goes last, on its own line
    ask = f"{ASK_MARK}{question_priority if question_priority in ('red', 'orange', 'green') else 'orange'}: {q}" if q else ""
    if not body:
        return ask
    first, rest = _first_line(body)
    lines: list[str] = []
    plain = not answer_first or first.startswith(("**", "|", ">", "•"))
    lines.append(first if plain else f"**{first}**")
    rows = (facts or {}).get("rows") or []
    is_fir_rows = isinstance(rows, list) and len(rows) >= 3 and all(isinstance(r, dict) and r.get("label") for r in rows)
    if is_fir_rows and facts.get("type") in ("cases", "count_cases", "serious_cases"):
        # Three or more FIRs side by side: a table instead of the bullets - only when the
        # table keeps every number, FIR and source the bullets had.
        bullets = [ln for ln in rest.splitlines() if ln.strip().startswith(("•", "-"))]
        table = _table(rows, language)
        if bullets and len(rows) <= 12 and not format_guard("\n".join(bullets), "\n".join(table)):
            rest = "\n".join(ln for ln in rest.splitlines() if ln not in bullets).strip()
            lines += table
    if rest:
        lines.append(highlight(rest, highlight_terms or []) if answer_first else rest)
    if answer_first:
        lines += _evidence(case, body, language, llm)
    if view:
        lines.append(f"{VIEW_MARK} {VIEW_LABEL.get(language, VIEW_LABEL['en'])}: {view.strip()}")
    if ask:
        lines.append(ask)
    shown = "\n".join(lines)
    lost = format_guard(bracket_ids(checked, case) + ("\n" + view if view else ""), shown)
    if lost:
        logger.info("Presenter format guard: %s changed in layout - sending the checked answer as it is", lost[:5])
        return "\n\n".join(x for x in (body, f"{VIEW_LABEL.get(language, VIEW_LABEL['en'])}: {view}" if view else "", ask)
                             if x)
    return shown
