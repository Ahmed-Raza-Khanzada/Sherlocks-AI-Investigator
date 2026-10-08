"""One system's answer -> :class:`SystemRecord`.

Input is a dumped ``cdr_report_app`` ``ProviderResult`` (``provider``, ``hit``,
``status``, ``summary``, ``data``, ``raw``, ``errors``). The typed ``data`` is used
where cdr_report_app parsed it well; everything it discarded - the second and later
rows, the other people in a row, photographs - is recovered from ``raw``.

Every person found is sorted into one of two buckets:

* **the subject** - the person who was searched for. Matched by CNIC; by phone only
  when no CNIC is known yet (that is how a phone resolves to a CNIC).
* **related** - anyone else. A related person carries the relation the *record*
  states ("Landlord", "Registered owner of SIM 0300...", "Co-accused in FIR 45/2023"),
  which becomes a strong edge.

A different CNIC always means a different person. Two people are never merged here
on a name.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from sherlocks.linkgraph.images import ImageStore, extract_images
from sherlocks.linkgraph.models import FirKey, HotelStay, PersonRef, RelatedPerson, SystemRecord
from sherlocks.linkgraph.normalize import (
    clean_address,
    clean_name,
    clean_text,
    cnic13,
    mobile11,
    name_key,
    split_relation,
)
from sherlocks.linkgraph.refs import FoundRef, find_people, snake
from sherlocks.linkgraph.systems import system_label

# cdr_report_app counts PSRMS ``WIT`` rows as suspects and ``SUS`` rows as witnesses,
# in both its provider summary and its PDF renderer - the upstream codes are observed to
# be inverted. Follow the production app rather than the code letters.
PSRMS_ROLES = {"FIR": "Complainant", "WIT": "Accused / suspect", "SUS": "Witness", "AFFP": "Victim"}

_ROLE_LABELS = {
    "owner": "Owner",
    "landlord": "Landlord",
    "tenant": "Tenant",
    "tenancy": "Landlord (tenancy)",
    "property owned": "Tenant of owned property",
    "complainant": "Complainant",
    "reporting": "Reported by",
    "reporter": "Reported by",
    "accused": "Accused",
    "co-accused": "Co-accused",
    "suspect": "Suspect",
    "witness": "Witness",
    "victim": "Victim",
    "employer": "Employer",
    "employee": "Employee",
    "driver": "Driver",
    "guarantor": "Guarantor",
    "reference": "Reference",
    "family": "Family member",
    "relative": "Relative",
    "associate": "Associate",
    "co-guest": "Co-guest",
    "challan": "Vehicle owner (challan)",
    "sim": "SIM holder",
    "member": "Member",
    "investigating officer": "Investigating officer",
    "nominee": "Nominee",
    "husband": "Husband",
    "wife": "Wife",
    "father": "Father",
    "mother": "Mother",
    "son": "Son",
    "daughter": "Daughter",
    "brother": "Brother",
    "sister": "Sister",
}

# When a record names another person with no role prefix, what does it mean?
_DEFAULT_RELATION = {
    "subscriber": "Registered owner of a shared SIM",
    "psrms": "Same identifier in an FIR record",
    "hotel_eye": "Used same phone/CNIC at hotel check-in",
    "sbvs": "Named in verification record",
    "trust": "Named in property record",
    "prvs": "Named in tenancy verification",
    "watchlist": "Named in watchlist entry",
    "tracs": "Named on traffic challan",
    "dls": "Driving licence registered with the same number",
    "evs": "Employee record with the same contact",
    "hope": "Employee record with the same contact",
    "hrmis": "Police officer record with the same number",
}

_CONTEXT_KEYS = (
    "fir_no", "fir_year", "vehicle_num_plate", "vehicle_no", "registration_no",
    "challan_number", "hotel_name", "property_address", "property_house_no",
    "org_name", "organization_name", "company_name", "police_station", "ps_name",
    "check_in", "relation", "relationship", "purpose_name", "tenant_id",
)


@dataclass(slots=True)
class _Ctx:
    rec: SystemRecord
    query: PersonRef

    # -- identity -----------------------------------------------------------------

    def known_cnics(self) -> set[str]:
        return {c for c in (self.query.cnic, self.rec.subject.cnic) if c}

    def known_phones(self) -> set[str]:
        return set(self.query.phones) | set(self.rec.subject.phones)

    def is_subject(self, ref: PersonRef) -> bool:
        cnics = self.known_cnics()
        if ref.cnic and cnics:
            return ref.cnic in cnics
        return bool(set(ref.phones) & self.known_phones())

    def name_compatible(self, ref: PersonRef) -> bool:
        current = self.rec.subject.name or self.query.name
        if not ref.name or not current:
            return True
        return fuzz.token_set_ratio(name_key(ref.name), name_key(current)) >= 80

    # -- accumulation -------------------------------------------------------------

    def absorb(self, ref: PersonRef) -> None:
        subject = self.rec.subject
        if ref.name:
            if not subject.name:
                subject.name = ref.name
            elif name_key(ref.name) != name_key(subject.name):
                aliases = subject.extra.get("aliases", "")
                if ref.name.lower() not in aliases.lower():
                    subject.extra["aliases"] = f"{aliases}; {ref.name}".strip("; ")
        subject.father_name = subject.father_name or ref.father_name
        subject.cnic = subject.cnic or ref.cnic
        for phone in ref.phones:
            if phone not in subject.phones:
                subject.phones.append(phone)
        for address in ref.addresses:
            if address not in subject.addresses:
                subject.addresses.append(address)
        for image in ref.images:
            if image not in subject.images:
                subject.images.append(image)
        for key, value in ref.extra.items():
            subject.extra.setdefault(key, value)

    def take(self, ref: PersonRef, detail: str | None = None) -> None:
        """A record's own typed subject: absorb it if it is who we searched for, else it
        is somebody else who shares the identifier we searched with - a relation."""
        if self.is_subject(ref) or not (ref.cnic or ref.phones) or not (self.known_cnics() or self.known_phones()):
            self.absorb(ref)
        else:
            self.relate(ref, _DEFAULT_RELATION.get(self.rec.system, f"Named in {system_label(self.rec.system)} record"), detail)

    def relate(self, ref: PersonRef, relation: str, detail: str | None = None) -> None:
        if self.is_subject(ref) or ref.is_empty:
            return
        for existing in self.rec.related:
            if _same_ref(existing.ref, ref):
                _merge_ref(existing.ref, ref)
                return
        self.rec.related.append(RelatedPerson(ref=ref, relation=relation, detail=detail))


def _same_ref(a: PersonRef, b: PersonRef) -> bool:
    if a.cnic and b.cnic:
        return a.cnic == b.cnic
    if set(a.phones) & set(b.phones):
        return True
    if not (a.cnic or b.cnic or a.phones or b.phones) and a.name and b.name:
        return name_key(a.name) == name_key(b.name) and name_key(a.father_name) == name_key(b.father_name)
    return False


def _merge_ref(into: PersonRef, other: PersonRef) -> None:
    into.name = into.name or other.name
    into.father_name = into.father_name or other.father_name
    into.cnic = into.cnic or other.cnic
    into.phones.extend(p for p in other.phones if p not in into.phones)
    into.addresses.extend(a for a in other.addresses if a not in into.addresses)


def _ref(name=None, father=None, cnic=None, phones=(), addresses=()) -> PersonRef:
    name, father_from_name = split_relation(clean_name(name))
    out_phones: list[str] = []
    for value in phones:
        phone = mobile11(value)
        if phone and phone not in out_phones:
            out_phones.append(phone)
    return PersonRef(
        name=name,
        father_name=clean_name(father) or father_from_name,
        cnic=cnic13(cnic),
        phones=out_phones,
        addresses=[a for a in (clean_address(v) for v in addresses) if a],
    )


def _detail(context: dict[str, str]) -> str | None:
    parts = [f"{key.replace('_', ' ')}: {context[key]}" for key in _CONTEXT_KEYS if context.get(key)]
    return "; ".join(parts[:3]) or None


# The same prefix means different things in different systems.
_SYSTEM_ROLE_LABELS = {
    ("tracs", "owner"): "Vehicle owner",
    ("tracs", "driver"): "Driver of vehicle",
    ("prvs", "owner"): "Landlord",
    ("prvs", "landlord"): "Landlord",
    ("trust", "owner"): "Landlord",
    ("trust", "tenant"): "Tenant",
    ("sbvs", "employer"): "Employer",
    ("sbvs", "owner"): "Employer / house owner",
    ("milap", "reporting"): "Reported by",
    ("pfc", "complainant"): "Complainant",
    ("igp_cms", "complainant"): "Complainant",
}


def _relation_for(system: str, found: FoundRef) -> str:
    role = found.role
    if role:
        return _SYSTEM_ROLE_LABELS.get((system, role), _ROLE_LABELS.get(role, role.title()))
    return _DEFAULT_RELATION.get(system, f"Named in {system_label(system)} record")


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _flat_fields(rec: SystemRecord, raw: Any, limit: int = 14) -> None:
    """First scalar values in the payload, as label/value pairs, for untyped systems."""
    skip = {"status", "status_code", "code", "message", "success", "summary", "error", "detail", "hit"}
    seen: set[str] = {f.label for f in rec.fields}

    def walk(value: Any, key: str = "") -> None:
        if len(rec.fields) >= limit:
            return
        if isinstance(value, dict):
            for k, v in value.items():
                if snake(k) not in skip:
                    walk(v, str(k))
        elif isinstance(value, list):
            for item in value[:3]:
                walk(item, key)
        elif key and value not in (None, "", False):
            text = str(value)
            label = snake(key).replace("_", " ").title()
            if label not in seen and len(text) <= 200 and not text.startswith("<image"):
                seen.add(label)
                rec.add_field(label, text)

    walk(raw)


# --------------------------------------------------------------------------------------
# Per-system handlers. Each fills the subject, the related people and the record facts
# it knows how to read; the generic walker then sweeps up anyone they did not name.
# --------------------------------------------------------------------------------------


def _simsdb(ctx: _Ctx, data: dict, raw: Any) -> None:
    owner = _ref(data.get("name"), cnic=data.get("cnic"), addresses=[data.get("address")])
    sims: list[str] = []
    for sim in data.get("sims") or []:
        number = mobile11(sim.get("number"))
        sim_cnic = cnic13(sim.get("cnic"))
        if number and (not sim_cnic or not owner.cnic or sim_cnic == owner.cnic):
            sims.append(number)
            if sim.get("name") and not owner.name:
                owner.name = clean_name(sim.get("name"))
        elif number:
            ctx.relate(_ref(sim.get("name"), cnic=sim_cnic, phones=[number]), f"SIM {number} on another CNIC")
    owner.phones = sims

    queried = mobile11(data.get("mobile"))
    if ctx.query.cnic and owner.cnic and owner.cnic != ctx.query.cnic:
        # The number this person uses is registered to somebody else's CNIC.
        ctx.relate(owner, f"Registered owner of SIM {queried or ''}".strip(), f"{len(sims)} SIM(s) on that CNIC")
    else:
        ctx.absorb(owner)
    ctx.rec.add_field("Name", owner.name)
    ctx.rec.add_field("CNIC", owner.cnic)
    ctx.rec.add_field("Address", data.get("address"))
    ctx.rec.add_field("SIMs on this CNIC", ", ".join(sims) or data.get("sim_count"))


def _subscriber(ctx: _Ctx, data: dict, raw: Any) -> None:
    rows: list[tuple[str, dict]] = []
    if isinstance(raw, dict):
        for branch in ("by_cnic", "by_mobile"):
            payload = raw.get(branch)
            if isinstance(payload, dict):
                rows += [(branch, r) for r in _rows((payload.get("Data") or {}).get("SubscriberList"))]
    for branch, row in rows:
        phone = row.get("Phone") or row.get("MSISDN") or row.get("Mobile")
        ref = _ref(row.get("Name"), cnic=row.get("Cnic"), phones=[phone], addresses=[row.get("Address")])
        if ctx.is_subject(ref) or (branch == "by_cnic" and ref.cnic == ctx.query.cnic):
            ctx.absorb(ref)
        else:
            ctx.relate(ref, f"Registered owner of SIM {mobile11(phone) or phone}", f"activated {row.get('ActivationDate') or '-'}")
    ctx.rec.add_field("Name", data.get("name"))
    ctx.rec.add_field("CNIC", data.get("cnic"))
    ctx.rec.add_field("Activation date", data.get("activation_date"))
    ctx.rec.add_field("Address", data.get("address"))
    ctx.rec.add_field("SIMs", ", ".join(ctx.rec.subject.phones))
    ctx.rec.add_field("Note", "Subscriber data is current only to 2020")


def _cro(ctx: _Ctx, data: dict, raw: Any) -> None:
    ctx.absorb(_ref(data.get("name"), data.get("father_name")))
    ctx.rec.flags.append("criminal_record")
    for label, key in [("CRO No.", "cro_no"), ("Name", "name"), ("Father", "father_name"), ("Age", "age"),
                       ("Category", "category"), ("District", "district"), ("FIR count", "fir_count")]:
        ctx.rec.add_field(label, data.get(key))
    for fir in data.get("firs") or []:
        if not (fir.get("fir_no") and fir.get("fir_year")):
            continue
        key = FirKey(
            fir_no=str(fir["fir_no"]), fir_year=str(fir["fir_year"]),
            police_station=str(fir.get("police_station") or ""), offence=fir.get("offence"),
            status=fir.get("status"), role="Accused (CRO)",
        )
        ctx.rec.firs.append(key)
        ctx.rec.add_field(f"FIR {key.fir_no}/{key.fir_year}", f"{key.police_station} | {key.offence or '-'} | {key.status or '-'}")


def _psrms(ctx: _Ctx, data: dict, raw: Any) -> None:
    rows = list(data.get("firs") or [])
    # Pass 1: the subject's own rows, so the FIRs they are named in - and their role in
    # each - are known before anybody else is described.
    subject_role: dict[str, str] = {}
    others: list[tuple[dict, str, PersonRef]] = []
    for fir in rows:
        role = PSRMS_ROLES.get(str(fir.get("person_type") or "").upper().strip(), fir.get("person_type") or "Named")
        ref = _ref(fir.get("person_name"), fir.get("person_father"), fir.get("person_cnic"),
                   [fir.get("person_phone")], [fir.get("person_address")])
        label = f"{fir.get('fir_no')}/{fir.get('fir_year')}"
        if ctx.is_subject(ref) or not (ref.cnic or ref.phones):
            ctx.absorb(ref)
            subject_role[label] = role
            ps = fir.get("ps_name") or f"PS#{fir.get('ps_id')}"
            ctx.rec.firs.append(FirKey(
                fir_no=str(fir.get("fir_no")), fir_year=str(fir.get("fir_year")),
                police_station=str(ps), ps_id=str(fir.get("ps_id") or ""),
                role=role, status=fir.get("fir_status"), offence=fir.get("offence"),
            ))
            ctx.rec.add_field(f"FIR {label}", " | ".join(
                str(v) for v in (ps, role, fir.get("offence"), fir.get("fir_status")) if v))
        else:
            others.append((fir, role, ref))

    # Pass 2: everyone else, named by what they are to the subject in that FIR.
    def side(r: str | None) -> str | None:
        return "Accused" if r and r.startswith("Accused") else r

    for fir, role, ref in others:
        label = f"{fir.get('fir_no')}/{fir.get('fir_year')}"
        mine, role = side(subject_role.get(label)), side(role) or role
        if mine is None:
            # A different FIR that merely shares the identifier we searched on.
            relation = f"Same identifier in FIR {label} ({role})"
        elif mine == "Accused" and role == "Accused":
            relation = f"Co-accused in FIR {label}"
        elif mine == "Accused" and role == "Complainant":
            relation = f"Complainant against the subject in FIR {label}"
        elif mine == "Complainant" and role == "Accused":
            relation = f"Accused by the subject in FIR {label}"
        elif mine == "Victim" and role == "Accused":
            relation = f"Accused of harming the subject in FIR {label}"
        elif role == "Witness" and mine == "Witness":
            relation = f"Co-witness in FIR {label}"   # becomes a weak link (graph.link_related)
        elif role == "Witness":
            relation = f"Witness in FIR {label}"
        else:
            relation = f"{role} in FIR {label}"
        ctx.relate(ref, relation, f"PS#{fir.get('ps_id')} | {fir.get('fir_status') or ''}".strip(" |"))
    if ctx.rec.firs:
        ctx.rec.flags.append("fir_record")


def _watchlist(ctx: _Ctx, data: dict, raw: Any) -> None:
    ctx.rec.flags.append("watchlist")
    rows = _rows(raw.get("data")) if isinstance(raw, dict) else []
    ctx.rec.add_field("Entries", len(rows))
    _flat_fields(ctx.rec, rows[:1])


# Above this many people on ONE number/CNIC, PRVS is returning a shared line (an
# office/agent number verifying many tenants) - not one person's contacts. Linking them
# all would fabricate a hairball, so they are counted and flagged, not linked.
_PRVS_SHARED_MAX = 6


_PRVS_ROLES = {"applicant": "Applicant", "witness1": "Witness", "witness2": "Witness", "witness": "Witness"}


def _prvs_profiles(ctx: _Ctx, profiles: list[dict]) -> None:
    """PRVS unified profile(s). The subject's own profile brings their details (email,
    passport), cases, verification witnesses and CRO record; a profile found on their
    number that belongs to someone else is only a relation."""
    for prof in profiles:
        pd = prof.get("person_details") or {}
        ref = _ref(pd.get("name"), pd.get("father_name"), pd.get("cnic"), [pd.get("mobile")], [pd.get("address")])
        for key in ("email", "passport"):
            if clean_text(pd.get(key)):
                ref.extra[key] = str(pd[key]).strip()
        own = ctx.is_subject(ref) or not (ctx.known_cnics() or ctx.known_phones()) or \
            (not ctx.known_cnics() and not ref.cnic)
        if not own:
            # Someone else's PRVS profile, reached through the identifier we searched with
            # (often a number they gave as a witness). Say why, so the link reads as a lead.
            summary = pd.get("cases_summary") or {}
            dates = sorted(str(c.get("request_date") or "")[:4] for c in pd.get("cases") or [] if c.get("request_date"))
            span = f" ({dates[0]}-{dates[-1]})" if dates and dates[0] != dates[-1] else (f" ({dates[0]})" if dates else "")
            roles = ", ".join(x for x in (
                f"applicant in {summary['as_applicant']}" if summary.get("as_applicant") else "",
                f"witness in {summary['as_witness']}" if summary.get("as_witness") else "") if x)
            searched_by = ", ".join(f"{k} {v}" for k, v in (prof.get("query") or {}).items())
            own_number = mobile11(pd.get("mobile"))
            detail = " · ".join(x for x in (
                f"{roles} PRVS verification case(s){span}" if roles else "",
                f"their own number is {own_number}" if own_number and own_number not in ctx.known_phones() else "",
                pd.get("zone_name") or pd.get("district_name") or "") if x)
            ctx.relate(ref, "PRVS profile found by this number", detail)
            ctx.rec.add_field("PRVS profile of someone else",
                              f"{pd.get('name') or '?'} (CNIC {pd.get('cnic') or '-'}) - found by {searched_by or 'this identifier'}"
                              + (f"; {detail}" if detail else ""))
            continue
        ctx.absorb(ref)
        for label, key in (("Email", "email"), ("Passport", "passport"), ("Zone", "zone_name"),
                           ("District", "district_name"), ("Address", "address")):
            ctx.rec.add_field(label, pd.get(key))
        summary = pd.get("cases_summary") or {}
        if summary:
            ctx.rec.add_field("Cases", f"{summary.get('total_cases', 0)} total · {summary.get('as_applicant', 0)} as applicant"
                                       f" · {summary.get('as_witness', 0)} as witness")
        cases = {c.get("case_id"): c for c in pd.get("cases") or [] if isinstance(c, dict)}
        for case in cases.values():
            roles = ", ".join(_PRVS_ROLES.get(str(r).lower(), str(r)) for r in case.get("roles") or [])
            ctx.rec.add_field(f"Case {case.get('case_id')}", " · ".join(str(x) for x in (
                roles, case.get("purpose_name"), case.get("status_name"), str(case.get("request_date") or "")[:10],
                case.get("zone_name") or case.get("district_name")) if x))
        # Witnesses of the subject's own verification vouched for the subject: a stated
        # relation. In a case where the subject is only a witness, the other witnesses
        # vouched for the applicant, not for the subject: a weak co-witness link.
        for w in prof.get("witnesses") or []:
            case = cases.get(w.get("case_id")) or {}
            wref = _ref(w.get("name"), w.get("father_name"), w.get("cnic"), [w.get("mobile")], [w.get("address")])
            detail = " · ".join(str(x) for x in (f"case {w.get('case_id')}", case.get("purpose_name"),
                                                  case.get("zone_name")) if x)
            subject_roles = [str(r).lower() for r in case.get("roles") or []]
            if subject_roles and not any("applicant" in r for r in subject_roles):
                ctx.relate(wref, f"Co-witness in PRVS case {w.get('case_id')}", detail)
            else:
                ctx.relate(wref, "Verification witness (PRVS)", detail)
        # CRO records PRVS itself matched to this person.
        for link in prof.get("criminal_links") or []:
            if link.get("cro_no"):
                ctx.rec.add_field("CRO No. (via PRVS)", link["cro_no"])
            for r in link.get("records") or []:
                m = re.search(r"(\d+)\s*/\s*(\d{2,4})", str(r.get("fir_no") or ""))
                if not m:
                    continue
                key = FirKey(fir_no=m.group(1), fir_year=m.group(2), police_station=str(r.get("police_station") or ""),
                             offence=r.get("fir_offence"), status=r.get("status"),
                             role=f"Accused ({link.get('source_system') or 'CRO'} via PRVS)")
                ctx.rec.firs.append(key)
                ctx.rec.add_field(f"FIR {key.fir_no}/{key.fir_year}", f"{key.police_station} | {key.offence or '-'}")
            if link.get("records") or link.get("cro_no"):
                for flag in ("criminal_record", "fir_record"):
                    if flag not in ctx.rec.flags:
                        ctx.rec.flags.append(flag)


def _prvs(ctx: _Ctx, data: dict, raw: Any) -> None:
    if isinstance(raw, dict) and isinstance(raw.get("profiles"), list):
        _prvs_profiles(ctx, raw["profiles"])
        return
    # Older per-case endpoints. EMS returns raw {"data": [rows]}; the Shield/cdr adapter
    # returns raw {"by_cnic": {"data": [rows]}, "by_mobile": {...}}. Read both.
    rows: list[dict] = []
    if isinstance(raw, dict):
        rows += _rows(raw.get("data"))
        for branch in ("by_mobile", "by_cnic"):
            payload = raw.get(branch)
            if isinstance(payload, dict):
                rows += _rows(payload.get("data"))
    if not rows and data:
        rows = [data]
    known = ctx.known_cnics()

    def mkref(r: dict) -> PersonRef:
        return _ref(r.get("name"), r.get("father_name"), r.get("cnic"),
                    [r.get("mobile")], [r.get("address")])

    subject_rows = [r for r in rows if known and cnic13(r.get("cnic")) in known]
    if subject_rows:
        ctx.absorb(mkref(subject_rows[0]))
    elif len(rows) == 1:
        ctx.take(mkref(rows[0]))

    # A number/CNIC on this many PRVS records is a shared line: count it, do not link each.
    if len(rows) > _PRVS_SHARED_MAX and not subject_rows:
        ctx.rec.flags.append("shared_contact")
        ctx.rec.add_field("Shared contact", f"{len(rows)} PRVS records share this number — "
                          "likely a shared/office line; individuals not linked")
        return

    for r in rows:
        ref = mkref(r)
        if r not in subject_rows and not ctx.is_subject(ref):
            ctx.relate(ref, "Same number on a PRVS tenancy record", r.get("district_or_zone"))
        # A tenancy row may also name the property owner / landlord. Only the subject's
        # own row makes that person the subject's landlord; on someone else's row (same
        # number) the landlord is that other person's, kept as a detail.
        owner = _ref(r.get("owner_name"), cnic=r.get("owner_cnic"), phones=[r.get("owner_mobile")])
        if not owner.is_empty and not ctx.is_subject(owner):
            someone_else = bool(ref.cnic or ref.phones) and r not in subject_rows and not ctx.is_subject(ref)
            if not someone_else:
                ctx.relate(owner, "Landlord (PRVS)", r.get("tenant_address") or r.get("address"))
            else:
                ctx.rec.add_field("Landlord on a record sharing this number",
                                  " · ".join(x for x in (owner.name, owner.cnic) if x) or "-")
    ctx.rec.add_field("PRVS records", len(rows))


def _old_tenant(ctx: _Ctx, data: dict, raw: Any) -> None:
    rows: list[dict] = []
    seen: set[str] = set()
    if isinstance(raw, dict):
        # Both branches are searched now (a tenancy can be filed under the mobile with a
        # different CNIC on it), so take rows from each and de-duplicate.
        for branch in ("by_mobile", "by_cnic"):
            payload = raw.get(branch)
            if isinstance(payload, dict) and payload.get("success") is True:
                for row in _rows(payload.get("data")):
                    ident = str(row.get("id") or row.get("tenant_cnic") or row)
                    if ident not in seen:
                        seen.add(ident)
                        rows.append(row)
    for row in rows:
        inner = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        address = ", ".join(
            str(part).strip() for part in (inner.get("property_house_no"), inner.get("property_street_mohalla"),
                                           inner.get("property_address")) if str(part or "").strip()
        )
        # Fields may sit at the row's top level (Shield old_tenant) or nested under
        # ``raw`` (EMS old-db). Read both.
        tenant = _ref(row.get("tenant_name") or inner.get("tenant_name"),
                      row.get("tenant_father_name") or inner.get("tenant_father_name"),
                      row.get("tenant_cnic") or inner.get("tenant_cnic"),
                      [row.get("tenant_mobile"), inner.get("tenant_mobile_number")],
                      [address, inner.get("tenant_permanent_address")])
        owner = _ref(inner.get("owner_name") or row.get("owner_name"),
                     inner.get("owner_father_name") or row.get("owner_father_name"),
                     cnic=inner.get("owner_cnic") or row.get("owner_cnic"),
                     phones=[inner.get("owner_mobile_number"), row.get("owner_mobile_number")])
        where = f"property: {address}" if address else None
        witnesses = []
        for i in (1, 2, 3):
            wname = inner.get(f"tenant_witness_name_{i}") or row.get(f"tenant_witness_name_{i}")
            wcnic = inner.get(f"tenant_witness_cnic_{i}") or row.get(f"tenant_witness_cnic_{i}")
            wphone = inner.get(f"tenant_witness_mobile_{i}") or row.get(f"tenant_witness_mobile_{i}")
            if wname or wcnic:
                witnesses.append(_ref(wname, cnic=wcnic, phones=[wphone]))
        if not (ctx.is_subject(tenant) or ctx.is_subject(owner)) and any(ctx.is_subject(w) for w in witnesses):
            # The subject only witnessed this tenancy: they vouched for the tenant. The
            # landlord is the tenant's relation, not theirs (kept as a detail), and the
            # other witnesses witnessed for the tenant too - co-witnesses, a weak link.
            ctx.absorb(next(w for w in witnesses if ctx.is_subject(w)))
            ctx.relate(tenant, "Tenancy witnessed by the subject", where)
            for w in witnesses:
                if not ctx.is_subject(w):
                    ctx.relate(w, "Co-witness on a tenancy", where)
            if not owner.is_empty:
                ctx.rec.add_field("Landlord of the tenancy witnessed", " · ".join(
                    x for x in (owner.name, owner.cnic, (owner.phones or [None])[0]) if x))
            ctx.rec.add_field(f"Tenancy #{row.get('tenant_id') or len(ctx.rec.fields) + 1}",
                              f"witnessed by the subject · {address or '-'}")
            continue
        if ctx.is_subject(tenant):
            ctx.absorb(tenant)
            ctx.relate(owner, "Landlord", where)
        elif ctx.is_subject(owner):
            ctx.absorb(owner)
            ctx.relate(tenant, "Tenant", where)
        else:
            ctx.relate(owner, "Landlord (tenancy record)", where)
            ctx.relate(tenant, "Tenant (tenancy record)", where)
        # Witnesses named on the tenancy vouched for the tenant. Only when the subject IS
        # the tenant are they the subject's witnesses; otherwise they are not linked here.
        if ctx.is_subject(tenant):
            for w in witnesses:
                ctx.relate(w, "Tenancy witness", where)
        ctx.rec.add_field(f"Tenancy #{row.get('tenant_id') or len(ctx.rec.fields) + 1}", address or "-")


def _hotel_profiles(ctx: _Ctx, profiles: list[dict]) -> None:
    """Hotel Eye ``/api/person``: one profile per person the phone / CNIC found."""
    known = ctx.known_cnics()
    for prof in profiles:
        p = prof.get("person") or {}
        ref = _ref(p.get("name"), p.get("father_name"), p.get("cnic"), [p.get("phone")],
                   [p.get("temporary_address"), p.get("permanent_address")])
        stays = _rows(prof.get("hotel_eye_stays"))
        own = (ref.cnic in known) if (known and ref.cnic) else (ctx.is_subject(ref) or not (ref.cnic or ref.phones))
        if not own:
            # Another person's profile on the searched number / CNIC.
            last = stays[0] if stays else {}
            ctx.relate(ref, "Used same phone/CNIC at hotel check-in",
                       " · ".join(str(x) for x in (last.get("hotel"), last.get("check_in")) if x) or None)
            continue
        ctx.absorb(ref)
        for key, label in (("gender", "Gender"), ("date_of_birth", "Date of birth"), ("passport", "Passport"),
                           ("email", "Email"), ("nationality", "Nationality")):
            if p.get(key):
                ctx.rec.add_field(label, p[key])
                if key in ("passport", "email"):
                    ctx.rec.subject.extra[key] = str(p[key])
        for i, s in enumerate(stays, 1):
            stay = HotelStay(hotel=str(s.get("hotel") or "Unknown hotel"), district=s.get("district") or None,
                             room=str(s.get("room_no") or "") or None, check_in=s.get("check_in") or None,
                             check_out=s.get("check_out") or None)
            ctx.rec.stays.append(stay)
            ctx.rec.add_field(f"Stay {i}", f"{stay.hotel} ({stay.district or '-'}, PS {s.get('police_station') or '-'}) "
                                           f"room {stay.room or '-'} | {stay.check_in or '-'} → {stay.check_out or '-'} | "
                                           f"{s.get('visit_purpose') or ''} | {str(s.get('role') or '').replace('_', ' ')}")
        # People who stayed with them: a shared hotel stay, stated by Hotel Eye.
        for w in _rows(prof.get("stay_with_persons")):
            if not isinstance(w, dict):
                continue
            wref = _ref(w.get("name") or w.get("guest_name"), w.get("father_name"),
                        w.get("cnic") or w.get("cnic_or_passport"), [w.get("phone")])
            detail = " · ".join(str(x) for x in (w.get("hotel"), f"room {w['room_no']}" if w.get("room_no") else None,
                                                  w.get("check_in"), str(w.get("role") or "").replace("_", " ")) if x)
            ctx.relate(wref, "Shared hotel stay", detail or None)
        for link in _rows(prof.get("criminal_links")):
            if not isinstance(link, dict):
                continue
            if link.get("cro_no"):
                ctx.rec.add_field("CRO No. (via Hotel Eye)", link["cro_no"])
            for r in _rows(link.get("records")):
                no, _, year = str(r.get("fir_no") or "").partition("/")
                if no and year:
                    ctx.rec.firs.append(FirKey(fir_no=no, fir_year=year, police_station=str(r.get("police_station") or ""),
                                               offence=r.get("fir_offence"), status=r.get("status"),
                                               role="Accused (CRO via Hotel Eye)"))
            if link.get("records") or link.get("cro_no"):
                for flag in ("criminal_record", "fir_record"):
                    if flag not in ctx.rec.flags:
                        ctx.rec.flags.append(flag)
    ctx.rec.add_field("Hotel stays", len(ctx.rec.stays))


def _hotel_eye(ctx: _Ctx, data: dict, raw: Any) -> None:
    if isinstance(raw, dict) and isinstance(raw.get("profiles"), list):
        _hotel_profiles(ctx, raw["profiles"])
        return
    records = _rows(raw.get("records")) if isinstance(raw, dict) else []
    originals: list[dict] = []
    if isinstance(raw, dict):
        for branch in ("by_mobile", "by_cnic"):
            payload = raw.get(branch)
            if isinstance(payload, dict):
                originals += _rows(payload.get("data"))
    others: list[tuple[PersonRef, HotelStay]] = []
    for index, row in enumerate(records):
        guest = _ref(row.get("guest_name"), cnic=row.get("guest_cnic"), phones=[row.get("guest_cell")])
        stay = HotelStay(
            hotel=str(row.get("hotel_name") or "Unknown hotel"), district=row.get("district") or None,
            room=str(row.get("room_no") or "") or None, check_in=row.get("check_in") or None,
            check_out=row.get("check_out") or None,
        )
        if ctx.is_subject(guest) or not (guest.cnic or guest.phones):
            ctx.absorb(guest)
            ctx.rec.stays.append(stay)
            original = originals[index] if index < len(originals) else {}
            district = original.get("guest_permanent_district")
            if district:
                ctx.rec.subject.extra.setdefault("permanent_district", str(district))
            ctx.rec.add_field(f"Stay {index + 1}", f"{stay.hotel} ({stay.district or '-'}) room {stay.room or '-'} | {stay.check_in or '-'} → {stay.check_out or '-'} | {row.get('visit_purpose') or ''}")
        else:
            others.append((guest, stay))

    # Someone else on the same booking is not just "the same identifier" - say which:
    # sharing the room is the strongest, then the same hotel on the same day.
    for guest, stay in others:
        same_hotel = [s for s in ctx.rec.stays if name_key(s.hotel) == name_key(stay.hotel)]
        same_room = [s for s in same_hotel if s.room and stay.room and str(s.room) == str(stay.room)]
        same_day = [s for s in same_hotel if s.check_in and stay.check_in and str(s.check_in)[:10] == str(stay.check_in)[:10]]
        if same_room and same_day:
            relation, detail = "Room-mate at hotel", f"{stay.hotel}, room {stay.room}, {stay.check_in or ''}"
        elif same_day:
            relation, detail = "Co-guest (same hotel, same day)", f"{stay.hotel}, {stay.check_in or ''}"
        elif same_hotel:
            relation, detail = "Guest at the same hotel", f"{stay.hotel}, {stay.check_in or ''}"
        else:
            relation, detail = "Used same phone/CNIC at hotel check-in", f"{stay.hotel} {stay.check_in or ''}"
        ctx.relate(guest, relation, detail)


def _sbvs(ctx: _Ctx, data: dict, raw: Any) -> None:
    ref = _ref(data.get("name"), data.get("father_name"), data.get("cnic"), [data.get("phone")], [data.get("address")])
    if data.get("passport"):
        ref.extra["passport"] = str(data["passport"])
    if ctx.is_subject(ref) or not (ref.cnic or ref.phones):
        ctx.absorb(ref)
    else:
        ctx.relate(ref, "Verification record on same identifier")
    for entry in data.get("entries") or []:
        org = clean_text(entry.get("organization_name"))
        if org and org not in ctx.rec.organisations:
            ctx.rec.organisations.append(org)
        ctx.rec.add_field(
            "Verification",
            " | ".join(str(entry.get(k)) for k in ("organization_name", "profession", "purpose", "district", "ps", "status") if entry.get(k)),
        )


def _trust(ctx: _Ctx, data: dict, raw: Any) -> None:
    """Tenant register (comprehensive-info): the tenant, their landlord, their family
    members and any co-tenants of the same property - each with the relation named, not
    left to the generic walker to call "named in property record"."""
    details = data.get("details") if isinstance(data.get("details"), dict) else {}
    payload = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else {}
    person = payload.get("person_details") if isinstance(payload.get("person_details"), dict) else details
    address = clean_text(person.get("present_address") or person.get("address") or person.get("permanent_address"))
    ctx.take(_ref(person.get("name"), person.get("father_name"), person.get("cnic"),
                  [person.get("mobile"), person.get("contact")], [address]))
    where = f"property: {address}" if address else None

    for key, relation in (("owner_details", "Landlord"), ("owner", "Landlord"),
                          ("family_members", "Family member"), ("family", "Family member"),
                          ("tenants", "Co-tenant"), ("tenant_details", "Tenant"),
                          ("witnesses", "Tenancy witness")):
        block = payload.get(key)
        for row in ([block] if isinstance(block, dict) else _rows(block)):
            ref = _ref(row.get("name") or row.get("full_name"), row.get("father_name"), row.get("cnic"),
                       [row.get("mobile"), row.get("contact")],
                       [row.get("address") or row.get("present_address"), address])
            if not (ref.name or ref.cnic or ref.phones):
                continue
            # "Family member (Wife)" when the row says how they are related
            stated = clean_text(row.get("relation") or row.get("relationship"))
            label = f"{relation} ({stated})" if stated and relation == "Family member" else relation
            if ctx.is_subject(ref):
                ctx.absorb(ref)
            else:
                ctx.relate(ref, label, where)
    for label, key in (("Name", "name"), ("CNIC", "cnic"), ("Mobile", "mobile"), ("Address", "present_address")):
        ctx.rec.add_field(label, person.get(key))


def _milap(ctx: _Ctx, data: dict, raw: Any) -> None:
    """Lost persons / property. The reporter is usually the subject; the missing person
    (or the person the property was recovered from) is the link worth drawing."""
    for row in _rows(raw.get("data")) if isinstance(raw, dict) else []:
        reporter = _ref(row.get("reporting_name") or row.get("reporter_name"),
                        row.get("reporting_father_name"),
                        row.get("reporting_cnic"), [row.get("reporting_contact")],
                        [row.get("reporting_address")])
        ctx.take(reporter)
        what = clean_text(row.get("record_type") or row.get("type") or row.get("lost_item") or row.get("item"))
        where = clean_text(row.get("police_station") or row.get("ps_name") or row.get("district"))
        ctx.rec.add_field("MILAP record", " | ".join(
            str(v) for v in (row.get("record_no") or row.get("id"), what, row.get("status"), where) if v))
        for name_key_, cnic_key, relation in (
            ("lost_person_name", "lost_person_cnic", "Missing person"),
            ("missing_person_name", "missing_person_cnic", "Missing person"),
            ("found_person_name", "found_person_cnic", "Found person"),
            ("recovered_from_name", "recovered_from_cnic", "Recovered from"),
        ):
            ref = _ref(row.get(name_key_), None, row.get(cnic_key), [row.get("lost_person_contact")])
            if ref.name or ref.cnic:
                detail = f"MILAP {what}" if what else None
                ctx.relate(ref, relation, detail) if not ctx.is_subject(ref) else ctx.absorb(ref)


def _against_ref(row: Any) -> PersonRef:
    """One person a complaint is against (the API's keys vary by form version)."""
    row = row if isinstance(row, dict) else {"name": row}
    pick = lambda *keys: next((row.get(k) for k in keys if row.get(k)), None)  # noqa: E731
    return _ref(pick("name", "respondent_name", "against_name", "accused_name", "person_name"),
                pick("fathername", "father_name", "respondent_fathername", "against_fathername"),
                pick("cnic", "respondent_cnic", "against_cnic"),
                [pick("phone", "cell", "contact", "mobile", "respondent_phone", "against_phone")],
                [pick("address", "respondent_address", "against_address")])


def _igp_cms(ctx: _Ctx, data: dict, raw: Any) -> None:
    """IGP complaints, both ways: complaints the person filed (the complainant is the
    subject; everyone complained against is linked) and complaints filed against the
    person (the complainant is linked; the subject is found among those complained
    against)."""
    for row in _rows(raw.get("complaints")) if isinstance(raw, dict) else []:
        if not isinstance(row, dict):
            continue
        complainant = _ref(row.get("complainant_name"), row.get("complainant_fathername"),
                           row.get("complainant_cnic"),
                           [row.get("complainant_phone"), row.get("complainant_cell")],
                           [row.get("complainant_address")])
        against = [_against_ref(a) for a in _rows(row.get("complaint_against"))]
        role = str(row.get("cnic_role") or "").lower()
        about = clean_text(row.get("other_subject") or row.get("subject") or row.get("complaint_category"))
        when = str(row.get("created_at") or "")[:10]
        tracking = row.get("tracking_id") or row.get("complaint_no") or row.get("id")
        is_against = "against" in role or (role != "complainant" and any(ctx.is_subject(a) for a in against))
        ctx.rec.add_field("IGP complaint" + (" against the subject" if is_against else " filed"), " | ".join(
            str(v) for v in (tracking, when, row.get("complaint_category"), about, row.get("district_name"),
                             row.get("status")) if v))
        detail = f"IGP complaint {tracking} ({when}): {about}" if about else f"IGP complaint {tracking}"
        if is_against:
            for a in against:
                if ctx.is_subject(a):
                    ctx.absorb(a)
            if not complainant.is_empty:
                ctx.relate(complainant, "Filed a complaint against the subject", detail)
            for a in against:
                if not ctx.is_subject(a) and not a.is_empty:
                    ctx.relate(a, "Complained against alongside the subject", detail)
        else:
            ctx.take(complainant)
            for a in against:
                if not a.is_empty:
                    ctx.relate(a, "Complained against by the subject", detail)


def _employment(ctx: _Ctx, data: dict, raw: Any) -> None:
    ref = _ref(data.get("name"), data.get("father_name"), data.get("cnic"),
               [data.get("contact"), data.get("other_contact")], [data.get("permanent_address")])
    ctx.take(ref)
    if not ctx.is_subject(ref) and (ref.cnic or ref.phones):
        return
    if data.get("designation"):
        ctx.rec.subject.extra.setdefault("designation", str(data["designation"]))
    org_direct = clean_text(data.get("organisation"))
    if org_direct and org_direct not in ctx.rec.organisations:
        ctx.rec.organisations.append(org_direct)
    for row in _rows(raw.get("data")) if isinstance(raw, dict) and isinstance(raw.get("data"), list) else []:
        for key, value in row.items():
            k = snake(key)
            if any(t in k for t in ("company", "employer", "organization", "organisation", "org_name", "department")) and not isinstance(value, (dict, list)):
                org = clean_text(value)
                if org and not any(t in k for t in ("cnic", "mobile", "phone", "contact")) and org not in ctx.rec.organisations:
                    ctx.rec.organisations.append(org)
    for label, key in [("Name", "name"), ("Father", "father_name"), ("CNIC", "cnic"), ("Contact", "contact"),
                       ("Other contact", "other_contact"), ("Address", "permanent_address"), ("Designation", "designation")]:
        ctx.rec.add_field(label, data.get(key))
    for org in ctx.rec.organisations:
        ctx.rec.add_field("Organisation", org)


def _hrmis(ctx: _Ctx, data: dict, raw: Any) -> None:
    officer = _ref(data.get("officer_name"), cnic=data.get("officer_cnic"),
                   phones=[data.get("officer_phone")], addresses=[data.get("officer_address")])
    ctx.take(officer, " | ".join(str(data.get(k)) for k in ("rank", "police_station_name") if data.get(k)) or None)
    if not ctx.is_subject(officer) and (officer.cnic or officer.phones):
        return
    ctx.rec.flags.append("police_officer")
    ctx.rec.police_station = clean_text(data.get("police_station_name"))
    for label, key in [("Rank", "rank"), ("Belt No.", "officer_belt_no"), ("Current posting", "current_posting"),
                       ("Police station", "police_station_name"), ("District", "district"), ("Date of birth", "date_of_birth")]:
        ctx.rec.add_field(label, data.get(key))


def _dls(ctx: _Ctx, data: dict, raw: Any) -> None:
    rows = _rows(raw.get("data")) if isinstance(raw, dict) else []
    father = next((r.get("fathername") or r.get("father_name") for r in rows if r.get("fathername") or r.get("father_name")), None)
    name = " ".join(str(data.get(k) or "").strip() for k in ("firstname", "lastname")).strip()
    holder = _ref(name, father, data.get("cnic"), [data.get("phone")], [data.get("address")])
    ctx.take(holder, f"licence {(data.get('licenses') or [{}])[0].get('license_no') or ''}".strip())
    # Someone else's licence: its photo is theirs, not the subject's.
    if not ctx.is_subject(holder) and (holder.cnic or holder.phones) and ctx.rec.related:
        ctx.rec.related[-1].ref.extra["_owns_images"] = "1"
    ctx.rec.add_field("Name", name)
    ctx.rec.add_field("Father", father)
    ctx.rec.add_field("CNIC", data.get("cnic"))
    ctx.rec.add_field("Address", data.get("address"))
    for lic in data.get("licenses") or []:
        ctx.rec.add_field(
            f"Licence {lic.get('license_no') or ''}".strip(),
            " | ".join(str(lic.get(k)) for k in ("category", "license_type", "issued_office", "expiry_date", "status") if lic.get(k)),
        )


def _tracs(ctx: _Ctx, data: dict, raw: Any) -> None:
    payload = raw.get("data") if isinstance(raw, dict) else raw
    challans: list[dict] = []
    if isinstance(payload, dict):
        for key in ("challans", "oldChallans", "old_challans"):
            challans += _rows(payload.get(key))
    else:
        challans = _rows(payload)
    for challan in challans:
        plate = clean_text(challan.get("vehicleNumPlate") or challan.get("vehicle_no") or challan.get("registration_no"))
        if plate and plate not in ctx.rec.vehicles:
            ctx.rec.vehicles.append(plate)
        ctx.rec.add_field(
            f"Challan {challan.get('challanNumber') or challan.get('challan_no') or ''}".strip(),
            f"vehicle {plate or '-'} | owner {challan.get('ownerName') or '-'}",
        )


def _arms(ctx: _Ctx, data: dict, raw: Any) -> None:
    """ARMS profile: nested main / bioData / addresses / firs. Owner is the subject."""
    rows = data.get("records") or _rows(raw.get("data")) if isinstance(raw, dict) else data.get("records") or []
    if not rows:
        return
    rec = rows[0]
    main = rec.get("main", rec) if isinstance(rec, dict) else {}
    addresses = [a.get("detailed_address") for a in _rows(rec.get("addresses")) if a.get("detailed_address")]
    ctx.take(_ref(main.get("full_name"), main.get("father_name"), main.get("cnic"),
                  [main.get("contact_number_1"), main.get("contact_number_2")], addresses))
    ctx.rec.flags.append("arms_record")
    bio = rec.get("bioData", {}) if isinstance(rec, dict) else {}
    for label, value in (("Name", main.get("full_name")), ("Father", main.get("father_name")),
                         ("District", main.get("district_name")), ("Status", main.get("current_status")),
                         ("Crime category", bio.get("crime_category_name")), ("Cast", bio.get("cast_name"))):
        ctx.rec.add_field(label, value)
    for fir in _rows(rec.get("firs")):
        if fir.get("fir_number") and fir.get("fir_year"):
            key = FirKey(fir_no=str(fir["fir_number"]), fir_year=str(fir["fir_year"]),
                         police_station=str(fir.get("ps_name") or ""), offence=fir.get("fir_offence"), role="Accused (ARMS)")
            ctx.rec.firs.append(key)
            ctx.rec.add_field(f"FIR {key.fir_no}/{key.fir_year}", f"{key.police_station} | {key.offence or '-'}")
    if ctx.rec.firs:
        ctx.rec.flags.append("fir_record")


def _vehicles(ctx: _Ctx, data: dict, raw: Any) -> None:
    """Excise / AVLC: the owner is the subject; co-owner, complainant and driver are
    related; plates are attributes. FIR / crime → flag."""
    rows = data.get("vehicles") or (_rows(raw.get("data")) if isinstance(raw, dict) else [])
    for v in rows:
        owner = _ref(v.get("owner_name") or v.get("ComplainantName") if False else v.get("owner_name"),
                     v.get("owner_father_name"), v.get("owner_cnic") or v.get("CNIC") or v.get("ownerCNIC"),
                     [v.get("owner_mobile") or v.get("MobileNo") or v.get("ownerMobile")],
                     [v.get("owner_address")])
        if not owner.is_empty:
            ctx.take(owner)
        plate = clean_text(v.get("registration_number") or v.get("RegNo") or v.get("vehicleNumPlate"))
        if plate and plate not in ctx.rec.vehicles:
            ctx.rec.vehicles.append(plate)
        if v.get("Crime") or v.get("FirNo") or v.get("crime"):
            ctx.rec.flags.append("stolen_vehicle" if ctx.rec.system == "avlc" else "fir_record")
        complainant = _ref(v.get("ComplainantName"), cnic=v.get("complainant_cnic"))
        if not complainant.is_empty:
            ctx.relate(complainant, "Complainant (vehicle case)",
                       f"{v.get('Crime') or ''} {v.get('RegNo') or ''}".strip() or None)
        details = " | ".join(str(v.get(k)) for k in ("make", "model", "manufacturer", "Make", "Model", "color", "Color", "Crime", "PoliceStation") if v.get(k))
        ctx.rec.add_field(f"Vehicle {plate or '-'}", details or "-")


def _generic(ctx: _Ctx, data: dict, raw: Any) -> None:
    details = data.get("details") if isinstance(data.get("details"), dict) else None
    if details:
        for key, value in details.items():
            ctx.rec.add_field(str(key).replace("_", " ").title(), value)
    else:
        _flat_fields(ctx.rec, raw)


def _caller_id(ctx: _Ctx, data: dict, raw: Any) -> None:
    # Crowd-sourced name tags: aliases for the subject, never an identity.
    for alias in data.get("aliases") or []:
        alias = clean_name(alias)
        if alias:
            aliases = ctx.rec.subject.extra.get("aliases", "")
            if alias.lower() not in aliases.lower():
                ctx.rec.subject.extra["aliases"] = f"{aliases}; {alias}".strip("; ")
    ctx.rec.add_field("Primary tag", data.get("primary_name"))
    ctx.rec.add_field("All tags", ", ".join(data.get("aliases") or []))
    if data.get("spam"):
        ctx.rec.flags.append("spam_reported")
        ctx.rec.add_field("Spam", "Reported as spam")
    ctx.rec.add_field("Facebook", data.get("facebook_link") or data.get("facebook_id"))


# -- FIR roster ------------------------------------------------------------------------

_ROSTER_TABLES = {
    "nominated_suspects": "Co-accused (nominated)",
    "witnesses": "Witness",
    "investigating_officers": "Investigating officer",
    "case_positions": "Named",
}
_URDU_NAME = ("نام", "name")
_URDU_FATHER = ("والد", "ولد", "ولدیت", "father")
_URDU_ADDRESS = ("پتہ", "سکونت", "ساکن", "address")
_URDU_COMPLAINANT = ("مستغیث", "اطلاع دہندہ", "complainant")
_RESIDENT_SPLIT = re.compile(r"\b(?:r/o|resident of)\b|ساکن", re.IGNORECASE)


_CNIC_IN_TEXT = re.compile(r"\d{5}-?\d{7}-?\d")
_PHONE_IN_TEXT = re.compile(r"(?:\+?92|0)3\d{2}[\s-]?\d{7}")
_LABEL_NOISE = re.compile(r"\b(?:cnic|nic|cell|mobile|phone|contact|no\.?)\s*[:#]?\s*$", re.IGNORECASE)


def _roster_person(cells: dict[str, str]) -> PersonRef | None:
    """One FIR table row -> a person. Headers are Urdu and not guaranteed, so a CNIC or
    phone is found by shape wherever it sits, and removed before the rest of the cell
    is read as a name or address."""
    name = father = cnic = None
    phones: list[str] = []
    addresses: list[str] = []
    leftovers: list[str] = []
    for header, value in cells.items():
        text = str(value or "").strip()
        if not text:
            continue
        if match := _CNIC_IN_TEXT.search(text):
            cnic = cnic or cnic13(match.group(0))
            text = text.replace(match.group(0), " ")
        for match in _PHONE_IN_TEXT.findall(text):
            if (phone := mobile11(match)) and phone not in phones:
                phones.append(phone)
            text = text.replace(match, " ")
        text = _LABEL_NOISE.sub("", re.sub(r"\s+", " ", text)).strip(" ,;:-")
        if not text:
            continue
        header_l = str(header).lower()
        if any(t in header_l for t in _URDU_FATHER):
            father = father or clean_name(text)
        elif any(t in header_l for t in _URDU_ADDRESS):
            addresses.append(text)
        elif any(t in header_l for t in _URDU_NAME) and not name:
            name = text
        else:
            leftovers.append(text)
    if not name:
        # Headers were guessed ("Detail 1"): the first wordy, short cell is the name.
        name = next((v for v in leftovers if re.search(r"[A-Za-z؀-ۿ]{3}", v) and len(v) <= 80), None)
    if name:
        parts = _RESIDENT_SPLIT.split(name, maxsplit=1)
        if len(parts) == 2:
            name = parts[0]
            addresses.append(parts[1])
    ref = _ref(name, father, cnic, phones, addresses)
    return None if ref.is_empty else ref


_DOC_ROLES = {
    "complainant": "Complainant", "nominated_suspects": "Accused", "arrested_suspects": "Accused",
    "witnesses": "Witness", "guarantors": "Guarantor", "investigating_officers": "Investigating officer",
}


def _fir_document(ctx: _Ctx, doc: dict) -> None:
    """A FIR file report read by sherlocks.evidence.fir_document: people by role."""
    label = f"{doc.get('fir_no')}/{doc.get('fir_year')}"
    # The subject's own role; a complainant also listed as a witness is the complainant.
    held = {_DOC_ROLES.get(key) for key, people in (doc.get("roster") or {}).items() for p in people
            if ctx.is_subject(_ref(p.get("name"), p.get("father"), p.get("cnic"), [p.get("phone")]))}
    mine = next((r for r in ("Complainant", "Accused", "Witness", "Guarantor", "Investigating officer") if r in held), None)
    for key, people in (doc.get("roster") or {}).items():
        role = _DOC_ROLES.get(key, "Named")
        if role in ("Witness", "Guarantor") and mine in ("Witness", "Guarantor"):
            role = "Co-witness"   # witnesses of the same case: a weak link, not a statement
        elif role == "Accused" and mine == "Complainant":
            role = "Accused by the subject"     # he filed this FIR against them - not co-accused
        elif role == "Complainant" and mine == "Accused":
            role = "Complainant against the subject"
        for p in people:
            ref = _ref(p.get("name"), p.get("father"), p.get("cnic"), [p.get("phone")], [p.get("address")])
            if ref.is_empty:
                continue
            if ctx.is_subject(ref):
                ctx.absorb(ref)
                continue
            detail = " · ".join(x for x in (
                "nominated" if key == "nominated_suspects" else "", p.get("rank") or "",
                p.get("occupation") or "", p.get("date") or "") if x)
            ctx.relate(ref, f"{role} in FIR {label}", detail or None)
    ctx.rec.add_field("FIR", f"{label} · PS {doc.get('police_station') or doc.get('ps_id')}")
    for field_label, key in (("Sections", "sections"), ("Offence", "offence"), ("Occurred", "occurred"),
                             ("Place", "place"), ("Result", "investigation_result")):
        if doc.get(key):
            ctx.rec.add_field(field_label, doc[key])
    if doc.get("case_positions"):
        last = doc["case_positions"][-1]
        ctx.rec.add_field("Case position", " · ".join(x for x in (last.get("position"), last.get("date")) if x))
    if doc.get("narrative"):
        ctx.rec.add_field("Narrative", str(doc["narrative"])[:300])
    ctx.rec.add_field("Case diaries", len(doc.get("case_diaries") or []))
    ctx.rec.add_field("People named", len(ctx.rec.related))


def _fir_roster(ctx: _Ctx, data: dict, raw: Any) -> None:
    if not isinstance(raw, dict):
        return
    if "roster" in raw:
        _fir_document(ctx, raw)
        return
    fir_label = raw.get("fir_label") or "FIR"
    for table, relation in _ROSTER_TABLES.items():
        for row in _rows(raw.get(table)):
            ref = _roster_person({str(k): str(v) for k, v in row.items()})
            if not ref:
                continue
            if ctx.is_subject(ref):
                ctx.absorb(ref)
            else:
                ctx.relate(ref, f"{relation} in {fir_label}")
    for section in _rows(raw.get("fir_sections")):
        for pair in _rows(section.get("pairs")):
            label = str(pair.get("label") or "")
            if any(t in label.lower() for t in _URDU_COMPLAINANT):
                ref = _roster_person({"name": str(pair.get("value") or "")})
                if ref:
                    ctx.relate(ref, f"Complainant in {fir_label}")
    ctx.rec.add_field("FIR", fir_label)
    if raw.get("sections_of_law"):
        ctx.rec.add_field("Sections", raw.get("sections_of_law"))
    if raw.get("main_narrative"):
        ctx.rec.add_field("Narrative", str(raw["main_narrative"])[:300])
    ctx.rec.add_field("People named", len(ctx.rec.related))


_HANDLERS: dict[str, Callable[[_Ctx, dict, Any], None]] = {
    "simsdb": _simsdb,
    "subscriber": _subscriber,
    "cro": _cro,
    "psrms": _psrms,
    "watchlist": _watchlist,
    "prvs": _prvs,
    "old_tenant": _old_tenant,
    "hotel_eye": _hotel_eye,
    "sbvs": _sbvs,
    "trust": _trust,
    "milap": _milap,
    "igp_cms": _igp_cms,
    "evs": _employment,
    "hope": _employment,
    "hrmis": _hrmis,
    "dls": _dls,
    "tracs": _tracs,
    "arms": _arms,
    "excise": _vehicles,
    "avlc": _vehicles,
    "caller_id": _caller_id,
    "fir_roster": _fir_roster,
}

# Systems whose handler already reads every row of the payload. Running the generic
# walker on them too would re-find the same people under less precise relations.
_HANDLER_IS_COMPLETE = {"simsdb", "subscriber", "psrms", "prvs", "old_tenant", "hotel_eye", "caller_id", "fir_roster",
                        "igp_cms"}


def _sweep(ctx: _Ctx, system: str, raw: Any, cap: int) -> None:
    for found in find_people(raw):
        if len(ctx.rec.related) >= cap:
            return
        ref = found.ref
        if ctx.is_subject(ref):
            ctx.absorb(ref)
        elif found.is_self_prefix and not found.container_role and not (ref.cnic or ref.phones):
            if ctx.name_compatible(ref):
                ctx.absorb(ref)
        elif found.is_self_prefix and not found.container_role and not ctx.known_cnics() and not ctx.known_phones():
            ctx.absorb(ref)
        else:
            ctx.relate(ref, _relation_for(system, found), _detail(found.context))


def extract_record(
    system: str,
    result: dict[str, Any],
    query: PersonRef,
    images: ImageStore,
    *,
    related_cap: int = 25,
) -> SystemRecord:
    rec = SystemRecord(
        system=system,
        status=str(result.get("status") or "error"),
        hit=bool(result.get("hit")),
        summary=str(result.get("summary") or ""),
        errors=[str(e) for e in (result.get("errors") or [])],
        cached=bool(result.get("_cached")),
    )
    if not rec.hit:
        return rec

    image_refs, raw = extract_images(result.get("raw"), images)
    rec.raw = raw
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    ctx = _Ctx(rec=rec, query=query)

    _HANDLERS.get(system, _generic)(ctx, data, raw)
    if system not in _HANDLER_IS_COMPLETE:
        _sweep(ctx, system, raw, related_cap)

    # A photograph in a record about this person is a photograph of this person - unless
    # the record turned out to be someone else's (a licence registered on a number the
    # subject shares), in which case it is theirs. The FIR roster is a record about a
    # case, so its images are not attributed at all.
    owner = next((r.ref for r in rec.related if r.ref.extra.pop("_owns_images", None)), None)
    if system != "fir_roster":
        target = owner or rec.subject
        for image in image_refs:
            if image not in target.images:
                target.images.append(image)
    rec.related = rec.related[:related_cap]
    rec.flags = list(dict.fromkeys(rec.flags))
    return rec
