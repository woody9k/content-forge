"""Credential resolution for the Claude CLI.

The CLI's own precedence is:

    ANTHROPIC_API_KEY  >  CLAUDE_CODE_OAUTH_TOKEN  >  ~/.claude/.credentials.json

which is a trap: a stray API key in the environment silently wins and bills you
per token instead of using the subscription. So we do not inherit the ambient
environment blindly — `subprocess_env()` builds the child environment
explicitly, strips the API-key variables, and injects the token we manage.

That indirection is also what makes the token hot-swappable from the UI. If the
token only arrived via systemd's `EnvironmentFile=`, changing it would mean
editing a file and restarting the unit; here the token is read off disk at the
moment we spawn the CLI, so saving a new one on /auth takes effect immediately.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import TOKEN_FILE

TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
# Recorded beside the token so the UI can age it. `claude setup-token` tokens are
# opaque — there is no expiry to read out of the string — so the saved date plus
# the documented ~1 year lifetime is the best estimate available.
SAVED_AT_VAR = "CF_TOKEN_SAVED_AT"
TOKEN_LIFETIME_DAYS = 365
# Both outrank the OAuth token inside the CLI, so both get stripped.
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_MANAGED_VARS = (TOKEN_VAR, SAVED_AT_VAR)


def credentials_path() -> Path:
    """Where `claude login` parks its OAuth grant."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / ".credentials.json"


# --------------------------------------------------------------------------
# the managed token
# --------------------------------------------------------------------------

def read_token() -> tuple[str, str]:
    """(token, source). Source is 'file', 'environment' or ''."""
    if TOKEN_FILE.exists():
        try:
            for line in TOKEN_FILE.read_text(encoding="utf-8").splitlines():
                if line.startswith(f"{TOKEN_VAR}="):
                    tok = line.split("=", 1)[1].strip()
                    if tok:
                        return tok, "file"
        except OSError:
            pass
    tok = os.environ.get(TOKEN_VAR, "").strip()
    return (tok, "environment") if tok else ("", "")


def _other_lines() -> list[str]:
    """Everything in the file that is not ours to manage, so we preserve it."""
    if not TOKEN_FILE.exists():
        return []
    try:
        return [ln for ln in TOKEN_FILE.read_text(encoding="utf-8").splitlines()
                if ln.strip()
                and not any(ln.startswith(f"{v}=") for v in _MANAGED_VARS)]
    except OSError:
        return []


def _write(lines: list[str]) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(TOKEN_FILE.suffix + ".tmp")
    # Create with 0600 from the outset — never let the secret exist world-readable,
    # not even for the instant between write and chmod.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(ln + "\n")
    tmp.replace(TOKEN_FILE)
    os.chmod(TOKEN_FILE, 0o600)


def save_token(token: str) -> None:
    """Write the token 0600, preserving any other variables in the file."""
    token = token.strip()
    if not token:
        raise ValueError("Empty token.")
    if "\n" in token or "\r" in token:
        raise ValueError("Token contains a line break — paste it as one line.")

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write(_other_lines() + [f"{TOKEN_VAR}={token}", f"{SAVED_AT_VAR}={stamp}"])


def clear_token() -> bool:
    """Drop the managed token; the CLI falls back to ~/.claude/.credentials.json."""
    if not TOKEN_FILE.exists():
        return False
    keep = _other_lines()
    if keep:
        _write(keep)
    else:
        TOKEN_FILE.unlink()
    return True


def token_age() -> dict:
    """When the current token was saved, and roughly when it lapses.

    Falls back to the file's mtime for tokens written by set-token.sh or
    install-service.sh, which predate the saved-at stamp.
    """
    out = {"saved_at": "", "expires_at": "", "days_left": None,
           "expired": False, "exact": False}
    saved, exact = None, False

    if TOKEN_FILE.exists():
        try:
            for line in TOKEN_FILE.read_text(encoding="utf-8").splitlines():
                if line.startswith(f"{SAVED_AT_VAR}="):
                    saved = datetime.fromisoformat(line.split("=", 1)[1].strip())
                    exact = True
                    break
        except (OSError, ValueError):
            saved = None
        if saved is None:
            try:
                saved = datetime.fromtimestamp(TOKEN_FILE.stat().st_mtime,
                                               timezone.utc)
            except OSError:
                saved = None

    if saved is None:
        return out
    if saved.tzinfo is None:
        saved = saved.replace(tzinfo=timezone.utc)

    lapses = saved + timedelta(days=TOKEN_LIFETIME_DAYS)
    delta = lapses - datetime.now(timezone.utc)
    out.update(saved_at=saved.isoformat(timespec="seconds"),
               expires_at=lapses.isoformat(timespec="seconds"),
               days_left=int(delta.total_seconds() // 86400),
               expired=delta.total_seconds() <= 0, exact=exact)
    return out


def mask(token: str) -> str:
    """Enough to tell two tokens apart, never enough to use one."""
    if not token:
        return ""
    if len(token) <= 16:
        return "•" * len(token)
    return f"{token[:12]}…{token[-4:]}"


# --------------------------------------------------------------------------
# the fallback credential
# --------------------------------------------------------------------------

def credentials_info() -> dict:
    """What `claude login` left behind, and whether it is still good.

    `claude auth status` reports this as logged-in whether or not the grant has
    expired, which is how a four-month-dead credential can sit behind a green
    banner. Reading expiresAt ourselves is the only cheap way to know.
    """
    p = credentials_path()
    out = {"exists": p.exists(), "path": str(p), "expires_at": "",
           "expired": False, "subscription": "", "days_left": None}
    if not p.exists():
        return out
    try:
        oauth = json.loads(p.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return out

    out["subscription"] = str(oauth.get("subscriptionType") or "")
    raw = oauth.get("expiresAt")
    if isinstance(raw, (int, float)) and raw > 0:
        # milliseconds since epoch in every version that writes this key
        when = datetime.fromtimestamp(raw / 1000, timezone.utc)
        out["expires_at"] = when.isoformat(timespec="seconds")
        delta = when - datetime.now(timezone.utc)
        out["expired"] = delta.total_seconds() <= 0
        out["days_left"] = int(delta.total_seconds() // 86400)
    return out


# --------------------------------------------------------------------------
# what the CLI actually gets
# --------------------------------------------------------------------------

def subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    for var in API_KEY_VARS:
        env.pop(var, None)
    token, _ = read_token()
    if token:
        env[TOKEN_VAR] = token
    else:
        env.pop(TOKEN_VAR, None)
    return env


def stray_api_keys() -> list[str]:
    """API-key variables present in our environment that we are suppressing."""
    return [v for v in API_KEY_VARS if os.environ.get(v)]


def state() -> dict:
    """Everything the /auth page and /health need, minus the secret itself."""
    token, source = read_token()
    creds = credentials_info()

    if token:
        method = "token"
        summary = f"subscription token ({source})"
    elif creds["exists"]:
        method = "credentials"
        summary = "~/.claude/.credentials.json (from `claude login`)"
    else:
        method = "none"
        summary = "no credential configured"

    return {
        "method": method,
        "summary": summary,
        # Whether the fallback credential matters at all right now. A stale
        # .credentials.json behind a working token is trivia, not a problem.
        "credentials_in_use": method == "credentials",
        "token_age": token_age() if token else
                     {"saved_at": "", "expires_at": "", "days_left": None,
                      "expired": False, "exact": False},
        "token_present": bool(token),
        "token_source": source,
        "token_masked": mask(token),
        "token_file": str(TOKEN_FILE),
        "token_file_exists": TOKEN_FILE.exists(),
        "credentials": creds,
        "stray_api_keys": stray_api_keys(),
    }
