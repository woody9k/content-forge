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

APP_DIR = Path(__file__).resolve().parent
