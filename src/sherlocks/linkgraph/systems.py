"""The systems a person is searched in, and how each one is presented.

The names are cdr_report_app's provider keys so results line up with its adapters one
to one. ``category`` drives node colour in the portal; ``needs`` says which identifier
the system can be queried with, which decides whether a CNIC-only person needs a
phone-resolving pass before it can be searched there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Category = Literal[
    "identity", "criminal", "property", "employment", "travel", "traffic", "police",
    "complaint", "osint",
]


@dataclass(frozen=True, slots=True)
class SystemInfo:
    key: str
    label: str
    description: str
    category: Category
    needs: Literal["cnic", "phone", "either"]


SYSTEMS: dict[str, SystemInfo] = {
    info.key: info
    for info in [
        SystemInfo("simsdb", "SIMs Database", "Owner CNIC and every SIM registered on it", "identity", "phone"),
        SystemInfo("subscriber", "Telecom Subscriber", "SIM ownership (data to 2020)", "identity", "either"),
        SystemInfo("cro", "CRO", "Criminal Record Office", "criminal", "cnic"),
        SystemInfo("arms", "ARMS", "Arms-licence criminal profile", "criminal", "cnic"),
        SystemInfo("nadra", "NADRA", "Citizen identity (name, father, DOB, address, photo)", "identity", "cnic"),
        SystemInfo("psrms", "PSRMS", "FIR person search", "criminal", "either"),
        SystemInfo("watchlist", "Watchlist", "Suspect watchlist", "criminal", "cnic"),
        SystemInfo("prvs", "PRVS", "Tenant / rental verification", "property", "either"),
        SystemInfo("old_tenant", "Old Tenant", "Legacy tenancy register", "property", "either"),
        SystemInfo("trust", "TRUST", "Property and tenancy", "property", "either"),
        SystemInfo("hotel_eye", "Hotel Eye", "Hotel guest registrations", "travel", "either"),
        SystemInfo("sbvs", "SBVS", "Servant / background verification", "employment", "either"),
        SystemInfo("evs", "EVS", "Employee verification", "employment", "cnic"),
        SystemInfo("hope", "HOPE", "Employee registry", "employment", "cnic"),
        SystemInfo("hrmis", "HRMIS", "Police officer directory", "police", "either"),
        SystemInfo("dls", "DLS", "Driving licences", "traffic", "either"),
        SystemInfo("tracs", "TRACS", "Traffic challans", "traffic", "cnic"),
        SystemInfo("excise", "Excise Vehicle", "Registered vehicles (owner)", "traffic", "cnic"),
        SystemInfo("avlc", "AVLC", "Stolen / recovered vehicles", "traffic", "either"),
        SystemInfo("cfms", "CFMS", "Foreigner management", "identity", "cnic"),
        SystemInfo("pfc", "PFC", "Police facilitation complaints", "complaint", "either"),
        SystemInfo("igp_cms", "IGP CMS", "IGP complaint management", "complaint", "either"),
        SystemInfo("milap", "MILAP", "Lost persons / property", "complaint", "either"),
        SystemInfo("fir_roster", "FIR Roster", "People named in an FIR (PSRMS report)", "criminal", "either"),
        SystemInfo("caller_id", "Caller ID", "Name tags for a number", "osint", "phone"),
        SystemInfo("osint", "OSINT", "Open-source footprint (unverified)", "osint", "either"),
    ]
}

# Queried for every searched person, in this order. Identity systems first: the CNIC
# they resolve is what the CNIC-only systems are then keyed on.
DEFAULT_SYSTEMS: list[str] = [
    "simsdb", "subscriber", "cro", "psrms", "watchlist", "prvs", "old_tenant", "trust",
    "hotel_eye", "sbvs", "evs", "hope", "hrmis", "dls", "tracs", "cfms", "pfc",
    "igp_cms", "milap",
]

IDENTITY_SYSTEMS = ("simsdb", "subscriber")


def system_label(key: str) -> str:
    info = SYSTEMS.get(key)
    return info.label if info else key.replace("_", " ").upper()


# What each system's answer tells an investigator - what the record is, what it proves, and
# how a link that comes from it should be read. Taken from the shape of each API's response
# (``extractors.py`` reads them). Given to the agents with every record and link, so a
# connection is explained by what the record actually is.
MEANING: dict[str, str] = {
    "nadra": "The citizen register. Gives the person's own identity: name, father's name, date of birth, address, photo. "
             "The most reliable source for a name.",
    "simsdb": "The SIM register by CNIC: every SIM registered on that CNIC. A number someone USES may be registered on "
              "another person's CNIC ('registered owner of SIM') - common, and not suspicious by itself.",
    "subscriber": "The telecom subscriber record (data to 2020): the name, CNIC and address a SIM was registered with, "
                  "and its activation date. The registered owner is often not the person using the number, so its "
                  "name is the least reliable name for a person. 'Registered owner of SIM' links the user of a number "
                  "to whoever it is registered to.",
    "caller_id": "Crowd-sourced name tags for a phone number. Unverified: an alias, never an identity.",
    "psrms": "The FIR register. Each row is one FIR naming the person, with their ROLE in it: complainant, accused, "
             "witness or victim. Links from it say who is what to whom in that FIR: co-accused, accused by the "
             "complainant, complainant against the accused, witness, co-witness. Only 'accused' is a charge.",
    "fir_roster": "The FIR's own file: sections, date and place of occurrence, the narrative, the people named "
                  "(nominated accused, witnesses, investigating officer), case position, case diaries and result.",
    "cro": "The Criminal Record Office: a CRO number and the FIRs in which the person was an accused. A past record - "
           "background, not evidence about a new incident.",
    "arms": "The arms / criminal profile register: the person's profile, crime category and the FIRs against them.",
    "watchlist": "The suspect watchlist: the person is on a police watchlist.",
    "cfms": "The foreigners register: the person is recorded as a foreign national.",
    "prvs": "Tenant and rental verification (police verification of tenants). Gives the person's profile (email, passport, "
            "address) and their verification cases - as APPLICANT (the tenant being verified) or as WITNESS (they vouched "
            "for another tenant). 'Verification witness' means that person vouched for the applicant. Two witnesses of "
            "one case are co-witnesses: they vouched for the same person, which does not mean they live together.",
    "old_tenant": "The old tenancy register. Each row is one rental: the TENANT, the OWNER (landlord), the property "
                  "address, and up to three WITNESSES who vouched for the tenant. 'Landlord' / 'Tenant' is a rental "
                  "between the two; 'tenancy witnessed by' means the person signed as a witness for that tenant; two "
                  "witnesses of the same tenancy are co-witnesses, not people at the same address.",
    "trust": "The tenant register (TRUST), searched by a person's CNIC or mobile. It answers with every TENANCY the "
             "person is on, by the part they have in it: as the TENANT (the property address, the landlord's name, "
             "the lease dates, the police station that verified it, the TWO WITNESSES who signed the tenancy form, and "
             "the family members living there); as a WITNESS of someone else's tenancy; as a FAMILY MEMBER living in "
             "someone's rented home; or as the OWNER of a rented property. A 'tenancy witness' signed the tenancy "
             "form to vouch for the tenant - an acquaintance, guarantor or neighbour; it does not mean they live "
             "there or own anything. The register's 'relation' field next to a name is that person's FATHER'S NAME "
             "(s/o), not how they are related. 'Family member' is declared on the form; 'co-tenant' means they rent "
             "the same property.",
    "hotel_eye": "Hotel guest registrations. Gives each STAY of the person: hotel, district, police station, room, "
                 "check-in and check-out, purpose of visit - and the people who stayed with them. 'Shared hotel stay' "
                 "(or room-mate) means both were registered on the same stay; 'used the same phone / CNIC at check-in' "
                 "means another guest gave the same number or CNIC, not that they were together.",
    "sbvs": "Servant and background verification: where the person works or serves (organisation, profession), verified "
            "by the police.",
    "evs": "Employee verification: the person's employer and designation.",
    "hope": "The employee registry: the person's employer and designation.",
    "hrmis": "The police officer directory: the person is a serving police officer - rank, belt number, posting, police "
             "station. A police officer on a graph is usually the investigating officer of an FIR, not a suspect.",
    "dls": "Driving licences: name, father's name, CNIC, address, licence numbers and the licence photo.",
    "tracs": "Traffic challans: the vehicles the person was challaned on, and the owner named on each challan (who may be "
             "someone else: the person was driving another's vehicle).",
    "excise": "The vehicle register: vehicles registered in the person's name (make, model, number).",
    "avlc": "Stolen and recovered vehicles: a vehicle case, with its complainant. 'Complainant (vehicle case)' is the "
            "person who reported that vehicle.",
    "pfc": "Police facilitation complaints the person filed.",
    "igp_cms": "IGP complaints, both ways: complaints the person FILED (those complained against are linked) and "
               "complaints filed AGAINST the person (the complainant is linked).",
    "milap": "Lost persons and property: reports the person made, with the missing or found person named.",
    "osint": "Open-source footprint from the web. Unverified.",
    "identity": "Our own matching: the same phone number or CNIC appears in the records of both people.",
}


def meaning(key: str) -> str:
    """What that system's record tells (empty for a system not described)."""
    return MEANING.get(key, "")


def meanings(keys: Any) -> dict[str, str]:
    """``{label: what it tells}`` for the systems named - by key or by label."""
    by_label = {info.label.lower(): k for k, info in SYSTEMS.items()}
    out: dict[str, str] = {}
    for raw in keys or []:
        k = str(raw or "").strip().lower()
        k = k if k in MEANING else by_label.get(k, k.replace(" ", "_"))
        if k in MEANING:
            out[system_label(k) if k in SYSTEMS else k.title()] = MEANING[k]
    return out
