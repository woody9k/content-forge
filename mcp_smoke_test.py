"""End-to-end MCP test against a real server, with the Claude CLI stubbed out.

    CF_FAKE_CLAUDE=1 CF_CONTENT_ROOT=/tmp/cf-mcp .venv/bin/python mcp_smoke_test.py

Starts uvicorn on an ephemeral port and drives it with a genuine MCP client over
streamable HTTP, so the mount, the lifespan wiring and the protocol are all
exercised — not just the Python functions behind the tools.
"""
import asyncio
import json
import os
import shutil
import socket
import sys
import threading
from pathlib import Path

os.environ.setdefault("CF_FAKE_CLAUDE", "1")
ROOT = Path(os.environ.setdefault("CF_CONTENT_ROOT", "/tmp/cf-mcp"))
os.environ.setdefault("CF_TOKEN_FILE", str(ROOT / "token-env"))
# Deletes are opt-in; the test asserts both halves of that switch.
os.environ["CF_MCP_ALLOW_DELETE"] = "1"
if ROOT.exists():
    shutil.rmtree(ROOT)

import uvicorn  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

from app.main import app  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(f"{'  ok ' if cond else 'FAIL '} {label}"
          + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT,
                                       log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()


def text_of(result):
    """First text block of a tool result."""
    return result.content[0].text if result.content else ""


async def main():
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.1)
    check("server started", server.started)

    url = f"http://127.0.0.1:{PORT}/mcp"
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            check("initialize", init.serverInfo.name == "content_forge_mcp",
                  init.serverInfo.name)
            check("instructions advertised", bool(init.instructions))

            tools = {t.name: t for t in (await s.list_tools()).tools}
            check("tools listed", len(tools) >= 17, str(len(tools)))
            check("delete tools gated on", "content_forge_delete_topic" in tools)
            check("every tool documented",
                  all(t.description and len(t.description) > 60
                      for t in tools.values()),
                  str([n for n, t in tools.items()
                       if not t.description or len(t.description) <= 60]))
            check("every tool annotated",
                  all(t.annotations and t.annotations.title
                      for t in tools.values()))
            check("read tools flagged read-only",
                  tools["content_forge_list_topics"].annotations.readOnlyHint
                  is True)
            check("destructive tools flagged",
                  tools["content_forge_delete_topic"].annotations.destructiveHint
                  is True)
            check("schemas name their params",
                  "topic" in tools["content_forge_get_article"].inputSchema["properties"])

            # ---------------- topics ----------------
            r = await s.call_tool("content_forge_create_topic", {
                "name": "Boat Life", "description": "Docks, wakeboards, coolers.",
                "audience": "adults who own boats"})
            topic = json.loads(text_of(r))
            check("create_topic", topic["slug"] == "boat-life", str(topic))

            r = await s.call_tool("content_forge_list_topics", {})
            page = json.loads(text_of(r))
            check("list_topics paginates",
                  {"total", "count", "offset", "has_more", "items"} <= page.keys())
            check("list_topics finds it", page["total"] == 1)

            r = await s.call_tool("content_forge_list_topics",
                                  {"response_format": "markdown"})
            check("markdown format", "`boat-life`" in text_of(r))

            r = await s.call_tool("content_forge_get_topic", {"topic": "nope"})
            check("unknown topic is an actionable error",
                  r.isError and "boat-life" in text_of(r), text_of(r)[:160])

            # ---------------- voice ----------------
            r = await s.call_tool("content_forge_add_voice_sample", {
                "topic": "boat-life", "text": "Sample writing about docks. " * 40,
                "name": "dock notes"})
            check("add_voice_sample", json.loads(text_of(r))["total_samples"] == 1)

            r = await s.call_tool("content_forge_build_voice_profile",
                                  {"topic": "boat-life"})
            job = json.loads(text_of(r))
            check("build_voice_profile returns a job", bool(job["job_id"]), str(job))

            r = await s.call_tool("content_forge_get_job",
                                  {"job_id": job["job_id"], "wait_seconds": 20})
            done = json.loads(text_of(r))
            check("voice job completes", done["status"] == "done", str(done))

            r = await s.call_tool("content_forge_get_voice_profile",
                                  {"topic": "boat-life"})
            check("voice profile readable",
                  json.loads(text_of(r))["has_profile"] is True)

            # ---------------- articles ----------------
            r = await s.call_tool("content_forge_write_article", {
                "topic": "boat-life", "title": "Choosing a dock ladder",
                "brief": "Compare the options.", "format": "how-to",
                "words": 800, "references": True})
            started = json.loads(text_of(r))
            check("write_article returns job and slug",
                  started["job_id"] and started["article"], str(started))
            aslug = started["article"]

            r = await s.call_tool("content_forge_get_job",
                                  {"job_id": started["job_id"], "wait_seconds": 25})
            done = json.loads(text_of(r))
            check("article job completes", done["status"] == "done", str(done))
            check("job result carries word count",
                  done["result"]["words"] > 0, str(done["result"]))

            r = await s.call_tool("content_forge_get_article",
                                  {"topic": "boat-life", "article": aslug})
            md = text_of(r)
            check("get_article markdown", md.startswith("# Stub Article"), md[:80])
            check("references section present", "## References" in md)

            r = await s.call_tool("content_forge_get_article", {
                "topic": "boat-life", "article": aslug, "format": "html"})
            html = text_of(r)
            check("get_article html renders",
                  "<h1>" in html and "<li>" in html and "<a href=" in html,
                  html[:120])

            r = await s.call_tool("content_forge_get_article", {
                "topic": "boat-life", "article": aslug, "format": "json"})
            meta = json.loads(text_of(r))
            check("get_article json has metadata",
                  meta["status"] == "ready" and meta["words"] > 0
                  and meta["has_references_section"] is True, str(meta)[:200])

            r = await s.call_tool("content_forge_list_articles",
                                  {"topic": "boat-life", "status": "ready"})
            check("list_articles filters by status",
                  json.loads(text_of(r))["total"] == 1)
            r = await s.call_tool("content_forge_list_articles",
                                  {"topic": "boat-life", "status": "error"})
            check("status filter excludes", json.loads(text_of(r))["total"] == 0)

            body = "# Edited By Agent\n\nRewritten **body** for publishing.\n"
            r = await s.call_tool("content_forge_update_article", {
                "topic": "boat-life", "article": aslug, "body": body})
            check("update_article", json.loads(text_of(r))["title"]
                  == "Edited By Agent", text_of(r)[:150])
            r = await s.call_tool("content_forge_get_article",
                                  {"topic": "boat-life", "article": aslug})
            check("update round-trips", "Rewritten **body**" in text_of(r))

            r = await s.call_tool("content_forge_export_article_docx",
                                  {"topic": "boat-life", "article": aslug})
            exp = json.loads(text_of(r))
            check("export_article_docx", exp["bytes"] > 5000
                  and Path(exp["path"]).exists(), str(exp))

            # ---------------- ideas ----------------
            r = await s.call_tool("content_forge_suggest_ideas",
                                  {"topic": "boat-life", "count": 2})
            job = json.loads(text_of(r))
            r = await s.call_tool("content_forge_get_job",
                                  {"job_id": job["job_id"], "wait_seconds": 20})
            check("ideas job completes",
                  json.loads(text_of(r))["status"] == "done", text_of(r)[:200])
            r = await s.call_tool("content_forge_list_ideas", {"topic": "boat-life"})
            check("list_ideas", json.loads(text_of(r))["count"] == 2)

            # ---------------- health and errors ----------------
            r = await s.call_tool("content_forge_health", {})
            h = json.loads(text_of(r))
            check("health", h["ok"] is True and h["topics"] == 1, str(h)[:200])

            r = await s.call_tool("content_forge_get_job", {"job_id": "deadbeef"})
            check("unknown job is an actionable error",
                  r.isError and "content_forge_list_articles" in text_of(r),
                  text_of(r)[:160])

            r = await s.call_tool("content_forge_get_article",
                                  {"topic": "boat-life", "article": "ghost"})
            check("unknown article names the known ones",
                  r.isError and aslug in text_of(r), text_of(r)[:160])

            r = await s.call_tool("content_forge_write_article",
                                  {"topic": "boat-life", "title": "x", "words": 99})
            check("out-of-range input rejected", r.isError, text_of(r)[:160])

            # ---------------- deletes ----------------
            r = await s.call_tool("content_forge_delete_article",
                                  {"topic": "boat-life", "article": aslug})
            check("delete_article", json.loads(text_of(r))["deleted"] is True)
            r = await s.call_tool("content_forge_delete_topic", {"topic": "boat-life"})
            check("delete_topic", json.loads(text_of(r))["deleted"] is True)

    server.should_exit = True


asyncio.run(main())

print()
if fails:
    print(f"{len(fails)} FAILURE(S): " + ", ".join(fails))
    sys.exit(1)
print("ALL MCP CHECKS PASSED")
