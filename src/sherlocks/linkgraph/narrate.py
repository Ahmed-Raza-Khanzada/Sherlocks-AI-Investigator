"""Natural replies without the AI model: each Facts-agent answer opens with what an
investigator would say first - in English, Roman Urdu or Urdu - and its lines are written
in words ("FIR 391/26, Bin Qasim: attempted rape, causing hurt - he is the accused"),
not raw section numbers. The cited list follows, so every claim keeps its source.

``narrate(facts, language)`` -> the reply text, or ``None`` for types it does not cover
(the fixed templates in ``conversation.py`` are used then).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sherlocks.linkgraph.offences import CRIMES, label, labels

ROLE = {
    "accused": ("accused", "mulzim", "ملزم"),
    "complainant": ("the complainant", "muddai", "مدعی"),
    "victim": ("the affected party", "mutasira fareeq", "متاثرہ فریق"),
    "witness": ("a witness", "gawah", "گواہ"),
    "investigator": ("the investigating officer", "tafteeshi officer", "تفتیشی افسر"),
    "named": ("named", "naamzad / mazkoor", "مذکور"),
}
OUTCOME = {
    "convicted": ("convicted", "saza ho chuki", "سزا ہو چکی"),
    "acquitted": ("acquitted", "bari ho chuka", "بری"),
    "under trial": ("under trial in court", "adalat mein zer-e-samaat (challan pesh)", "عدالت میں زیر سماعت"),
    "on bail": ("on bail", "zamanat par", "ضمانت پر"),
    "under investigation": ("still under investigation", "abhi zer-e-tafteesh", "زیر تفتیش"),
    "proclaimed offender": ("a proclaimed offender", "ishtihari / mafroor", "اشتہاری"),
    "disposed / closed": ("disposed / closed", "band / kharij", "خارج / داخل دفتر"),
    "status not stated": ("status not recorded", "status record mein nahi", "صورتحال درج نہیں"),
}
_I = {"en": 0, "roman": 1, "ur": 2}
# What the IO concluded, said plainly (key from case_queries.IO_VERDICTS).
VERDICT = {
    "challan": ("found enough evidence against the accused and sent the case to court (challan)",
                "mulzim ke khilaf kafi shahadat payi aur challan adalat bhej diya",
                "ملزم کے خلاف کافی شہادت پائی اور چالان عدالت بھیج دیا"),
    "challan_absconding": ("found the accused liable and sent an interim challan to court (512 CrPC - accused absconding / not arrested)",
                           "mulzim ko qabil-e-muakhza paya aur 512 ض ف ke tehat interim challan adalat bheja (mulzim mafroor / giraftar nahi)",
                           "ملزم کو قابل مواخذہ پایا اور 512 ض ف کے تحت عبوری چالان عدالت بھیجا (ملزم مفرور / گرفتار نہیں)"),
    "c_class": ("asked to close it as C class - a civil dispute / no cognizable offence; the accused was not held guilty",
                "case C class mein band karne ki sifarish ki - yeh deewani tanaza / ghair qabil-e-dast-andazi jurm hai; mulzim qasoorwar nahi thehra",
                "مقدمہ سی کلاس میں بند کرنے کی سفارش کی - یہ دیوانی تنازعہ ہے؛ ملزم قصوروار نہیں ٹھہرا"),
    "b_class": ("asked to close it as B class - the case was found false",
                "case B class mein band karne ki sifarish ki - muqadma jhoota paya gaya",
                "مقدمہ بی کلاس میں بند کرنے کی سفارش کی - مقدمہ جھوٹا پایا گیا"),
    "a_class": ("asked to close it as A class - the offence happened but the accused is untraced",
                "case A class mein band karne ki sifarish ki - waqia sach hai lekin mulzim ka suragh nahi mila",
                "مقدمہ اے کلاس میں بند کرنے کی سفارش کی - واقعہ درست ہے مگر ملزم کا سراغ نہیں ملا"),
    "released_169": ("released the accused under 169 CrPC for lack of evidence",
                     "shahadat na milne par mulzim ko 169 ض ف ke tehat reha kar diya",
                     "شہادت نہ ملنے پر ملزم کو 169 ض ف کے تحت رہا کر دیا"),
    "innocent": ("found the accused innocent", "mulzim ko be-gunah paya", "ملزم کو بے گناہ پایا"),
    "disposed": ("asked to dispose of / close the case", "case ke ikhraj / band karne ki sifarish ki", "مقدمہ کے اخراج کی سفارش کی"),
    "investigating": ("has not concluded yet - still under investigation", "abhi kisi nateeje par nahi pohncha - tafteesh jari hai",
                      "ابھی کسی نتیجے پر نہیں پہنچا - تفتیش جاری ہے"),
    "unknown": ("wrote a report whose conclusion I could not read clearly", "ki report ka nateeja wazeh nahi parha ja saka",
                "کی رپورٹ کا نتیجہ واضح نہیں پڑھا جا سکا"),
}
_HELD = {"challan", "challan_absconding"}
_NOT_HELD = {"c_class", "b_class", "released_169", "innocent"}


def _w(table: dict[str, tuple[str, str, str]], key: str, lang: str) -> str:
    return table.get(key, (key, key, key))[_I[lang]]


def _t(lang: str, en: str, roman: str, ur: str) -> str:
    return {"en": en, "roman": roman, "ur": ur}[lang]


def _join(parts: list[str], lang: str) -> str:
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return "".join(parts)
    last = _t(lang, " and ", " aur ", " اور ")
    sep = "، " if lang == "ur" else ", "
    return sep.join(parts[:-1]) + last + parts[-1]


def _fir(row: dict[str, Any], lang: str, *, role: bool = True) -> str:
    crimes = labels(row.get("crimes") or [], lang) or _t(lang, "offence not classified", "jurm wazeh nahi",
                                                          "جرم واضح نہیں")
    who = _w(ROLE, row.get("role_kind") or row.get("role") or "named", lang)
    tail = ""
    if role:
        tail = _t(lang, f" - he is {who}", f" - is mein woh {who} hai", f" - اس میں وہ {who} ہے")
    return f"FIR {row.get('label') or row.get('fir')} ({crimes}){tail}"


def _line(it: dict[str, Any], lang: str) -> str:
    """One FIR line in words, with its sections and source kept for checking."""
    if "fir" not in it or "crimes" not in it:
        return f"• {it['text']}" + (f" [{it['source']}]" if it.get("source") else "")
    text = it["text"]
    ps = it.get("ps") or (text.split(" at ", 1)[1].split(":", 1)[0].split(" (")[0] if " at " in text else "")
    crimes = labels(it["crimes"], lang) or _t(lang, "offence not classified", "jurm wazeh nahi", "جرم واضح نہیں")
    who = _w(ROLE, it.get("role") or "named", lang)
    when = str(it.get("occurred") or "").split("  ")[0][:20]
    out = (f"• FIR {it['fir']}" + (f", {ps}" if ps and ps != "-" else "")
           + (f", {_t(lang, 'occurred', 'waqia', 'وقوعہ')} {when}" if when else "") + f": {crimes} ({who})")
    if it.get("outcome"):
        out += f" - {_w(OUTCOME, it['outcome'], lang)}"
    return out + (f" [{it['source']}]" if it.get("source") else "")


def _list(items: list[dict[str, Any]], lang: str, limit: int = 12) -> list[str]:
    lines = [_line(it, lang) for it in items[:limit]]
    if len(items) > limit:
        lines.append(f"… +{len(items) - limit}")
    return lines


# -- per answer type ---------------------------------------------------------------------------


def _cases(f: dict[str, Any], lang: str) -> list[str]:
    s, rows = f["subject"], f.get("rows") or []
    by_role = Counter(r["role_kind"] for r in rows)
    parts = [_t(lang, f"{_w(ROLE, k, lang)} in {n}", f"{n} mein {_w(ROLE, k, lang)}", f"{n} میں {_w(ROLE, k, lang)}")
             for k, n in by_role.most_common()]
    out = [_t(lang, f"{s} appears in {len(rows)} FIR(s): " + _join(parts, lang) + ".",
              f"{s} ka naam {len(rows)} FIRs mein hai: " + _join(parts, lang) + ".",
              f"{s} کا نام {len(rows)} ایف آئی آر میں ہے: " + _join(parts, lang) + "۔")]
    heavy = [r for r in rows if r["role_kind"] == "accused" and r["severity"] >= 3]
    if heavy:
        out.append(_t(lang, "The most serious charges against him: ", "Us par sab se sangeen ilzamaat: ",
                      "اس پر سب سے سنگین الزامات: ") + _join([_fir(r, lang, role=False) for r in heavy[:3]], lang) + ".")
    other = [r for r in rows if r["role_kind"] != "accused" and r["severity"] >= 3]
    for r in other[:2]:
        who = _w(ROLE, r["role_kind"], lang)
        out.append(_t(lang, f"In FIR {r['label']} ({labels(r['crimes'], lang)}) he is {who}, not the accused.",
                      f"FIR {r['label']} ({labels(r['crimes'], lang)}) mein woh khud {who} hai, mulzim nahi.",
                      f"ایف آئی آر {r['label']} ({labels(r['crimes'], lang)}) میں وہ خود {who} ہے، ملزم نہیں۔"))
    light = [r for r in rows if r["role_kind"] == "accused" and r["severity"] < 3]
    if light:
        kinds = Counter(k for r in light for k in r["crimes"][:1])
        what = _join([f"{label(k, lang)} ({n})" for k, n in kinds.most_common()], lang)
        out.append(_t(lang, f"The other charges are lighter: {what}.", f"Baqi ilzamaat halke hain: {what}.",
                      f"باقی الزامات نسبتاً ہلکے ہیں: {what}۔"))
    return out


def _serious(f: dict[str, Any], lang: str) -> list[str]:
    s, rows, rest, asked = f["subject"], f.get("rows") or [], f.get("rest") or [], f.get("asked") or []
    asked_words = _join(list(dict.fromkeys(label(k, lang) for k in asked if CRIMES[k][0] >= 1)), lang)
    if not rows:
        if asked:
            return [_t(lang, f"No - none of {s}'s {f.get('total', 0)} FIR(s) is about {asked_words}.",
                       f"Nahi - {s} ke {f.get('total', 0)} FIRs mein se kisi mein {asked_words} ka ilzaam nahi.",
                       f"نہیں - {s} کی {f.get('total', 0)} ایف آئی آر میں سے کسی میں {asked_words} کا الزام نہیں۔")]
        return [_t(lang, f"None of {s}'s FIRs is for a violent or heinous crime.",
                   f"{s} ka koi FIR sangeen / khatarnak jurm ka nahi.",
                   f"{s} کی کوئی ایف آئی آر سنگین جرم کی نہیں۔")]
    accused = [r for r in rows if r["role_kind"] == "accused"]
    out = []
    if accused:
        out.append(_t(lang, f"Yes - {s} is accused in {len(accused)} serious FIR(s): ",
                      f"Haan - {s} {len(accused)} sangeen FIR(s) mein mulzim hai: ",
                      f"جی ہاں - {s} {len(accused)} سنگین ایف آئی آر میں ملزم ہے: ")
                   + _join([_fir(r, lang, role=False) for r in accused], lang) + ".")
    if not accused:
        what = asked_words or _t(lang, "a serious crime", "kisi sangeen jurm", "کسی سنگین جرم")
        out.append(_t(lang, f"No - {s} is not accused of {what} in any FIR.",
                      f"Nahi - {s} kisi FIR mein {what} ka mulzim nahi.",
                      f"نہیں - {s} کسی ایف آئی آر میں {what} کا ملزم نہیں۔"))
    for r in [r for r in rows if r["role_kind"] != "accused"][:3]:
        who = _w(ROLE, r["role_kind"], lang)
        out.append(_t(lang, f"FIR {r['label']} ({labels(r['crimes'], lang)}) is also serious, but there he is {who}, "
                            "not the accused.",
                      f"FIR {r['label']} ({labels(r['crimes'], lang)}) bhi sangeen hai, lekin us mein woh {who} hai, "
                      "mulzim nahi.",
                      f"ایف آئی آر {r['label']} ({labels(r['crimes'], lang)}) بھی سنگین ہے، لیکن اس میں وہ {who} ہے، "
                      "ملزم نہیں۔"))
    if rest and not asked:
        kinds = Counter(k for r in rest for k in r["crimes"][:1])
        what = _join([f"{label(k, lang)} ({n})" for k, n in kinds.most_common()], lang)
        if what:
            out.append(_t(lang, f"His other {len(rest)} FIR(s) are lighter: {what}.",
                          f"Baqi {len(rest)} FIRs halke jurm ke hain: {what}.",
                          f"باقی {len(rest)} ایف آئی آر ہلکے جرائم کی ہیں: {what}۔"))
    return out


def _status(f: dict[str, Any], lang: str) -> list[str]:
    s, n, o = f["subject"], f["count"], f.get("outcomes") or {}
    conv, acq = o.get("convicted", 0), o.get("acquitted", 0)
    if not conv and not acq:
        out = [_t(lang, f"{s} has {n} FIR(s); none has ended yet - no conviction and no acquittal.",
                  f"{s} ke {n} FIRs hain; abhi tak kisi mein na saza hui hai na woh bari hua hai.",
                  f"{s} کی {n} ایف آئی آر ہیں؛ ابھی تک کسی میں نہ سزا ہوئی نہ وہ بری ہوا۔")]
    else:
        out = [_t(lang, f"Of {s}'s {n} FIR(s): convicted in {conv}, acquitted in {acq}.",
                  f"{s} ke {n} FIRs mein se {conv} mein saza hui aur {acq} mein bari hua.",
                  f"{s} کی {n} ایف آئی آر میں سے {conv} میں سزا ہوئی اور {acq} میں بری ہوا۔")]
    others = [(k, v) for k, v in o.items() if k not in ("convicted", "acquitted")]
    if others:
        out.append(_t(lang, "The rest: ", "Baqi: ", "باقی: ")
                   + _join([f"{v} {_w(OUTCOME, k, lang)}" for k, v in sorted(others, key=lambda x: -x[1])], lang) + ".")
    return out


def _details(f: dict[str, Any], lang: str) -> list[str]:
    s = f["subject"]
    if len(f["items"]) == 1:
        no = (f["items"][0].get("detail") or {}).get("fir")
        s = None
        out = [_t(lang, f"What FIR {no} says:", f"FIR {no} mein yeh likha hai:", f"ایف آئی آر {no} میں یہ لکھا ہے:")]
    out = [_t(lang, f"What each of {s}'s FIRs alleges:", f"{s} ke har FIR mein ilzaam yeh hai:",
              f"{s} کی ہر ایف آئی آر میں الزام یہ ہے:") if s else out[0] if len(f["items"]) == 1 else
           _t(lang, "What the FIR says:", "FIR mein yeh likha hai:", "ایف آئی آر میں یہ لکھا ہے:")]
    for it in f["items"][:8]:
        d = it.get("detail") or {}
        crimes = labels(d.get("crimes") or [], lang) or _t(lang, "offence not classified", "jurm wazeh nahi",
                                                           "جرم واضح نہیں")
        line = f"• FIR {d.get('fir')}" + (f", {d['ps']}" if d.get("ps") else "") + f": {crimes}."
        if d.get("role"):
            line += " " + _t(lang, f"He is {_w(ROLE, d.get('role') or 'named', lang)}.",
                             f"Is mein woh {_w(ROLE, d.get('role') or 'named', lang)} hai.",
                             f"اس میں وہ {_w(ROLE, d.get('role') or 'named', lang)} ہے۔")
        if d.get("complainant"):
            line += " " + _t(lang, f"Filed by {d['complainant']}", f"Muddai: {d['complainant']}",
                             f"مدعی: {d['complainant']}")
            line += _t(lang, f", occurred {d['occurred']}." if d.get("occurred") else ".",
                       f", waqia {d['occurred']}." if d.get("occurred") else ".",
                       f"، وقوعہ {d['occurred']}۔" if d.get("occurred") else "۔")
        if d.get("accused"):
            line += " " + _t(lang, "Accused: ", "Mulzimaan: ", "ملزمان: ") + "، ".join(d["accused"]) + "."
        if d.get("story"):
            line += "\n  " + _t(lang, "Complainant's account: ", "Muddai ka bayan: ", "مدعی کا بیان: ") + f"\"{d['story']}…\""
        if d.get("outcome"):
            line += "\n  " + _t(lang, "Status: ", "Status: ", "صورتحال: ") + _w(OUTCOME, d["outcome"], lang)
        if it.get("source"):
            line += f" [{it['source']}]"
        out.append(line)
    if len(f["items"]) > 8:
        out.append(f"… +{len(f['items']) - 8}")
    return out


def _connection(f: dict[str, Any], lang: str) -> list[str]:
    a, b, routes = f["a"], f["b"], f["routes"]
    first = routes[0]
    if not first["inferred"] and len(first["hops"]) == 1:
        head = _t(lang, f"Yes - {a} and {b} are directly linked: {first['hops'][0]}.",
                  f"Haan - {a} aur {b} ka seedha taluq hai: {first['hops'][0]}.",
                  f"جی ہاں - {a} اور {b} کا براہ راست تعلق ہے: {first['hops'][0]}۔")
    elif not first["inferred"]:
        head = _t(lang, f"{a} and {b} are linked through {len(first['hops']) - 1} other person(s):",
                  f"{a} aur {b} {len(first['hops']) - 1} aur logon ke zariye jure hain:",
                  f"{a} اور {b} {len(first['hops']) - 1} دیگر افراد کے ذریعے منسلک ہیں:")
    else:
        head = _t(lang, f"No stated link between {a} and {b}; only an inferred one (a lead, not a fact):",
                  f"{a} aur {b} ka koi tasdeeq shuda taluq nahi; sirf andaze wala (lead, fact nahi):",
                  f"{a} اور {b} کا کوئی تصدیق شدہ تعلق نہیں؛ صرف قیاسی (سراغ، حقیقت نہیں):")
    out = [head]
    for r in routes[:3]:
        if r is first and len(r["hops"]) == 1 and not r["inferred"]:
            continue
        tag = _t(lang, "inferred" if r["inferred"] else "stated", "andaza" if r["inferred"] else "tasdeeq shuda",
                 "قیاسی" if r["inferred"] else "تصدیق شدہ")
        out.append(f"• ({tag}) " + " → ".join(r["hops"]) + (f" [{r['source']}]" if r.get("source") else ""))
    if first is routes[0] and len(first["hops"]) == 1 and not first["inferred"] and first.get("source"):
        out[0] += f" [{first['source']}]"
    return out


def _io(f: dict[str, Any], lang: str) -> list[str]:
    s, items = f.get("subject"), f["items"]
    accused = [it for it in items if (it.get("detail") or {}).get("role") in ("accused", None)]
    held = [it["detail"]["fir"] for it in accused if it["detail"]["verdict"] in _HELD]
    cleared = [it["detail"]["fir"] for it in accused if it["detail"]["verdict"] in _NOT_HELD]
    out = []
    if s and accused:
        parts = []
        if held:
            parts.append(_t(lang, f"in {len(held)} FIR(s) ({', '.join(held)}) the IO found him liable and sent the case to court",
                            f"{len(held)} FIR(s) ({', '.join(held)}) mein IO ne usay qabil-e-muakhza samjha aur challan adalat bheja",
                            f"{len(held)} ایف آئی آر ({', '.join(held)}) میں تفتیشی افسر نے اسے قابل مواخذہ سمجھا اور چالان عدالت بھیجا"))
        if cleared:
            parts.append(_t(lang, f"in {len(cleared)} ({', '.join(cleared)}) the IO did not hold him guilty",
                            f"{len(cleared)} ({', '.join(cleared)}) mein IO ne usay qasoorwar nahi thehraya",
                            f"{len(cleared)} ({', '.join(cleared)}) میں تفتیشی افسر نے اسے قصوروار نہیں ٹھہرایا"))
        rest = len(accused) - len(held) - len(cleared)
        if rest:
            parts.append(_t(lang, f"{rest} not concluded / unclear", f"{rest} ka nateeja abhi nahi / wazeh nahi",
                            f"{rest} کا نتیجہ ابھی نہیں / واضح نہیں"))
        out.append(_t(lang, f"According to the IO reports on {s}: ", f"IO reports ke mutabiq {s}: ",
                      f"تفتیشی رپورٹوں کے مطابق {s}: ") + "; ".join(parts) + ".")
        if held:
            out.append(_t(lang, "(A challan means the IO found enough evidence to prosecute; guilt itself is decided by the court.)",
                          "(Challan ka matlab hai IO ne muqadma chalane ke liye shahadat kafi samjhi; qasoorwar hone ka faisla adalat karti hai.)",
                          "(چالان کا مطلب ہے تفتیشی افسر نے مقدمہ چلانے کے لیے شہادت کافی سمجھی؛ قصوروار ہونے کا فیصلہ عدالت کرتی ہے۔)"))
    elif not items:
        return out
    for it in items[:8]:
        d = it["detail"]
        crimes = labels(d.get("crimes") or [], lang)
        who = d.get("io") or _t(lang, "The IO", "IO", "تفتیشی افسر")
        role = ""
        if d.get("role") and d["role"] != "accused":
            role = _t(lang, f" (he is {_w(ROLE, d['role'], lang)} here, not the accused)",
                      f" (is mein woh {_w(ROLE, d['role'], lang)} hai, mulzim nahi)",
                      f" (اس میں وہ {_w(ROLE, d['role'], lang)} ہے، ملزم نہیں)")
        line = (f"• FIR {d['fir']}" + (f", {d['ps']}" if d.get("ps") else "") + (f" ({crimes})" if crimes else "") + role
                + f": {who} " + _w(VERDICT, d["verdict"], lang) + ".")
        if d.get("quote"):
            line += "\n  " + _t(lang, "The report says: ", "Report mein likha hai: ", "رپورٹ میں لکھا ہے: ") + f"\"{d['quote']}\""
        out.append(line + (f" [{it['source']}]" if it.get("source") else ""))
    return out


def _simple(f: dict[str, Any], lang: str) -> list[str] | None:
    s, n, items = f.get("subject") or "", f.get("count", 0), f.get("items") or []
    short = _join([it["text"] for it in items[:3]], lang)
    if f["type"] == "vehicles" and items:
        return [_t(lang, f"{s} is linked to {n} vehicle(s): {short}.", f"{s} se {n} gaari(yan) juri hain: {short}.",
                   f"{s} سے {n} گاڑی/گاڑیاں منسلک ہیں: {short}۔")]
    if f["type"] == "phones" and items:
        return [_t(lang, f"{s} has {n} number(s) on record" + (f": {short}." if n <= 3 else ":"),
                   f"{s} ke {n} number record mein hain" + (f": {short}." if n <= 3 else ":"),
                   f"{s} کے {n} نمبر ریکارڈ میں ہیں" + (f": {short}۔" if n <= 3 else ":"))]
    if f["type"] == "hotels" and items:
        return [_t(lang, f"{s} has stayed in hotels {n} time(s) on record. Most recent first:",
                   f"{s} record ke mutabiq {n} martaba hotel mein thehra. Tafseel:",
                   f"{s} ریکارڈ کے مطابق {n} مرتبہ ہوٹل میں ٹھہرا۔ تفصیل:")]
    if f["type"] == "criminals_near" and items:
        return [_t(lang, f"{n} of the people linked to {s} have a criminal record:",
                   f"{s} se jure logon mein se {n} ka criminal record hai:",
                   f"{s} سے منسلک افراد میں سے {n} کا مجرمانہ ریکارڈ ہے:")]
    if f["type"] == "criminals_all" and items:
        return [_t(lang, f"Of the {f.get('people')} people on the graph, {n} have a criminal record. Those with the most FIRs first:",
                   f"Graph ke {f.get('people')} logon mein se {n} ka criminal record hai. Sab se zyada FIRs walay pehle:",
                   f"گراف کے {f.get('people')} افراد میں سے {n} کا مجرمانہ ریکارڈ ہے۔ سب سے زیادہ ایف آئی آر والے پہلے:")]
    return None


def narrate(facts: dict[str, Any], language: str, limit: int = 12) -> str | None:
    kind = facts.get("type")
    if facts.get("empty") and kind not in ("serious_cases",):
        return None
    lang = language if language in _I else "en"
    if kind in ("cases", "count_cases") and facts.get("rows"):
        head = " ".join(_cases(facts, lang))
        if facts.get("brief"):
            return _t(lang, "On record: ", "Records ke mutabiq: ", "ریکارڈ کے مطابق: ") + head
        return "\n".join([head, "", *_list(facts["items"], lang, limit)])
    if kind == "connection" and facts.get("routes"):
        return "\n".join(_connection(facts, lang))
    if kind == "serious_cases":
        head = " ".join(_serious(facts, lang))
        return "\n".join([head, "", *_list(facts["items"], lang, limit)]) if facts["items"] else head
    if kind == "fir_status" and facts.get("rows"):
        items = [it for it in facts["items"] if not it.get("overall")]
        return "\n".join([" ".join(_status(facts, lang)), "", *_list(items, lang, limit)])
    if kind == "fir_details":
        return "\n".join(_details(facts, lang))
    if kind == "io_report" and facts["items"]:
        return "\n".join(_io(facts, lang))
    head = _simple(facts, lang)
    if head:
        return "\n".join([*head, *_list(facts["items"], lang, limit)])
    return None
