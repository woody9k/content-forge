from __future__ import annotations

import base64
import hmac
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import (FileResponse, HTMLResponse, PlainTextResponse,
                               RedirectResponse, JSONResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, claude_runner, generate, jobs, storage
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

    header = request.headers.get("authorization", "")
    ok = False
    if header.startswith("Bearer "):
        ok = _token_matches(header[len("Bearer "):].strip())
    elif header.startswith("Basic "):
        try:
            _, _, password = base64.b64decode(header[len("Basic "):]).decode(
                "utf-8", "replace").partition(":")
        except (ValueError, UnicodeDecodeError):
            password = ""
        ok = _token_matches(password)

    if not ok:
        return PlainTextResponse(
            "Unauthorized", status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Content Forge"'})
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


def _render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx["request"] = request
    ctx.setdefault("model", CLAUDE_MODEL)
    ctx.setdefault("content_root", str(CONTENT_ROOT))
    return templates.TemplateResponse(name, ctx)


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
                   samples=storage.list_samples(slug),
                   profile=storage.get_voice_profile(slug),
                   ideas=storage.get_ideas(slug))


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
# voice
# --------------------------------------------------------------------------

@app.post("/topics/{slug}/voice/samples")
async def upload_samples(slug: str, files: list[UploadFile] = File(...)):
    _topic_or_404(slug)
    for f in files:
        if f.filename:
            storage.save_sample(slug, f.filename, await f.read())
    return RedirectResponse(f"/topics/{slug}?tab=voice", status_code=303)


@app.post("/topics/{slug}/voice/paste")
def paste_sample(slug: str, name: str = Form("pasted-sample"),
                 text: str = Form(...)):
    _topic_or_404(slug)
    if text.strip():
        storage.save_sample(slug, f"{storage.slugify(name, 'sample')}.md",
                            text.encode("utf-8"))
    return RedirectResponse(f"/topics/{slug}?tab=voice", status_code=303)


@app.post("/topics/{slug}/voice/samples/delete")
def remove_sample(slug: str, name: str = Form(...)):
    storage.delete_sample(slug, name)
    return RedirectResponse(f"/topics/{slug}?tab=voice", status_code=303)


@app.post("/topics/{slug}/voice/build")
def build_voice(slug: str, notes: str = Form("")):
    t = _topic_or_404(slug)
    job = jobs.submit("voice", f"Voice profile — {t.name}",
                      lambda: generate.build_voice(t, notes),
                      redirect=f"/topics/{slug}?tab=voice")
    return RedirectResponse(f"/jobs/{job.id}/wait", status_code=303)


@app.post("/topics/{slug}/voice/profile")
def save_profile(slug: str, profile: str = Form(...)):
    _topic_or_404(slug)
    storage.save_voice_profile(slug, profile)
    return RedirectResponse(f"/topics/{slug}?tab=voice", status_code=303)


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
                references: str = Form("")):
    t = _topic_or_404(slug)
    a = storage.create_article(slug, title, prompt or title,
                               with_references=bool(references),
                               word_target=max(150, min(words, 6000)), fmt=fmt)
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
                   preview_html=render_markdown(body))


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
               references: str = Form(""), words: int = Form(0)):
    t = _topic_or_404(slug)
    a = _article_or_404(slug, aslug)
    if prompt.strip():
        a.prompt = prompt.strip()
    a.with_references = bool(references)
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
        "active_jobs": len(jobs.active()),
    })
