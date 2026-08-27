from __future__ import annotations

import base64
import hmac
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import (FileResponse, HTMLResponse, PlainTextResponse,
                               RedirectResponse, JSONResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, claude_runner, generate, jobs, prompts, storage
from .config import ACCESS_TOKEN, APP_DIR, CLAUDE_BIN, CLAUDE_MODEL, CONTENT_ROOT
from .docx_export import markdown_to_docx
from .mcp_server import lifespan as mcp_lifespan, mount_mcp
from .render import clean_markdown as _clean_markdown, render_markdown
from .render import title_from_markdown as _title_from_markdown

app = FastAPI(title="Content Forge", lifespan=mcp_lifespan)
# Streamable HTTP MCP, same process as the web UI so agent-started generations
# land in the same job registry the browser polls.
mount_mcp(app)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


# --------------------------------------------------------------------------
# access control — a no-op unless CF_ACCESS_TOKEN is set (see config.py)
# --------------------------------------------------------------------------

def _token_matches(candidate: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, ACCESS_TOKEN)


@app.middleware("http")
async def require_access_token(request: Request, call_next):
    if not ACCESS_TOKEN or request.url.path in ("/health",):
        return await call_next(request)

    # An MCP client probes for OAuth metadata before it will talk to us
    # (RFC 9728), at both /.well-known/... and <mount>/.well-known/... .
    # Answering 401 there says "you may not even ask how to authenticate",
    # which reads as a broken handshake rather than "no OAuth here" — some
    # clients then give up instead of falling back to their configured token.
    # We do not implement OAuth, and 404 is how you say so.
    if "/.well-known/" in request.url.path:
        return PlainTextResponse("Not Found", status_code=404)

    # RFC 7235: the auth scheme is case-insensitive. Matching it exactly meant
    # a spec-compliant client sending "bearer" got a 401 indistinguishable from
    # a wrong token — an afternoon lost to a credential that was fine.
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    scheme, credentials = scheme.lower(), credentials.strip()

    ok = False
    if scheme == "bearer":
        ok = _token_matches(credentials)
    elif scheme == "basic":
        try:
            _, _, password = base64.b64decode(credentials).decode(
                "utf-8", "replace").partition(":")
        except (ValueError, UnicodeDecodeError):
            password = ""
        ok = _token_matches(password)

    if not ok:
        # A hosted connector platform validates the base URL before it will
        # register the server, and does it WITHOUT credentials. Answering 401
        # to that reads as "this host is not usable", so the connector never
        # reaches the MCP handshake at all. These two paths therefore answer
        # unauthenticated — and deliberately carry nothing worth having: no
        # topic, voice, article or tool data, just the service's name.
        # Everything that touches content stays gated below.
        if request.url.path == "/robots.txt":
            return PlainTextResponse("User-agent: *\nDisallow: /\n")
        if request.url.path == "/" and request.method in ("GET", "HEAD"):
            return HTMLResponse(
                "<!doctype html><meta charset=utf-8>"
                "<title>Content Forge</title>"
                "<h1>Content Forge</h1>"
                "<p>This service requires authentication. "
                '<a href="/voices">Sign in</a>.</p>',
                status_code=200)

        # Both schemes are offered. Basic comes first so a browser still shows
        # its login box for the web UI; Bearer is named so a machine client is
        # told, rather than left to guess, that a token is what we want.
        return PlainTextResponse(
            "Unauthorized", status_code=401,
            headers={"WWW-Authenticate":
                     'Basic realm="Content Forge", Bearer realm="Content Forge"'})
    return await call_next(request)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _topic_or_404(slug: str) -> storage.Topic:
    t = storage.get_topic(slug)
    if not t:
        raise HTTPException(404, f"No topic '{slug}'")
    return t


def _article_or_404(topic: str, slug: str) -> storage.Article:
    a = storage.get_article(topic, slug)
    if not a:
        raise HTTPException(404, f"No article '{slug}'")
    return a


def _voice_or_404(slug: str) -> storage.Voice:
    v = storage.get_voice(slug)
    if not v:
        raise HTTPException(404, f"No voice '{slug}'")
    return v


def _usable_voice(slug: str) -> str:
    """Validate a voice chosen for an article, returning the slug to store.

    A voice with no built profile cannot shape anything — generation would
    quietly fall back to generic prose while the article page claimed the
    voice was applied. Refuse it here rather than lie about it later.
    """
    if not slug:
        return ""
    v = storage.get_voice(slug)
    if not v:
        raise HTTPException(400, f"No voice '{slug}'")
    if not v.has_profile:
        raise HTTPException(
            400, f"Voice '{v.name}' has no profile built yet, so it cannot "
                 "shape the writing. Build it from its samples first, or "
                 "choose no voice.")
    return v.slug


def _render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx["request"] = request
    ctx.setdefault("model", CLAUDE_MODEL)
    ctx.setdefault("content_root", str(CONTENT_ROOT))
    # Starlette 1.x takes the request first; the old (name, context) form,
    # where the request rode inside the context, was removed.
    return templates.TemplateResponse(request, name, ctx)


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    ok, ver = claude_runner.cli_available()
    return _render(request, "index.html", topics=storage.list_topics(),
                   cli_ok=ok, cli_version=ver, active_jobs=jobs.active())


@app.post("/topics")
def create_topic(name: str = Form(...), description: str = Form(""),
                 audience: str = Form("")):
    if not name.strip():
        raise HTTPException(400, "Topic name required")
    t = storage.create_topic(name, description, audience)
    return RedirectResponse(f"/topics/{t.slug}", status_code=303)


@app.get("/topics/{slug}", response_class=HTMLResponse)
def topic_page(request: Request, slug: str, tab: str = "articles"):
    t = _topic_or_404(slug)
    return _render(request, "topic.html", topic=t, tab=tab,
                   articles=storage.list_articles(slug),
                   ideas=storage.get_ideas(slug),
                   styles=prompts.WRITING_STYLES,
                   voices=storage.list_voices())


@app.post("/topics/{slug}/edit")
def edit_topic(slug: str, name: str = Form(...), description: str = Form(""),
               audience: str = Form("")):
    _topic_or_404(slug)
    storage.update_topic(slug, name=name.strip(), description=description.strip(),
                         audience=audience.strip())
    return RedirectResponse(f"/topics/{slug}?tab=settings", status_code=303)


@app.post("/topics/{slug}/delete")
def delete_topic(slug: str):
    storage.delete_topic(slug)
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------
# voices — independent of topic, applied to individual articles
# --------------------------------------------------------------------------

@app.get("/voices", response_class=HTMLResponse)
def voices_index(request: Request):
    return _render(request, "voices.html", voices=storage.list_voices())


@app.post("/voices")
def create_voice(name: str = Form(...), description: str = Form("")):
    if not name.strip():
        raise HTTPException(400, "Voice name required")
    v = storage.create_voice(name, description)
    return RedirectResponse(f"/voices/{v.slug}", status_code=303)


@app.get("/voices/{slug}", response_class=HTMLResponse)
def voice_page(request: Request, slug: str, tab: str = "profile"):
    v = _voice_or_404(slug)
    return _render(request, "voice.html", voice=v, tab=tab,
                   samples=storage.list_voice_samples(slug),
                   profile=storage.get_voice_profile(slug))


@app.post("/voices/{slug}/edit")
def edit_voice(slug: str, name: str = Form(...), description: str = Form("")):
    _voice_or_404(slug)
    # Same rule as creating one: a nameless voice is unidentifiable in the
    # list. The input is marked required, but that only binds a browser.
    if not name.strip():
        raise HTTPException(400, "Voice name required")
    storage.update_voice(slug, name=name.strip(), description=description.strip())
    return RedirectResponse(f"/voices/{slug}?tab=settings", status_code=303)


@app.post("/voices/{slug}/delete")
def delete_voice(slug: str):
    storage.delete_voice(slug)
    return RedirectResponse("/voices", status_code=303)


@app.get("/voices/{slug}/image")
def voice_image(slug: str):
    _voice_or_404(slug)
    p = storage.voice_image_path(slug)
    if not p:
        raise HTTPException(404, "This voice has no portrait.")
    # nosniff matters: the bytes are user-supplied, and without it a browser
    # could decide to interpret them as something executable.
    return FileResponse(str(p),
                        media_type=storage.voice_image_media_type(p),
                        headers={"X-Content-Type-Options": "nosniff",
                                 "Cache-Control": "no-cache"})


@app.post("/voices/{slug}/image")
async def upload_voice_image(slug: str, image: UploadFile = File(...)):
    _voice_or_404(slug)

    # Read in chunks and stop the moment the cap is passed. Reading the whole
    # body first would mean a 1GB upload costs 1GB of RAM before we get as far
    # as rejecting it — on a small box that is the difference between a 400 and
    # the OOM killer.
    limit = storage.MAX_IMAGE_BYTES
    chunks, total = [], 0
    while True:
        chunk = await image.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                400, f"That image is larger than the "
                     f"{limit // (1024 * 1024)}MB limit.")
        chunks.append(chunk)

    try:
        storage.save_voice_image(slug, b"".join(chunks))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(f"/voices/{slug}?tab=settings", status_code=303)


@app.post("/voices/{slug}/image/delete")
def remove_voice_image(slug: str):
    _voice_or_404(slug)
    storage.delete_voice_image(slug)
    return RedirectResponse(f"/voices/{slug}?tab=settings", status_code=303)


@app.post("/voices/{slug}/samples")
async def upload_voice_samples(slug: str, files: list[UploadFile] = File(...)):
    _voice_or_404(slug)
    for f in files:
        if f.filename:
            storage.save_voice_sample(slug, f.filename, await f.read())
    return RedirectResponse(f"/voices/{slug}?tab=profile", status_code=303)


@app.post("/voices/{slug}/paste")
def paste_voice_sample(slug: str, name: str = Form("pasted-sample"),
                       text: str = Form(...)):
    _voice_or_404(slug)
    if text.strip():
        storage.save_voice_sample(slug, f"{storage.slugify(name, 'sample')}.md",
                                  text.encode("utf-8"))
    return RedirectResponse(f"/voices/{slug}?tab=profile", status_code=303)


@app.post("/voices/{slug}/samples/delete")
def remove_voice_sample(slug: str, name: str = Form(...)):
    storage.delete_voice_sample(slug, name)
    return RedirectResponse(f"/voices/{slug}?tab=profile", status_code=303)


@app.post("/voices/{slug}/build")
def build_voice(slug: str, notes: str = Form("")):
    v = _voice_or_404(slug)
    job = jobs.submit("voice", f"Voice profile — {v.name}",
                      lambda: generate.build_voice(v, notes),
                      redirect=f"/voices/{slug}")
    return RedirectResponse(f"/jobs/{job.id}/wait", status_code=303)


@app.post("/voices/{slug}/profile")
def save_profile(slug: str, profile: str = Form(...)):
    _voice_or_404(slug)
    storage.save_voice_profile(slug, profile)
    return RedirectResponse(f"/voices/{slug}?tab=profile", status_code=303)


# --------------------------------------------------------------------------
# ideas
# --------------------------------------------------------------------------

@app.post("/topics/{slug}/ideas")
def suggest(slug: str, count: int = Form(8), research: str = Form("")):
    t = _topic_or_404(slug)
    job = jobs.submit("ideas", f"Ideas — {t.name}",
                      lambda: generate.suggest_ideas(t, max(1, min(count, 20)), bool(research)),
                      redirect=f"/topics/{slug}?tab=ideas")
    return RedirectResponse(f"/jobs/{job.id}/wait", status_code=303)


# --------------------------------------------------------------------------
# articles
# --------------------------------------------------------------------------

@app.post("/topics/{slug}/articles")
def new_article(slug: str, title: str = Form(...), prompt: str = Form(""),
                fmt: str = Form("blog post"), words: int = Form(900),
                references: str = Form(""), styles: list[str] = Form([]),
                voice: str = Form("")):
    t = _topic_or_404(slug)
    valid_styles = [s for s in styles if s in prompts.WRITING_STYLES]
    vslug = _usable_voice(voice)
    a = storage.create_article(slug, title, prompt or title,
                               with_references=bool(references),
                               word_target=max(150, min(words, 6000)), fmt=fmt,
                               voice=vslug, styles=valid_styles)
    a.status = "generating"
    job = jobs.submit("article", f"{t.name} — {a.title}",
                      lambda: generate.generate_article(t, a),
                      redirect=f"/topics/{slug}/articles/{a.slug}")
    a.job_id = job.id
    storage.save_article_meta(a)
    return RedirectResponse(f"/jobs/{job.id}/wait", status_code=303)


@app.get("/topics/{slug}/articles/{aslug}", response_class=HTMLResponse)
def article_page(request: Request, slug: str, aslug: str):
    t = _topic_or_404(slug)
    a = _article_or_404(slug, aslug)
    body = storage.read_article_body(slug, aslug)
    return _render(request, "article.html", topic=t, article=a, body=body,
                   preview_html=render_markdown(body), styles=prompts.WRITING_STYLES,
                   voices=storage.list_voices(),
                   voice_obj=storage.get_voice(a.voice) if a.voice else None)


@app.post("/topics/{slug}/articles/{aslug}")
def save_article(slug: str, aslug: str, body: str = Form(...),
                 title: str = Form("")):
    _topic_or_404(slug)
    a = _article_or_404(slug, aslug)
    storage.write_article_body(slug, aslug, body)
    a.title = (title.strip() or _title_from_markdown(body, a.title))
    if a.status in ("draft", "error"):
        a.status = "ready"
    storage.save_article_meta(a)
    exp = storage.article_dir(slug, aslug) / "export.docx"
    if exp.exists():
        exp.unlink()
    return RedirectResponse(f"/topics/{slug}/articles/{aslug}?saved=1",
                            status_code=303)


@app.post("/topics/{slug}/articles/{aslug}/regenerate")
def regenerate(slug: str, aslug: str, prompt: str = Form(""),
               references: str = Form(""), words: int = Form(0),
               styles: list[str] = Form([]), voice: str = Form("")):
    t = _topic_or_404(slug)
    a = _article_or_404(slug, aslug)
    if prompt.strip():
        a.prompt = prompt.strip()
    a.with_references = bool(references)
    a.styles = [s for s in styles if s in prompts.WRITING_STYLES]
    a.voice = _usable_voice(voice)
    if words:
        a.word_target = max(150, min(words, 6000))
    a.status = "generating"
    storage.save_article_meta(a)
    job = jobs.submit("article", f"Rewrite — {a.title}",
                      lambda: generate.generate_article(t, a),
                      redirect=f"/topics/{slug}/articles/{aslug}")
    a.job_id = job.id
    storage.save_article_meta(a)
    return RedirectResponse(f"/jobs/{job.id}/wait", status_code=303)


@app.post("/topics/{slug}/articles/{aslug}/delete")
def remove_article(slug: str, aslug: str):
    storage.delete_article(slug, aslug)
    return RedirectResponse(f"/topics/{slug}", status_code=303)


@app.get("/topics/{slug}/articles/{aslug}/export.docx")
def export_docx(slug: str, aslug: str):
    t = _topic_or_404(slug)
    a = _article_or_404(slug, aslug)
    body = storage.read_article_body(slug, aslug)
    if not body.strip():
        raise HTTPException(400, "Nothing to export yet.")
    out = storage.article_dir(slug, aslug) / "export.docx"
    markdown_to_docx(body, out, title=a.title,
                     subtitle=f"{t.name} · {a.created_at[:10]}")
    return FileResponse(
        str(out),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"{a.slug}.docx",
    )


@app.get("/topics/{slug}/articles/{aslug}/raw.md")
def export_md(slug: str, aslug: str):
    _topic_or_404(slug)
    a = _article_or_404(slug, aslug)
    p = storage.article_dir(slug, aslug) / "article.md"
    if not p.exists():
        raise HTTPException(404, "No body yet.")
    return FileResponse(str(p), media_type="text/markdown",
                        filename=f"{a.slug}.md")


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

@app.get("/jobs/{job_id}/wait", response_class=HTMLResponse)
def job_wait(request: Request, job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return _render(request, "job.html", job=job)


@app.get("/jobs/{job_id}/status", response_class=HTMLResponse)
def job_status(request: Request, job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    jobs.prune()
    return _render(request, "partials/job_status.html", job=job)


@app.post("/preview", response_class=HTMLResponse)
def preview(body: str = Form("")):
    return HTMLResponse(render_markdown(body))


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

def _auth_ctx(**extra) -> dict:
    ok, version = claude_runner.cli_version()
    ctx = {"auth": auth.state(), "cli_ok": ok, "cli_version": version,
           "probe": claude_runner.last_probe(), "claude_bin": CLAUDE_BIN}
    ctx.update(extra)
    return ctx


@app.get("/auth", response_class=HTMLResponse)
def auth_page(request: Request):
    return _render(request, "auth.html", **_auth_ctx())


@app.post("/auth/test", response_class=HTMLResponse)
def auth_test(request: Request):
    """Spend one real request to find out whether the credential works."""
    claude_runner.probe()
    return _render(request, "partials/auth_status.html", **_auth_ctx())


@app.post("/auth/token", response_class=HTMLResponse)
def auth_save_token(request: Request, token: str = Form(...),
                    skip_test: str = Form("")):
    token = token.strip()
    if not token:
        return _render(request, "partials/auth_status.html",
                       **_auth_ctx(flash_err="Paste a token first."))

    # Test before saving so a mistyped token cannot displace a working one.
    if not skip_test:
        result = claude_runner.probe(token=token)
        if not result["ok"]:
            return _render(request, "partials/auth_status.html", **_auth_ctx(
                flash_err=f"Token rejected, nothing saved — {result['detail']}"))
    try:
        auth.save_token(token)
    except (ValueError, OSError) as exc:
        return _render(request, "partials/auth_status.html",
                       **_auth_ctx(flash_err=f"Could not save: {exc}"))

    claude_runner.probe()
    return _render(request, "partials/auth_status.html",
                   **_auth_ctx(flash_ok="Token saved. Generation uses it immediately."))


@app.post("/auth/token/delete", response_class=HTMLResponse)
def auth_clear_token(request: Request):
    removed = auth.clear_token()
    return _render(request, "partials/auth_status.html", **_auth_ctx(
        flash_ok="Token removed — falling back to the stored login."
        if removed else None,
        flash_err=None if removed else "No managed token to remove."))


@app.get("/health")
def health():
    ok, ver = claude_runner.cli_available()
    st = auth.state()
    return JSONResponse({
        "ok": ok,
        "claude": ver,
        "auth": {
            "method": st["method"],
            "summary": st["summary"],
            "token_source": st["token_source"],
            "credentials_expires_at": st["credentials"]["expires_at"],
            "credentials_expired": st["credentials"]["expired"],
            "ignored_api_keys": st["stray_api_keys"],
            "last_test": claude_runner.last_probe() or None,
        },
        "model": CLAUDE_MODEL,
        "content_root": str(CONTENT_ROOT),
        "topics": len(storage.list_topics()),
        "voices": len(storage.list_voices()),
        "active_jobs": len(jobs.active()),
    })
