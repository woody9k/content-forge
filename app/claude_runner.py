"""Thin wrapper around the Claude Code CLI in headless mode.

Auth: credentials come from `auth.subprocess_env()`, which prefers the token
managed on the /auth page and strips any ambient API key so we stay on the
subscription. No API key is used, so this runs on the Claude subscription.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import auth
from .config import (ALLOWED_TOOLS, CLAUDE_BIN, CLAUDE_MAX_TURNS, CLAUDE_MODEL,
                     CLAUDE_TIMEOUT, FAKE_CLAUDE)


class ClaudeError(RuntimeError):
    pass


@dataclass
class ClaudeResult:
    text: str
    cost_usd: float = 0.0
    duration_s: float = 0.0
    num_turns: int = 0
    session_id: str = ""


def cli_version() -> tuple[bool, str]:
    """(binary present and runnable, version string or the reason it isn't)."""
    if FAKE_CLAUDE:
        return True, "fake"
    try:
        ver = subprocess.run([CLAUDE_BIN, "--version"], capture_output=True,
                             text=True, timeout=20)
        if ver.returncode != 0:
            return False, (ver.stderr or ver.stdout).strip()[:400]
        return True, ver.stdout.strip()
    except FileNotFoundError:
        return False, f"{CLAUDE_BIN} not found on PATH"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# Last real API round-trip, so the banner can report a verified state without
# spending a request on every page load. See probe().
_probe_lock = threading.Lock()
_last_probe: dict = {}


def last_probe() -> dict:
    with _probe_lock:
        return dict(_last_probe)


def _record_probe(ok: bool, detail: str) -> dict:
    entry = {"ok": ok, "detail": detail,
             "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with _probe_lock:
        _last_probe.clear()
        _last_probe.update(entry)
    return entry


def probe(token: str = "", timeout: int = 120) -> dict:
    """One real turn against the API — the only way to know a credential works.

    `claude auth status` reports a stored grant as logged-in whether or not it
    has expired, so it cannot answer this question. Pass `token` to test a
    candidate before saving it; otherwise the currently-resolved credential is
    used. Result is cached in `last_probe()`.
    """
    if FAKE_CLAUDE:
        return _record_probe(True, "fake CLI — not contacting the API")

    ok, version = cli_version()
    if not ok:
        return _record_probe(False, version)

    env = auth.subprocess_env()
    if token.strip():
        env[auth.TOKEN_VAR] = token.strip()

    cmd = [CLAUDE_BIN, "-p", "--output-format", "json", "--model", CLAUDE_MODEL,
           "--allowedTools", "", "--max-turns", "1"]
    with tempfile.TemporaryDirectory(prefix="content-forge-probe-") as workdir:
        try:
            proc = subprocess.run(cmd, input="Reply with the single word OK.",
                                  capture_output=True, text=True,
                                  timeout=timeout, cwd=workdir, env=env)
        except subprocess.TimeoutExpired:
            return _record_probe(False, f"No response within {timeout}s.")
        except FileNotFoundError:
            return _record_probe(False, f"Claude CLI not found at '{CLAUDE_BIN}'.")

    # A bad credential surfaces as an is_error payload on stdout with returncode 0
    # as often as it does a non-zero exit, so both paths have to be checked.
    try:
        payload = _parse_payload(proc.stdout)
    except ClaudeError:
        payload = {}

    if payload.get("is_error"):
        return _record_probe(False, str(payload.get("result", ""))[:400])
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return _record_probe(False, _friendly_error(err, proc.returncode))
    if not (payload.get("result") or "").strip():
        return _record_probe(False, "Empty response from the CLI.")

    return _record_probe(True, f"{version} — credential accepted")


def cli_available() -> tuple[bool, str]:
    """(usable, human-readable status) for the banner and /health.

    Deliberately does not spend an API request: it reports what can be known
    from disk, plus the last probe if one has been run. The one thing it will
    not do is call an expired credential healthy — that is what previously let
    a four-month-dead grant sit behind a green banner while every generation
    failed with a 401.
    """
    if FAKE_CLAUDE:
        return True, "fake"

    ok, version = cli_version()
    if not ok:
        return False, version

    st = auth.state()
    probed = last_probe()

    if st["method"] == "none":
        return False, (f"{version} — no credential. Paste a token from "
                       "`claude setup-token` on the Auth page.")

    if st["method"] == "credentials":
        creds = st["credentials"]
        if creds["expired"]:
            return False, (f"{version} — the stored login expired on "
                           f"{creds['expires_at'][:10]}. Paste a token from "
                           "`claude setup-token` on the Auth page.")
        note = f"{version} — auth: claude login"
        if creds["expires_at"]:
            note += f", expires {creds['expires_at'][:10]}"
    else:
        note = f"{version} — auth: subscription token"

    if probed:
        note += f" · {'verified' if probed['ok'] else 'FAILED'} {probed['at'][11:16]}Z"
        if not probed["ok"]:
            return False, note + f" — {probed['detail'][:200]}"
    else:
        note += " · untested"

    if st["stray_api_keys"]:
        note += f"  (ignoring {', '.join(st['stray_api_keys'])})"
    return True, note


def run(prompt: str, *, system: str = "", use_tools: bool = False,
        model: str | None = None, timeout: int | None = None) -> ClaudeResult:
    """Run one headless turn and return the assistant's final text."""
    if FAKE_CLAUDE:
        return _fake(prompt, use_tools)

    cmd = [CLAUDE_BIN, "-p", "--output-format", "json",
           "--model", model or CLAUDE_MODEL]

    if system:
        cmd += ["--append-system-prompt", system]

    if use_tools:
        cmd += ["--allowedTools", *shlex.split(ALLOWED_TOOLS),
                "--max-turns", str(CLAUDE_MAX_TURNS)]
    else:
        # No tools at all: pure text generation, one turn.
        cmd += ["--allowedTools", "", "--max-turns", "1"]

    started = time.time()
    # Run in a throwaway cwd so nothing gets written into the content root and
    # no stray CLAUDE.md from elsewhere leaks into the context.
    with tempfile.TemporaryDirectory(prefix="content-forge-") as workdir:
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True,
                timeout=timeout or CLAUDE_TIMEOUT, cwd=workdir,
                env=auth.subprocess_env(),
            )
        except subprocess.TimeoutExpired:
            raise ClaudeError(
                f"Claude timed out after {timeout or CLAUDE_TIMEOUT}s. "
                "Raise CF_CLAUDE_TIMEOUT or shorten the brief."
            ) from None
        except FileNotFoundError:
            raise ClaudeError(f"Claude CLI not found at '{CLAUDE_BIN}'.") from None

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise ClaudeError(_friendly_error(err, proc.returncode))

    payload = _parse_payload(proc.stdout)

    if payload.get("is_error"):
        # An auth failure usually lands here — returncode 0, error in the payload.
        raise ClaudeError(_friendly_error(
            str(payload.get("result", "Claude reported an error.")), 0))

    text = payload.get("result") or ""
    if not text.strip():
        raise ClaudeError("Claude returned an empty response.")

    return ClaudeResult(
        text=text.strip(),
        cost_usd=float(payload.get("total_cost_usd") or 0.0),
        duration_s=round(time.time() - started, 1),
        num_turns=int(payload.get("num_turns") or 0),
        session_id=str(payload.get("session_id") or ""),
    )


def _parse_payload(raw: str) -> dict:
    raw = (raw or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Some versions can emit a non-JSON preamble line; salvage the object.
        m = re.search(r"\{.*\}\s*$", raw, re.S)
        if not m:
            raise ClaudeError(f"Unparseable CLI output: {raw[:500]}") from None
        return json.loads(m.group(0))


def _friendly_error(err: str, code: int) -> str:
    low = err.lower()
    # code 0 means the failure came back inside the JSON payload, not from the
    # process — reporting "exit 0" there reads as a bug rather than a diagnosis.
    exit_note = f" (exit {code})" if code else ""
    if ("not logged in" in low or "authentication" in low or "unauthorized" in low
            or "oauth" in low or "401" in low):
        return ("Claude rejected the credential. Open the Auth page to test it "
                "and paste a fresh token from `claude setup-token`. "
                f"{err[:300]}{exit_note}")
    if "usage limit" in low or "rate limit" in low:
        return f"Claude usage limit reached — try again later.{exit_note}"
    return f"Claude CLI failed{exit_note}: {err[:800]}"


# --------------------------------------------------------------------------
# stub used by the smoke test / offline development
# --------------------------------------------------------------------------

def _fake(prompt: str, use_tools: bool) -> ClaudeResult:
    time.sleep(0.2)
    if "VOICE PROFILE" in prompt:
        text = ("## Voice in one paragraph\nDirect, dry, allergic to filler.\n\n"
                "## Signature moves\n- Opens on a concrete detail.\n\n"
                "## Do not\n- No 'in today's fast-paced world'.\n")
    elif prompt.lstrip().startswith("Topic area:"):
        text = json.dumps([
            {"title": "Stub idea one", "angle": "An angle.", "why": "A reason.",
             "format": "blog post", "words": 900},
            {"title": "Stub idea two", "angle": "Another angle.", "why": "Another reason.",
             "format": "how-to", "words": 1200},
        ])
    else:
        refs = ("\n\n## References\n\n1. Example Publication — A Real Article — "
                "https://example.com/a\n") if use_tools else ""
        text = ("# Stub Article\n\nThis is stub output from the fake Claude runner.\n\n"
                "## A subhead\n\nBody text with **bold**, *italic*, `code` and a "
                "[link](https://example.com).\n\n- bullet one\n- bullet two\n\n"
                "1. first\n2. second\n\n> A blockquote.\n\n```python\n"
                "print('hello')\n```\n" + refs)
    return ClaudeResult(text=text, cost_usd=0.0, duration_s=0.2, num_turns=1)
