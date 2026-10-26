"""P11 Case papers: the papers a case of this kind needs - which are on the board, which are not.

An investigating officer taking over a case first checks the file: is the post-mortem on it,
the inquest, the site plan, the recovery memos, the statements of the witnesses? A report
that reads only what is on the board cannot see what is missing from it. So, by code, from
the kind of case (its offence: murder, fraud, a dishonoured cheque, theft, narcotics, a sexual
offence, kidnapping, extortion - and what its records show happened: a death, a firearm), each
paper the case needs is looked for:

* **on the board** - a document of the case IS that paper (its title, its record type, or
  what the Document reader found it to be);
* **mentioned, not on the board** - another document speaks of it (an inquiry report
  describes the post-mortem): get a copy;
* **not on the board**.

It never judges what a paper says; it only says whether the file holds it.
"""

from __future__ import annotations

import re
from typing import Any

# name, what the paper is (title / record type / reading), how other documents speak of it, kinds of case
PAPERS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("First information report (FIR)", r"^fir\b|first information", r"\bF\.?I\.?R\b", ("any",)),
    ("Statements of witnesses (161 Cr.P.C.)", r"\b161\b|statement", r"\b161\b", ("any",)),
    ("Case diaries", r"case diar", r"case diar|ضمنی", ("any",)),
    ("Site plan (naqsha-e-mauqa)", r"site plan|naqsha|sketch of", r"site plan|naqsha|sketch", ("any",)),
    ("Recovery and seizure memos (mashirnama)", r"recovery memo|seizure memo|mashirnama|recovery_memo",
     r"seizure memo|recovery memo|mashirnama|مشیرنامہ", ("any",)),
    ("Call records (CDR) and IPDR of the phones", r"\bcdr\b|\bipdr\b|call data|tower dump|geofenc", r"\bCDR\b|\bIPDR\b|call data", ("any",)),
    ("CCTV footage, with seizure memos of the recorders (DVR)", r"cctv|dvr", r"\bCCTV\b|\bDVR\b", ("any",)),
    ("Statement before a magistrate (164 Cr.P.C.)", r"\b164\b", r"\b164\b", ("sexual", "kidnap", "death", "terrorism")),
    ("Post-mortem report", r"post[- ]?mortem|autopsy|postmortem", r"post[- ]?mortem|autopsy", ("death",)),
    ("Medical examination / medico-legal (MLO) report", r"\bmlo\b|medico[- ]?legal|medical examination",
     r"\bMLO\b|medico[- ]?legal|medical examination", ("death", "injury", "sexual")),
    ("Inquest report (174 Cr.P.C.)", r"inquest|\b174\b", r"inquest|\b174\b", ("death",)),
    ("Inspection memo of the dead body", r"inspection memo", r"inspection memo", ("death",)),
    ("Chemical examiner report", r"chemical", r"chemical (?:examin|report|analysis)|viscera", ("death", "narcotics")),
    ("DNA report", r"\bdna\b", r"\bDNA\b", ("death", "sexual")),
    ("Exhumation proceedings and board report", r"exhum", r"exhum", ("exhumed",)),
    ("Forensic (FSL) report on the weapon and the casings", r"ballistic|fsl|forensic science|firearm",
     r"ballistic|FSL|test empties|forensic report", ("firearm", "arms")),
    ("Arms licence check (the people of the case)", r"arms licen|weapon licen", r"licen[cs]ed? (?:weapon|pistol|gun)|arms licen",
     ("firearm", "arms")),
    ("Weight and sealing memo of the seized substance", r"weigh|sealed sample|sample sealed", r"\bweigh|\bsealed\b",
     ("narcotics",)),
    ("Identification parade", r"identification parade|شناخت پریڈ", r"identification parade", ("property", "sexual", "kidnap")),
    ("Ownership papers of the property (receipts, registration)", r"ownership|registration book|purchase receipt|invoice",
     r"registration book|ownership|purchase receipt|invoice", ("property",)),
    ("Vehicle registration and excise record", r"excise|registration book|vehicle registration",
     r"excise|registration (?:book|number)", ("vehicle",)),
    ("The cheque and the bank's return memo", r"cheque|return memo|dishono", r"\bcheque\b|return memo", ("cheque",)),
    ("Bank statements and account records", r"bank statement|account statement|account opening|transaction record",
     r"bank statement|account statement|transactions?\b", ("fraud",)),
    ("Agreements, receipts and letters between the parties", r"agreement|contract|deed|receipt", r"agreement|contract|deed",
     ("fraud",)),
    ("Handwriting / signature expert report", r"handwriting|signature|questioned document",
     r"handwriting|signature (?:expert|comparison)|questioned document", ("forgery",)),
    ("Recordings or call records of the threats and demands", r"recording|audio|voice note", r"recording|audio|voice note",
     ("threat", "kidnap")),
    ("Phone forensic (extraction) report", r"(?:phone|mobile)\s+(?:data\s+)?extraction|mobile forensic|phone forensic",
     r"forensic (?:examination|report) of (?:the )?(?:mobile|phone)|(?:phone|mobile)\s+(?:data\s+)?extraction",
     ("death", "kidnap", "fraud", "threat", "terrorism")),
    ("Joint investigation team (JIT) / CTD report", r"\bjit\b|joint investigation|\bctd\b", r"\bJIT\b|joint investigation|\bCTD\b",
     ("terrorism",)),
)

# What the records themselves show happened - in any case: a death, a firearm, an exhumation.
_DEATH = re.compile(r"dead body|deceased|post[- ]?mortem|\b302\b|corpse|killed|death|لاش|قتل", re.IGNORECASE)
_FIREARM = re.compile(r"gunshot|\bshot\b|shooting|firearm|pistol|bullet|casing|\b(?:13|23)-?e\b|فائر", re.IGNORECASE)
_EXHUMED = re.compile(r"exhum", re.IGNORECASE)
# The kind of case from its offence (the FIR's sections, read by ``offences.classify``).
_FROM_OFFENCE = {"murder": "death", "attempted_murder": "injury", "hurt": "injury", "rape": "sexual",
                 "attempted_rape": "sexual", "sexual_assault": "sexual", "kidnapping": "kidnap", "kidnap_ransom": "kidnap",
                 "dacoity": "property", "robbery": "property", "theft": "property", "stolen_property": "property",
                 "vehicle_theft": "vehicle", "extortion": "threat", "threat": "threat", "terrorism": "terrorism",
                 "arms": "arms", "narcotics": "narcotics", "gutka": "narcotics", "cheque": "cheque", "fraud": "fraud",
                 "breach_of_trust": "fraud", "forgery": "forgery"}
_ALSO = {"vehicle": ("property",), "cheque": ("fraud",), "forgery": ("fraud",)}


def kinds(case: Any, scope: dict[str, Any], parts: dict[str, Any]) -> set[str]:
    """What kind of case this is: its offence (from the FIR's sections and the officer's words), and what its records
    show happened (a death, a firearm, an exhumation)."""
    from sherlocks.linkgraph.offences import classify

    offence = " ".join([str((case.incident or {}).get("offence") or "")] + [
        str(((case.documents.get(d) or {}).get("data") or {}).get("sections") or "") for d in scope["documents"]])
    out = {"any"}
    for key in classify(offence):
        kind = _FROM_OFFENCE.get(key)
        if kind:
            out |= {kind, *_ALSO.get(kind, ())}
    texts = [offence]
    for d in scope["documents"]:
        doc = case.documents.get(d) or {}
        texts += [doc.get("title") or "", doc.get("summary") or "", doc.get("ai_summary") or "",
                  ((doc.get("reading") or {}).get("summary") or "")]
        if doc.get("kind") != "upload":
            texts.append((doc.get("text") or "")[:20000])
    story = parts.get("case_story") or {}
    texts += [e.get("what") or "" for e in story.get("events") or []]
    texts += [f"{p.get('part')} {p.get('status')}" for p in story.get("people") or []]
    text = " ".join(texts)
    for name, pattern in (("death", _DEATH), ("firearm", _FIREARM), ("exhumed", _EXHUMED)):
        if pattern.search(text):
            out.add(name)
    if "firearm" in out and "death" not in out:
        out.add("injury")
    return out


def check(case: Any, net: Any, scope: dict[str, Any], parts: dict[str, Any]) -> dict[str, Any]:
    """``{kinds, papers: [{paper, status: on the board | mentioned | not on the board, where}]}``."""
    have = kinds(case, scope, parts)
    cards = (case.register or {}).get("cards") or {}
    docs = [case.documents[d] for d in scope["documents"] if d in case.documents]
    # What each document IS: its title, its record type, what the Document reader found it to be.
    what = {d["id"]: " ".join(str(x or "") for x in (d.get("title"), d.get("kind"), (cards.get(d["id"]) or {}).get("record"),
                                                      (d.get("reading") or {}).get("kind")))
            for d in docs}
    statements = [w for w in case.statements.values() if w.get("doc") in set(scope["documents"])]
    rows = []
    for paper, is_it, mention, needed in PAPERS:
        if not (have & set(needed)):
            continue
        found = [d for d in docs if re.search(is_it, what[d["id"]], re.IGNORECASE) and d.get("kind") != "notes"]
        if paper.startswith("Statements") and any(re.search(r"161|statement", str(w.get("kind") or ""), re.IGNORECASE)
                                                  and "first information" not in str(w.get("kind") or "").lower()
                                                  for w in statements):
            found = found or [case.documents[statements[0]["doc"]]]
        if paper.startswith("Case diaries"):
            found = [d for d in docs if d.get("kind") == "fir" and (d.get("data") or {}).get("case_diaries")]
        if found:
            rows.append({"paper": paper, "status": "on the board",
                         "where": "; ".join(str(d.get("title") or d["id"]).replace("Upload: ", "") for d in found[:3])})
            continue
        mentioned = [d for d in docs if d.get("kind") in ("upload", "notes", "lab") and
                     re.search(mention, d.get("text") or "", re.IGNORECASE)]
        if mentioned:
            rows.append({"paper": paper, "status": "mentioned, not on the board",
                         "where": "named in " + "; ".join(str(d.get("title") or d["id"]).replace("Upload: ", "")
                                                          for d in mentioned[:2]) + " - get a copy for the file"})
        else:
            rows.append({"paper": paper, "status": "not on the board", "where": ""})
    order = {"not on the board": 0, "mentioned, not on the board": 1, "on the board": 2}
    rows.sort(key=lambda r: order[r["status"]])
    return {"kinds": sorted(have - {"any"}), "papers": rows}
