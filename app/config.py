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
