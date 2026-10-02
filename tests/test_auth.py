"""Portal sign-in: credentials, signed session tokens, and the routes they guard."""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sherlocks.api.auth import AuthError, SessionAuth
from sherlocks.settings import Settings


def _settings(**auth) -> Settings:
    s = Settings()
    s.auth.username = "operator"
    s.auth.password = "correct horse"
    s.auth.secret = "test-secret"
    for k, v in auth.items():
        setattr(s.auth, k, v)
    return s


def test_right_credentials_issue_a_token_that_verifies():
    a = SessionAuth(_settings())
    session = a.login("Operator", "correct horse")          # username is case-insensitive
    assert a.verify(session["token"]) == "operator"


@pytest.mark.parametrize(("user", "password"), [("operator", "wrong"), ("someone", "correct horse"), ("", "")])
def test_wrong_credentials_are_refused(user, password):
    with pytest.raises(AuthError):
        SessionAuth(_settings()).login(user, password)


def test_tampered_and_foreign_tokens_fail():
    a = SessionAuth(_settings())
    token = a.login("operator", "correct horse")["token"]
    body, sig = token.rsplit(".", 1)
    with pytest.raises(AuthError):
        a.verify(f"{body}x.{sig}")
    # signed with a different secret (another deployment) - rejected
    other = SessionAuth(_settings(secret="other"))
    with pytest.raises(AuthError):
        other.verify(token)


def test_expired_token_fails(monkeypatch):
    a = SessionAuth(_settings(session_hours=1))
    token = a.login("operator", "correct horse")["token"]
    monkeypatch.setattr(time, "time", lambda: 10**11)
    with pytest.raises(AuthError):
        a.verify(token)


def test_password_hash_alternative():
    import hashlib

    a = SessionAuth(_settings(password=None, password_sha256=hashlib.sha256(b"correct horse").hexdigest()))
    assert a.verify(a.login("operator", "correct horse")["token"]) == "operator"


def test_no_password_means_sign_in_is_off():
    assert SessionAuth(_settings(password=None)).enabled is False


def test_graph_routes_require_a_session():
    from sherlocks.api.linkgraph import build_router

    s = _settings()
    a = SessionAuth(s)
    app = FastAPI()
    app.include_router(build_router(s, lambda: None, a))
    client = TestClient(app)
    url = f"{s.api.prefix}/graph/runs/nope/connection?a=x&b=y"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"X-Session-Token": "forged.token"}).status_code == 401
    token = a.login("operator", "correct horse")["token"]
    # authenticated: gets past auth (then fails on the fake manager, not with 401)
    with pytest.raises(Exception):  # noqa: B017
        client.get(url, headers={"X-Session-Token": token})
