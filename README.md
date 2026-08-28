# Content Forge

A web UI over the Claude Code CLI for generating articles in your own voice,
organised by topic. Runs on your own box, uses your Claude **subscription**
(no API key, no per-token billing).

```
Browser ──HTTP──> FastAPI + HTMX ──subprocess──> claude -p ──> Anthropic
Agent ────MCP───>      │  /mcp           │
                       └──────────────────┴──> plain folders on disk (the "database")
```

---

## What it does

Every article is the combination of three **independent** things:

| | What it decides | Scope |
|---|---|---|
| **Topic** | the subject area | owns articles and ideas |
| **Voice** | who it sounds like | reusable across every topic |
| **Style** | how it is built and argued | chosen per article, combinable |

So you can pre-build a *Bob* voice and a *Dana* voice, then write one boating
article persuasively as Bob and the next one lyrically as Dana.

- **Topics** — each is a folder with its own articles and idea list.
- **Voices** — a separate folder per voice. Drop in samples of someone's writing
  (`.md` `.txt` `.docx`, or paste); Claude extracts a style guide that becomes a
  binding system prompt for any article you apply it to. The profile is a plain
  Markdown file you can hand-edit, which is the single biggest lever on output
  quality. Each voice can carry a name, a description and a picture — rename it
  whenever you like, the id underneath never moves, so articles stay linked.
- **Styles** — expository, persuasive, descriptive, narrative, minimalist,
  lyrical, conversational, analytical. Layered on top of the voice: the voice
  governs the diction and rhythm, the style governs the argument and structure.
  Combine as many as you like.
- **Write** — a title plus a brief becomes an article. Pick a voice and any
  styles. Tick *Research and cite sources* and the agent runs real web searches
  and appends a numbered `## References` section with live URLs.
- **Ideas** — ask for N article suggestions for a topic; anything already covered
  by existing articles is excluded. One click turns an idea into an article.
- **Edit** — Markdown editor with live preview in the browser; **Download .docx**
  opens in Word with real headings, lists, blockquotes, code blocks and clickable
  hyperlinks.
- **Accounts** — everyone signs in with their own username and password. Your
  **Account** page mints personal access tokens for scripts and lists the
  connectors you have approved. See [Accounts](#accounts).
- **Auth** — an **administrators only** page at `/auth` showing which Claude
  credential this host is actually using, when it expires, and whether it still
  works. Paste a new token there and
  it takes effect on the next generation — no restart, no shell.

  There are **two independent credentials** on a typical box, and the page keeps
  them visually apart under *In use* and *Not in use*:

  | | Written by | Renews? |
  |---|---|---|
  | `~/.config/content-forge/env` | `/auth`, `set-token.sh`, `install-service.sh` | no, ~1 year |
  | `~/.claude/.credentials.json` | interactive `claude login` | not on a headless box |

  Saving a token never touches `.credentials.json`, so a long-expired login can
  sit there indefinitely while everything works. That is why it is greyed out and
  labelled *Not in use* rather than flagged red — the CLI only falls back to it
  if the token is removed.

---

## Accounts

Three credentials, one identity — a session cookie for the browser, a personal
access token for scripts, an OAuth token for connectors. All three resolve to
the same user, which the single shared secret they replace could never do: it
could not say who was acting, and could not be revoked for one person without
locking out everybody.

**There is no self-signup.** This box answers on the open internet and every
generation spends the owner's Claude subscription, so accounts are created from
a shell by someone who already has one:

```bash
./deploy/add-user.sh alice --admin
```

That prints a single-use invite link, valid 48 hours. Send it to the person;
they choose their own password, which nobody else ever sees. Reissuing an
invite voids the previous link.

```bash
./.venv/bin/python -m app.accounts list
./.venv/bin/python -m app.accounts invite alice      # new link
./.venv/bin/python -m app.accounts disable bob       # also kills live sessions
./.venv/bin/python -m app.accounts promote alice     # grant admin
```

Admins are the only ones who may open `/auth` and rotate or delete the Claude
subscription token the whole service runs on. Ordinary users can do everything
else — the workspace is shared, so there is no per-user content.

Accounts, sessions, tokens and OAuth registrations live in a SQLite file at
`$CF_CONTENT_ROOT/auth.db`, mode 0600, override with `CF_AUTH_DB`. Passwords
are scrypt-hashed; every token is stored only as a SHA-256 hash, so a copy of
the database hands over no live credential — and is why a personal access
token is shown exactly once, when it is created.

| variable | default | meaning |
|---|---|---|
| `CF_REQUIRE_AUTH` | on when `CF_PUBLIC_HOST` is set | force authentication on or off |
| `CF_AUTH_DB` | `$CF_CONTENT_ROOT/auth.db` | account database |
| `CF_SESSION_TTL` | `1209600` (14 days) | browser session lifetime |
| `CF_INVITE_TTL` | `172800` (48 hours) | invite link lifetime |
| `CF_OAUTH_ACCESS_TTL` | `3600` | OAuth access token lifetime |
| `CF_OAUTH_REFRESH_TTL` | `2592000` (30 days) | OAuth refresh token lifetime |

---

## HTTPS

`deploy/nginx-contentforge.conf` terminates TLS for `contentforge.boblab.net`
and proxies to uvicorn on `127.0.0.1:8080`. It listens on **8443**, not 443,
because on the orca box port 443 is already published by an unrelated container.

Two settings in that file are load-bearing rather than cosmetic:

- `proxy_buffering off` — MCP streams over SSE, and with buffering on nginx
  holds the frames until the response completes, which for a generation is
  minutes of apparent hang.
- `proxy_read_timeout 1200s` — comfortably above `CF_CLAUDE_TIMEOUT` (900s), so
  a slow researched article is not cut off mid-flight.

### Host validation

The MCP SDK enables DNS-rebinding protection on its own, but the host list it
builds is loopback-only. Behind a proxy the forwarded `Host` is the public name,
which is not on that list, and every call fails with a bare
`421 Invalid Host header`. `CF_PUBLIC_HOST` / `CF_PUBLIC_PORT` add it:

| variable | default | meaning |
|---|---|---|
| `CF_PUBLIC_HOST` | `contentforge.boblab.net` | proxy `server_name`; empty = local only |
| `CF_PUBLIC_PORT` | `8443` | port callers use |
| `CF_MCP_ALLOWED_HOSTS` | derived | comma-separated override |
| `CF_MCP_ALLOWED_ORIGINS` | derived | comma-separated override |

Loopback keeps the SDK's `:*` wildcard, so the smoke tests still work on their
ephemeral ports.

### Certificate

Issued by the BobLab internal CA (step-ca at `ca.boblab.net:9000`). The key and
CSR live in `/etc/ssl/contentforge/` and are reused on renewal, so the key never
leaves the box:

```bash
sudo /etc/ssl/contentforge/issue-cert.sh --provisioner <name>
```

That writes `fullchain.pem`, reloads nginx, and prints the new dates.
Clients already trust it if `/usr/local/share/ca-certificates/boblab-ca.crt`
is installed; otherwise pass the CA bundle explicitly.

---

## Install

Unpack the project wherever you like — it does not need to live in `/opt`.

### Claude CLI + subscription auth (do this first)

Install the CLI as the user the service will run as, **not root**.

```bash
# as your normal login user, NOT root
curl -fsSL https://claude.ai/install.sh | bash     # or: npm i -g @anthropic-ai/claude-code
claude setup-token                                 # prints a ~1-year token
```

**`claude setup-token` does not save the token anywhere.** It prints it once and
expects it supplied as the `CLAUDE_CODE_OAUTH_TOKEN` environment variable.
(Interactive `claude login` does write `~/.claude/.credentials.json`, but that
grant expires and nothing on a headless box renews it — which is exactly how you
end up with a green status banner and a 401 on every generation.)

The token lives in a 0600 file:

```
~/.config/content-forge/env      override the path with CF_TOKEN_FILE
```

It is kept there rather than in the systemd unit because units in
`/etc/systemd/system` are world-readable. The app reads it **at the moment it
spawns the CLI**, not at startup, so a token saved on `/auth` is live
immediately — no `EnvironmentFile=`, no restart.

Three ways to set or rotate it, all writing the same file:

```bash
./deploy/install-service.sh           # prompts during a first install
./deploy/set-token.sh                 # prompts, hidden input
./deploy/set-token.sh <token>         # non-interactive
```

…or just paste it into `/auth` in the browser, which is the only one of the four
that needs neither a shell on the box nor a service restart. All of them verify
the token against the API before saving, so a mistyped paste cannot displace a
working one.

> **API key precedence.** The CLI's auth order puts `ANTHROPIC_API_KEY` *above*
> `CLAUDE_CODE_OAUTH_TOKEN`. If an API key is present in the environment it
> silently wins, and you get billed per request instead of using your
> subscription. `app/auth.py` strips both `ANTHROPIC_API_KEY` and
> `ANTHROPIC_AUTH_TOKEN` from the environment of every CLI call, so this cannot
> happen regardless of how the service was started. `/auth` and `GET /health`
> both report when a key was found and ignored.

> **Expiry.** These tokens last about a year and do not auto-renew. When one
> lapses, generation fails with an auth error and the banner on the Topics page
> goes red — rerun `claude setup-token` and paste the result into `/auth`.
> Note that **nothing short of a live request proves a credential is good**:
> `claude auth status` reports an expired grant as logged-in. That is what the
> *Test now* button on `/auth` is for, and why the status line says `untested`
> until you press it.

> **Do not add `--bare`** to the CLI invocation in `claude_runner.py`. Bare mode
> ignores `CLAUDE_CODE_OAUTH_TOKEN` entirely and demands an API key.

### systemd — use the installer

Do **not** hand-edit `deploy/content-forge.service`; every path in it is a
placeholder. Run the installer instead — it reads your real username, home
directory, install path and `claude` location, builds the unit from those, and
refuses to proceed on the mistakes that produce cryptic systemd errors:

```bash
cd /path/to/content-forge
./deploy/install-service.sh
```

It creates the venv if missing, warns if the CLI is unauthenticated, starts the
service, and prints `/health` so you know it actually came up.

To override defaults, set them in the environment for that one command:

```bash
CF_CONTENT_ROOT=/srv/content-forge-data CF_PORT=9000 ./deploy/install-service.sh
```

Then open **http://<host>:8080** from anywhere on the LAN.
`GET /health` reports CLI status, model, storage path and active job count.

For a quick manual run without systemd: `./run.sh`.

### When systemd complains

| Status | Means | Fix |
|---|---|---|
| `217/USER` | `User=` doesn't exist on this box | run the installer; it uses `id -un` |
| `200/CHDIR` | `WorkingDirectory=` is wrong | run the installer |
| `203/EXEC` | venv or uvicorn missing at that path | `pip install -r requirements.txt` in `.venv` |
| `226/NAMESPACE` | a `ReadWritePaths=` entry doesn't exist | prefix that path with `-` |
| starts, but auth fails | no token saved, or the stored `claude login` grant has expired | open `/auth`, press **Test now**, paste a fresh token |

---

## Configuration

All environment variables, all optional:

| Variable | Default | Notes |
|---|---|---|
| `CF_CONTENT_ROOT` | `~/content-forge-data` | where topics and articles live |
| `CF_CLAUDE_BIN` | `claude` | path to the CLI |
| `CF_TOKEN_FILE` | `~/.config/content-forge/env` | 0600 file holding the token |
| `CF_CLAUDE_MODEL` | `sonnet` | `opus` for heavier pieces |
| `CF_CLAUDE_TIMEOUT` | `900` | seconds per generation |
| `CF_CLAUDE_MAX_TURNS` | `40` | agent turns for research runs |
| `CF_MAX_CONCURRENT_JOBS` | `2` | parallel generations |
| `CF_ALLOWED_TOOLS` | `WebSearch WebFetch` | tools for research mode |
| `CF_MCP_ALLOW_DELETE` | unset | `1` also exposes the MCP delete tools |
| `CF_HOST` / `CF_PORT` | `0.0.0.0` / `8080` | bind address |
| `CF_FAKE_CLAUDE` | unset | `1` stubs the CLI — used by the smoke test |
| `CF_REQUIRE_AUTH` | on with `CF_PUBLIC_HOST` | authentication — see [Accounts](#accounts) for the rest |

---

## On-disk layout

The filesystem *is* the database. Nothing here needs the app to be readable.

```
$CF_CONTENT_ROOT/
├── voices/
│   └── bob/
│       ├── voice.json                name, description
│       ├── portrait.png              optional picture (png/jpg/gif/webp)
│       ├── samples/                  the uploaded writing
│       └── profile.md                the generated style guide (edit me)
└── topics/
    └── youth-sports-parenting/
        ├── topic.json                name, description, audience
        ├── ideas.json                cached suggestions
        └── articles/
            └── what-club-tryouts-cost/
                ├── article.md        the piece
                ├── meta.json         title, brief, status, voice, styles
                └── export.docx       rebuilt on each download
```

Voices sit beside topics, not inside them — that is what lets one voice write
across every topic, and one topic draw on several voices.

### Migrating from topic-owned voices

Older content roots kept the voice inside the topic, at `topics/<t>/voice/`.
`migrate_voices.py` promotes each of those into a standalone voice, moves its
samples and profile, and back-fills the `voice` field on that topic's existing
articles so they keep the voice they were written in:

```bash
python migrate_voices.py
```

That is a dry run — it prints the plan and changes nothing. Re-run with
`--apply` to do it. Stop the service first; it moves folders. Re-running after
a successful pass is a no-op.

Back it up with `rsync`, or `git init` in the content root and commit — you get
free version history on every article.

---

## MCP

The same service speaks MCP at **`/mcp`** (streamable HTTP, stateless JSON), so
an agent harness can drive it over the LAN with nothing installed locally:

```bash
claude mcp add --transport http content-forge https://<host>/mcp \
    --header "Authorization: Bearer <personal-access-token>" --scope user
```

Mint the token on your **Account** page. On a LAN box with no public hostname
authentication is off and the header can be dropped.

It runs inside the web process on purpose. Generation is asynchronous and the
job registry lives in memory, so a separate MCP process would keep its own and
the two would disagree about what is running. Sharing the process means an
article an agent started is the same job the browser is watching.

### Tools

| Tool | |
|---|---|
| `content_forge_list_topics` / `_get_topic` / `_create_topic` | topics, paginated |
| `content_forge_list_voices` / `_create_voice` | voices, independent of topic |
| `content_forge_update_voice` | rename a voice, or change its description |
| `content_forge_get_voice_profile` / `_set_voice_profile` | read and hand-edit the style guide |
| `content_forge_add_voice_sample` / `_build_voice_profile` | feed samples, then derive the profile |
| `content_forge_list_writing_styles` | the style catalogue, with descriptions |
| `content_forge_list_ideas` / `_suggest_ideas` | cached ideas, and a run to refresh them |
| `content_forge_list_articles` / `_get_article` | discovery, then retrieval for publishing |
| `content_forge_write_article` / `_rewrite_article` | start a generation: topic + voice + styles |
| `content_forge_update_article` | save an edited body back |
| `content_forge_export_article_docx` | build the .docx, returns path and URL |
| `content_forge_get_job` | poll a generation, optionally blocking |
| `content_forge_health` | can it generate right now, and on which credential |
| `content_forge_delete_article` / `_delete_topic` / `_delete_voice` | only with `CF_MCP_ALLOW_DELETE=1` |

### Driving it in plain language

The point of splitting topic, voice and style is that an assistant can map a
sentence straight onto one call. Told *"write an article using the Bob voice in
a persuasive style in the boating topic about why living on a lake in Florida
beats the ocean"*, it calls:

```json
{
  "tool": "content_forge_write_article",
  "topic": "boating",
  "voice": "bob",
  "styles": ["persuasive"],
  "title": "Why a Florida lake beats the ocean",
  "brief": "Make the case that lake living beats coastal living in Florida."
}
```

Topic and voice accept the display name as well as the slug, so `"Bob"` and
`"bob"` both resolve — no lookup call needed first. A voice with no profile
built yet is refused with an error naming the tool that fixes it.

### Retrieving an article to publish

`content_forge_get_article` is the one a publishing agent wants. It takes a
`format`:

- `markdown` — the raw source, for a Markdown-native CMS or a static site
- `html` — rendered HTML with headings, lists, code blocks and links intact,
  ready to drop into a post body
- `json` — the Markdown plus metadata: title, word count, status, and whether a
  `## References` section is present

### Generation is a job, not a call

Articles take 30–90 seconds, or several minutes with `references=true` — far
longer than an MCP call should block. So `content_forge_write_article` returns a
`job_id` and the article's slug immediately, and `content_forge_get_job` polls
it, blocking up to `wait_seconds` if you want it to. The article folder exists
from the moment the job is submitted, marked `generating`.

Jobs are in memory. If the service restarts mid-generation the job id is gone
even though the article folder survives — an agent that loses a job should fall
back to `content_forge_list_articles` and check the article's `status`.

### Connecting a hosted connector

`/mcp` speaks OAuth 2.1, so a connector platform that will not accept a pasted
token — ChatGPT, claude.ai — can onboard itself. Point it at the base URL and
it discovers the rest:

| Endpoint | |
|---|---|
| `/.well-known/oauth-authorization-server` | RFC 8414 metadata |
| `/.well-known/oauth-protected-resource/mcp` | RFC 9728, named in the 401 |
| `/register` | RFC 7591 dynamic client registration |
| `/authorize` → `/consent` | you sign in and approve, in a browser |
| `/token`, `/revoke` | code exchange, refresh rotation, RFC 7009 |

The flow ends at a consent screen, so a token is always bound to a real
account — a client that registers itself gets nothing until a person approves
it. Approved connectors are listed on your Account page and can be
disconnected there.

> **This spends your Claude subscription.** Anyone you give an account to can
> start generations against your usage window, and the workspace is shared, so
> they see and can edit everyone's topics, voices and articles. Invite
> accordingly.

---

## Hardening

The public box runs `deploy/harden.sh`. Everything below was applied by hand
first and then checked in, so the next host does not have to be hand-built:

```bash
sudo ./deploy/harden.sh <your-ip>
```

| | |
|---|---|
| **App binds loopback** | uvicorn listens on `127.0.0.1:8080`, never `0.0.0.0` |
| **Firewall** | ufw: deny inbound except 22 (rate limited), 80, 443 |
| **fail2ban** | jails for sshd, nginx-botsearch, nginx-bad-request |
| **sshd** | key only, no root login, no X11/TCP/agent forwarding |
| **Rate limits** | nginx: 20/min on the auth endpoints, 300/min on `/mcp` |
| **CSP** | same-origin, `frame-ancestors 'none'` |
| **Backups** | `deploy/backup.sh` nightly at 03:15, 14 days retained |

Three of those are load-bearing rather than box-ticking:

- **Binding to loopback is the important one.** With uvicorn on `0.0.0.0` and
  no host firewall, the only thing between the internet and a plaintext HTTP
  copy of the whole app — bypassing TLS, HSTS and every security header — was
  the cloud provider's security group. nginx already proxies to
  `127.0.0.1:8080`, so this costs nothing and closes the hole for good.

- **No jail bans on 401.** An MCP connector's *first* request is
  unauthenticated and MUST return 401 — that is how RFC 9728 advertises where
  to get a token. A `nginx-http-auth`-style jail would blocklist the connector
  platform mid-onboarding and break the exact flow this server exists to serve.

- **Rate limits are per-endpoint, not site-wide.** The browser polls
  `/jobs/<id>/status` about once a second while an article generates, so a
  limit tight enough for the credential endpoints would break the UI. The auth
  zone is tight; `/mcp` is generous, because an agent legitimately bursts.

Backups cover what is not in git: `auth.db` (accounts, tokens, grants) and
`topics/` + `voices/`. `auth.db` is copied through SQLite's backup API rather
than `cp` — it runs in WAL mode, where a plain copy taken mid-write restores
successfully and is silently short. They are written to local disk, which
covers a bad deploy or a mistaken delete but **not** loss of the instance;
getting them off the box needs credentials this host does not have.

---

## Testing

```bash
CF_FAKE_CLAUDE=1 CF_CONTENT_ROOT=/tmp/cf-smoke ./.venv/bin/python smoke_test.py
```

Runs every route against a stubbed CLI, then opens the produced `.docx` and
asserts the headings, lists, bold/italic runs and hyperlinks actually landed.
No Claude quota consumed.

```bash
CF_FAKE_CLAUDE=1 ./.venv/bin/python mcp_smoke_test.py
```

```bash
CF_FAKE_CLAUDE=1 ./.venv/bin/python auth_smoke_test.py
```

Covers sign-in, the admin gate, invites, personal access tokens and the whole
OAuth flow — registration, consent, code exchange, refresh rotation and
revocation — finishing by opening a real MCP session on an OAuth-issued token.
Written because three earlier rounds of connector fixes were validated with
hand-written `curl` calls and shipped without a real client ever exercising
them.

Starts a real uvicorn server on an ephemeral port and drives it with a genuine
MCP client, so the mount, the lifespan wiring and the protocol are exercised —
not just the Python functions behind the tools.

---

## Notes and gotchas

- **Authentication is on whenever `CF_PUBLIC_HOST` is set**, and off otherwise,
  so a LAN box behaves exactly as it always did. Set `CF_REQUIRE_AUTH` to force
  either way. With it off there are no identities, and `/auth` is open to
  anyone who can reach the port.
- **The workspace is shared.** Every account sees and can edit every topic,
  voice and article. There is no per-user content and no read-only role.
- **Jobs are in-memory.** A restart mid-generation loses the job (the article
  folder survives, marked `generating`); just regenerate. Articles themselves are
  always written to disk before the job reports done.
- **Usage limits** are your subscription's. Two concurrent jobs is a deliberate
  default; raising `CF_MAX_CONCURRENT_JOBS` burns your window faster.
- **Voice quality tracks sample quality.** A few thousand words of your real
  writing beats a lot of mediocre filler. Then read the generated `profile.md`
  and sharpen the *Do not* section by hand — that's where drift back to generic
  assistant prose gets caught.
- **References mode is slower** (multiple search turns) and costs more of your
  usage window. Leave it off for opinion pieces.
