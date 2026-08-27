"""OAuth 2.1 authorization server, so hosted MCP connectors can sign in.

A connector platform (ChatGPT, claude.ai) will not accept a pasted bearer
token — it discovers an authorization server, registers itself, and runs a
browser flow. Previously this service answered that probe with a 404 at
``/.well-known/*`` meaning "no OAuth here", then 401'd everything the
connector asked for, which left it no way in at all. This module is that way in.

The protocol itself is the SDK's: ``mcp.server.auth`` ships the ``/authorize``,
``/token``, ``/register`` and ``/revoke`` handlers, the RFC 8414 and RFC 9728
metadata documents, PKCE checking and client authentication. What is left —
and all that lives here — is persistence and the human step:

    connector --> /authorize --> authorize() stores a pending request
                                 and returns a URL to /consent
              <-- browser lands on /consent, user logs in and approves
                  complete_authorization() mints a code, redirects back
    connector --> /token     --> exchange_authorization_code()

Every secret is stored as a SHA-256 hash, so a copy of the database does not
hand over live tokens. Access and refresh tokens issued from one approval
share a ``grant_id``, which is what makes revoking either one kill both.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time

from mcp.server.auth.provider import (AccessToken, AuthorizationCode,
                                      AuthorizationParams, AuthorizeError,
                                      RefreshToken, RegistrationError,
                                      construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import accounts
from .accounts import User, connect
from .config import (MCP_SCOPE, OAUTH_ACCESS_TTL, OAUTH_CODE_TTL,
                     OAUTH_REFRESH_TTL, public_base_url)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id   TEXT    PRIMARY KEY,
    info_json   TEXT    NOT NULL,
    created_at  INTEGER NOT NULL
);

-- An /authorize request parked while the browser logs in and approves. Holds
-- no credential, only what is needed to finish the redirect afterwards.
CREATE TABLE IF NOT EXISTS oauth_pending (
    id             TEXT    PRIMARY KEY,
    client_id      TEXT    NOT NULL,
    redirect_uri   TEXT    NOT NULL,
    uri_explicit   INTEGER NOT NULL,
    code_challenge TEXT    NOT NULL,
    state          TEXT,
    scopes         TEXT    NOT NULL,
    resource       TEXT,
    expires_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash      TEXT    PRIMARY KEY,
    grant_id       TEXT    NOT NULL,
    client_id      TEXT    NOT NULL,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    redirect_uri   TEXT    NOT NULL,
    uri_explicit   INTEGER NOT NULL,
    code_challenge TEXT    NOT NULL,
    scopes         TEXT    NOT NULL,
    resource       TEXT,
    expires_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_access (
    token_hash  TEXT    PRIMARY KEY,
    grant_id    TEXT    NOT NULL,
    client_id   TEXT    NOT NULL,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scopes      TEXT    NOT NULL,
    resource    TEXT,
    expires_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_access_grant ON oauth_access(grant_id);

CREATE TABLE IF NOT EXISTS oauth_refresh (
    token_hash  TEXT    PRIMARY KEY,
    grant_id    TEXT    NOT NULL,
    client_id   TEXT    NOT NULL,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scopes      TEXT    NOT NULL,
    resource    TEXT,
    expires_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_refresh_grant ON oauth_refresh(grant_id);
"""


def init_db() -> None:
    accounts.init_db()
    with connect() as conn:
        conn.executescript(_SCHEMA)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> int:
    return int(time.time())


# --------------------------------------------------------------------------
# the consent step, driven from main.py
# --------------------------------------------------------------------------

class PendingAuthorization:
    """An /authorize request waiting on a human."""

    def __init__(self, row):
        self.id = row["id"]
        self.client_id = row["client_id"]
        self.redirect_uri = row["redirect_uri"]
        self.uri_explicit = bool(row["uri_explicit"])
        self.code_challenge = row["code_challenge"]
        self.state = row["state"]
        self.scopes = json.loads(row["scopes"])
        self.resource = row["resource"]

    @property
    def client_name(self) -> str:
        """What to call this client on the consent screen. Falls back to the
        id, which is opaque but at least unambiguous — a dynamically
        registered client often supplies no name at all."""
        info = get_client_sync(self.client_id)
        if info and info.client_name:
            return info.client_name
        return self.client_id


def load_pending(pending_id: str) -> PendingAuthorization | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_pending WHERE id=? AND expires_at > ?",
            (pending_id, _now())).fetchone()
    return PendingAuthorization(row) if row else None


def complete_authorization(pending_id: str, user: User) -> str | None:
    """Approve a parked request: mint a single-use code bound to this user and
    return where to send the browser. None if the request expired while the
    person was logging in."""
    pending = load_pending(pending_id)
    if pending is None:
        return None

    code = secrets.token_urlsafe(32)          # 256 bits, well over RFC 6749's floor
    grant_id = secrets.token_urlsafe(16)
    with connect() as conn:
        conn.execute(
            "INSERT INTO oauth_codes (code_hash, grant_id, client_id, user_id,"
            " redirect_uri, uri_explicit, code_challenge, scopes, resource,"
            " expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_hash(code), grant_id, pending.client_id, user.id,
             pending.redirect_uri, int(pending.uri_explicit),
             pending.code_challenge, json.dumps(pending.scopes),
             pending.resource, _now() + OAUTH_CODE_TTL))
        conn.execute("DELETE FROM oauth_pending WHERE id=?", (pending_id,))

    return construct_redirect_uri(pending.redirect_uri, code=code,
                                  state=pending.state)


def deny_authorization(pending_id: str) -> str | None:
    """Decline: drop the request and hand the client the RFC 6749 error."""
    pending = load_pending(pending_id)
    if pending is None:
        return None
    with connect() as conn:
        conn.execute("DELETE FROM oauth_pending WHERE id=?", (pending_id,))
    return construct_redirect_uri(
        pending.redirect_uri, error="access_denied",
        error_description="The user declined this request.",
        state=pending.state)


# --------------------------------------------------------------------------
# resolving a token back to a user
# --------------------------------------------------------------------------

def user_for_access_token(token: str) -> User | None:
    """Used by the web middleware so one Bearer header works for both the MCP
    endpoint and the ordinary routes."""
    with connect() as conn:
        row = conn.execute(
            "SELECT u.* FROM oauth_access a JOIN users u ON u.id = a.user_id"
            " WHERE a.token_hash=? AND a.expires_at > ? AND u.disabled=0",
            (_hash(token), _now())).fetchone()
    return accounts._row_to_user(row) if row else None


def get_client_sync(client_id: str) -> OAuthClientInformationFull | None:
    with connect() as conn:
        row = conn.execute("SELECT info_json FROM oauth_clients WHERE client_id=?",
                           (client_id,)).fetchone()
    if row is None:
        return None
    return OAuthClientInformationFull.model_validate_json(row["info_json"])


def list_grants(user_id: int) -> list[dict]:
    """Connectors this user has approved, for the account page. Grouped by
    client so three refreshed tokens read as one connection."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT client_id, COUNT(*) n, MAX(expires_at) newest"
            " FROM oauth_refresh WHERE user_id=? AND expires_at > ?"
            " GROUP BY client_id", (user_id, _now())).fetchall()
    out = []
    for r in rows:
        info = get_client_sync(r["client_id"])
        out.append({
            "client_id": r["client_id"],
            "name": (info.client_name if info and info.client_name
                     else r["client_id"]),
            "expires_at": r["newest"],
        })
    return out


def revoke_client_for_user(client_id: str, user_id: int) -> None:
    """Disconnect a connector: drop every token it holds for this user."""
    with connect() as conn:
        conn.execute("DELETE FROM oauth_access WHERE client_id=? AND user_id=?",
                     (client_id, user_id))
        conn.execute("DELETE FROM oauth_refresh WHERE client_id=? AND user_id=?",
                     (client_id, user_id))


# --------------------------------------------------------------------------
# the provider the SDK drives
# --------------------------------------------------------------------------

class ContentForgeOAuthProvider:
    """Implements ``OAuthAuthorizationServerProvider`` against SQLite."""

    # -- clients ----------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return get_client_sync(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Dynamic client registration (RFC 7591). This is open by design —
        it is how a hosted connector onboards itself, and there is no way to
        pre-register one. Registering is not access: a client still cannot do
        anything until a real account approves it at /consent, so the worst an
        unsolicited registration achieves is a row in this table."""
        if not client_info.redirect_uris:
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="At least one redirect_uri is required.")
        with connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO oauth_clients (client_id, info_json,"
                " created_at) VALUES (?,?,?)",
                (client_info.client_id, client_info.model_dump_json(), _now()))

    # -- authorization ----------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        """Park the request and send the browser to the consent screen.

        No code is minted here — at this point nobody has proved who they are.
        The code is created in ``complete_authorization`` once a real account
        has logged in and approved, which is what binds the eventual token to
        a user rather than merely to a client.
        """
        base = public_base_url()
        if not base:
            raise AuthorizeError(
                error="server_error",
                error_description="This instance has no public URL configured, "
                                  "so it cannot complete a browser flow.")

        pending_id = secrets.token_urlsafe(24)
        scopes = params.scopes or [MCP_SCOPE]
        with connect() as conn:
            conn.execute(
                "INSERT INTO oauth_pending (id, client_id, redirect_uri,"
                " uri_explicit, code_challenge, state, scopes, resource,"
                " expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (pending_id, client.client_id, str(params.redirect_uri),
                 int(params.redirect_uri_provided_explicitly),
                 params.code_challenge, params.state, json.dumps(scopes),
                 params.resource, _now() + OAUTH_CODE_TTL))
            conn.execute("DELETE FROM oauth_pending WHERE expires_at < ?", (_now(),))

        return f"{base}/consent?req={pending_id}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM oauth_codes WHERE code_hash=? AND client_id=?"
                " AND expires_at > ?",
                (_hash(authorization_code), client.client_id, _now())).fetchone()
        if row is None:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=json.loads(row["scopes"]),
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["uri_explicit"]),
            resource=row["resource"],
            subject=str(row["user_id"]),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode
    ) -> OAuthToken:
        code_hash = _hash(authorization_code.code)
        with connect() as conn:
            row = conn.execute(
                "SELECT grant_id, user_id FROM oauth_codes WHERE code_hash=?",
                (code_hash,)).fetchone()
            if row is None:
                from mcp.server.auth.provider import TokenError
                raise TokenError(error="invalid_grant",
                                 error_description="This code is not valid.")
            # Single use. Deleting before issuing means a replayed code cannot
            # mint a second token even if two requests race.
            conn.execute("DELETE FROM oauth_codes WHERE code_hash=?", (code_hash,))

        return self._issue(row["grant_id"], client.client_id, row["user_id"],
                           authorization_code.scopes, authorization_code.resource)

    # -- refresh ----------------------------------------------------------

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM oauth_refresh WHERE token_hash=? AND client_id=?"
                " AND expires_at > ?",
                (_hash(refresh_token), client.client_id, _now())).fetchone()
        if row is None:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=row["expires_at"],
            subject=str(row["user_id"]),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        token_hash = _hash(refresh_token.token)
        with connect() as conn:
            row = conn.execute(
                "SELECT grant_id, user_id, resource FROM oauth_refresh"
                " WHERE token_hash=?", (token_hash,)).fetchone()
            if row is None:
                from mcp.server.auth.provider import TokenError
                raise TokenError(error="invalid_grant",
                                 error_description="This refresh token is not valid.")
            # Rotate: the presented refresh token dies here, and the whole
            # grant's access tokens go with it so the old pair cannot be reused.
            conn.execute("DELETE FROM oauth_refresh WHERE token_hash=?", (token_hash,))
            conn.execute("DELETE FROM oauth_access WHERE grant_id=?",
                         (row["grant_id"],))

        # A refresh may narrow scope but never widen it (RFC 6749 §6).
        granted = [s for s in (scopes or refresh_token.scopes)
                   if s in refresh_token.scopes] or refresh_token.scopes
        return self._issue(row["grant_id"], client.client_id, row["user_id"],
                           granted, row["resource"])

    # -- verification and revocation --------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        with connect() as conn:
            row = conn.execute(
                "SELECT a.* FROM oauth_access a JOIN users u ON u.id = a.user_id"
                " WHERE a.token_hash=? AND a.expires_at > ? AND u.disabled=0",
                (_hash(token), _now())).fetchone()
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=row["expires_at"],
            resource=row["resource"],
            subject=str(row["user_id"]),
        )

    async def revoke_token(self, token) -> None:
        """RFC 7009. The spec says revoking either half SHOULD take the other
        with it, which the shared grant_id makes a two-statement job."""
        token_hash = _hash(token.token)
        with connect() as conn:
            row = conn.execute(
                "SELECT grant_id FROM oauth_access WHERE token_hash=?"
                " UNION SELECT grant_id FROM oauth_refresh WHERE token_hash=?",
                (token_hash, token_hash)).fetchone()
            if row is None:
                return                      # unknown or already gone: do nothing
            conn.execute("DELETE FROM oauth_access WHERE grant_id=?", (row["grant_id"],))
            conn.execute("DELETE FROM oauth_refresh WHERE grant_id=?", (row["grant_id"],))

    # -- helper -----------------------------------------------------------

    def _issue(self, grant_id: str, client_id: str, user_id: int,
               scopes: list[str], resource: str | None) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = _now()
        with connect() as conn:
            conn.execute(
                "INSERT INTO oauth_access (token_hash, grant_id, client_id,"
                " user_id, scopes, resource, expires_at) VALUES (?,?,?,?,?,?,?)",
                (_hash(access), grant_id, client_id, user_id,
                 json.dumps(scopes), resource, now + OAUTH_ACCESS_TTL))
            conn.execute(
                "INSERT INTO oauth_refresh (token_hash, grant_id, client_id,"
                " user_id, scopes, resource, expires_at) VALUES (?,?,?,?,?,?,?)",
                (_hash(refresh), grant_id, client_id, user_id,
                 json.dumps(scopes), resource, now + OAUTH_REFRESH_TTL))
            # Sweep expired rows while we are here; nothing else runs on a timer.
            conn.execute("DELETE FROM oauth_access WHERE expires_at < ?", (now,))
            conn.execute("DELETE FROM oauth_refresh WHERE expires_at < ?", (now,))
            conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=OAUTH_ACCESS_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )


class CombinedTokenVerifier:
    """Accepts either credential the MCP endpoint can legitimately be given.

    A connector arrives with an OAuth access token; Claude Code and scripts
    arrive with a personal access token, because a CLI has nowhere to run a
    browser flow. Verifying only the first would make the setup line on the
    account page a lie, so both resolve here — to the same user either way.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        granted = await provider.load_access_token(token)
        if granted is not None:
            return granted

        user = accounts.pat_user(token)
        if user is None:
            return None
        # Presented in the shape the SDK expects. A PAT has no expiry of its
        # own — it lives until revoked — and no client, so client_id names the
        # user instead of a registration that does not exist.
        return AccessToken(
            token=token,
            client_id=f"pat:{user.id}",
            scopes=[MCP_SCOPE],
            expires_at=None,
            subject=str(user.id),
        )


provider = ContentForgeOAuthProvider()
verifier = CombinedTokenVerifier()
