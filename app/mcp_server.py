"""MCP server for Content Forge, mounted at /mcp on the web app.

Streamable HTTP, stateless, JSON responses — the transport the MCP guidance
recommends for a remote multi-client server, and the one an agent harness can
reach over the LAN without installing anything.

It runs *inside* the FastAPI process on purpose. Generation is asynchronous and
the job registry lives in memory (`jobs.py`), so a separate process would have
its own registry and the two would disagree about what is running. Sharing the
process means an article an agent started is the same job the browser polls.

Long generations do not fit an MCP call. Every generating tool submits a job and
returns a `job_id` straight away; `content_forge_get_job` polls it, optionally
blocking up to `wait_seconds`. Tools that only touch disk answer immediately.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from . import auth, claude_runner, generate, jobs, storage
from .config import (CLAUDE_MODEL, CONTENT_ROOT, MCP_ALLOWED_HOSTS,
                     MCP_ALLOWED_ORIGINS)
from .docx_export import markdown_to_docx
from .render import render_markdown, title_from_markdown

# Deletes remove whole folders from the content root. An agent misfire should not
# be able to do that, so they are only registered when explicitly enabled.
ALLOW_DELETE = os.environ.get("CF_MCP_ALLOW_DELETE", "").strip().lower() in (
    "1", "true", "yes", "on")

mcp = FastMCP(
    "content_forge_mcp",
    instructions=(
        "Content Forge generates articles in a specific author's voice, grouped "
        "by topic. Each topic carries its own voice profile — a style guide "
        "derived from the author's real writing — which is applied as a binding "
        "constraint to every article written under it.\n\n"
        "Typical flow: content_forge_list_topics to find a topic, "
        "content_forge_write_article to start a generation (returns a job_id), "
        "content_forge_get_job to wait for it, then content_forge_get_article "
        "with format='markdown' or 'html' to retrieve the finished piece for "
        "publishing.\n\n"
        "Generation is slow: 30-90s without research, several minutes with "
        "references=true. Always go through the job, never assume it is done. "
        "Article and topic identifiers are slugs (e.g. 'boat-life'), not titles."
    ),
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    # Keeps DNS-rebinding protection on, but teaches it the public name the
    # TLS proxy forwards. Without this the proxied Host is rejected with 421.
    transport_security=TransportSecuritySettings(
        allowed_hosts=MCP_ALLOWED_HOSTS,
        allowed_origins=MCP_ALLOWED_ORIGINS,
    ),
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

class ToolError(Exception):
    """Reported back to the agent as tool output, not a protocol error."""


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def _topic(slug: str) -> storage.Topic:
    t = storage.get_topic(slug)
    if not t:
        known = [x.slug for x in storage.list_topics()]
        raise ToolError(
            f"No topic with slug '{slug}'. Known slugs: {known or 'none yet'}. "
            "Use content_forge_list_topics, or content_forge_create_topic to "
            "make one."
        )
    return t


def _article(topic: str, slug: str) -> storage.Article:
    a = storage.get_article(topic, slug)
    if not a:
        known = [x.slug for x in storage.list_articles(topic)][:20]
        raise ToolError(
            f"No article '{slug}' in topic '{topic}'. Known slugs: "
            f"{known or 'none yet'}. Use content_forge_list_articles."
        )
    return a


def _topic_dict(t: storage.Topic) -> dict:
    return {"slug": t.slug, "name": t.name, "description": t.description,
            "audience": t.audience, "has_voice_profile": t.has_voice,
            "voice_sample_count": t.sample_count,
            "article_count": t.article_count,
            "created_at": t.created_at, "updated_at": t.updated_at}


def _article_dict(a: storage.Article) -> dict:
    return {"slug": a.slug, "topic": a.topic, "title": a.title,
            "status": a.status, "format": a.format,
            "word_target": a.word_target, "with_references": a.with_references,
            "brief": a.prompt, "error": a.error, "job_id": a.job_id,
            "created_at": a.created_at, "updated_at": a.updated_at}


def _job_dict(job: jobs.Job) -> dict:
    return {"job_id": job.id, "kind": job.kind, "label": job.label,
            "status": job.status, "settled": job.settled,
            "result": job.result, "error": job.error,
            "created_at": job.created_at, "finished_at": job.finished_at}


def _paginate(items: list, limit: int, offset: int) -> dict:
    window = items[offset:offset + limit]
    nxt = offset + len(window)
    return {"total": len(items), "count": len(window), "offset": offset,
            "has_more": nxt < len(items),
            "next_offset": nxt if nxt < len(items) else None,
            "items": window}


def _topics_md(rows: list[dict]) -> str:
    if not rows:
        return "No topics yet. Create one with content_forge_create_topic."
    out = []
    for t in rows:
        voice = "voice ready" if t["has_voice_profile"] else "no voice profile"
        out.append(f"## {t['name']} (`{t['slug']}`)\n"
                   f"{t['description'] or '_No description._'}\n\n"
                   f"- Audience: {t['audience'] or 'unspecified'}\n"
                   f"- {t['article_count']} articles, {voice}, "
                   f"{t['voice_sample_count']} writing samples")
    return "\n\n".join(out)


def _articles_md(rows: list[dict]) -> str:
    if not rows:
        return "No articles in this topic yet."
    lines = ["| Slug | Title | Status | Format | Refs | Created |",
             "|---|---|---|---|---|---|"]
    for a in rows:
        lines.append(f"| `{a['slug']}` | {a['title']} | {a['status']} | "
                     f"{a['format']} | {'yes' if a['with_references'] else 'no'} | "
                     f"{a['created_at'][:10]} |")
    return "\n".join(lines)


Fmt = Literal["markdown", "json"]


# --------------------------------------------------------------------------
# topics
# --------------------------------------------------------------------------

@mcp.tool(
    name="content_forge_list_topics",
    annotations={"title": "List topics", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def list_topics(
    response_format: Annotated[Fmt, Field(
        description="'markdown' for a readable summary, 'json' for structured "
                    "data. Default 'json'.")] = "json",
    limit: Annotated[int, Field(description="Max topics to return.",
                                ge=1, le=100)] = 50,
    offset: Annotated[int, Field(description="Topics to skip, for pagination.",
                                 ge=0)] = 0,
) -> str:
    """List every topic, with its article count and whether a voice profile exists.

    A topic is the unit of organisation: it owns a voice profile, a set of
    articles and an idea list. Start here to discover the `slug` values that
    every other tool takes.

    Returns JSON: {total, count, offset, has_more, next_offset, items[]} where
    each item is {slug, name, description, audience, has_voice_profile,
    voice_sample_count, article_count, created_at, updated_at}.

    Use when: "what topics exist", "which topic should this article go under".
    """
    rows = [_topic_dict(t) for t in storage.list_topics()]
    page = _paginate(rows, limit, offset)
    if response_format == "markdown":
        return _topics_md(page["items"])
    return _dump(page)


@mcp.tool(
    name="content_forge_get_topic",
    annotations={"title": "Get topic", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def get_topic(
    topic: Annotated[str, Field(description="Topic slug, e.g. 'boat-life'.",
                                min_length=1, max_length=80)],
    response_format: Annotated[Fmt, Field(
        description="'markdown' or 'json'. Default 'json'.")] = "json",
) -> str:
    """Get one topic with its articles, voice-profile status and cached ideas.

    A fuller view than content_forge_list_topics: includes the article list, the
    voice sample filenames, and how many ideas are cached.

    Returns JSON: {topic{...}, articles[], voice{has_profile, profile_chars,
    samples[]}, idea_count}.

    Use when: you have a topic slug and need to see what is already written
    under it before deciding what to write next.
    """
    t = _topic(topic)
    profile = storage.get_voice_profile(topic)
    data = {
        "topic": _topic_dict(t),
        "articles": [_article_dict(a) for a in storage.list_articles(topic)],
        "voice": {"has_profile": bool(profile), "profile_chars": len(profile),
                  "samples": storage.list_samples(topic)},
        "idea_count": len(storage.get_ideas(topic)),
    }
    if response_format == "markdown":
        return (_topics_md([data["topic"]]) + "\n\n### Articles\n"
                + _articles_md(data["articles"]))
    return _dump(data)


@mcp.tool(
    name="content_forge_create_topic",
    annotations={"title": "Create topic", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": False},
)
async def create_topic(
    name: Annotated[str, Field(description="Display name, e.g. 'Boat Life'. The "
                                           "slug is derived from this.",
                               min_length=1, max_length=200)],
    description: Annotated[str, Field(
        description="What this topic covers — scope and recurring themes. Fed to "
                    "the model as context for every article.", max_length=2000)] = "",
    audience: Annotated[str, Field(
        description="Who the writing is for, e.g. 'adults who own boats'.",
        max_length=500)] = "",
) -> str:
    """Create a topic folder with an empty voice profile and article list.

    The slug is derived from the name; a collision gets a numeric suffix, so
    calling this twice with the same name creates two topics. Check
    content_forge_list_topics first if you might be duplicating.

    A new topic has no voice profile, so articles written under it immediately
    will use a generic voice. Add samples with content_forge_add_voice_sample
    and then call content_forge_build_voice_profile.

    Returns JSON: the created topic object, including its assigned `slug`.
    """
    if not name.strip():
        raise ToolError("Topic name cannot be empty.")
    t = storage.create_topic(name, description, audience)
    return _dump(_topic_dict(t))


# --------------------------------------------------------------------------
# voice
# --------------------------------------------------------------------------

@mcp.tool(
    name="content_forge_get_voice_profile",
    annotations={"title": "Get voice profile", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def get_voice_profile(
    topic: Annotated[str, Field(description="Topic slug.", min_length=1)],
) -> str:
    """Read a topic's voice profile — the style guide applied to its articles.

    The profile is Markdown describing the author's tone, sentence mechanics,
    vocabulary, structural habits and the things they never do. It is passed as
    a binding system prompt on every generation under this topic, so it is the
    single biggest lever on output quality.

    Returns JSON: {topic, has_profile, profile} where profile is the raw
    Markdown, or an empty string if none has been built.
    """
    _topic(topic)
    profile = storage.get_voice_profile(topic)
    return _dump({"topic": topic, "has_profile": bool(profile),
                  "profile": profile})


@mcp.tool(
    name="content_forge_add_voice_sample",
    annotations={"title": "Add voice sample", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": False},
)
async def add_voice_sample(
    topic: Annotated[str, Field(description="Topic slug.", min_length=1)],
    text: Annotated[str, Field(description="The author's own writing, as plain "
                                           "text or Markdown.", min_length=1)],
    name: Annotated[str, Field(description="Short label for this sample, used as "
                                           "the filename.", max_length=100)] = "sample",
) -> str:
    """Add a sample of the author's real writing to a topic.

    Samples are the raw material for the voice profile. Quality tracks sample
    quality: a few thousand words of genuine writing beats a lot of filler.
    Adding a sample does NOT rebuild the profile — call
    content_forge_build_voice_profile afterwards.

    Returns JSON: {topic, filename, words, total_samples}.
    """
    _topic(topic)
    if not text.strip():
        raise ToolError("Sample text cannot be empty.")
    fname = storage.save_sample(
        topic, f"{storage.slugify(name, 'sample')}.md", text.encode("utf-8"))
    return _dump({"topic": topic, "filename": fname, "words": len(text.split()),
                  "total_samples": len(storage.list_samples(topic))})


@mcp.tool(
    name="content_forge_set_voice_profile",
    annotations={"title": "Set voice profile", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True,
                 "openWorldHint": False},
)
async def set_voice_profile(
    topic: Annotated[str, Field(description="Topic slug.", min_length=1)],
    profile: Annotated[str, Field(description="The full replacement profile, as "
                                              "Markdown.", min_length=1)],
) -> str:
    """Overwrite a topic's voice profile with hand-edited Markdown.

    Replaces the whole file — there is no merge. Read the current profile with
    content_forge_get_voice_profile first if you intend to amend rather than
    replace it. Sharpening the 'Do not' section by hand is the usual way to stop
    output drifting back toward generic assistant prose.

    Returns JSON: {topic, chars}.
    """
    _topic(topic)
    storage.save_voice_profile(topic, profile)
    return _dump({"topic": topic, "chars": len(profile)})


@mcp.tool(
    name="content_forge_build_voice_profile",
    annotations={"title": "Build voice profile", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": False,
                 "openWorldHint": True},
)
async def build_voice_profile(
    topic: Annotated[str, Field(description="Topic slug.", min_length=1)],
    notes: Annotated[str, Field(
        description="Anything the samples do not show — words to avoid, how "
                    "formal to go, running jokes.", max_length=2000)] = "",
) -> str:
    """Start a job that reads every sample and writes a new voice profile.

    Asynchronous and costs Claude usage. Overwrites any existing profile, so
    save a copy via content_forge_get_voice_profile if the current one has been
    hand-tuned. Requires at least a few hundred words of samples or the job
    fails.

    Returns JSON: {job_id, status, topic}. Poll content_forge_get_job; on
    success its result is {topic, chars, cost}.
    """
    t = _topic(topic)
    if len(storage.collect_samples_text(topic)) < 400:
        raise ToolError(
            f"Topic '{topic}' has too little sample material "
            f"({len(storage.list_samples(topic))} samples). Add more with "
            "content_forge_add_voice_sample first — a few hundred words minimum."
        )
    job = jobs.submit("voice", f"Voice profile — {t.name}",
                      lambda: generate.build_voice(t, notes),
                      redirect=f"/topics/{topic}?tab=voice")
    return _dump({"job_id": job.id, "status": job.status, "topic": topic})


# --------------------------------------------------------------------------
# ideas
# --------------------------------------------------------------------------

@mcp.tool(
    name="content_forge_list_ideas",
    annotations={"title": "List cached ideas", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def list_ideas(
    topic: Annotated[str, Field(description="Topic slug.", min_length=1)],
) -> str:
    """Read the cached article ideas for a topic.

    These are whatever content_forge_suggest_ideas last produced; the list is
    overwritten on each run and may be stale or empty. This tool does not
    generate anything and costs nothing.

    Returns JSON: {topic, count, ideas[{title, angle, why, format, words}]}.
    """
    _topic(topic)
    ideas = storage.get_ideas(topic)
    return _dump({"topic": topic, "count": len(ideas), "ideas": ideas})


@mcp.tool(
    name="content_forge_suggest_ideas",
    annotations={"title": "Suggest article ideas", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def suggest_ideas(
    topic: Annotated[str, Field(description="Topic slug.", min_length=1)],
    count: Annotated[int, Field(description="How many ideas to produce.",
                                ge=1, le=20)] = 8,
    research: Annotated[bool, Field(
        description="Search the web for what is current in this space. Slower "
                    "and costs more usage.")] = False,
) -> str:
    """Start a job that proposes article ideas, excluding what is already written.

    Asynchronous and costs Claude usage. Existing article titles in the topic are
    passed to the model as things to avoid repeating. Overwrites the cached idea
    list.

    Returns JSON: {job_id, status, topic}. Poll content_forge_get_job, then read
    the ideas with content_forge_list_ideas.
    """
    t = _topic(topic)
    job = jobs.submit("ideas", f"Ideas — {t.name}",
                      lambda: generate.suggest_ideas(t, count, research),
                      redirect=f"/topics/{topic}?tab=ideas")
    return _dump({"job_id": job.id, "status": job.status, "topic": topic})


# --------------------------------------------------------------------------
# articles
# --------------------------------------------------------------------------

@mcp.tool(
    name="content_forge_list_articles",
    annotations={"title": "List articles", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def list_articles(
    topic: Annotated[str, Field(description="Topic slug.", min_length=1)],
    status: Annotated[Literal["any", "ready", "generating", "error", "draft"],
                      Field(description="Filter by status. 'ready' is the set "
                                        "safe to publish.")] = "any",
    response_format: Annotated[Fmt, Field(
        description="'markdown' for a table, 'json' for structured data.")] = "json",
    limit: Annotated[int, Field(description="Max articles to return.",
                                ge=1, le=100)] = 50,
    offset: Annotated[int, Field(description="Articles to skip.", ge=0)] = 0,
) -> str:
    """List a topic's articles, newest first, without their bodies.

    Bodies can be long, so they are omitted here — fetch one with
    content_forge_get_article. Filter to status='ready' to get only finished
    pieces; 'generating' means a job is still running.

    Returns JSON: {total, count, offset, has_more, next_offset, items[]} where
    each item is {slug, topic, title, status, format, word_target,
    with_references, brief, error, job_id, created_at, updated_at}.
    """
    _topic(topic)
    rows = [_article_dict(a) for a in storage.list_articles(topic)]
    if status != "any":
        rows = [r for r in rows if r["status"] == status]
    page = _paginate(rows, limit, offset)
    if response_format == "markdown":
        return _articles_md(page["items"])
    return _dump(page)


@mcp.tool(
    name="content_forge_get_article",
    annotations={"title": "Get article", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def get_article(
    topic: Annotated[str, Field(description="Topic slug.", min_length=1)],
    article: Annotated[str, Field(description="Article slug, from "
                                              "content_forge_list_articles.",
                                  min_length=1)],
    format: Annotated[Literal["markdown", "html", "json"], Field(
        description="'markdown' for the raw source, 'html' for rendered HTML "
                    "ready to POST to a CMS, 'json' for body plus metadata.")] = "markdown",
) -> str:
    """Retrieve a finished article for publishing.

    This is the tool to call before posting to a blog or CMS. 'markdown' returns
    the raw source; 'html' returns rendered HTML with headings, lists, links and
    code blocks intact, suitable for a post body; 'json' wraps the Markdown with
    metadata (title, word count, status, references flag).

    If the article's status is 'generating' the body may be empty or stale —
    check status first, or wait on its job_id with content_forge_get_job.

    Returns: raw Markdown, raw HTML, or JSON {slug, topic, title, status,
    format, with_references, words, has_references_section, body, created_at,
    updated_at} depending on `format`.
    """
    _topic(topic)
    a = _article(topic, article)
    body = storage.read_article_body(topic, article)

    if a.status == "generating" and not body.strip():
        raise ToolError(
            f"Article '{article}' is still generating and has no body yet. "
            f"Wait on job '{a.job_id}' with content_forge_get_job."
        )
    if a.status == "error":
        raise ToolError(f"Article '{article}' failed to generate: {a.error}")

    if format == "markdown":
        return body
    if format == "html":
        return render_markdown(body)
    return _dump({**_article_dict(a), "words": len(body.split()),
                  "has_references_section": "## References" in body,
                  "body": body})


@mcp.tool(
    name="content_forge_write_article",
    annotations={"title": "Write article", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def write_article(
    topic: Annotated[str, Field(description="Topic slug — determines the voice "
                                            "profile applied.", min_length=1)],
    title: Annotated[str, Field(description="Working title. The model may sharpen "
                                            "it; the final title comes from the "
                                            "generated H1.",
                                min_length=1, max_length=200)],
    brief: Annotated[str, Field(
        description="What the piece should argue or deliver: angle, must-hit "
                    "points, anything to avoid. Defaults to the title.",
        max_length=5000)] = "",
    format: Annotated[Literal["blog post", "how-to", "listicle", "opinion",
                              "explainer", "review", "newsletter", "interview"],
                      Field(description="Shape of the piece.")] = "blog post",
    words: Annotated[int, Field(description="Target word count.",
                                ge=150, le=6000)] = 900,
    references: Annotated[bool, Field(
        description="Run real web searches and append a numbered References "
                    "section with live URLs. Much slower — several minutes — and "
                    "costs more usage. Leave false for opinion pieces.")] = False,
) -> str:
    """Start a job that writes an article in the topic's voice.

    Asynchronous and costs Claude usage. Returns immediately with a job_id; the
    article folder exists straight away with status 'generating'. Poll
    content_forge_get_job, then fetch the finished piece with
    content_forge_get_article.

    Timing: roughly 30-90 seconds without references, several minutes with. If
    the topic has no voice profile the piece is written in a generic voice —
    check has_voice_profile via content_forge_get_topic first.

    Returns JSON: {job_id, status, topic, article, title, url} where `article`
    is the slug to pass to content_forge_get_article.
    """
    t = _topic(topic)
    a = storage.create_article(topic, title, brief or title,
                               with_references=references, word_target=words,
                               fmt=format)
    a.status = "generating"
    job = jobs.submit("article", f"{t.name} — {a.title}",
                      lambda: generate.generate_article(t, a),
                      redirect=f"/topics/{topic}/articles/{a.slug}")
    a.job_id = job.id
    storage.save_article_meta(a)
    return _dump({"job_id": job.id, "status": job.status, "topic": topic,
                  "article": a.slug, "title": a.title,
                  "url": f"/topics/{topic}/articles/{a.slug}"})


@mcp.tool(
    name="content_forge_rewrite_article",
    annotations={"title": "Rewrite article", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": False,
                 "openWorldHint": True},
)
async def rewrite_article(
    topic: Annotated[str, Field(description="Topic slug.", min_length=1)],
    article: Annotated[str, Field(description="Article slug.", min_length=1)],
    brief: Annotated[str, Field(description="Replacement brief. Leave empty to "
                                            "reuse the existing one.",
                                max_length=5000)] = "",
    words: Annotated[int, Field(description="New target word count, or 0 to keep "
                                            "the current one.", ge=0, le=6000)] = 0,
    references: Annotated[bool, Field(
        description="Whether this run should research and cite sources.")] = False,
) -> str:
    """Start a job that regenerates an existing article, replacing its body.

    Destructive: the current body is overwritten when the job completes. Save a
    copy with content_forge_get_article first if it matters.

    Returns JSON: {job_id, status, topic, article}. Poll content_forge_get_job.
    """
    t = _topic(topic)
    a = _article(topic, article)
    if brief.strip():
        a.prompt = brief.strip()
    a.with_references = references
    if words:
        a.word_target = words
    a.status = "generating"
    storage.save_article_meta(a)
    job = jobs.submit("article", f"Rewrite — {a.title}",
                      lambda: generate.generate_article(t, a),
                      redirect=f"/topics/{topic}/articles/{article}")
    a.job_id = job.id
    storage.save_article_meta(a)
    return _dump({"job_id": job.id, "status": job.status, "topic": topic,
                  "article": article})


@mcp.tool(
    name="content_forge_update_article",
    annotations={"title": "Update article body", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True,
                 "openWorldHint": False},
)
async def update_article(
    topic: Annotated[str, Field(description="Topic slug.", min_length=1)],
    article: Annotated[str, Field(description="Article slug.", min_length=1)],
    body: Annotated[str, Field(description="Full replacement Markdown body.",
                               min_length=1)],
    title: Annotated[str, Field(description="New title. If empty, taken from the "
                                            "body's H1.", max_length=200)] = "",
) -> str:
    """Overwrite an article's Markdown body with edited content.

    Replaces the whole file — no merge. Use this to save edits an agent has made
    to text fetched with content_forge_get_article. Clears any stale .docx
    export and marks a draft or errored article as ready.

    Returns JSON: {topic, article, title, words, status}.
    """
    _topic(topic)
    a = _article(topic, article)
    storage.write_article_body(topic, article, body)
    a.title = title.strip() or title_from_markdown(body, a.title)
    if a.status in ("draft", "error"):
        a.status = "ready"
    storage.save_article_meta(a)
    exp = storage.article_dir(topic, article) / "export.docx"
    if exp.exists():
        exp.unlink()
    return _dump({"topic": topic, "article": article, "title": a.title,
                  "words": len(body.split()), "status": a.status})


@mcp.tool(
    name="content_forge_export_article_docx",
    annotations={"title": "Export article to .docx", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def export_article_docx(
    topic: Annotated[str, Field(description="Topic slug.", min_length=1)],
    article: Annotated[str, Field(description="Article slug.", min_length=1)],
) -> str:
    """Build a Word .docx of an article and return its path on the server.

    Writes export.docx into the article's folder with real headings, lists,
    blockquotes, code blocks and clickable hyperlinks. The file is on the
    Content Forge host — useful if the agent shares that filesystem, otherwise
    fetch it over HTTP at the returned `url`.

    Returns JSON: {topic, article, path, url, bytes}.
    """
    t = _topic(topic)
    a = _article(topic, article)
    body = storage.read_article_body(topic, article)
    if not body.strip():
        raise ToolError(f"Article '{article}' has no body to export yet.")
    out = storage.article_dir(topic, article) / "export.docx"
    markdown_to_docx(body, out, title=a.title,
                     subtitle=f"{t.name} · {a.created_at[:10]}")
    return _dump({"topic": topic, "article": article, "path": str(out),
                  "url": f"/topics/{topic}/articles/{article}/export.docx",
                  "bytes": out.stat().st_size})


# --------------------------------------------------------------------------
# jobs and health
# --------------------------------------------------------------------------

@mcp.tool(
    name="content_forge_get_job",
    annotations={"title": "Get job status", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def get_job(
    job_id: Annotated[str, Field(description="Job id returned by a generating "
                                             "tool.", min_length=1)],
    wait_seconds: Annotated[int, Field(
        description="Block up to this many seconds waiting for the job to "
                    "finish. 0 returns immediately. Use 60-120 for a plain "
                    "article, and expect to call repeatedly for research runs.",
        ge=0, le=600)] = 0,
) -> str:
    """Check a generation job, optionally waiting for it to finish.

    Jobs are queued when all generation slots are busy, so a job may sit in
    'queued' before it starts. If this returns still-running after
    wait_seconds, call it again — the work continues on the server regardless.

    Jobs live in memory: a service restart loses them, though any article
    already written to disk survives.

    Returns JSON: {job_id, kind, label, status, settled, result, error,
    created_at, finished_at}. status is queued|running|done|error.
    """
    job = jobs.get(job_id)
    if not job:
        raise ToolError(
            f"No job '{job_id}'. Job ids are lost on service restart; if the "
            "work was an article, check its status with "
            "content_forge_list_articles instead."
        )
    deadline = asyncio.get_event_loop().time() + wait_seconds
    while not job.settled and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(1.0)
    return _dump(_job_dict(job))


@mcp.tool(
    name="content_forge_health",
    annotations={"title": "Health and auth status", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def health() -> str:
    """Report whether Content Forge can currently generate anything.

    Check this first if a generation job fails: the usual cause is an expired
    Claude credential, which must be fixed by a human on the host's /auth page.
    Does not spend an API request.

    Returns JSON: {ok, claude, model, content_root, topics, active_jobs,
    auth{method, summary, credentials_expired, last_test}}.
    """
    ok, ver = claude_runner.cli_available()
    st = auth.state()
    return _dump({
        "ok": ok, "claude": ver, "model": CLAUDE_MODEL,
        "content_root": str(CONTENT_ROOT),
        "topics": len(storage.list_topics()),
        "active_jobs": len(jobs.active()),
        "auth": {"method": st["method"], "summary": st["summary"],
                 "credentials_expired": st["credentials"]["expired"],
                 "last_test": claude_runner.last_probe() or None},
    })


# --------------------------------------------------------------------------
# destructive tools, only registered when CF_MCP_ALLOW_DELETE is set
# --------------------------------------------------------------------------

if ALLOW_DELETE:

    @mcp.tool(
        name="content_forge_delete_article",
        annotations={"title": "Delete article", "readOnlyHint": False,
                     "destructiveHint": True, "idempotentHint": True,
                     "openWorldHint": False},
    )
    async def delete_article(
        topic: Annotated[str, Field(description="Topic slug.", min_length=1)],
        article: Annotated[str, Field(description="Article slug.", min_length=1)],
    ) -> str:
        """Permanently delete an article folder, body and metadata included.

        Irreversible — there is no trash. Returns JSON: {topic, article, deleted}.
        """
        _topic(topic)
        _article(topic, article)
        return _dump({"topic": topic, "article": article,
                      "deleted": storage.delete_article(topic, article)})

    @mcp.tool(
        name="content_forge_delete_topic",
        annotations={"title": "Delete topic", "readOnlyHint": False,
                     "destructiveHint": True, "idempotentHint": True,
                     "openWorldHint": False},
    )
    async def delete_topic(
        topic: Annotated[str, Field(description="Topic slug.", min_length=1)],
    ) -> str:
        """Permanently delete a topic and every article and voice sample in it.

        Irreversible — there is no trash. Returns JSON: {topic, deleted,
        articles_removed}.
        """
        t = _topic(topic)
        n = t.article_count
        return _dump({"topic": topic, "deleted": storage.delete_topic(topic),
                      "articles_removed": n})


# --------------------------------------------------------------------------
# mounting
# --------------------------------------------------------------------------

mcp_http_app = mcp.streamable_http_app()


class _ExactPathBridge:
    """Serves the MCP app at `/mcp` itself, not just `/mcp/`.

    A Starlette Mount only matches paths that continue past the prefix, so a
    POST to the bare `/mcp` falls through to the trailing-slash redirect. Most
    clients follow a 307 and preserve the body, but not all do, and `/mcp` is
    the URL people will actually configure. This rewrites the scope so the
    mounted app sees its own root.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        scope = dict(scope)
        scope["path"] = "/"
        scope["raw_path"] = b"/"
        scope["root_path"] = scope.get("root_path", "") + "/mcp"
        await self.inner(scope, receive, send)


def mount_mcp(app) -> None:
    """Attach the MCP server to a FastAPI app at /mcp and /mcp/."""
    from starlette.routing import Route

    app.mount("/mcp", mcp_http_app)
    # Registered after the mount but matched first for the exact path.
    app.router.routes.insert(0, Route("/mcp", _ExactPathBridge(mcp_http_app)))


@asynccontextmanager
async def lifespan(app):
    """FastAPI lifespan that also starts the MCP session manager.

    Starlette does not run a mounted sub-app's lifespan, so the parent has to
    enter it explicitly or every /mcp request fails on an unstarted manager.
    """
    async with mcp_http_app.router.lifespan_context(app):
        yield


def tool_names() -> list[str]:
    """Registered tool names — used by the smoke test."""
    return sorted(t.name for t in mcp._tool_manager.list_tools())
