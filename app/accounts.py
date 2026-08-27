"""User accounts, sessions, invites and personal access tokens.

This replaces the single shared ``CF_ACCESS_TOKEN``. That token was one secret
for every caller and every route: it could not say *who* was acting, could not
be revoked for one person without locking out everybody, and had to be pasted
into the browser as a Basic-auth password. Now there are real accounts.

Three ways to prove who you are, all resolving to the same ``User``:

  - **session cookie** — the browser, after a form login
  - **personal access token** — scripts and CLI clients, as a Bearer token
  - **OAuth access token** — MCP connectors, issued by ``oauth_provider``

Storage is SQLite rather than the flat folders the *content* lives in. Tokens
need atomic expiry, indexed lookup and revocation; a directory of files gives
none of those. ``storage.py`` is untouched — content stays exactly as it was.

Note there is no self-signup. This box answers on the public internet and every
generation spends the owner's Claude subscription, so accounts are created from
the shell by someone who already has it::

    python -m app.accounts add alice --admin
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import AUTH_DB, INVITE_TTL, PAT_PREFIX, SESSION_TTL

# scrypt work factors. n=2**14 costs ~16MB and a few tens of milliseconds per
# hash on this box's 2 vCPUs. The next notch up (2**15) is stronger but wants
# 33MB, which both trips OpenSSL's default 32MB envelope limit and turns the
# login form into a memory-exhaustion lever on a 2GB machine — every anonymous
# attempt would allocate it. maxmem is therefore set explicitly rather than
# left to the library default, so the cost is a decision and not an accident.
_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str
    is_admin: bool
    disabled: bool = False


@dataclass(frozen=True)
class TokenInfo:
    """A personal access token as shown on the account page (never the secret)."""
    id: int
    name: str
    created_at: int
    last_used: int | None


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT    NOT NULL DEFAULT '',
    password_hash TEXT,                      -- NULL until an invite is claimed
    salt          BLOB,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    token_hash  TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  INTEGER NOT NULL,
    used_at     INTEGER
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS pats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash  TEXT    NOT NULL UNIQUE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    created_at  INTEGER NOT NULL,
    last_used   INTEGER
);
CREATE INDEX IF NOT EXISTS pats_user ON pats(user_id);
"""


def _db_path() -> Path:
    AUTH_DB.parent.mkdir(parents=True, exist_ok=True)
    return AUTH_DB


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """A short-lived connection. Opened per operation: traffic here is a
    handful of requests a minute, and per-call connections sidestep the
    thread-affinity rules that bite when a pooled handle meets an async app."""
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create the schema, and lock the file down before anything is written to
    it. The database holds password hashes and token hashes; on a box where the
    content directory is deliberately readable, this one should not be."""
    path = _db_path()
    with connect() as conn:
        conn.executescript(_SCHEMA)
    # Unconditionally, not just on creation: a database that already exists
    # with loose permissions is exactly the one worth fixing.
    os.chmod(path, 0o600)


# --------------------------------------------------------------------------
# hashing
# --------------------------------------------------------------------------

def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT).hex()


def _hash_token(raw: str) -> str:
    """Bearer-style secrets are 256 bits of CSPRNG output, so a plain SHA-256
    is right here — there is no low-entropy guess space for a KDF to slow down,
    and these are checked on every request."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_secret() -> str:
    return secrets.token_urlsafe(32)


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"] or row["username"],
        is_admin=bool(row["is_admin"]),
        disabled=bool(row["disabled"]),
    )


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------

def count_users() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]


def list_users() -> list[User]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    return [_row_to_user(r) for r in rows]


def get_user(user_id: int) -> User | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_name(username: str) -> User | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    return _row_to_user(row) if row else None


def create_user(username: str, *, display_name: str = "", is_admin: bool = False,
                password: str | None = None) -> User:
    """Create an account. With no password the account cannot be logged into
    until an invite is claimed, which is the normal path."""
    username = username.strip()
    if not username:
        raise ValueError("username may not be blank")
    if len(username) > 64:
        raise ValueError("username is too long (64 characters max)")

    if password is not None and len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    salt = secrets.token_bytes(16) if password else None
    pw_hash = _hash_password(password, salt) if password else None
    try:
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, display_name, password_hash, salt,"
                " is_admin, disabled, created_at) VALUES (?,?,?,?,?,0,?)",
                (username, display_name or username, pw_hash, salt,
                 int(is_admin), int(time.time())))
            uid = cur.lastrowid
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"a user named {username!r} already exists") from exc
    user = get_user(uid)
    assert user is not None
    return user


def set_password(user_id: int, password: str) -> None:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    salt = secrets.token_bytes(16)
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash=?, salt=? WHERE id=?",
                     (_hash_password(password, salt), salt, user_id))


def set_admin(user_id: int, is_admin: bool) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET is_admin=? WHERE id=?",
                     (int(is_admin), user_id))


def set_disabled(user_id: int, disabled: bool) -> None:
    """Disabling drops every live session too — otherwise the account stays
    usable until whatever cookie they already hold happens to expire."""
    with connect() as conn:
        conn.execute("UPDATE users SET disabled=? WHERE id=?",
                     (int(disabled), user_id))
        if disabled:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def delete_user(user_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


def verify_password(username: str, password: str) -> User | None:
    """Check a login. Returns None for unknown user, wrong password, an
    unclaimed invite, or a disabled account — the caller must not distinguish
    them in what it shows, or it becomes a username oracle."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()

    if row is None:
        # Hash anyway. Returning early on an unknown username makes the reply
        # measurably faster than a wrong password, which enumerates accounts.
        _hash_password(password, b"\x00" * 16)
        return None
    if row["password_hash"] is None or row["disabled"]:
        _hash_password(password, b"\x00" * 16)
        return None

    expected = row["password_hash"]
    actual = _hash_password(password, row["salt"])
    if not hmac.compare_digest(expected, actual):
        return None
    return _row_to_user(row)


# --------------------------------------------------------------------------
# invites
# --------------------------------------------------------------------------

def create_invite(user_id: int, ttl: int = INVITE_TTL) -> str:
    """Mint a single-use link for a user to set their own password. Returns the
    raw token — the only time it exists in plaintext."""
    raw = _new_secret()
    with connect() as conn:
        # One live invite per user: minting a new one voids any earlier link,
        # so a leaked invite stops working the moment you reissue.
        conn.execute("DELETE FROM invites WHERE user_id=?", (user_id,))
        conn.execute(
            "INSERT INTO invites (token_hash, user_id, expires_at) VALUES (?,?,?)",
            (_hash_token(raw), user_id, int(time.time()) + ttl))
    return raw


def peek_invite(raw: str) -> User | None:
    """Who a still-valid invite belongs to, without consuming it."""
    with connect() as conn:
        row = conn.execute(
            "SELECT user_id, expires_at, used_at FROM invites WHERE token_hash=?",
            (_hash_token(raw),)).fetchone()
    if row is None or row["used_at"] is not None:
        return None
    if row["expires_at"] < time.time():
        return None
    return get_user(row["user_id"])


def claim_invite(raw: str, password: str) -> User | None:
    """Consume an invite and set the password. Returns None if the link is
    unknown, already used or expired."""
    user = peek_invite(raw)
    if user is None:
        return None
    set_password(user.id, password)
    with connect() as conn:
        conn.execute("UPDATE invites SET used_at=? WHERE token_hash=?",
                     (int(time.time()), _hash_token(raw)))
    return user


# --------------------------------------------------------------------------
# sessions (browser)
# --------------------------------------------------------------------------

def create_session(user_id: int, ttl: int = SESSION_TTL) -> str:
    raw = _new_secret()
    now = int(time.time())
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at)"
            " VALUES (?,?,?,?)", (_hash_token(raw), user_id, now, now + ttl))
        # Opportunistic sweep; there is no cron on this box and expired rows
        # are dead weight that would otherwise accumulate forever.
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    return raw


def session_user(raw: str) -> User | None:
    if not raw:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash=? AND s.expires_at > ? AND u.disabled=0",
            (_hash_token(raw), int(time.time()))).fetchone()
    return _row_to_user(row) if row else None


def destroy_session(raw: str) -> None:
    if not raw:
        return
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(raw),))


# --------------------------------------------------------------------------
# personal access tokens (scripts, CLI)
# --------------------------------------------------------------------------

def create_pat(user_id: int, name: str) -> str:
    """Mint a personal access token. Returned once, stored only as a hash —
    which is why the account page can show you the list but never the secret."""
    name = (name or "").strip() or "unnamed"
    raw = f"{PAT_PREFIX}{_new_secret()}"
    with connect() as conn:
        conn.execute(
            "INSERT INTO pats (token_hash, user_id, name, created_at)"
            " VALUES (?,?,?,?)", (_hash_token(raw), user_id, name, int(time.time())))
    return raw


def pat_user(raw: str) -> User | None:
    """Resolve a personal access token. The prefix check is a cheap reject so a
    session cookie or an OAuth token presented here does not cost a DB round
    trip before being turned away."""
    if not raw or not raw.startswith(PAT_PREFIX):
        return None
    token_hash = _hash_token(raw)
    with connect() as conn:
        row = conn.execute(
            "SELECT u.*, p.id pat_id FROM pats p JOIN users u ON u.id = p.user_id"
            " WHERE p.token_hash=? AND u.disabled=0", (token_hash,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE pats SET last_used=? WHERE id=?",
                     (int(time.time()), row["pat_id"]))
    return _row_to_user(row)


def list_pats(user_id: int) -> list[TokenInfo]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, last_used FROM pats WHERE user_id=?"
            " ORDER BY created_at DESC", (user_id,)).fetchall()
    return [TokenInfo(r["id"], r["name"], r["created_at"], r["last_used"])
            for r in rows]


def revoke_pat(pat_id: int, user_id: int) -> bool:
    """Scoped to the owner on purpose: the id alone must not let one user
    revoke another's token."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM pats WHERE id=? AND user_id=?",
                           (pat_id, user_id))
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# resolving a request to a user
# --------------------------------------------------------------------------

def user_from_bearer(token: str) -> User | None:
    """A Bearer credential that is not a session cookie: either a personal
    access token or an OAuth access token. Imported lazily because
    ``oauth_provider`` imports this module."""
    user = pat_user(token)
    if user is not None:
        return user
    from . import oauth_provider
    return oauth_provider.user_for_access_token(token)


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="python -m app.accounts",
        description="Manage Content Forge accounts.")
    sub = p.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="create a user and print an invite link")
    add.add_argument("username")
    add.add_argument("--admin", action="store_true",
                     help="may rotate the Claude token and manage users")
    add.add_argument("--name", default="", help="display name")

    inv = sub.add_parser("invite", help="reissue an invite link")
    inv.add_argument("username")

    sub.add_parser("list", help="list accounts")

    for name, helptext in (("disable", "block sign-in"), ("enable", "unblock"),
                           ("promote", "grant admin"), ("demote", "revoke admin"),
                           ("delete", "remove the account entirely")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("username")

    args = p.parse_args(argv)
    init_db()

    def _find(name: str) -> User:
        u = get_user_by_name(name)
        if u is None:
            raise SystemExit(f"no such user: {name}")
        return u

    if args.cmd == "add":
        try:
            user = create_user(args.username, display_name=args.name,
                               is_admin=args.admin)
        except ValueError as exc:
            raise SystemExit(str(exc))
        raw = create_invite(user.id)
        _print_invite(user, raw)

    elif args.cmd == "invite":
        user = _find(args.username)
        _print_invite(user, create_invite(user.id))

    elif args.cmd == "list":
        users = list_users()
        if not users:
            print("no accounts yet — create one with: "
                  "python -m app.accounts add <name> --admin")
            return 0
        width = max(len(u.username) for u in users)
        for u in users:
            flags = ", ".join(
                f for f in ("admin" if u.is_admin else "",
                            "disabled" if u.disabled else "") if f)
            print(f"  {u.username:<{width}}  {flags or 'user'}")

    elif args.cmd in ("disable", "enable"):
        user = _find(args.username)
        set_disabled(user.id, args.cmd == "disable")
        print(f"{user.username} {args.cmd}d")

    elif args.cmd in ("promote", "demote"):
        user = _find(args.username)
        set_admin(user.id, args.cmd == "promote")
        print(f"{user.username} is now {'an admin' if args.cmd == 'promote' else 'a regular user'}")

    elif args.cmd == "delete":
        user = _find(args.username)
        delete_user(user.id)
        print(f"{user.username} deleted")

    return 0


def _print_invite(user: User, raw: str) -> None:
    from .config import public_base_url
    base = public_base_url() or "http://localhost:8080"
    print(f"\nInvite for {user.username}"
          f"{' (admin)' if user.is_admin else ''} — valid {INVITE_TTL // 3600}h,"
          f" single use:\n\n    {base}/invite/{raw}\n")


if __name__ == "__main__":
    raise SystemExit(_main())
