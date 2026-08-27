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

from . import auth, claude_runner, generate, jobs, prompts, storage
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
        "Content Forge writes articles. Three independent things combine on "
        "every piece:\n"
        "  - TOPIC: the subject area (e.g. 'boating'). Owns the articles and "
        "the idea list.\n"
        "  - VOICE: who it sounds like (e.g. 'bob'). A style guide derived "
        "from that person's real writing, reusable across every topic.\n"
        "  - STYLES: how the piece is built and argued (persuasive, "
        "descriptive, narrative, ...). Chosen per article, combinable.\n\n"
        "So \"write an article using the Bob voice in a persuasive style in the "
        "boating topic about X\" maps to content_forge_write_article with "
        "topic='boating', voice='bob', styles=['persuasive'], brief='X'.\n\n"
        "Typical flow: content_forge_list_topics and content_forge_list_voices "
        "to resolve names to slugs, content_forge_write_article to start a "
        "generation (returns a job_id), content_forge_get_job to wait, then "
        "content_forge_get_article with format='markdown' or 'html' to "
        "retrieve the finished piece for publishing.\n\n"
        "Generation is slow: 30-90s without research, several minutes with "
        "references=true. Always go through the job, never assume it is done. "
        "Topic, voice and article identifiers are slugs (e.g. 'boat-life'), "
        "not display names — list them if you only have a name."
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


def _resolve(value: str, items: list, kind: str, make_hint: str):
    """Find by slug, then case-insensitively by name.

    Agents are driven by humans who say "the Bob voice" or "the boating
    topic", so accepting the display name avoids a mandatory list call
    before every write.
    """
    v = value.strip()
    for it in items:
        if it.slug == v:
            return it
    lowered = v.lower()
    hits = [it for it in items if it.name.lower() == lowered]
    if len(hits) == 1:
        return hits[0]
    known = [f"{it.slug} ({it.name})" for it in items]
    if len(hits) > 1:
        raise ToolError(
            f"Several {kind}s are named '{v}': {[h.slug for h in hits]}. "
            f"Pass the slug instead."
        )
    raise ToolError(
        f"No {kind} matching '{v}'. Known {kind}s: {known or 'none yet'}. "
        f"{make_hint}"
    )


def _topic(slug: str) -> storage.Topic:
    return _resolve(slug, storage.list_topics(), "topic",
                    "Use content_forge_list_topics, or "
                    "content_forge_create_topic to make one.")


def _voice(slug: str) -> storage.Voice:
    return _resolve(slug, storage.list_voices(), "voice",
                    "Use content_forge_list_voices, or "
                    "content_forge_create_voice to make one.")


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
            "audience": t.audience, "article_count": t.article_count,
            "created_at": t.created_at, "updated_at": t.updated_at}


def _voice_dict(v: storage.Voice) -> dict:
    return {"slug": v.slug, "name": v.name, "description": v.description,
            "has_profile": v.has_profile, "sample_count": v.sample_count,
            "has_image": v.has_image,
            "image_url": f"/voices/{v.slug}/image" if v.has_image else None,
            "created_at": v.created_at, "updated_at": v.updated_at}


def _article_dict(a: storage.Article) -> dict:
    return {"slug": a.slug, "topic": a.topic, "title": a.title,
            "status": a.status, "format": a.format,
            "word_target": a.word_target, "with_references": a.with_references,
            "voice": a.voice, "styles": a.styles, "brief": a.prompt,
            "error": a.error, "job_id": a.job_id,
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
        out.append(f"## {t['name']} (`{t['slug']}`)\n"
                   f"{t['description'] or '_No description._'}\n\n"
                   f"- Audience: {t['audience'] or 'unspecified'}\n"
                   f"- {t['article_count']} articles")
    return "\n\n".join(out)


def _voices_md(rows: list[dict]) -> str:
    if not rows:
        return "No voices yet. Create one with content_forge_create_voice."
    lines = ["| Slug | Name | Profile | Samples | Picture |",
             "|---|---|---|---|---|"]
    for v in rows:
        lines.append(f"| `{v['slug']}` | {v['name']} | "
                     f"{'ready' if v['has_profile'] else 'not built'} | "
                     f"{v['sample_count']} | "
                     f"{'yes' if v['has_image'] else 'no'} |")
    return "\n".join(lines)


def _articles_md(rows: list[dict]) -> str:
    if not rows:
        return "No articles in this topic yet."
    lines = ["| Slug | Title | Status | Format | Voice | Styles | Refs | Created |",
             "|---|---|---|---|---|---|---|---|"]
    for a in rows:
        lines.append(f"| `{a['slug']}` | {a['title']} | {a['status']} | "
                     f"{a['format']} | {a['voice'] or 'generic'} | "
                     f"{', '.join(a['styles']) or '—'} | "
                     f"{'yes' if a['with_references'] else 'no'} | "
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
    """List every topic — the subject areas articles are filed under.

    A topic owns articles and an idea list. It does NOT own a voice: voices are
    separate and reusable across topics, see content_forge_list_voices. Start
    here to turn a topic name into the `slug` other tools take.

    Returns JSON: {total, count, offset, has_more, next_offset, items[]} where
    each item is {slug, name, description, audience, article_count, created_at,
    updated_at}.

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
    topic: Annotated[str, Field(description="Topic slug, e.g. 'boat-life'. The "
                                            "display name also works.",
                                min_length=1, max_length=80)],
    response_format: Annotated[Fmt, Field(
        description="'markdown' or 'json'. Default 'json'.")] = "json",
) -> str:
    """Get one topic with its articles and cached idea count.

    A fuller view than content_forge_list_topics: includes the article list,
    each article's voice and styles, and how many ideas are cached.

    Returns JSON: {topic{...}, articles[], idea_count}.

    Use when: you have a topic and need to see what is already written under it
    before deciding what to write next.
    """
    t = _topic(topic)
    data = {
        "topic": _topic_dict(t),
        "articles": [_article_dict(a) for a in storage.list_articles(t.slug)],
        "idea_count": len(storage.get_ideas(t.slug)),
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
    """Create a topic folder with an empty article list.

    The slug is derived from the name; a collision gets a numeric suffix, so
    calling this twice with the same name creates two topics. Check
    content_forge_list_topics first if you might be duplicating.

    Topics carry no voice. To write in someone's voice under this topic, build
    a voice separately with content_forge_create_voice and pass its slug to
    content_forge_write_article.

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
    name="content_forge_list_voices",
    annotations={"title": "List voices", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def list_voices(
    response_format: Annotated[Fmt, Field(
        description="'markdown' for a table, 'json' for structured data.")] = "json",
    ready_only: Annotated[bool, Field(
        description="Only voices with a built profile — the ones that can "
                    "actually be applied to an article.")] = False,
) -> str:
    """List every voice — the reusable style guides articles can be written in.

    A voice is independent of topic: 'bob' can write in the boating topic and
    the gardening topic alike. Only voices with has_profile=true actually shape
    output; one without a profile still needs
    content_forge_build_voice_profile.

    Start here to turn a person's name ("the Bob voice") into the `voice` slug
    that content_forge_write_article takes.

    Returns JSON: {total, count, offset, has_more, next_offset, items[]} where
    each item is {slug, name, description, has_profile, sample_count,
    has_image, image_url, created_at, updated_at}.

    Note that `slug` and `name` can diverge: a voice keeps the slug it was
    created with even after being renamed, so match on `name` when a human
    said the name, and pass `slug` to the other tools.
    """
    rows = [_voice_dict(v) for v in storage.list_voices()]
    if ready_only:
        rows = [r for r in rows if r["has_profile"]]
    page = _paginate(rows, 100, 0)
    if response_format == "markdown":
        return _voices_md(page["items"])
    return _dump(page)


@mcp.tool(
    name="content_forge_list_writing_styles",
    annotations={"title": "List writing styles", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def list_writing_styles() -> str:
    """List the writing-style modes an article can be written in.

    Styles are the third axis, alongside topic and voice: the voice decides who
    the piece sounds like, the style decides how it is built and argued. They
    combine — ['persuasive', 'descriptive'] is a valid pairing.

    Costs nothing and touches no disk. Call it when a human names a style you
    want to confirm exists before passing it to content_forge_write_article.

    Returns JSON: {count, styles[{key, description}]} where `key` is the value
    to pass in the `styles` list.
    """
    return _dump({
        "count": len(prompts.WRITING_STYLES),
        "styles": [{"key": k, "description": d}
                   for k, d in prompts.WRITING_STYLES.items()],
    })


@mcp.tool(
    name="content_forge_create_voice",
    annotations={"title": "Create voice", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": False},
)
async def create_voice(
    name: Annotated[str, Field(description="Display name, e.g. 'Bob'. The slug "
                                           "is derived from this.",
                               min_length=1, max_length=200)],
    description: Annotated[str, Field(
        description="Whose voice this is and when to reach for it.",
        max_length=2000)] = "",
) -> str:
    """Create an empty voice, ready for writing samples.

    A new voice has no profile and cannot shape output yet. The full sequence
    is: create_voice, then content_forge_add_voice_sample one or more times,
    then content_forge_build_voice_profile.

    The slug is derived from the name; a collision gets a numeric suffix, so
    check content_forge_list_voices first if you might be duplicating.

    Returns JSON: the created voice object, including its assigned `slug`.
    """
    if not name.strip():
        raise ToolError("Voice name cannot be empty.")
    v = storage.create_voice(name, description)
    return _dump(_voice_dict(v))


@mcp.tool(
    name="content_forge_update_voice",
    annotations={"title": "Rename or describe a voice", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def update_voice(
    voice: Annotated[str, Field(description="Voice slug (or its current "
                                            "display name).", min_length=1)],
    name: Annotated[str, Field(
        description="New display name. Leave empty to keep the current one.",
        max_length=200)] = "",
    description: Annotated[str | None, Field(
        description="New description. Leave unset (null) to keep the current "
                    "one; pass an empty string to clear it.",
        max_length=2000)] = None,
) -> str:
    """Rename a voice, or change its description.

    Renaming is safe at any time: the slug is the identity and never changes,
    so every article already written in this voice stays linked to it. Only
    the label people see moves. That means the slug and the display name can
    legitimately diverge — a voice created as 'boat-life' and later renamed
    'Bob' keeps the slug 'boat-life'.

    A voice's picture is set in the web UI, not here; content_forge_list_voices
    reports whether one exists.

    Returns JSON: the updated voice object.
    """
    v = _voice(voice)
    changes = {}
    if name.strip():
        changes["name"] = name.strip()
    if description is not None:
        changes["description"] = description.strip()
    if not changes:
        raise ToolError(
            "Nothing to change — pass `name`, `description`, or both.")
    updated = storage.update_voice(v.slug, **changes)
    return _dump(_voice_dict(updated))


@mcp.tool(
    name="content_forge_get_voice_profile",
    annotations={"title": "Get voice profile", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def get_voice_profile(
    voice: Annotated[str, Field(description="Voice slug, e.g. 'bob'. The "
                                            "display name also works.",
                                min_length=1)],
) -> str:
    """Read a voice's profile — the style guide applied to articles using it.

    The profile is Markdown describing the author's tone, sentence mechanics,
    vocabulary, structural habits and the things they never do. It is passed as
    a binding system prompt on every generation that names this voice, so it is
    the single biggest lever on output quality.

    Returns JSON: {voice, has_profile, profile} where profile is the raw
    Markdown, or an empty string if none has been built.
    """
    v = _voice(voice)
    profile = storage.get_voice_profile(v.slug)
    return _dump({"voice": v.slug, "has_profile": bool(profile),
                  "profile": profile})


@mcp.tool(
    name="content_forge_add_voice_sample",
    annotations={"title": "Add voice sample", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": False},
)
async def add_voice_sample(
    voice: Annotated[str, Field(description="Voice slug, from "
                                            "content_forge_list_voices.",
                                min_length=1)],
    text: Annotated[str, Field(description="The author's own writing, as plain "
                                           "text or Markdown.", min_length=1)],
    name: Annotated[str, Field(description="Short label for this sample, used as "
                                           "the filename.", max_length=100)] = "sample",
) -> str:
    """Add a sample of an author's real writing to a voice.

    Samples are the raw material for the voice profile. Quality tracks sample
    quality: a few thousand words of genuine writing beats a lot of filler.
    Adding a sample does NOT rebuild the profile — call
    content_forge_build_voice_profile afterwards.

    Returns JSON: {voice, filename, words, total_samples}.
    """
    v = _voice(voice)
    if not text.strip():
        raise ToolError("Sample text cannot be empty.")
    fname = storage.save_voice_sample(
        v.slug, f"{storage.slugify(name, 'sample')}.md", text.encode("utf-8"))
    return _dump({"voice": v.slug, "filename": fname, "words": len(text.split()),
                  "total_samples": len(storage.list_voice_samples(v.slug))})


@mcp.tool(
    name="content_forge_set_voice_profile",
    annotations={"title": "Set voice profile", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True,
                 "openWorldHint": False},
)
async def set_voice_profile(
    voice: Annotated[str, Field(description="Voice slug.", min_length=1)],
    profile: Annotated[str, Field(description="The full replacement profile, as "
                                              "Markdown.", min_length=1)],
) -> str:
    """Overwrite a voice's profile with hand-edited Markdown.

    Replaces the whole file — there is no merge. Read the current profile with
    content_forge_get_voice_profile first if you intend to amend rather than
    replace it. Sharpening the 'Do not' section by hand is the usual way to stop
    output drifting back toward generic assistant prose.

    Returns JSON: {voice, chars}.
    """
    v = _voice(voice)
    storage.save_voice_profile(v.slug, profile)
    return _dump({"voice": v.slug, "chars": len(profile)})


@mcp.tool(
    name="content_forge_build_voice_profile",
    annotations={"title": "Build voice profile", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": False,
                 "openWorldHint": True},
)
async def build_voice_profile(
    voice: Annotated[str, Field(description="Voice slug.", min_length=1)],
    notes: Annotated[str, Field(
        description="Anything the samples do not show — words to avoid, how "
                    "formal to go, running jokes.", max_length=2000)] = "",
) -> str:
    """Start a job that reads every sample and writes a new voice profile.

    Asynchronous and costs Claude usage. Overwrites any existing profile, so
    save a copy via content_forge_get_voice_profile if the current one has been
    hand-tuned. Requires at least a few hundred words of samples or the job
    fails.

    Returns JSON: {job_id, status, voice}. Poll content_forge_get_job; on
    success its result is {voice, chars, cost}.
    """
    v = _voice(voice)
    if len(storage.collect_voice_samples_text(v.slug)) < 400:
        raise ToolError(
            f"Voice '{v.slug}' has too little sample material "
            f"({len(storage.list_voice_samples(v.slug))} samples). Add more "
            "with content_forge_add_voice_sample first — a few hundred words "
            "minimum."
        )
    job = jobs.submit("voice", f"Voice profile — {v.name}",
                      lambda: generate.build_voice(v, notes),
                      redirect=f"/voices/{v.slug}")
    return _dump({"job_id": job.id, "status": job.status, "voice": v.slug})


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
    t = _topic(topic)
    ideas = storage.get_ideas(t.slug)
    return _dump({"topic": t.slug, "count": len(ideas), "ideas": ideas})


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
                      redirect=f"/topics/{t.slug}?tab=ideas")
    return _dump({"job_id": job.id, "status": job.status, "topic": t.slug})


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
    with_references, voice, styles, brief, error, job_id, created_at,
    updated_at}.
    """
    t = _topic(topic)
    rows = [_article_dict(a) for a in storage.list_articles(t.slug)]
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
    format, voice, styles, with_references, words, has_references_section,
    body, created_at, updated_at} depending on `format`.
    """
    t = _topic(topic)
    topic = t.slug
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
    topic: Annotated[str, Field(description="Topic slug — the subject area this "
                                            "piece is filed under. The display "
                                            "name also works.", min_length=1)],
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
    voice: Annotated[str, Field(
        description="Voice slug (or display name) whose style guide this piece "
                    "must follow — e.g. 'bob'. From content_forge_list_voices. "
                    "Leave empty for a generic professional tone.",
        max_length=80)] = "",
    styles: Annotated[list[Literal["expository", "persuasive", "descriptive",
                                   "narrative", "minimalist", "lyrical",
                                   "conversational", "analytical"]],
                      Field(description="Writing-style modes to layer on top of "
                                        "the voice for this piece. Independent "
                                        "of voice — combine any number, e.g. "
                                        "['persuasive', 'descriptive'].")] = [],
) -> str:
    """Start a job that writes an article: a topic, a voice and a set of styles.

    This is the tool behind requests like "write an article using the Bob voice
    in a persuasive style in the boating topic about X" — topic='boating',
    voice='bob', styles=['persuasive'], brief='X'.

    Asynchronous and costs Claude usage. Returns immediately with a job_id; the
    article folder exists straight away with status 'generating'. Poll
    content_forge_get_job, then fetch the finished piece with
    content_forge_get_article.

    Timing: roughly 30-90 seconds without references, several minutes with. A
    voice with no built profile is rejected — build it first with
    content_forge_build_voice_profile, or pass no voice for a generic tone.

    Returns JSON: {job_id, status, topic, voice, styles, article, title, url}
    where `article` is the slug to pass to content_forge_get_article.
    """
    t = _topic(topic)
    vslug = ""
    if voice.strip():
        v = _voice(voice)
        if not v.has_profile:
            raise ToolError(
                f"Voice '{v.slug}' has no profile built yet, so it cannot "
                "shape output. Add samples with content_forge_add_voice_sample "
                "and run content_forge_build_voice_profile, or omit `voice` to "
                "write in a generic tone."
            )
        vslug = v.slug
    a = storage.create_article(t.slug, title, brief or title,
                               with_references=references, word_target=words,
                               fmt=format, voice=vslug, styles=list(styles))
    a.status = "generating"
    job = jobs.submit("article", f"{t.name} — {a.title}",
                      lambda: generate.generate_article(t, a),
                      redirect=f"/topics/{t.slug}/articles/{a.slug}")
    a.job_id = job.id
    storage.save_article_meta(a)
    return _dump({"job_id": job.id, "status": job.status, "topic": t.slug,
                  "voice": vslug, "styles": a.styles,
                  "article": a.slug, "title": a.title,
                  "url": f"/topics/{t.slug}/articles/{a.slug}"})


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
    voice: Annotated[str | None, Field(
        description="Replacement voice slug. Leave unset (null) to keep the "
                    "current one; pass an empty string to drop to a generic "
                    "tone.", max_length=80)] = None,
    styles: Annotated[list[Literal["expository", "persuasive", "descriptive",
                                   "narrative", "minimalist", "lyrical",
                                   "conversational", "analytical"]] | None,
                      Field(description="Replacement writing-style modes to "
                                        "layer on top of the voice. Leave "
                                        "unset (null) to keep the current "
                                        "ones; pass [] to clear them.")] = None,
) -> str:
    """Start a job that regenerates an existing article, replacing its body.

    Use this to re-cut a finished piece in a different voice or style without
    starting over: pass voice='bob' or styles=['narrative'] and leave the rest
    unset to inherit.

    Destructive: the current body is overwritten when the job completes. Save a
    copy with content_forge_get_article first if it matters.

    Returns JSON: {job_id, status, topic, article, voice, styles}. Poll
    content_forge_get_job.
    """
    t = _topic(topic)
    topic = t.slug
    a = _article(topic, article)
    if brief.strip():
        a.prompt = brief.strip()
    a.with_references = references
    if styles is not None:
        a.styles = list(styles)
    if voice is not None:
        if voice.strip():
            v = _voice(voice)
            if not v.has_profile:
                raise ToolError(
                    f"Voice '{v.slug}' has no profile built yet. Run "
                    "content_forge_build_voice_profile first, or pass an empty "
                    "voice for a generic tone."
                )
            a.voice = v.slug
        else:
            a.voice = ""
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
                  "article": article, "voice": a.voice, "styles": a.styles})


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
    topic = _topic(topic).slug
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
    topic = t.slug
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

    Returns JSON: {ok, claude, model, content_root, topics, voices,
    voices_ready, active_jobs, auth{method, summary, credentials_expired,
    last_test}}.
    """
    ok, ver = claude_runner.cli_available()
    st = auth.state()
    voices = storage.list_voices()
    return _dump({
        "ok": ok, "claude": ver, "model": CLAUDE_MODEL,
        "content_root": str(CONTENT_ROOT),
        "topics": len(storage.list_topics()),
        "voices": len(voices),
        "voices_ready": len([v for v in voices if v.has_profile]),
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
        topic = _topic(topic).slug
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
        """Permanently delete a topic and every article in it.

        Voices are untouched — they live outside topics and stay available to
        every other topic.

        Irreversible — there is no trash. Returns JSON: {topic, deleted,
        articles_removed}.
        """
        t = _topic(topic)
        n = t.article_count
        return _dump({"topic": t.slug, "deleted": storage.delete_topic(t.slug),
                      "articles_removed": n})

    @mcp.tool(
        name="content_forge_delete_voice",
        annotations={"title": "Delete voice", "readOnlyHint": False,
                     "destructiveHint": True, "idempotentHint": True,
                     "openWorldHint": False},
    )
    async def delete_voice(
        voice: Annotated[str, Field(description="Voice slug.", min_length=1)],
    ) -> str:
        """Permanently delete a voice, its samples and its built profile.

        Articles already written with it keep their text but lose the link, and
        rewriting one will fall back to a generic tone unless a new voice is
        named.

        Irreversible — there is no trash. Returns JSON: {voice, deleted,
        samples_removed}.
        """
        v = _voice(voice)
        n = v.sample_count
        return _dump({"voice": v.slug, "deleted": storage.delete_voice(v.slug),
                      "samples_removed": n})


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
