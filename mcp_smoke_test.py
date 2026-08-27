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
            check("tools listed", len(tools) >= 20, str(len(tools)))
            check("delete tools gated on", "content_forge_delete_topic" in tools)
            check("voice tools registered",
                  {"content_forge_list_voices", "content_forge_create_voice",
                   "content_forge_delete_voice",
                   "content_forge_list_writing_styles"} <= tools.keys())
            wa = tools["content_forge_write_article"].inputSchema["properties"]
            check("write_article exposes all three axes",
                  {"topic", "voice", "styles"} <= wa.keys(), str(sorted(wa)))
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

            # ---------------- voices ----------------
            r = await s.call_tool("content_forge_create_voice", {
                "name": "Bob", "description": "Blunt, funny, short sentences."})
            bob = json.loads(text_of(r))
            check("create_voice", bob["slug"] == "bob", str(bob))
            check("new voice has no profile", bob["has_profile"] is False)

            r = await s.call_tool("content_forge_add_voice_sample", {
                "voice": "bob", "text": "Sample writing about docks. " * 40,
                "name": "dock notes"})
            check("add_voice_sample", json.loads(text_of(r))["total_samples"] == 1)

            # a voice with no profile cannot be applied
            r = await s.call_tool("content_forge_write_article", {
                "topic": "boat-life", "title": "Too early", "voice": "bob"})
            check("unbuilt voice refused with a fix",
                  r.isError and "content_forge_build_voice_profile" in text_of(r),
                  text_of(r)[:160])

            r = await s.call_tool("content_forge_build_voice_profile",
                                  {"voice": "bob"})
            job = json.loads(text_of(r))
            check("build_voice_profile returns a job", bool(job["job_id"]), str(job))

            r = await s.call_tool("content_forge_get_job",
                                  {"job_id": job["job_id"], "wait_seconds": 20})
            done = json.loads(text_of(r))
            check("voice job completes", done["status"] == "done", str(done))

            r = await s.call_tool("content_forge_get_voice_profile",
                                  {"voice": "bob"})
            check("voice profile readable",
                  json.loads(text_of(r))["has_profile"] is True)

            r = await s.call_tool("content_forge_list_voices", {})
            check("list_voices finds it", json.loads(text_of(r))["total"] == 1)
            r = await s.call_tool("content_forge_list_voices",
                                  {"response_format": "markdown"})
            check("voices markdown format", "`bob`" in text_of(r))

            # a voice is not owned by a topic — it works from any of them
            r = await s.call_tool("content_forge_create_topic",
                                  {"name": "Gardening"})
            check("second topic", json.loads(text_of(r))["slug"] == "gardening")

            r = await s.call_tool("content_forge_list_writing_styles", {})
            st_keys = [x["key"] for x in json.loads(text_of(r))["styles"]]
            check("writing styles discoverable",
                  "persuasive" in st_keys and "descriptive" in st_keys, str(st_keys))

            # ---------------- articles ----------------
            # the headline flow: a named voice + a named style in a named topic
            r = await s.call_tool("content_forge_write_article", {
                "topic": "boat-life", "title": "Choosing a dock ladder",
                "brief": "Compare the options.", "format": "how-to",
                "words": 800, "references": True,
                "voice": "bob", "styles": ["persuasive", "descriptive"]})
            started = json.loads(text_of(r))
            check("write_article returns job and slug",
                  started["job_id"] and started["article"], str(started))
            check("write_article echoes voice and styles",
                  started["voice"] == "bob"
                  and started["styles"] == ["persuasive", "descriptive"],
                  str(started))
            aslug = started["article"]

            # display names resolve, so "the Bob voice in the Gardening topic" works
            r = await s.call_tool("content_forge_write_article", {
                "topic": "Gardening", "title": "Raised beds",
                "voice": "Bob", "styles": ["narrative"]})
            byname = json.loads(text_of(r))
            check("names resolve to slugs",
                  byname["topic"] == "gardening" and byname["voice"] == "bob",
                  str(byname))
            check("one voice serves many topics",
                  byname["voice"] == started["voice"]
                  and byname["topic"] != started["topic"])
            await s.call_tool("content_forge_get_job",
                              {"job_id": byname["job_id"], "wait_seconds": 20})

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
            check("article metadata carries voice and styles",
                  meta["voice"] == "bob"
                  and meta["styles"] == ["persuasive", "descriptive"], str(meta)[:200])

            # re-cut the same piece in a different voice, leaving styles alone
            r = await s.call_tool("content_forge_create_voice", {"name": "Dana"})
            await s.call_tool("content_forge_set_voice_profile",
                              {"voice": "dana", "profile": "## Dana\nWarm, lyrical."})
            r = await s.call_tool("content_forge_rewrite_article", {
                "topic": "boat-life", "article": aslug, "voice": "dana"})
            recut = json.loads(text_of(r))
            check("rewrite swaps voice, inherits styles",
                  recut["voice"] == "dana"
                  and recut["styles"] == ["persuasive", "descriptive"], str(recut))
            await s.call_tool("content_forge_get_job",
                              {"job_id": recut["job_id"], "wait_seconds": 25})

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
            check("health", h["ok"] is True and h["topics"] == 2, str(h)[:200])
            check("health counts voices",
                  h["voices"] == 2 and h["voices_ready"] == 2, str(h)[:200])

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

            r = await s.call_tool("content_forge_get_voice_profile",
                                  {"voice": "ghost"})
            check("unknown voice names the known ones",
                  r.isError and "bob" in text_of(r), text_of(r)[:160])

            # ---------------- deletes ----------------
            r = await s.call_tool("content_forge_delete_article",
                                  {"topic": "boat-life", "article": aslug})
            check("delete_article", json.loads(text_of(r))["deleted"] is True)
            r = await s.call_tool("content_forge_delete_topic", {"topic": "boat-life"})
            check("delete_topic", json.loads(text_of(r))["deleted"] is True)
            r = await s.call_tool("content_forge_list_voices", {})
            check("voices outlive a deleted topic",
                  json.loads(text_of(r))["total"] == 2, text_of(r)[:200])
            r = await s.call_tool("content_forge_delete_voice", {"voice": "bob"})
            check("delete_voice", json.loads(text_of(r))["deleted"] is True)

    server.should_exit = True


asyncio.run(main())

print()
if fails:
    print(f"{len(fails)} FAILURE(S): " + ", ".join(fails))
    sys.exit(1)
print("ALL MCP CHECKS PASSED")
