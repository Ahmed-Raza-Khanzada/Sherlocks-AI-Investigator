"""The systems a person is searched in, and how each one is presented.

The names are cdr_report_app's provider keys so results line up with its adapters one
to one. ``category`` drives node colour in the portal; ``needs`` says which identifier
the system can be queried with, which decides whether a CNIC-only person needs a
phone-resolving pass before it can be searched there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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
