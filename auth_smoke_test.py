"""End-to-end test of sign-in, personal access tokens and the OAuth flow.

    CF_FAKE_CLAUDE=1 .venv/bin/python auth_smoke_test.py

Drives a real uvicorn server over HTTP: the browser login, the admin gate, a
personal access token against /mcp, and the complete OAuth authorization code
flow with PKCE — dynamic client registration, consent, token exchange, refresh
rotation and revocation — finishing with a genuine MCP client session opened on
an OAuth-issued token.

The point is that nothing here is checked with curl against a hand-written
request. Every previous round of this was validated that way, which is exactly
why three "fixes" shipped without a real client ever exercising them.
"""
import asyncio
import base64
import hashlib
import json
import os
import shutil
import socket
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# The port has to be settled before the app is imported: the OAuth issuer is
# baked into the metadata at import time and RFC 8414 compares it by exact
# string, so a mismatch here is a mismatch on the wire.
PORT = free_port()
ROOT = Path(os.environ.setdefault("CF_CONTENT_ROOT", "/tmp/cf-auth-smoke"))
if ROOT.exists():
    shutil.rmtree(ROOT)

os.environ.setdefault("CF_FAKE_CLAUDE", "1")
os.environ["CF_TOKEN_FILE"] = str(ROOT / "token-env")
os.environ["CF_AUTH_DB"] = str(ROOT / "auth.db")
os.environ["CF_PUBLIC_HOST"] = "127.0.0.1"
os.environ["CF_PUBLIC_PORT"] = str(PORT)
os.environ["CF_PUBLIC_SCHEME"] = "http"
os.environ["CF_REQUIRE_AUTH"] = "1"

import httpx2  # noqa: E402
import uvicorn  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from app import accounts, oauth_provider, storage  # noqa: E402
from app.main import app  # noqa: E402

BASE = f"http://127.0.0.1:{PORT}"
MCP_URL = f"{BASE}/mcp"

ALICE_PW = "correct-horse-battery"
BOB_PW = "another-long-passphrase"

fails = []


def check(label, cond, detail=""):
    print(f"{'  ok ' if cond else 'FAIL '} {label}"
          + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

# Only what ./deploy/add-user.sh does — create the account tables and a user.
# The OAuth schema is deliberately NOT created here: the app's lifespan owns
# that, and calling it here would mask a startup path that never ran.
accounts.init_db()
alice = accounts.create_user("alice", is_admin=True, password=ALICE_PW)
bob = accounts.create_user("bob", password=BOB_PW)
storage.create_topic("Boating", "Boats and water", "")

server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT,
                                       log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()


def absolute(location: str) -> str:
    """The app returns relative Location headers, which is correct HTTP but
    not something the client can re-request as-is."""
    return location if location.startswith("http") else BASE + location


def pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


async def main():
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.1)
    check("server started", server.started)

    async with httpx2.AsyncClient(follow_redirects=False) as c:

        # -- anonymous surface -------------------------------------------
        r = await c.get(f"{BASE}/")
        check("/ answers anonymously", r.status_code == 200, r.status_code)
        check("/ leaks no content", "Boating" not in r.text)

        r = await c.get(f"{BASE}/robots.txt")
        check("/robots.txt answers anonymously", r.status_code == 200)

        r = await c.get(f"{BASE}/health")
        check("/health answers anonymously", r.status_code == 200)

        r = await c.get(f"{BASE}/voices", headers={"accept": "text/html"})
        check("browser is redirected to /login", r.status_code == 303,
              r.status_code)
        check("redirect preserves where it was going",
              "next=%2Fvoices" in r.headers.get("location", ""),
              r.headers.get("location"))

        r = await c.get(f"{BASE}/voices", headers={"accept": "application/json"})
        check("API caller gets 401 not a redirect", r.status_code == 401,
              r.status_code)

        # An unknown Bearer token has to reach the OAuth tables to be ruled
        # out. If the app never created them this is a 500, which is exactly
        # how a lifespan that silently never ran showed up in production.
        r = await c.get(f"{BASE}/voices",
                        headers={"accept": "application/json",
                                 "Authorization": "Bearer not-a-real-token"})
        check("an unknown bearer token is refused, not a 500",
              r.status_code == 401, r.status_code)
        check("401 names Bearer",
              "Bearer" in r.headers.get("www-authenticate", ""))

        # /health must stay open for monitoring, but the full body names the
        # Claude credential in use, its expiry, and where content lives.
        r = await c.get(f"{BASE}/health")
        body = r.json()
        check("anonymous /health is liveness only", set(body) == {"ok"}, str(body))
        check("anonymous /health hides the credential", "auth" not in body)
        check("anonymous /health hides the content path", "content_root" not in body)

        # -- MCP is gated by the SDK, with the discovery pointer ----------
        r = await c.post(MCP_URL, json={"jsonrpc": "2.0", "id": 1,
                                        "method": "tools/list", "params": {}},
                         headers={"accept": "application/json, text/event-stream"})
        check("/mcp rejects anonymous calls", r.status_code == 401, r.status_code)
        www = r.headers.get("www-authenticate", "")
        check("/mcp 401 carries resource_metadata (RFC 9728)",
              "resource_metadata" in www, www or "(no header)")

        # -- discovery documents -----------------------------------------
        r = await c.get(f"{BASE}/.well-known/oauth-authorization-server")
        check("AS metadata served at the root", r.status_code == 200, r.status_code)
        meta = r.json() if r.status_code == 200 else {}
        check("issuer matches the public URL", meta.get("issuer", "").rstrip("/") == BASE,
              meta.get("issuer"))
        check("registration endpoint advertised (DCR)",
              bool(meta.get("registration_endpoint")))
        check("PKCE S256 advertised",
              "S256" in (meta.get("code_challenge_methods_supported") or []))

        r = await c.get(f"{BASE}/.well-known/oauth-protected-resource/mcp")
        check("protected resource metadata served", r.status_code == 200,
              r.status_code)
        prm = r.json() if r.status_code == 200 else {}
        check("PRM points back at this AS",
              any(str(a).rstrip("/") == BASE
                  for a in prm.get("authorization_servers", [])),
              prm.get("authorization_servers"))

        # -- browser sign-in ---------------------------------------------
        r = await c.post(f"{BASE}/login",
                         data={"username": "alice", "password": "wrong",
                               "next": "/"})
        check("wrong password is refused", "did not match" in r.text)
        check("wrong password sets no cookie",
              "cf_session" not in r.headers.get("set-cookie", ""))

        r = await c.post(f"{BASE}/login",
                         data={"username": "alice", "password": ALICE_PW,
                               "next": "/voices"})
        check("correct password signs in", r.status_code == 303, r.status_code)
        cookie = c.cookies.get("cf_session")
        check("session cookie issued", bool(cookie))

        r = await c.get(f"{BASE}/voices")
        check("signed in, content is reachable", r.status_code == 200,
              r.status_code)

        r = await c.get(f"{BASE}/auth")
        check("admin reaches the Claude credential page", r.status_code == 200,
              r.status_code)

        r = await c.get(f"{BASE}/health")
        check("signed in, /health reports in full",
              "auth" in r.json() and r.json()["auth"]["method"], str(r.json())[:120])

        # `next` must never bounce a freshly authenticated user off-site.
        for evil in ("//evil.example/", "/\\evil.example/",
                     "https://evil.example/"):
            r = await c.post(f"{BASE}/login",
                             data={"username": "alice", "password": ALICE_PW,
                                   "next": evil})
            check(f"open redirect refused: {evil}",
                  r.headers.get("location") == "/", r.headers.get("location"))

        # -- the admin gate ----------------------------------------------
        async with httpx2.AsyncClient(follow_redirects=False) as bobc:
            await bobc.post(f"{BASE}/login",
                            data={"username": "bob", "password": BOB_PW,
                                  "next": "/"})
            r = await bobc.get(f"{BASE}/voices")
            check("non-admin can use the app", r.status_code == 200, r.status_code)
            r = await bobc.get(f"{BASE}/auth")
            check("non-admin is refused the credential page",
                  r.status_code == 403, r.status_code)
            r = await bobc.post(f"{BASE}/auth/token/delete")
            check("non-admin cannot delete the Claude token",
                  r.status_code == 403, r.status_code)

        # -- personal access token ----------------------------------------
        pat = accounts.create_pat(alice.id, "smoke test")
        async with streamable_http_client(
                MCP_URL, http_client=httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {pat}"})) as (rd, wr):
            async with ClientSession(rd, wr) as s:
                init = await s.initialize()
                check("PAT opens an MCP session",
                      init.server_info.name == "content_forge_mcp")
                tools = await s.list_tools()
                check("PAT session lists tools", len(tools.tools) >= 21,
                      str(len(tools.tools)))

        r = await c.post(MCP_URL,
                         json={"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                               "params": {}},
                         headers={"Authorization": "Bearer cfp_not-a-real-token",
                                  "accept": "application/json, text/event-stream"})
        check("a bogus PAT is refused", r.status_code == 401, r.status_code)

        # -- OAuth: dynamic client registration ---------------------------
        r = await c.post(f"{BASE}/register", json={
            "client_name": "Test Connector",
            "redirect_uris": ["https://connector.example/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        })
        check("client registers itself", r.status_code in (200, 201),
              f"{r.status_code} {r.text[:120]}")
        reg = r.json()
        client_id = reg.get("client_id")
        client_secret = reg.get("client_secret")
        check("registration returns a client_id", bool(client_id))

        # -- OAuth: authorize, with alice already signed in ---------------
        verifier, challenge = pkce()
        r = await c.get(f"{BASE}/authorize", params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://connector.example/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz-state",
            "scope": "content-forge",
        })
        check("/authorize redirects to consent", r.status_code in (302, 303, 307),
              f"{r.status_code} {r.text[:160]}")
        consent_url = r.headers.get("location", "")
        check("consent URL is on this site", consent_url.startswith(BASE),
              consent_url)
        req_id = parse_qs(urlparse(consent_url).query).get("req", [""])[0]
        check("consent carries a request id", bool(req_id))

        r = await c.get(consent_url)
        check("consent page renders", r.status_code == 200, r.status_code)
        check("consent names the client", "Test Connector" in r.text)
        check("consent names the user", "alice" in r.text.lower())

        # -- OAuth: approve ------------------------------------------------
        r = await c.post(f"{BASE}/consent",
                         data={"req": req_id, "decision": "approve"})
        check("approval redirects back to the client",
              r.status_code == 303, r.status_code)
        cb = r.headers.get("location", "")
        q = parse_qs(urlparse(cb).query)
        code = q.get("code", [""])[0]
        check("callback carries an authorization code", bool(code))
        check("callback echoes state", q.get("state", [""])[0] == "xyz-state")

        # -- OAuth: token exchange ----------------------------------------
        r = await c.post(f"{BASE}/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://connector.example/callback",
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": verifier,
        })
        check("code exchanges for a token", r.status_code == 200,
              f"{r.status_code} {r.text[:160]}")
        tok = r.json() if r.status_code == 200 else {}
        access, refresh = tok.get("access_token"), tok.get("refresh_token")
        check("access token issued", bool(access))
        check("refresh token issued", bool(refresh))

        # A code is single use — replaying it must not mint a second token.
        r = await c.post(f"{BASE}/token", data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": "https://connector.example/callback",
            "client_id": client_id, "client_secret": client_secret,
            "code_verifier": verifier,
        })
        check("a replayed code is refused", r.status_code >= 400, r.status_code)

        # -- OAuth: the token actually works on MCP -----------------------
        async with streamable_http_client(
                MCP_URL, http_client=httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {access}"})) as (rd, wr):
            async with ClientSession(rd, wr) as s:
                init = await s.initialize()
                check("OAuth token opens an MCP session",
                      init.server_info.name == "content_forge_mcp")
                res = await s.call_tool("content_forge_list_topics", {})
                body = res.content[0].text if res.content else ""
                check("OAuth session can call a tool", "Boating" in body,
                      body[:120])

        # -- OAuth: the grant is attributed to a real user ----------------
        check("token maps back to alice",
              (oauth_provider.user_for_access_token(access) or
               accounts.User(0, "", "", False)).username == "alice")
        grants = oauth_provider.list_grants(alice.id)
        check("connection shows on the account page",
              any(g["name"] == "Test Connector" for g in grants), str(grants))

        # -- OAuth: refresh rotates ---------------------------------------
        r = await c.post(f"{BASE}/token", data={
            "grant_type": "refresh_token", "refresh_token": refresh,
            "client_id": client_id, "client_secret": client_secret,
        })
        check("refresh returns a new token", r.status_code == 200,
              f"{r.status_code} {r.text[:160]}")
        tok2 = r.json() if r.status_code == 200 else {}
        access2, refresh2 = tok2.get("access_token"), tok2.get("refresh_token")
        check("refresh rotated the access token", access2 and access2 != access)
        check("refresh rotated the refresh token", refresh2 and refresh2 != refresh)

        r = await c.post(f"{BASE}/token", data={
            "grant_type": "refresh_token", "refresh_token": refresh,
            "client_id": client_id, "client_secret": client_secret,
        })
        check("the old refresh token is dead", r.status_code >= 400, r.status_code)

        check("the old access token is dead",
              oauth_provider.user_for_access_token(access) is None)

        # -- OAuth: revocation --------------------------------------------
        r = await c.post(f"{BASE}/revoke", data={
            "token": access2, "client_id": client_id,
            "client_secret": client_secret,
        })
        check("revocation accepted", r.status_code == 200, r.status_code)
        check("revoked access token stops working",
              oauth_provider.user_for_access_token(access2) is None)
        r = await c.post(f"{BASE}/token", data={
            "grant_type": "refresh_token", "refresh_token": refresh2,
            "client_id": client_id, "client_secret": client_secret,
        })
        check("revoking took the refresh token with it (RFC 7009)",
              r.status_code >= 400, r.status_code)

        # -- consent may be declined --------------------------------------
        verifier2, challenge2 = pkce()
        r = await c.get(f"{BASE}/authorize", params={
            "response_type": "code", "client_id": client_id,
            "redirect_uri": "https://connector.example/callback",
            "code_challenge": challenge2, "code_challenge_method": "S256",
            "state": "deny-state", "scope": "content-forge",
        })
        req2 = parse_qs(urlparse(r.headers.get("location", "")).query).get("req", [""])[0]
        r = await c.post(f"{BASE}/consent", data={"req": req2, "decision": "deny"})
        q = parse_qs(urlparse(r.headers.get("location", "")).query)
        check("declining returns access_denied",
              q.get("error", [""])[0] == "access_denied", str(q))

        # -- the real connector path: nobody is signed in yet -------------
        # Every check above had alice's cookie already in hand. In production
        # the connector bounces a cold browser at /authorize, so the login
        # redirect has to carry the authorization request through intact.
        async with httpx2.AsyncClient(follow_redirects=False) as cold:
            v3, ch3 = pkce()
            r = await cold.get(f"{BASE}/authorize", params={
                "response_type": "code", "client_id": client_id,
                "redirect_uri": "https://connector.example/callback",
                "code_challenge": ch3, "code_challenge_method": "S256",
                "state": "cold-state", "scope": "content-forge",
            })
            consent = r.headers.get("location", "")
            check("cold /authorize still reaches consent", bool(consent), consent)

            r = await cold.get(absolute(consent), headers={"accept": "text/html"})
            check("an anonymous visitor is sent to sign in",
                  r.status_code == 303, r.status_code)
            login_url = r.headers.get("location", "")
            check("the login redirect carries the request back",
                  "next=" in login_url and "consent" in login_url, login_url)

            nxt = parse_qs(urlparse(login_url).query).get("next", ["/"])[0]
            r = await cold.post(f"{BASE}/login",
                                data={"username": "alice", "password": ALICE_PW,
                                      "next": nxt})
            check("signing in returns to the consent screen",
                  r.status_code == 303 and "consent" in r.headers.get("location", ""),
                  r.headers.get("location"))

            r = await cold.get(absolute(r.headers["location"]))
            check("consent renders after the detour", r.status_code == 200,
                  r.status_code)
            req3 = parse_qs(urlparse(consent).query).get("req", [""])[0]
            r = await cold.post(f"{BASE}/consent",
                                data={"req": req3, "decision": "approve"})
            code3 = parse_qs(urlparse(r.headers.get("location", "")).query
                             ).get("code", [""])[0]
            check("a cold visitor completes the whole flow", bool(code3),
                  r.headers.get("location"))

        # -- login throttling ----------------------------------------------
        # A public login form is a guessing oracle and, because each attempt
        # costs an scrypt hash, a CPU lever too.
        async with httpx2.AsyncClient(follow_redirects=False) as attacker:
            blocked = False
            for _ in range(12):
                r = await attacker.post(f"{BASE}/login",
                                        data={"username": "nobody",
                                              "password": "guess", "next": "/"})
                if "Too many attempts" in r.text:
                    blocked = True
                    break
            check("repeated failures are throttled", blocked)
            # The lockout is per client+username, so it must not become a way
            # to lock a real user out of their own account.
            r = await attacker.post(f"{BASE}/login",
                                    data={"username": "alice",
                                          "password": ALICE_PW, "next": "/"})
            check("throttling one name does not lock out another",
                  r.status_code == 303, r.status_code)

        # -- invites -------------------------------------------------------
        carol = accounts.create_user("carol")
        invite = accounts.create_invite(carol.id)
        async with httpx2.AsyncClient(follow_redirects=False) as cc:
            r = await cc.get(f"{BASE}/invite/{invite}")
            check("invite page is reachable without signing in",
                  r.status_code == 200, r.status_code)
            r = await cc.post(f"{BASE}/invite/{invite}",
                              data={"password": "short", "confirm": "short"})
            check("a short password is refused", "at least 12" in r.text)
            r = await cc.post(f"{BASE}/invite/{invite}",
                              data={"password": "a-perfectly-fine-passphrase",
                                    "confirm": "a-perfectly-fine-passphrase"})
            check("claiming an invite signs you in", r.status_code == 303,
                  r.status_code)
            r = await cc.get(f"{BASE}/voices")
            check("the new account works", r.status_code == 200, r.status_code)

        r = await c.get(f"{BASE}/invite/{invite}")
        check("an invite is single use", "expired" in r.text.lower())

        # -- disabling an account ------------------------------------------
        accounts.set_disabled(bob.id, True)
        check("a disabled account cannot sign in",
              accounts.verify_password("bob", BOB_PW) is None)

        # -- sign out -------------------------------------------------------
        r = await c.post(f"{BASE}/logout")
        check("logout redirects", r.status_code == 303, r.status_code)
        r = await c.get(f"{BASE}/voices", headers={"accept": "text/html"})
        check("session is dead after logout", r.status_code == 303, r.status_code)

    # The throttle key decides whether the limit can be walked past. nginx
    # appends the real peer to any client-supplied X-Forwarded-For, so the
    # first entry is attacker-controlled — and the header counts for nothing
    # at all unless the request actually came from the local proxy.
    class _Req:
        def __init__(self, xff, peer):
            self.headers = {"x-forwarded-for": xff} if xff else {}
            self.client = type("C", (), {"host": peer})()

    from app.main import _login_key
    check("a spoofed XFF prefix does not change the throttle key",
          _login_key(_Req("9.9.9.9, 203.0.113.5", "127.0.0.1"), "a")
          == "203.0.113.5|a")
    check("XFF is ignored on a direct connection",
          _login_key(_Req("9.9.9.9", "198.51.100.7"), "a") == "198.51.100.7|a")

    print()
    if fails:
        print(f"{len(fails)} CHECK(S) FAILED:")
        for f in fails:
            print("  -", f)
    else:
        print("ALL AUTH CHECKS PASSED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
