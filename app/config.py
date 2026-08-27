"""Configuration, all overridable by environment variable."""
import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Where topics/articles/voice samples live on disk.
CONTENT_ROOT = Path(
    os.environ.get("CF_CONTENT_ROOT", str(Path.home() / "content-forge-data"))
).expanduser()

# Path to the Claude Code CLI binary.
CLAUDE_BIN = os.environ.get("CF_CLAUDE_BIN", "claude")

# 0600 file holding CLAUDE_CODE_OAUTH_TOKEN. Read when we spawn the CLI rather
# than at startup, so rotating the token on /auth takes effect without a restart.
TOKEN_FILE = Path(
    os.environ.get("CF_TOKEN_FILE",
                   str(Path.home() / ".config" / "content-forge" / "env"))
).expanduser()

# Model alias passed to `claude --model`. sonnet is the sane default for
# article generation; "opus" for heavier work.
CLAUDE_MODEL = os.environ.get("CF_CLAUDE_MODEL", "sonnet")

# Hard ceiling on a single generation, seconds.
CLAUDE_TIMEOUT = int(os.environ.get("CF_CLAUDE_TIMEOUT", "900"))

# Max agent turns for a research-heavy article.
CLAUDE_MAX_TURNS = int(os.environ.get("CF_CLAUDE_MAX_TURNS", "40"))

# How many generations may run at once.
MAX_CONCURRENT_JOBS = int(os.environ.get("CF_MAX_CONCURRENT_JOBS", "2"))

# Set CF_FAKE_CLAUDE=1 to stub the CLI out (used by the smoke test).
FAKE_CLAUDE = _bool("CF_FAKE_CLAUDE", False)

HOST = os.environ.get("CF_HOST", "0.0.0.0")
PORT = int(os.environ.get("CF_PORT", "8080"))

# Tools the agent is allowed to use. WebSearch/WebFetch power the
# "with references" mode; nothing else is needed since we capture stdout.
ALLOWED_TOOLS = os.environ.get("CF_ALLOWED_TOOLS", "WebSearch WebFetch")

# Public hostname this instance is reached by, i.e. the reverse proxy's
# server_name. Empty means "local access only".
PUBLIC_HOST = os.environ.get("CF_PUBLIC_HOST", "contentforge.boblab.net")
PUBLIC_PORT = os.environ.get("CF_PUBLIC_PORT", "8443")

# Scheme clients reach this instance by. Behind the TLS proxy that is https;
# a bare local run is http. This is not cosmetic: the OAuth issuer identifier
# is compared by exact string (RFC 8414 3.3), so a wrong scheme or a stray
# trailing slash silently breaks connector discovery.
PUBLIC_SCHEME = os.environ.get("CF_PUBLIC_SCHEME",
                               "https" if PUBLIC_HOST else "http")


def public_base_url() -> str:
    """The origin this instance is reached by, with no trailing slash.

    Doubles as the OAuth issuer identifier and the base for invite links, so
    it must match what a browser actually types. Empty means "not publicly
    reachable", which disables the OAuth endpoints entirely.
    """
    override = os.environ.get("CF_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if override:
        return override
    if not PUBLIC_HOST:
        return ""
    implied = {"https": "443", "http": "80"}.get(PUBLIC_SCHEME)
    if PUBLIC_PORT and PUBLIC_PORT != implied:
        return f"{PUBLIC_SCHEME}://{PUBLIC_HOST}:{PUBLIC_PORT}"
    return f"{PUBLIC_SCHEME}://{PUBLIC_HOST}"


# --------------------------------------------------------------------------
# accounts, sessions and OAuth
#
# The single shared CF_ACCESS_TOKEN is gone. It could not say who was acting,
# could not be revoked for one person, and had to be typed into the browser.
# Callers now prove identity three ways, all resolving to a real user: a
# session cookie, a personal access token, or an OAuth token from a connector.
# --------------------------------------------------------------------------

# SQLite file holding accounts, sessions, tokens and OAuth registrations. It
# sits under the content root because systemd already grants that path write
# access; list_topics() scans a subdirectory and filters on is_dir(), so a
# stray file here does not show up as a topic. Created 0600.
AUTH_DB = Path(
    os.environ.get("CF_AUTH_DB", str(CONTENT_ROOT / "auth.db"))
).expanduser()

SESSION_COOKIE = os.environ.get("CF_SESSION_COOKIE", "cf_session")

# 14 days. Long enough not to nag a daily user, short enough that a forgotten
# session on a shared machine does not live forever.
SESSION_TTL = int(os.environ.get("CF_SESSION_TTL", str(14 * 24 * 3600)))

# Invite links are single use and short lived.
INVITE_TTL = int(os.environ.get("CF_INVITE_TTL", str(48 * 3600)))

# Marks a personal access token so it can be told apart from an OAuth token
# without a database round trip.
PAT_PREFIX = "cfp_"

# OAuth lifetimes. A short access token bounds the damage from a leaked one;
# the refresh token is what keeps a connector working without re-consent.
OAUTH_CODE_TTL = int(os.environ.get("CF_OAUTH_CODE_TTL", "300"))
OAUTH_ACCESS_TTL = int(os.environ.get("CF_OAUTH_ACCESS_TTL", "3600"))
OAUTH_REFRESH_TTL = int(os.environ.get("CF_OAUTH_REFRESH_TTL", str(30 * 24 * 3600)))

# The one scope this resource server understands. Kept deliberately coarse:
# the shared-workspace model means every signed-in user may already do
# everything the UI allows, so a finer split would claim a distinction the
# app does not actually enforce.
MCP_SCOPE = "content-forge"

# Set only so startup can warn that it no longer does anything.
LEGACY_ACCESS_TOKEN = os.environ.get("CF_ACCESS_TOKEN", "").strip()

# Whether callers must authenticate. On by default wherever a public URL is
# configured, which is the case that matters: this box answers on the open
# internet and every generation spends the owner's Claude subscription. The
# smoke tests bind loopback and turn it off explicitly.
REQUIRE_AUTH = _bool("CF_REQUIRE_AUTH", bool(public_base_url()))


_LOOPBACK = ("127.0.0.1", "localhost", "[::1]")


def _allowed_hosts() -> list[str]:
    """Host header values the MCP endpoint will answer to.

    FastMCP already turns DNS-rebinding protection on by itself, but the list
    it generates is loopback-only (``127.0.0.1:*`` and friends). Behind a TLS
    proxy the forwarded Host is the *public* name, which is not on that list,
    so every agent call comes back as a bare 421 "Invalid Host header".

    So this reproduces the SDK's loopback entries — the ":*" wildcard matters,
    the smoke tests bind an ephemeral port — and adds the proxy's server_name.
    """
    hosts = [f"{name}:*" for name in _LOOPBACK]
    if PUBLIC_HOST:
        # Host and host:port are separate entries as far as the SDK is
        # concerned, so a default-port URL still matches.
        hosts.append(PUBLIC_HOST)
        if PUBLIC_PORT:
            hosts.append(f"{PUBLIC_HOST}:{PUBLIC_PORT}")
    return hosts


def _allowed_origins() -> list[str]:
    """Origins accepted on cross-origin MCP calls (browser-based clients).

    Same story: replacing the setting drops the SDK's loopback defaults, so
    they are restored here alongside the public HTTPS origin.
    """
    origins = [f"http://{name}:*" for name in _LOOPBACK]
    if PUBLIC_HOST:
        origin = f"https://{PUBLIC_HOST}"
        origins.append(origin)
        if PUBLIC_PORT:
            origins.append(f"{origin}:{PUBLIC_PORT}")
    return origins


# Either list can be replaced wholesale with a comma-separated override.
MCP_ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get(
        "CF_MCP_ALLOWED_HOSTS", ",".join(_allowed_hosts())
    ).split(",") if h.strip()
]

MCP_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "CF_MCP_ALLOWED_ORIGINS", ",".join(_allowed_origins())
    ).split(",") if o.strip()
]


APP_DIR = Path(__file__).resolve().parent
