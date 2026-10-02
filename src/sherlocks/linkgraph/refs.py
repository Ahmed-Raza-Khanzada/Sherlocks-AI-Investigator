"""Find every person mentioned anywhere in an upstream payload.

Most of the upstream systems are untyped in cdr_report_app (TRUST, PRVS, MILAP, PFC,
IGP CMS, CFMS, Watchlist keep only a flat summary), and even the typed ones keep just
the first row. The people *other* than the subject - the landlord in a tenancy row,
the vehicle owner on a challan, the complainant in a lost-property report - are only
in the raw JSON. This walker recovers them.

It works on field names, because field names are the one stable thing across these
systems: ``owner_name`` / ``owner_cnic`` / ``owner_mobile_number`` share the prefix
``owner``, so they describe one person whose role is "owner". ``ownerCNIC`` is folded
to ``owner_cnic`` first. A prefix group becomes a person only if it carries a CNIC or
a mobile, or a name plus a father's name or address - a lone name is not enough to
tell two people apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sherlocks.linkgraph.models import PersonRef
from sherlocks.linkgraph.normalize import (
    clean_address,
    clean_name,
    cnic13,
    mobile11,
    split_relation,
)

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Name-ish keys that are not a person's name.
_NOT_A_PERSON_NAME = (
    "father", "husband", "guardian", "hotel", "station", "district", "org", "company",
    "city", "user_name", "username", "file", "model", "brand", "device", "vehicle",
    "type", "category", "purpose", "profession", "designation", "rank", "status",
    "office", "bank", "zone", "province", "area", "tehsil", "shop", "business", "firm",
    "department", "dept", "school", "college", "project", "ps_", "p_s",
    "police", "circle", "division", "region", "unit", "branch", "section", "posting",
    "offence", "crime", "property", "street", "road", "colony", "block", "sector",
    "country", "nationality", "caste", "tribe", "religion", "network", "operator",
    "relation", "display", "product", "item", "title", "app", "service", "fir",
)
_NAME_SUFFIXES = ("full_name", "fullname", "first_name", "firstname", "last_name", "lastname", "name")
_FATHER_TOKENS = ("father_name", "fathername", "father", "f_name", "guardian_name", "husband_name")
_CNIC_TOKENS = ("cnic_no", "cnic_number", "cnicno", "cnic", "nic", "id_card", "idcard")
_PHONE_TOKENS = (
    "mobile_number", "mobile_no", "phone_number", "phone_no", "cell_no", "contact_no",
    "contact_number", "other_contact", "mobile", "phone", "cell", "contact", "msisdn",
    "number",
)
_ADDRESS_TOKENS = ("permanent_address", "perm_address", "present_address", "address", "addr")

# Prefixes that mean "the record's own subject" rather than a named relation.
SELF_PREFIXES = {"", "person", "cro", "ofc", "nadra", "applicant", "subject", "user", "guest", "suspect_info", "data", "details"}

# Container keys that name the relationship of every person listed under them.
_CONTAINER_ROLES = {
    "tenants": "tenant", "tenacies": "tenancy", "tenancies": "tenancy",
    "properties_owned": "property owned", "owners": "owner", "landlords": "landlord",
    "nominated_suspects": "co-accused", "accused": "co-accused", "suspects": "suspect",
    "witnesses": "witness", "complainants": "complainant", "victims": "victim",
    "family": "family", "family_members": "family", "members": "member",
    "employees": "employee", "employers": "employer", "references": "reference",
    "guarantors": "guarantor", "associates": "associate", "relatives": "relative",
    "investigating_officers": "investigating officer", "guests": "co-guest",
    "sims": "sim", "challans": "challan", "old_challans": "challan", "oldchallans": "challan",
}


@dataclass(slots=True)
class FoundRef:
    ref: PersonRef
    prefix: str
    path: str
    container_role: str | None = None
    context: dict[str, str] = field(default_factory=dict)

    @property
    def is_self_prefix(self) -> bool:
        return self.prefix in SELF_PREFIXES

    @property
    def role(self) -> str | None:
        if not self.is_self_prefix:
            return self.prefix.replace("_", " ")
        return self.container_role


def snake(key: str) -> str:
    key = _CAMEL.sub("_", str(key)).lower()
    key = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
    return key.replace("c_n_i_c", "cnic")


def _split(key: str) -> tuple[str, str] | None:
    """``owner_mobile_number`` -> ``("owner", "phone")``. ``None`` if not a person field."""
    for token in _FATHER_TOKENS:
        if key == token or key.endswith("_" + token):
            return key[: -len(token)].rstrip("_"), "father"
    for token in _CNIC_TOKENS:
        if key == token or key.endswith("_" + token):
            return key[: -len(token)].rstrip("_"), "cnic"
    for token in _PHONE_TOKENS:
        if key == token or key.endswith("_" + token):
            prefix = key[: -len(token)].rstrip("_")
            # license_number, fir_number, challan_number, room_number ... are not phones.
            if token == "number" and prefix and prefix not in {"mobile", "phone", "cell", "contact"} and not prefix.endswith(("owner", "tenant", "guest", "person")):
                return None
            return prefix, "phone"
    for token in _ADDRESS_TOKENS:
        if key == token or key.endswith("_" + token):
            prefix = key[: -len(token)].rstrip("_")
            if prefix in {"permanent", "perm", "present", "current", "temporary", "mailing"}:
                prefix = ""
            return prefix, "address"
    for token in _NAME_SUFFIXES:
        if key == token or key.endswith("_" + token):
            prefix = key[: -len(token)].rstrip("_")
            if any(bad in key for bad in _NOT_A_PERSON_NAME):
                return None
            part = "first" if "first" in token else "last" if "last" in token else "name"
            return prefix, part
    return None


def _group_dict(obj: dict[str, Any]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for raw_key, value in obj.items():
        if isinstance(value, (dict, list)):
            continue
        key = snake(raw_key)
        split = _split(key)
        if not split:
            continue
        prefix, part = split
        bucket = groups.setdefault(prefix, {})
        if part == "phone":
            bucket.setdefault("phones", []).append(value)
        elif part == "address":
            bucket.setdefault("addresses", []).append(value)
        else:
            bucket.setdefault(part, value)
    return groups


def _to_ref(bucket: dict[str, Any]) -> PersonRef | None:
    name = bucket.get("name")
    if not name and (bucket.get("first") or bucket.get("last")):
        name = " ".join(str(bucket.get(p) or "").strip() for p in ("first", "last")).strip()
    name, father_from_name = split_relation(clean_name(name))
    father = clean_name(bucket.get("father")) or father_from_name
    cnic = cnic13(bucket.get("cnic"))
    phones: list[str] = []
    for value in bucket.get("phones", []):
        phone = mobile11(value)
        if phone and phone not in phones:
            phones.append(phone)
    addresses = [a for a in (clean_address(v) for v in bucket.get("addresses", [])) if a]
    if not (cnic or phones or (name and (father or addresses))):
        return None
    return PersonRef(name=name, father_name=father, cnic=cnic, phones=phones, addresses=addresses)


def _context(obj: dict[str, Any]) -> dict[str, str]:
    """Scalar, non-person fields beside the person - FIR number, hotel, vehicle..."""
    out: dict[str, str] = {}
    for raw_key, value in obj.items():
        if isinstance(value, (dict, list)) or value in (None, ""):
            continue
        key = snake(raw_key)
        if _split(key):
            continue
        text = str(value).strip()
        if text and len(text) <= 200:
            out[key] = text
    return out


def find_people(raw: Any, *, max_refs: int = 500) -> list[FoundRef]:
    found: list[FoundRef] = []

    def walk(value: Any, path: str, container_role: str | None, depth: int) -> None:
        if depth > 8 or len(found) >= max_refs:
            return
        if isinstance(value, dict):
            groups = _group_dict(value)
            context = _context(value) if groups else {}
            for prefix, bucket in groups.items():
                ref = _to_ref(bucket)
                if ref:
                    found.append(FoundRef(ref, prefix, path, container_role, context))
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    role = _CONTAINER_ROLES.get(snake(key), container_role)
                    walk(item, f"{path}.{key}" if path else str(key), role, depth + 1)
        elif isinstance(value, list):
            for index, item in enumerate(value[:max_refs]):
                walk(item, f"{path}[{index}]", container_role, depth + 1)

    walk(raw, "", None, 0)
    return found
