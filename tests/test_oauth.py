import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from anki_voice import oauth
from anki_voice.oauth import PasswordOAuthProvider
from anki_voice.server import create_http_app

PASSWORD = "correct horse battery staple"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
}
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


@pytest.fixture
def client(tmp_path):
    app = create_http_app("http://localhost", PASSWORD, tmp_path / "oauth.json")
    with TestClient(app, base_url="http://localhost") as c:
        yield c


def register(client, redirect=REDIRECT):
    return client.post("/register", json={
        "redirect_uris": [redirect],
        "client_name": "Claude",
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    })


def start_sign_in(client, client_id):
    """Start a sign-in at /authorize; returns the sign-in page's URL and the PKCE verifier."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    authorize = client.get("/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
    }, follow_redirects=False)
    assert authorize.status_code == 302, authorize.text
    return authorize.headers["location"], verifier


def sign_in(client, password=PASSWORD):
    registered = register(client)
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]

    login_url, verifier = start_sign_in(client, client_id)
    request_id = parse_qs(urlparse(login_url).query)["request"][0]

    page = client.get(login_url)
    assert "Claude wants to review your Anki cards" in page.text

    submitted = client.post("/login", data={"request": request_id, "password": password}, follow_redirects=False)
    if submitted.status_code != 302:
        return submitted
    callback = urlparse(submitted.headers["location"])
    query = parse_qs(callback.query)
    assert f"{callback.scheme}://{callback.netloc}{callback.path}" == REDIRECT
    assert query["state"] == ["xyz"]

    tokens = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": query["code"][0],
        "redirect_uri": REDIRECT,
        "client_id": client_id,
        "code_verifier": verifier,
    })
    assert tokens.status_code == 200, tokens.text
    return client_id, tokens.json()


def mcp_status(client, access_token=None):
    headers = dict(MCP_HEADERS)
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return client.post("/mcp", json=INIT, headers=headers).status_code


def test_mcp_needs_a_token(client):
    assert mcp_status(client) == 401
    assert mcp_status(client, "made-up") == 401


def test_sign_in_gives_a_working_token(client):
    _, tokens = sign_in(client)
    assert tokens["expires_in"] == 3600
    assert mcp_status(client, tokens["access_token"]) == 200


def test_wrong_password_gets_no_code(client):
    response = sign_in(client, password="wrong")
    assert response.status_code == 401
    assert "Wrong password" in response.text


def test_refresh_rotates_both_tokens(client):
    client_id, tokens = sign_in(client)
    refreshed = client.post("/token", data={
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": client_id,
    })
    assert refreshed.status_code == 200, refreshed.text
    new = refreshed.json()
    assert mcp_status(client, new["access_token"]) == 200
    assert mcp_status(client, tokens["access_token"]) == 401
    reused = client.post("/token", data={
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": client_id,
    })
    assert reused.status_code == 400


def test_only_claudes_callback_can_register(client):
    refused = register(client, "https://evil.example/callback")
    assert refused.status_code == 400
    assert refused.json()["error"] == "invalid_redirect_uri"


def test_sign_in_page_cant_be_framed(client):
    login_url, _ = start_sign_in(client, register(client).json()["client_id"])
    page = client.get(login_url)
    assert page.headers["X-Frame-Options"] == "DENY"
    assert page.headers["Content-Security-Policy"] == "frame-ancestors 'none'"


def test_keeps_only_the_newest_pending_sign_ins(client, monkeypatch):
    monkeypatch.setattr(oauth, "MAX_PENDING", 3)
    client_id = register(client).json()["client_id"]
    first, _ = start_sign_in(client, client_id)
    for _ in range(3):
        newest, _ = start_sign_in(client, client_id)
    assert client.get(first).status_code == 400
    assert client.get(newest).status_code == 200


def test_state_file_is_private_from_the_start(tmp_path, monkeypatch):
    modes = []
    real_open = oauth.os.open

    def spy(path, flags, mode=0o777):
        modes.append(mode)
        return real_open(path, flags, mode)

    monkeypatch.setattr(oauth.os, "open", spy)
    state = tmp_path / "oauth.json"
    state.with_suffix(".tmp").write_text("{}")
    state.with_suffix(".tmp").chmod(0o644)
    PasswordOAuthProvider(PASSWORD, state, "http://localhost/login")._issue("client", [], None)
    assert modes == [0o600]
    assert state.stat().st_mode & 0o777 == 0o600


@pytest.mark.anyio
async def test_tokens_survive_a_restart(tmp_path):
    state = tmp_path / "oauth.json"
    first = PasswordOAuthProvider(PASSWORD, state, "http://localhost/login")
    token = first._issue("client", [], None)
    second = PasswordOAuthProvider(PASSWORD, state, "http://localhost/login")
    assert await second.load_access_token(token.access_token) is not None
    assert token.access_token not in state.read_text()  # only hashes are stored


@pytest.fixture
def anyio_backend():
    return "asyncio"
