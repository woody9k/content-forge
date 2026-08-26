"""Markdown rendering and model-output tidying.

Split out of main.py so the MCP server can reuse it without importing the web
routes (which would import the MCP server back).
"""
from __future__ import annotations

import re

from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})


def render_markdown(text: str) -> str:
    return _md.render(text or "")


def clean_markdown(text: str) -> str:
    """Strip a wrapping code fence and any 'Here is your article' preamble."""
    t = (text or "").strip()
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*?)\n```\s*$", t, re.S)
    if m:
        t = m.group(1).strip()
    t = re.sub(r"^(?:here(?:'s| is)[^\n]*|sure[^\n]*)\n+(?=#)", "", t, flags=re.I)
    return t.strip()


def title_from_markdown(md: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", md or "", re.M)
    return m.group(1).strip() if m else fallback
