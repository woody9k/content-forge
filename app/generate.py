"""The three things that actually call Claude, as plain functions.

These run on the jobs thread pool. Both the web routes and the MCP server submit
them through `jobs.submit`, so a generation started by an agent is the same job
the browser is watching — one registry, one source of truth.
"""
from __future__ import annotations

import json
import re

from . import claude_runner, prompts, storage
from .render import clean_markdown, title_from_markdown


def generate_article(topic: storage.Topic, article: storage.Article) -> dict:
    voice = storage.get_voice_profile(topic.slug)
    prior = [a.title for a in storage.list_articles(topic.slug)
             if a.slug != article.slug and a.status == "ready"]
    prompt = prompts.article_prompt(
        topic_name=topic.name, topic_desc=topic.description,
        audience=topic.audience, user_prompt=article.prompt,
        fmt=article.format, word_target=article.word_target,
        with_references=article.with_references, prior_titles=prior,
    )
    try:
        res = claude_runner.run(prompt, system=prompts.article_system(voice),
                                use_tools=article.with_references)
    except Exception as exc:
        article.status = "error"
        article.error = str(exc)
        storage.save_article_meta(article)
        raise

    body = clean_markdown(res.text)
    storage.write_article_body(topic.slug, article.slug, body)
    article.title = title_from_markdown(body, article.title)
    article.status = "ready"
    article.error = ""
    storage.save_article_meta(article)
    # a stale export would be misleading
    exp = storage.article_dir(topic.slug, article.slug) / "export.docx"
    if exp.exists():
        exp.unlink()
    return {"topic": topic.slug, "article": article.slug,
            "title": article.title, "words": len(body.split()),
            "cost": res.cost_usd, "duration": res.duration_s}


def build_voice(topic: storage.Topic, notes: str) -> dict:
    samples = storage.collect_samples_text(topic.slug)
    if len(samples) < 400:
        raise RuntimeError(
            "Not enough sample material. Upload at least a few hundred words "
            "of your own writing for this topic first."
        )
    res = claude_runner.run(
        prompts.voice_profile_prompt(topic.name, samples, notes),
        system=prompts.VOICE_SYSTEM, use_tools=False,
    )
    storage.save_voice_profile(topic.slug, clean_markdown(res.text))
    return {"topic": topic.slug, "chars": len(samples), "cost": res.cost_usd}


def suggest_ideas(topic: storage.Topic, count: int, research: bool) -> dict:
    prior = [a.title for a in storage.list_articles(topic.slug)]
    res = claude_runner.run(
        prompts.suggest_prompt(topic_name=topic.name, topic_desc=topic.description,
                               audience=topic.audience, prior_titles=prior,
                               count=count, with_research=research),
        use_tools=research,
    )
    text = res.text.strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise RuntimeError("Could not parse suggestions from the model output.")
    ideas = json.loads(m.group(0))
    clean = []
    for it in ideas:
        if isinstance(it, dict) and it.get("title"):
            clean.append({
                "title": str(it.get("title", ""))[:200],
                "angle": str(it.get("angle", ""))[:600],
                "why": str(it.get("why", ""))[:600],
                "format": str(it.get("format", "blog post"))[:40],
                "words": int(it.get("words") or 900),
            })
    storage.save_ideas(topic.slug, clean)
    return {"topic": topic.slug, "count": len(clean)}
