"""A single-user OAuth authorization server: sign in with one password.

claude.ai registers itself as a client (Dynamic Client Registration), sends the
browser to /authorize, and the person signs in on /login with the password.
Access tokens last an hour and refresh tokens 48 hours; both are stored only as
hashes, in a file on this machine, so connections survive a restart.
"""

import hashlib
import hmac
import html
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

import anyio
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

ACCESS_TTL = 60 * 60
REFRESH_TTL = 48 * 60 * 60
CODE_TTL = 5 * 60
LOGIN_TTL = 10 * 60
MAX_CLIENTS = 20
MAX_PENDING = 100
# Claude's OAuth callback, and the one Anthropic says it may move to.
REDIRECT_URIS = {"https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback"}
MAX_FAILURES = 10
FAILURE_WINDOW = 15 * 60


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class PasswordOAuthProvider:
    def __init__(self, password: str, state_file: Path, login_url: str):
        self.password_hash = _hash(password)
        self.state_file = state_file
        self.login_url = login_url
        # Kept in memory only: they live for minutes.
        self.pending: dict[str, tuple[str, AuthorizationParams, float]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.failures: list[float] = []
        self.state = self._load()

    # Persistence

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text())
        except FileNotFoundError:
            return {"clients": {}, "access": {}, "refresh": {}}

    def _save(self) -> None:
        now = time.time()
        for kind in ("access", "refresh"):
            self.state[kind] = {h: t for h, t in self.state[kind].items() if t["expires_at"] > now}
        self.state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)  # a leftover .tmp keeps its old mode otherwise
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(self.state, indent=1))
        tmp.replace(self.state_file)

    # Clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if data := self.state["clients"].get(client_id):
            return OAuthClientInformationFull.model_validate(data)
        return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise RegistrationError(error="invalid_client_metadata", error_description="missing client_id")
        # Only Claude may receive codes: a client sending them anywhere else
        # could turn a link to this server's sign-in page into phishing.
        if not client_info.redirect_uris or not {str(u) for u in client_info.redirect_uris} <= REDIRECT_URIS:
            raise RegistrationError(error="invalid_redirect_uri", error_description="only Claude's OAuth callback is accepted")
        clients = self.state["clients"]
        clients[client_info.client_id] = client_info.model_dump(mode="json", exclude_none=True)
        # Registration is open to anyone, so cap how many clients are kept.
        while len(clients) > MAX_CLIENTS:
            oldest = min(clients, key=lambda c: clients[c].get("client_id_issued_at", 0))
            del clients[oldest]
        self._save()

    # Sign-in

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v[2] > now}
        # Anyone can start a sign-in, so keep only the newest.
        while len(self.pending) >= MAX_PENDING:
            del self.pending[next(iter(self.pending))]
        request_id = secrets.token_urlsafe(32)
        self.pending[request_id] = (client.client_id or "", params, now + LOGIN_TTL)
        return f"{self.login_url}?request={request_id}"

    async def login(self, request: Request) -> Response:
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
        else:
            form = await request.form()
            request_id = str(form.get("request", ""))
        pending = self.pending.get(request_id)
        if not pending or pending[2] < time.time():
            return self._page("This sign-in link has expired. Start again from Claude.", status=400)
        client_id, params, _ = pending
        client = await self.get_client(client_id)
        client_name = (client.client_name if client else None) or "An app"

        if request.method == "GET":
            return self._page(None, request_id=request_id, client_name=client_name)

        now = time.time()
        self.failures = [t for t in self.failures if t > now - FAILURE_WINDOW]
        if len(self.failures) >= MAX_FAILURES:
            return self._page("Too many wrong passwords. Try again in 15 minutes.", status=429)
        given = str(form.get("password", ""))
        if not hmac.compare_digest(_hash(given), self.password_hash):
            self.failures.append(now)
            await anyio.sleep(1)
            return self._page("Wrong password.", request_id=request_id, client_name=client_name, status=401)

        del self.pending[request_id]
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=now + CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject="owner",
        )
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), status_code=302)

    def _page(self, message: str | None, request_id: str = "", client_name: str = "", status: int = 200) -> HTMLResponse:
        form = ""
        if request_id:
            form = f"""
<p>{html.escape(client_name)} wants to review your Anki cards.</p>
<form method="post">
  <input type="hidden" name="request" value="{html.escape(request_id)}">
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" autofocus required>
  <button type="submit">Sign in</button>
</form>"""
        error = f'<p class="error">{html.escape(message)}</p>' if message else ""
        return HTMLResponse(
            LOGIN_PAGE.format(error=error, form=form),
            status_code=status,
            headers={"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"},
        )

    # Tokens

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        record = {"client_id": client_id, "scopes": scopes, "resource": resource}
        self.state["access"][_hash(access)] = {**record, "expires_at": now + ACCESS_TTL, "refresh": _hash(refresh)}
        self.state["refresh"][_hash(refresh)] = {**record, "expires_at": now + REFRESH_TTL, "access": _hash(access)}
        self._save()
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        code = self.codes.get(authorization_code)
        if code and code.client_id == client.client_id:
            return code
        return None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        if self.codes.pop(authorization_code.code, None) is None:
            raise TokenError(error="invalid_grant", error_description="authorization code already used")
        return self._issue(authorization_code.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        record = self.state["refresh"].get(_hash(refresh_token))
        if not record or record["client_id"] != client.client_id or record["expires_at"] < time.time():
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            resource=record["resource"],
            subject="owner",
        )

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        self._forget(_hash(refresh_token.token))  # rotate: the old pair stops working
        return self._issue(refresh_token.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        record = self.state["access"].get(_hash(token))
        if not record or record["expires_at"] < time.time():
            return None
        return AccessToken(
            token=token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            resource=record["resource"],
            subject="owner",
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._forget(_hash(token.token))

    def _forget(self, token_hash: str) -> None:
        """Drop a token and the other half of its access/refresh pair."""
        if record := self.state["access"].pop(token_hash, None):
            self.state["refresh"].pop(record["refresh"], None)
        if record := self.state["refresh"].pop(token_hash, None):
            self.state["access"].pop(record["access"], None)
        self._save()


LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Anki Voice sign-in</title>
<style>
  :root {{ --bg: #f6f5f2; --fg: #22211f; --muted: #6b6862; --line: #d8d5ce; --accent: #2f5d8a; --error: #a3322a; color-scheme: light; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #1b1b1a; --fg: #ecebe7; --muted: #a19e97; --line: #3a3936; --accent: #8db4dc; --error: #e58a82; color-scheme: dark; }}
  }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--fg);
         font: 16px/1.5 system-ui, -apple-system, sans-serif; padding: 24px; box-sizing: border-box; }}
  main {{ width: 100%; max-width: 340px; display: grid; gap: 12px; }}
  h1 {{ font-size: 22px; margin: 0; }}
  p {{ margin: 0; color: var(--muted); }}
  form {{ display: grid; gap: 8px; margin-top: 8px; }}
  input[type=password] {{ font: inherit; padding: 10px 12px; border: 1px solid var(--line); border-radius: 6px; background: transparent; color: var(--fg); }}
  button {{ font: inherit; padding: 10px 12px; border: 0; border-radius: 6px; background: var(--accent); color: var(--bg); cursor: pointer; }}
  :focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  .error {{ color: var(--error); }}
</style>
</head>
<body>
<main>
  <h1>Anki Voice</h1>
  {error}
  {form}
</main>
</body>
</html>
"""
