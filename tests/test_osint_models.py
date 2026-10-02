from __future__ import annotations

import pytest

from sherlocks.osint.models import IdentifierKind, OsintSubject


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("03001234567", "+923001234567"),
        ("0300-1234567", "+923001234567"),
        ("+92 300 1234567", "+923001234567"),
        ("923001234567", "+923001234567"),
        ("3001234567", "+923001234567"),
    ],
)
def test_pk_mobile_variants_normalise_to_one_e164_value(raw: str, expected: str) -> None:
    assert OsintSubject(phone=raw).phone == expected


def test_cnic_is_stored_as_digits_only() -> None:
    assert OsintSubject(cnic="42101-1234567-1").cnic == "4210112345671"


def test_empty_subject_is_recognised() -> None:
    assert OsintSubject().is_empty()
    assert not OsintSubject(cnic="4210112345671").is_empty()


def test_available_kinds_reflects_populated_fields() -> None:
    subject = OsintSubject(full_name="Ali Raza", email="a@b.com")
    assert subject.available_kinds() == {IdentifierKind.FULL_NAME, IdentifierKind.EMAIL}


def test_label_prefers_name_and_never_returns_empty() -> None:
    assert OsintSubject(full_name="Ali", username="ali99").label() == "Ali"
    assert OsintSubject(cnic="4210112345671").label() == "CNIC 4210112345671"
    assert OsintSubject().label() == "Unknown subject"
