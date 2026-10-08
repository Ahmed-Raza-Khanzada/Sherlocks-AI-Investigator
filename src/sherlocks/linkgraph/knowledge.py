"""The knowledge base: everything Sherlocks knows about the case, as searchable entries.

Built from the live graph and the case file each turn (cheap; cached per version), so it
always matches what has been found so far:

* **people** - every identifier, every record field from every system, FIRs with role,
  offence and status, hotel stays, vehicles, organisations, links (stated and inferred),
  the role the officer gave;
* **FIRs** - from the FIR files: complainant, accused, witnesses, officers, sections,
  place, time, case positions, case diaries, investigation result, stolen property;
* **documents** - lab / medical reports, CRO dossiers, uploads, CDR analyses: summary and
  every quoted fact;
* **the officer** - statements, answers, the incident, roles.

Every entry carries its source (system or document id), so an answer built from entries
can cite them. Search is lexical (BM25-style) over English, Roman Urdu and Urdu, with a
concept vocabulary ("gaari" = vehicle, "gawah" = witness, "thana" = police station...) and
names matched by sound across scripts.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sherlocks.linkgraph.normalize import sound_key
from sherlocks.linkgraph.systems import system_label

# Concept -> the words that mean it (English, Roman Urdu, Urdu).
CONCEPTS: dict[str, tuple[str, ...]] = {
    "phone": ("phone", "phones", "number", "numbers", "mobile", "sim", "sims", "cell", "fon", "numbr", "nmbr", "فون",
              "نمبر", "موبائل"),
    "vehicle": ("vehicle", "vehicles", "car", "cars", "gaari", "gari", "gadi", "gaadi", "bike", "motorcycle", "plate",
                "registration", "challan", "driver", "driving", "licence", "license", "گاڑی", "موٹر"),
    "address": ("address", "addresses", "ghar", "pata", "rehaish", "rehta", "makan", "lives", "residence", "سکونت",
                "پتہ", "رہائش", "مکان"),
    "father": ("father", "walid", "baap", "abba", "valad", "s/o", "ولد", "والد"),
    "fir": ("fir", "firs", "case", "cases", "muqadma", "muqadmay", "mukadma", "parcha", "crime", "offence", "offense",
            "مقدمہ", "مقدمات", "پرچہ"),
    "complainant": ("complainant", "muddai", "mudai", "applicant", "filed", "darj", "مدعی", "مستغیث", "درخواست"),
    "accused": ("accused", "mulzim", "mujrim", "suspect", "nominated", "criminal", "ملزم", "مجرم", "نامزد"),
    "witness": ("witness", "witnesses", "gawah", "gawahan", "گواہ", "گواہان"),
    "police": ("thana", "thane", "station", "ps", "police", "io", "investigating", "investigator", "investigators",
               "investigation", "officer", "tafteeshi", "afsar", "تھانہ", "تفتیشی", "افسر"),
    "status": ("status", "faisla", "outcome", "convicted", "acquitted", "bari", "saza", "trial", "challan", "position",
               "bail", "zamanat", "result", "pending", "پوزیشن", "چالان", "فیصلہ", "سزا", "بری", "ضمانت", "نتیجہ"),
    "hotel": ("hotel", "hotels", "stay", "stays", "stayed", "thehra", "room", "check", "ہوٹل"),
    "date": ("when", "kab", "date", "tareekh", "time", "waqt", "تاریخ", "وقت"),
    "place": ("where", "kahan", "place", "jagah", "location", "area", "جگہ", "مقام"),
    "cnic": ("cnic", "shanakhti", "id", "nic", "شناختی"),
    "lab": ("lab", "dna", "chemical", "medical", "medico", "forensic", "fsl", "mlo", "report", "reports", "لیب"),
    "cro": ("cro", "dossier", "record", "fingerprint", "photo", "photos"),
    "work": ("job", "kaam", "naukri", "employer", "employment", "company", "work", "office", "ملازمت", "نوکری"),
    "family": ("family", "bhai", "brother", "behan", "sister", "beta", "son", "wife", "biwi", "husband", "relative",
               "rishtedar", "رشتہ", "بھائی", "بیوی"),
    "property": ("tenant", "landlord", "kirayedar", "malik", "property", "tenancy", "rent", "کرایہ"),
    "link": ("link", "links", "linked", "connection", "connections", "taluq", "rabta", "relation", "associate",
             "saathi", "تعلق", "رابطہ", "ساتھی"),
    "diary": ("diary", "diaries", "zimni", "ضمنی", "statement", "bayan", "بیان"),
    "weapon": ("weapon", "pistol", "gun", "hathiyar", "arms", "licence", "اسلحہ", "پستول"),
}
_CONCEPT_OF = {w: c for c, words in CONCEPTS.items() for w in words}
# Record fields and link relations that are about a concept although they don't say its word.
_FIELD_CONCEPTS = [
    (re.compile(r"rank|belt|posting|designation|occupation|profession|employ|department|organi[sz]ation|company|"
                r"business|job|salary|pesha|پیشہ", re.IGNORECASE), "work"),
    (re.compile(r"address|residence|pata\b|sakoonat|پتہ|سکونت|رہائش", re.I), "address"),
    (re.compile(r"phone|mobile|cell|sims?\b|msisdn|contact", re.I), "phone"),
    (re.compile(r"vehicle|registration|reg\.? ?no|chassis|engine|plate|licence|license", re.I), "vehicle"),
    (re.compile(r"father|walid|mother|husband|wife|spouse|son\b|daughter|brother|sister|family|household|cast\b|caste|"
                r"relative|guardian|ولد|والد|بھائی|بیوی|خاندان", re.IGNORECASE), "family"),
]
_FAMILY_LINK = re.compile(r"same address|household|family|father|son of|brother|sister|wife|husband|spouse|relative|"
                          r"guardian", re.IGNORECASE)


def field_concepts(text: str) -> list[str]:
    return [c for pat, c in _FIELD_CONCEPTS if pat.search(text or "")]
_TOKEN = re.compile(r"[a-z]+|[؀-ۿ]+|\d+", re.IGNORECASE)
_STOP = {"the", "a", "an", "of", "in", "on", "is", "are", "was", "were", "to", "and", "or", "for", "with", "by", "at",
         "ka", "ki", "ke", "ko", "ne", "se", "me", "mein", "main", "ma", "hai", "ha", "hain", "tha", "thi", "kya", "kia",
         "batao", "btao", "bataein", "mujhe", "mjhe", "us", "uske", "uski", "iske", "ye", "yeh", "wo", "woh",
         "what", "which", "who", "how", "tell", "about", "please", "show", "give", "list", "all", "his", "her",
         "کا", "کی", "کے", "کو", "میں", "ہے", "ہیں", "کیا", "سے", "اور", "یہ", "وہ"}


def tokens(text: str) -> list[str]:
    out = []
    for t in _TOKEN.findall(str(text or "").lower()):
        if t in _STOP or (len(t) < 2 and not t.isdigit()):
            continue
        out.append(t)
    return out


def _expand(toks: list[str]) -> list[str]:
    """Words, their concepts ("c:vehicle"), and the sound of name-like words ("s:LTF")."""
    out = list(toks)
    for t in toks:
        if t in _CONCEPT_OF:
            out.append(f"c:{_CONCEPT_OF[t]}")
        if not t.isdigit() and len(t) >= 3:
            key = sound_key(t)
            if len(key) >= 2:
                out.append(f"s:{key}")
    return out


@dataclass
class Entry:
    id: str
    kind: str                 # person | fir | document | fact | link | officer | incident | finding
    title: str                # who / what it is about
    text: str                 # the knowledge, one line
    source: str = ""          # system or document id, for citing
    people: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    terms: Counter = field(default_factory=Counter)
    length: int = 1

    def line(self) -> str:
        return f"{self.title}: {self.text}" + (f" [{self.source}]" if self.source else "")

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "title": self.title, "text": self.text, "source": self.source,
                "people": self.people}


class KnowledgeBase:
    def __init__(self) -> None:
        self.entries: list[Entry] = []
        self.df: Counter = Counter()

    # -- building ---------------------------------------------------------------------------

    def add(self, kind: str, title: str, text: str, source: str = "", people: list[str] | None = None,
            concepts: list[str] | None = None) -> None:
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if not text:
            return
        e = Entry(id=f"K{len(self.entries) + 1}", kind=kind, title=title, text=text[:600], source=source,
                  people=people or [], concepts=concepts or [])
        e.terms = Counter(_expand(tokens(f"{title} {text}")) + [f"c:{c}" for c in e.concepts])
        e.length = max(1, sum(e.terms.values()))
        self.entries.append(e)
        self.df.update(set(e.terms))

    @classmethod
    def build(cls, graph: dict[str, Any], case: Any = None, net: Any = None) -> KnowledgeBase:
        from sherlocks.linkgraph.network import PersonNetwork

        kb = cls()
        net = net or PersonNetwork(graph)
        nodes = {n["id"]: n for n in graph.get("nodes") or []}
        roles = case.roles if case is not None else {}
        # People: identity, FIRs, stays, vehicles, links.
        for pid, node in net.people.items():
            d = node.get("data") or {}
            name = node.get("label") or pid
            if d.get("cnic"):
                kb.add("person", name, f"CNIC {d['cnic']}", people=[pid], concepts=["cnic"])
            if d.get("father_name"):
                kb.add("person", name, f"father: {d['father_name']}", people=[pid], concepts=["father", "family"])
            if d.get("seed"):
                kb.add("person", name, "is a target of this search", people=[pid])
            if roles.get(pid):
                kb.add("officer", name, f"role given by the officer: {roles[pid]}", "officer", [pid])
            if d.get("phones"):
                kb.add("person", name, "phone numbers: " + ", ".join(d["phones"]), people=[pid], concepts=["phone"])
            for a in (d.get("addresses") or [])[:4]:
                kb.add("person", name, f"address: {a}", people=[pid], concepts=["address"])
            flags = [f.replace("_", " ") for f in d.get("flags") or []]
            if flags:
                kb.add("person", name, "flags: " + ", ".join(flags), people=[pid], concepts=["accused"])
            for f in d.get("firs") or []:
                kb.add("person", name, f"FIR {f.get('label')} at {f.get('ps') or '-'}: {f.get('role') or 'named'}"
                       + (f", offence {f['offence']}" if f.get("offence") else "")
                       + (f", status {f['status']}" if f.get("status") else ""),
                       system_label(f.get("system") or ""), [pid], ["fir", "status"])
            for s in d.get("stays") or []:
                kb.add("person", name, f"hotel stay at {s.get('hotel')} ({s.get('district') or '-'}), room "
                       f"{s.get('room') or '-'}, {s.get('check_in') or '-'} to {s.get('check_out') or '-'}",
                       "Hotel Eye", [pid], ["hotel", "date"])
            for v in d.get("vehicles") or []:
                kb.add("person", name, f"vehicle {v}", people=[pid], concepts=["vehicle"])
            for o in d.get("organisations") or []:
                kb.add("person", name, f"organisation / employer: {o}", people=[pid], concepts=["work"])
            if pid in net.G:
                for other in net.neighbours(pid):
                    rel = other["relation"]
                    concepts = ["link"] + (["family"] if _FAMILY_LINK.search(rel) else [])
                    kb.add("link", name, rel + ("" if other["stated"] else " (inferred)"),
                           other["via"], [pid, other["id"]], concepts)
        # Every field of every record (NADRA address, CRO category, PRVS case, ARMS licence...).
        for node in nodes.values():
            d = node.get("data") or {}
            if node.get("kind") != "system":
                continue
            owner = d.get("owner")
            who = net.name(owner) if owner in net.people else ""
            src = system_label(d.get("system") or "")
            for f in (d.get("fields") or [])[:40]:
                kb.add("record", who or node.get("label", ""), f"{src} - {f.get('label')}: {f.get('value')}", src,
                       [owner] if owner else [], field_concepts(str(f.get("label") or "")))
        if case is not None:
            kb._case(case, net)
        return kb

    def _case(self, case: Any, net: Any) -> None:
        ids = {}
        for pid, node in net.people.items():
            d = node.get("data") or {}
            if d.get("cnic"):
                ids[d["cnic"]] = pid
            for p in d.get("phones") or []:
                ids[p] = pid

        def people_in(text: str) -> list[str]:
            return list(dict.fromkeys(pid for k, pid in ids.items() if k in text))

        for doc in list(case.documents.values()):      # a snapshot: readers may still be adding
            data = doc.get("data") or {}
            title = doc["title"]
            owners = list(doc.get("owners") or [])

            def people_in(text: str, _owners: list[str] = owners) -> list[str]:  # noqa: F811 - per document
                return list(dict.fromkeys(_owners + [pid for k, pid in ids.items() if k in text]))

            if doc["kind"] == "notes":
                for line in doc["text"].splitlines()[-30:]:
                    self.add("officer", "Officer said", line.split("] ", 1)[-1], "officer")
                continue
            self.add("document", title, doc.get("ai_summary") or doc.get("summary") or "", doc["id"], people_in(doc["text"]))
            if doc["kind"] == "fir":
                c = data.get("complainant") or {}
                if c.get("name"):
                    self.add("fir", title, f"complainant: {c['name']}" + (f" s/o {c['father']}" if c.get("father") else "")
                             + (f", CNIC {c['cnic']}" if c.get("cnic") else "") + (f", phone {c['phone']}" if c.get("phone") else "")
                             + (f", address {c['address']}" if c.get("address") else ""), doc["id"], people_in(str(c)),
                             ["complainant"])
                for key, label, concept in (("nominated_suspects", "accused", "accused"), ("witnesses", "witness", "witness"),
                                            ("investigating_officers", "investigating officer", "police"),
                                            ("stolen_property", "property", "vehicle")):
                    for p in data.get(key) or []:
                        self.add("fir", title, f"{label}: " + ", ".join(f"{k} {v}" for k, v in p.items() if v),
                                 doc["id"], people_in(str(p)), [concept])
                for label, key, concept in (("sections", "sections", "fir"), ("offence", "offence", "fir"),
                                            ("occurred", "occurred", "date"), ("reported", "reported", "date"),
                                            ("place of occurrence", "place", "place"),
                                            ("investigation result", "investigation_result", "status")):
                    if data.get(key):
                        self.add("fir", title, f"{label}: {data[key]}", doc["id"], concepts=[concept])
                for pos in data.get("case_positions") or []:
                    self.add("fir", title, f"case position: {pos.get('position')} on {pos.get('date')}", doc["id"],
                             concepts=["status"])
                if data.get("narrative"):
                    self.add("fir", title, f"first information: {data['narrative']}", doc["id"], people_in(data["narrative"]))
                for dia in data.get("case_diaries") or []:
                    self.add("fir", title, f"case diary {dia.get('no')} ({dia.get('date')}, {dia.get('officer')}): "
                             f"{dia.get('remarks')}", doc["id"], people_in(str(dia.get("remarks"))), ["diary"])
            for fid in list(doc.get("facts") or []):
                fact = case.facts.get(fid)
                if fact and not fact.get("replaced_by") and not fact.get("stale"):   # corrected / out of date: left out
                    tier = f" [{fact['tier']}]" if fact.get("tier") and fact["tier"] != "fact" else ""
                    self.add("fact", title, f"{fact['statement']}{tier} (quote: {fact['quote'][:160]})", fid,
                             people_in(fact["statement"] + fact["quote"]))
        for link in list(case.links.values()):
            self.add("link", link["name"], f"found in {case.documents[link['doc']]['title']}: {link['how']}", link["id"],
                     [link["pid"]])
        for h in list(getattr(case, "hypotheses", {}).values()):
            if not h.get("stale"):
                self.add("finding", "Sherlock's hypothesis", f"{h['statement']} ({h['status']}, {h['confidence']}, "
                         f"{h['tier']})", h["id"], [p for p in h.get("people") or [] if p in net.people])
        inc = case.incident or {}
        if inc:
            self.add("incident", "Incident", ", ".join(f"{k} {v}" for k, v in inc.items() if k != "at" and v), "officer",
                     concepts=["place", "date"])

    # -- searching -----------------------------------------------------------------------------

    def search(self, query: str, *, people: list[str] | None = None, k: int = 25) -> list[Entry]:
        """The entries that best answer ``query``; entries about ``people`` (the person being
        discussed or named) come first when they match at all."""
        q = Counter(_expand(tokens(query)))
        if not self.entries:
            return []
        avg = sum(e.length for e in self.entries) / len(self.entries)
        # "complainant", "witness", "diary", "address"... decide; "FIR" and "link" are too general to.
        topics = {t for t in q if t.startswith("c:")} - {"c:fir", "c:link", "c:date"}
        asked = re.sub(r"\s+", " ", query.strip().lower())
        firs = [f"{m.group(1).lstrip('0')}/{m.group(2)[-2:]}" for m in re.finditer(r"\b(\d{1,5})\s*/\s*(\d{2,4})\b", query)]
        n = len(self.entries)
        focus = set(people or [])
        scored = []
        for e in self.entries:
            if e.kind == "officer" and asked and e.text.lower().strip(" ?") in asked:
                continue          # the officer's own question is not an answer to it
            if e.kind == "record" and re.search(r":\s*\d+\s*$", e.text):
                continue          # "Case diaries: 7" - a count, not knowledge
            score = 0.0
            norm = 0.25 + 0.75 * e.length / avg
            for t in q:
                tf = e.terms.get(t, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                weight = 2.0 if t.startswith("c:") else (2.5 if t.isdigit() and len(t) >= 4 else 1.0)
                score += idf * weight * (tf * 2.2) / (tf + 1.2 * norm)
            if topics and score:
                # The question is about an address / complainant / diary...: entries on that topic first.
                score *= 2.5 if topics & set(e.terms) else 0.3
            if firs:
                text = f"{e.title} {e.text}"
                labels = {f"{m.group(1).lstrip('0')}/{m.group(2)[-2:]}" for m in re.finditer(r"(\d{1,5})\s*/\s*(\d{2,4})", text)}
                score = score * 2.5 + 3 if labels & set(firs) else score * 0.3
            if focus and set(e.people) & focus:
                score = score * 1.3 + (0.5 if score else 0.0)
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda x: -x[0])
        if focus and not firs:
            # Asked about a person: only what concerns them (nothing about others "instead") -
            # and, when a topic is asked ("his job", "his relatives"), only entries on it.
            scored = [(sc, e) for sc, e in scored if set(e.people) & focus]
            strict = topics - {"c:accused", "c:police", "c:place"}
            if strict:
                scored = [(sc, e) for sc, e in scored if strict & set(e.terms)]
                # Entries that ARE about it (a "Father" field) before ones that only say its word
                # (a witness named "... ولد").
                tagged = [(sc, e) for sc, e in scored if strict & {f"c:{c}" for c in e.concepts}]
                if tagged:
                    scored = tagged
        return [e for _, e in scored[:k]]

    @staticmethod
    def topics(query: str) -> list[str]:
        """The specific topics asked about ("work", "family"...), for saying what is missing."""
        q = _expand(tokens(query))
        return list(dict.fromkeys(t[2:] for t in q if t.startswith("c:") and t not in ("c:fir", "c:link", "c:date")))

    def about(self, pid: str, k: int = 60) -> list[Entry]:
        return [e for e in self.entries if pid in e.people][:k]


# Built once per (graph version, case version).
_CACHE: dict[tuple[Any, ...], KnowledgeBase] = {}


def knowledge(graph: dict[str, Any], case: Any = None, net: Any = None) -> KnowledgeBase:
    key = (id(case), graph.get("version"), len(graph.get("nodes") or []), getattr(case, "version", None))
    kb = _CACHE.get(key)
    if kb is None:
        kb = KnowledgeBase.build(graph, case, net)
        if len(_CACHE) > 20:
            _CACHE.clear()
        _CACHE[key] = kb
    return kb
