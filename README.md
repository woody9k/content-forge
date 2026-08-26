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

- **Topics** — each is a folder with its own voice profile, articles and idea list.
- **Voice** — drop in samples of your own writing (`.md` `.txt` `.docx`, or paste);
  Claude extracts a style guide that becomes a binding system prompt for every
  article in that topic. The profile is a plain Markdown file you can hand-edit,
  which is the single biggest lever on output quality.
- **Write** — a title plus a brief becomes an article. Tick *Research and cite
  sources* and the agent runs real web searches and appends a numbered
  `## References` section with live URLs.
- **Ideas** — ask for N article suggestions for a topic; anything already covered
  by existing articles is excluded. One click turns an idea into an article.
- **Edit** — Markdown editor with live preview in the browser; **Download .docx**
  opens in Word with real headings, lists, blockquotes, code blocks and clickable
  hyperlinks.
- **Auth** — a page at `/auth` showing which credential this host is actually
  using, when it expires, and whether it still works. Paste a new token there and
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

---

## On-disk layout

The filesystem *is* the database. Nothing here needs the app to be readable.

```
$CF_CONTENT_ROOT/
└── topics/
    └── youth-sports-parenting/
        ├── topic.json                 name, description, audience
        ├── ideas.json                 cached suggestions
        ├── voice/
        │   ├── samples/               your uploaded writing
        │   └── profile.md             the generated style guide (edit me)
        └── articles/
            └── what-club-tryouts-cost/
                ├── article.md         the piece
                ├── meta.json          title, brief, status, timestamps
                └── export.docx        rebuilt on each download
```

Back it up with `rsync`, or `git init` in the content root and commit — you get
free version history on every article.

---

## MCP

The same service speaks MCP at **`/mcp`** (streamable HTTP, stateless JSON), so
an agent harness can drive it over the LAN with nothing installed locally:

```bash
claude mcp add --transport http content-forge http://<host>:8080/mcp
```

It runs inside the web process on purpose. Generation is asynchronous and the
job registry lives in memory, so a separate MCP process would keep its own and
the two would disagree about what is running. Sharing the process means an
article an agent started is the same job the browser is watching.

### Tools

| Tool | |
|---|---|
| `content_forge_list_topics` / `_get_topic` / `_create_topic` | topics, paginated |
| `content_forge_get_voice_profile` / `_set_voice_profile` | read and hand-edit the style guide |
| `content_forge_add_voice_sample` / `_build_voice_profile` | feed samples, then derive the profile |
| `content_forge_list_ideas` / `_suggest_ideas` | cached ideas, and a run to refresh them |
| `content_forge_list_articles` / `_get_article` | discovery, then retrieval for publishing |
| `content_forge_write_article` / `_rewrite_article` | start a generation |
| `content_forge_update_article` | save an edited body back |
| `content_forge_export_article_docx` | build the .docx, returns path and URL |
| `content_forge_get_job` | poll a generation, optionally blocking |
| `content_forge_health` | can it generate right now, and on which credential |
| `content_forge_delete_article` / `_delete_topic` | only with `CF_MCP_ALLOW_DELETE=1` |

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

> **No authentication.** `/mcp` is as open as every other route on this service.
> Anyone who can reach the port can start a generation and spend your Claude
> usage window. Same trusted-LAN assumption as the rest of the app — do not
> port-forward it.

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

Starts a real uvicorn server on an ephemeral port and drives it with a genuine
MCP client, so the mount, the lifespan wiring and the protocol are exercised —
not just the Python functions behind the tools.

---

## Notes and gotchas

- **Nothing is authenticated.** This binds to `0.0.0.0` and assumes a trusted
  LAN. Do not port-forward it. If you want it reachable from outside, put it
  behind your existing ingress with auth in front. The `/auth` page raises the
  stakes on that: anyone who can reach the port can replace the token or run a
  test that spends a request. The page never displays a stored token — only a
  masked prefix — so it cannot be used to *read* the secret back out, but it can
  be used to overwrite it.
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
