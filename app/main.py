from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import (FileResponse, HTMLResponse, PlainTextResponse,
                               RedirectResponse, JSONResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (accounts, auth, claude_runner, generate, jobs, oauth_provider,
               prompts, storage)
from .config import (APP_DIR, CLAUDE_BIN, CLAUDE_MODEL, CONTENT_ROOT,
                     LEGACY_ACCESS_TOKEN, MCP_SCOPE, PUBLIC_SCHEME,
                     REQUIRE_AUTH, SESSION_COOKIE, SESSION_TTL,
                     public_base_url)
from .docx_export import markdown_to_docx
from . import mcp_server
from .mcp_server import lifespan as mcp_lifespan, mount_mcp
from .render import clean_markdown as _clean_markdown, render_markdown
from .render import title_from_markdown as _title_from_markdown

@asynccontextmanager
async def _lifespan(app):
    """Start-up work, then hand off to the MCP session manager.

    This has to be a lifespan rather than a startup event handler: Starlette
    ignores those entirely once a lifespan is passed to the constructor, and
    this app has always passed one. Creating the schema there meant it
    silently never ran, and the first request carrying an unknown token died
    on "no such table: oauth_access" — a 500 where a 401 belonged.
    """
    oauth_provider.init_db()
    if LEGACY_ACCESS_TOKEN:
        print("!!  CF_ACCESS_TOKEN is set but no longer used. Authentication "
              "is per-user now; remove it from the environment and create "
              "accounts with: ./deploy/add-user.sh <name> --admin")
    if REQUIRE_AUTH and accounts.count_users() == 0:
        print("!!  No accounts exist yet, so nobody can sign in. Create the "
              "first administrator with:\n"
              "      ./deploy/add-user.sh <name> --admin")
    async with mcp_lifespan(app):
        yield


app = FastAPI(title="Content Forge", lifespan=_lifespan)
# Streamable HTTP MCP, same process as the web UI so agent-started generations
# land in the same job registry the browser polls.
mount_mcp(app)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def _fmt_timestamp(ts) -> str:
    """Unix seconds as a local, human date — used on the account page for
    token ages, where an epoch integer tells the reader nothing."""
    if not ts:
        return "never"
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


templates.env.filters["timestamp"] = _fmt_timestamp


# --------------------------------------------------------------------------
# access control
#
# Three credentials, one identity. A session cookie for the browser, a
# personal access token for scripts, an OAuth access token for connectors —
# each resolves to the same User, which is what the old shared secret could
# never do. `request.state.user` is set for every request that gets through.
#
# /mcp is deliberately NOT gated here. The SDK gates it, and its 401 carries
# the `WWW-Authenticate: Bearer resource_metadata="..."` pointer a connector
# follows to discover the authorization server. Answering it ourselves would
# strip that pointer and put us back where we started.
# --------------------------------------------------------------------------

# Reachable with no credential at all. Kept as small as it can be: `/` and
# `/robots.txt` stay open because a connector platform validates the base URL
# before it will register anything, and they carry nothing but the service's
# name. Everything that touches content is gated.
_PUBLIC_EXACT = frozenset({"/", "/health", "/login", "/robots.txt",
                           "/favicon.ico"})
_PUBLIC_PREFIX = ("/static/", "/invite/")

# Endpoints that authenticate their own callers (OAuth client credentials,
# PKCE, a registration request) and must not be behind a session check.
_OAUTH_EXACT = frozenset({"/authorize", "/token", "/register", "/revoke"})


def _wants_html(request: Request) -> bool:
    """A browser gets a redirect to the login page; anything else gets a 401
    it can act on. Guessing wrong means an API client chasing an HTML page."""
    return "text/html" in request.headers.get("accept", "")


# A public login form with no throttle is both a password-guessing oracle and
# a CPU lever: every attempt costs an scrypt hash. This is deliberately a small
# in-memory counter rather than a dependency — one process, low traffic, and
# losing the counters on restart is an acceptable trade for the simplicity.
_LOGIN_FAILS: dict[str, tuple[int, float]] = {}
_LOGIN_MAX_FAILS = 8
_LOGIN_WINDOW = 300.0          # seconds a lockout lasts, and the counting window


def _login_blocked(key: str) -> bool:
    import time
    fails, first = _LOGIN_FAILS.get(key, (0, 0.0))
    if fails >= _LOGIN_MAX_FAILS and time.time() - first < _LOGIN_WINDOW:
        return True
    if first and time.time() - first >= _LOGIN_WINDOW:
        _LOGIN_FAILS.pop(key, None)     # window rolled over; start clean
    return False


def _note_login_failure(key: str) -> None:
    import time
    now = time.time()
    fails, first = _LOGIN_FAILS.get(key, (0, now))
    if now - first >= _LOGIN_WINDOW:
        fails, first = 0, now
    _LOGIN_FAILS[key] = (fails + 1, first)
    if len(_LOGIN_FAILS) > 4096:        # bound the map against distributed noise
        cutoff = now - _LOGIN_WINDOW
        for k, (_, t) in list(_LOGIN_FAILS.items()):
            if t < cutoff:
                _LOGIN_FAILS.pop(k, None)


def _login_key(request: Request, username: str) -> str:
    """Keyed on client and username together, so one attacker hammering a
    single account cannot lock every other user out of signing in."""
    # X-Forwarded-For is only worth anything if a proxy we trust wrote it.
    # nginx runs on this host, so a proxied request arrives from loopback;
    # uvicorn also binds 0.0.0.0, so a request straight to :8080 does not, and
    # its header is whatever the caller felt like sending.
    #
    # Even then it is the LAST entry that counts: $proxy_add_x_forwarded_for
    # appends the real peer to anything the client supplied, so the first
    # entry is attacker-controlled and would let one rotate a header value
    # and walk straight past this throttle.
    peer = request.client.host if request.client else "?"
    forwarded = request.headers.get("x-forwarded-for", "")
    if peer in ("127.0.0.1", "::1") and forwarded:
        client = forwarded.split(",")[-1].strip() or peer
    else:
        client = peer
    return f"{client}|{username.strip().lower()}"


def _user_from_request(request: Request) -> accounts.User | None:
    session = request.cookies.get(SESSION_COOKIE, "")
    if session:
        user = accounts.session_user(session)
        if user is not None:
            return user

    # RFC 7235: the scheme is case-insensitive. Matching it exactly once cost
    # an afternoon to a credential that was fine all along.
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and credential.strip():
        return accounts.user_from_bearer(credential.strip())
    return None


@app.middleware("http")
async def authenticate(request: Request, call_next):
    request.state.user = None
    path = request.url.path

    if not REQUIRE_AUTH:
        return await call_next(request)

    if path == "/mcp" or path.startswith("/mcp/"):
        if mcp_server.MCP_AUTH_ENABLED:
            return await call_next(request)      # gated by the SDK, see above
        # The SDK only gates /mcp when there is a public URL to build an
        # issuer from. Forcing CF_REQUIRE_AUTH on without one would otherwise
        # leave the whole site behind a login while /mcp — which can spend the
        # Claude subscription and read every article — stayed wide open. Fall
        # through to the token check below instead of assuming.
        request.state.user = _user_from_request(request)
        if request.state.user is None:
            return JSONResponse(
                {"error": "unauthorized",
                 "detail": "Present a personal access token."},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="Content Forge"'})
        return await call_next(request)

    if path.startswith("/.well-known/") or path in _OAUTH_EXACT:
        return await call_next(request)

    request.state.user = _user_from_request(request)

    # The "/" carve-out exists so a connector platform can validate the base
    # URL without credentials. That is a GET. Letting it cover every method
    # would turn a read-only exemption into an unauthenticated write surface.
    root_write = (path == "/" and request.method not in ("GET", "HEAD"))
    if not root_write and (path in _PUBLIC_EXACT
                           or path.startswith(_PUBLIC_PREFIX)):
        return await call_next(request)

    if request.state.user is None:
        if _wants_html(request):
            target = path
            if request.url.query:
                target += "?" + request.url.query
            return RedirectResponse(f"/login?next={quote(target, safe='')}",
                                    status_code=303)
        return JSONResponse(
            {"error": "unauthorized",
             "detail": "Sign in, or present a personal access token."},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="Content Forge"'})

    return await call_next(request)


def _require_admin(request: Request) -> accounts.User:
    """Guard for anything that changes the box rather than the content.

    The Claude subscription token lives behind this. With one shared secret
    every caller was implicitly an admin; now that other people can sign in,
    rotating or deleting the credential the whole service runs on must not be
    something any of them can do.
    """
    if not REQUIRE_AUTH:
        # Authentication is switched off entirely (a trusted-LAN deployment).
        # There are no identities to compare, so there is no admin to be: the
        # gate would otherwise lock everyone out of a page that was open
        # before this change.
        return accounts.User(0, "local", "local", is_admin=True)
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Sign in first.")
    if not user.is_admin:
        raise HTTPException(403, "This page is restricted to administrators.")
    return user


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
    ctx.setdefault("current_user", getattr(request.state, "user", None))
    ctx.setdefault("require_auth", REQUIRE_AUTH)
    # Starlette 1.x takes the request first; the old (name, context) form,
    # where the request rode inside the context, was removed.
    return templates.TemplateResponse(request, name, ctx)


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # Public on purpose: a connector platform validates the base URL without
    # credentials, and answering 401 there reads as "this host is unusable".
    # So signed-out callers get the service's name and nothing else — no
    # topics, no voices, no counts.
    if REQUIRE_AUTH and getattr(request.state, "user", None) is None:
        return _render(request, "landing.html")
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
    _require_admin(request)
    return _render(request, "auth.html", **_auth_ctx())


@app.post("/auth/test", response_class=HTMLResponse)
def auth_test(request: Request):
    """Spend one real request to find out whether the credential works."""
    _require_admin(request)
    claude_runner.probe()
    return _render(request, "partials/auth_status.html", **_auth_ctx())


@app.post("/auth/token", response_class=HTMLResponse)
def auth_save_token(request: Request, token: str = Form(...),
                    skip_test: str = Form("")):
    _require_admin(request)
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
    _require_admin(request)
    removed = auth.clear_token()
    return _render(request, "partials/auth_status.html", **_auth_ctx(
        flash_ok="Token removed — falling back to the stored login."
        if removed else None,
        flash_err=None if removed else "No managed token to remove."))


@app.get("/health")
def health(request: Request):
    ok, ver = claude_runner.cli_available()

    # Anonymous callers get liveness and nothing else. This route has to stay
    # open for monitoring, but the full body reports which Claude credential
    # is in use, when it expires, where content lives on disk and how much of
    # it there is — reconnaissance worth having, and this host is on the open
    # internet. Signed-in users still see everything.
    if REQUIRE_AUTH and getattr(request.state, "user", None) is None:
        return JSONResponse({"ok": ok})

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


# --------------------------------------------------------------------------
# sign-in, invitations and the account page
# --------------------------------------------------------------------------

def _signed_in(request: Request) -> accounts.User:
    """The account behind this request, or a clean 404.

    The middleware guarantees a user on gated routes, but only while
    REQUIRE_AUTH is on. With authentication switched off there is no identity
    to show, and reaching into `request.state.user` would raise on None and
    surface as a 500 instead of an answer.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(404, "Accounts are not enabled on this instance.")
    return user


def _safe_next(raw: str) -> str:
    """Only ever redirect within this site. A `next` of `//evil.example` is a
    protocol-relative URL that browsers follow off-site, so the leading-slash
    check has to reject a doubled one too."""
    # Browsers normalise a backslash to a slash in the authority position, so
    # "/\\evil.example" is protocol-relative too and has to go the same way
    # as "//evil.example".
    if (raw.startswith("/") and not raw.startswith("//")
            and not raw.startswith("/\\")):
        return raw
    return "/"


def _set_session(response: Response, user: accounts.User) -> None:
    token = accounts.create_session(user.id)
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_TTL,
        httponly=True,                      # unreadable from JavaScript
        samesite="lax",                     # blocks cross-site form POSTs (CSRF)
        secure=(PUBLIC_SCHEME == "https"),
        path="/")


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"


@app.get("/favicon.ico")
def favicon():
    # The pages carry an inline SVG icon; this only exists so a browser's
    # automatic request does not bounce off the login redirect.
    return Response(status_code=204)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return _render(request, "login.html", next=_safe_next(next),
                   no_accounts=(accounts.count_users() == 0))


@app.post("/login")
def login_submit(request: Request, username: str = Form(...),
                 password: str = Form(...), next: str = Form("/")):
    key = _login_key(request, username)
    if _login_blocked(key):
        return _render(request, "login.html", next=_safe_next(next),
                       error="Too many attempts. Wait a few minutes and try "
                             "again.",
                       username=username,
                       no_accounts=(accounts.count_users() == 0))

    user = accounts.verify_password(username, password)
    if user is None:
        _note_login_failure(key)
        # One message for every failure mode. Saying "no such user" would turn
        # this form into a way to enumerate who has an account here.
        return _render(request, "login.html", next=_safe_next(next),
                       error="That username and password did not match.",
                       username=username,
                       no_accounts=(accounts.count_users() == 0))
    _LOGIN_FAILS.pop(key, None)
    response = RedirectResponse(_safe_next(next), status_code=303)
    _set_session(response, user)
    return response


@app.post("/logout")
def logout(request: Request):
    accounts.destroy_session(request.cookies.get(SESSION_COOKIE, ""))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/invite/{token}", response_class=HTMLResponse)
def invite_page(request: Request, token: str):
    user = accounts.peek_invite(token)
    if user is None:
        return _render(request, "invite.html", expired=True)
    return _render(request, "invite.html", invited=user, token=token)


@app.post("/invite/{token}", response_class=HTMLResponse)
def invite_submit(request: Request, token: str, password: str = Form(...),
                  confirm: str = Form(...)):
    user = accounts.peek_invite(token)
    if user is None:
        return _render(request, "invite.html", expired=True)
    if password != confirm:
        return _render(request, "invite.html", invited=user, token=token,
                       error="Those two passwords do not match.")
    try:
        claimed = accounts.claim_invite(token, password)
    except ValueError as exc:
        return _render(request, "invite.html", invited=user, token=token,
                       error=str(exc))
    if claimed is None:
        return _render(request, "invite.html", expired=True)
    response = RedirectResponse("/", status_code=303)
    _set_session(response, claimed)
    return response


@app.get("/consent", response_class=HTMLResponse)
def consent_page(request: Request, req: str = ""):
    """Where an OAuth authorization lands once the user is signed in.

    The middleware has already bounced an anonymous caller to /login with this
    URL as `next`, so by the time we get here there is a real account to bind
    the eventual token to.
    """
    pending = oauth_provider.load_pending(req)
    if pending is None:
        return _render(request, "consent.html", expired=True)
    return _render(request, "consent.html", pending=pending, req=req,
                   scopes=pending.scopes)


@app.post("/consent")
def consent_submit(request: Request, req: str = Form(...),
                   decision: str = Form(...)):
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Sign in first.")

    if decision == "approve":
        target = oauth_provider.complete_authorization(req, user)
    else:
        target = oauth_provider.deny_authorization(req)

    if target is None:
        return _render(request, "consent.html", expired=True)
    # 303 to the client's registered redirect_uri, which the SDK validated
    # against the registration before we ever saw this request.
    return RedirectResponse(target, status_code=303)


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, created: str = ""):
    user = _signed_in(request)
    return _render(request, "account.html", user=user,
                   tokens=accounts.list_pats(user.id),
                   grants=oauth_provider.list_grants(user.id),
                   created=created,
                   mcp_url=(public_base_url() or "") + "/mcp")


@app.post("/account/tokens", response_class=HTMLResponse)
def account_create_token(request: Request, name: str = Form("")):
    user = _signed_in(request)
    raw = accounts.create_pat(user.id, name)
    # Shown exactly once — only the hash is stored, so there is no second
    # chance to read it back.
    return _render(request, "account.html", user=user,
                   tokens=accounts.list_pats(user.id),
                   grants=oauth_provider.list_grants(user.id),
                   created=raw,
                   mcp_url=(public_base_url() or "") + "/mcp")


@app.post("/account/tokens/{pat_id}/delete")
def account_revoke_token(request: Request, pat_id: int):
    accounts.revoke_pat(pat_id, _signed_in(request).id)
    return RedirectResponse("/account", status_code=303)


@app.post("/account/connections/{client_id}/delete")
def account_revoke_connection(request: Request, client_id: str):
    oauth_provider.revoke_client_for_user(client_id,
                                          _signed_in(request).id)
    return RedirectResponse("/account", status_code=303)


# --------------------------------------------------------------------------
# OAuth endpoints
#
# Mounted at the site root, not under /mcp. RFC 8414 puts authorization
# server metadata at the issuer's root, and RFC 9728 puts protected resource
# metadata at /.well-known/oauth-protected-resource/<resource path>. Letting
# the SDK register them inside the mounted MCP app would bury both under
# /mcp/, where no client looks for them.
# --------------------------------------------------------------------------

def _mount_oauth() -> None:
    base = public_base_url()
    if not REQUIRE_AUTH or not base:
        return
    from pydantic import AnyHttpUrl
    from mcp.server.auth.routes import (create_auth_routes,
                                        create_protected_resource_routes)
    from mcp.server.auth.settings import (ClientRegistrationOptions,
                                          RevocationOptions)

    routes = create_auth_routes(
        provider=oauth_provider.provider,
        issuer_url=AnyHttpUrl(base),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,                   # a hosted connector self-registers
            valid_scopes=[MCP_SCOPE],
            default_scopes=[MCP_SCOPE],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    routes += create_protected_resource_routes(
        resource_url=AnyHttpUrl(f"{base}/mcp"),
        authorization_servers=[AnyHttpUrl(base)],
        scopes_supported=[MCP_SCOPE],
        resource_name="Content Forge",
    )
    app.router.routes.extend(routes)


_mount_oauth()


