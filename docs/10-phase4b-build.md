# Phase 4B — The builder: web apps + docs, sandboxed (Build Steps)

**Goal:** Mochi can now **build things** — scaffold/write web apps and generate PDFs/Word docs, run
them in a **sandbox with no access to secrets**, and **serve them so Stephanie views them on her
phone**. Heavy code-gen routes to the hosted model (Groq `gpt-oss-120b`) via the 4A router; the local
agent orchestrates through **high-level, single-call tools** (the 7B chains tool calls poorly).

**Status: step 1 (foundation) shipped** — sandbox + workspace + static LAN serving + PDF/docx +
`build_web_app` (static) + `make_document`. Steps 2 (React/Vite) and 3 (cloudflared tunnel + hosted
code-gen quality/retry) build on it.

**Milestone (step 1):** "build me a landing page for X" → a LAN link that loads on her phone;
"make me a one-page PDF plan for Y" → the PDF arrives in Telegram. **Verified** offline
(`tests/test_builder.py`, 10) and against real components (`scripts/verify_phase4b.py`, 7/7: sandbox
runs node/python, scrubs secrets, `sandbox-exec` denies `.env`, real HTTP 200, real PDF, real Groq
code-gen).

---

## Architecture — `app/builder/`
- **`sandbox.py`** — `Sandbox` Protocol + `SubprocessSandbox`. Runs commands with a **scrubbed env**
  (allow-list only — no `.env`/DB/token/keys reach the child), **cwd jailed** to `workspace/<project>`,
  a **timeout**, and a best-effort **`sandbox-exec`** profile that **denies reads of `data/` and
  `.env`**. `DockerSandbox` is a later drop-in (the Protocol is the seam). Honest residual: network is
  open (npm needs it) and non-denied files are otherwise readable until Docker.
- **`workspace.py`** — projects under `workspace/` (git-ignored) with `_safe_join` path-safety
  (`resolve()` + `is_relative_to`) rejecting `../`/absolute escapes on every path.
- **`serve.py`** — `lan_ip()`, `serve_static()` (a threaded `http.server` on the LAN, returns the URL),
  a running-server registry (`stop_server`/`running`), free-port picking. (Vite dev serve = step 2;
  `tunnel()` = step 3.)
- **`docs.py`** — `generate_pdf` (reportlab) + `generate_docx` (python-docx) from light Markdown
  (headings/bullets/paragraphs). Pure-python, no native deps.
- **`codegen.py`** — `GeneratedProject`/`FileSpec`; `generate_project(description)` routes to the hosted
  model with `json_schema` structured output (scrubbed + audited like `consult_expert`; falls back to
  local when hosting is off). `generator` injectable for offline tests.

## Exposure: commands, not agent tools (a measured finding)
The builder was originally built as agent tools (`build_web_app`, `make_document`, …). But binding them
into the agent's set **broke tool-calling on the local 7B**: measured, **11 tools fire reliably, but
13–15 collapse entirely** — `add_reminder` *and* `build_web_app` both dropped to 0/3. This is a hard
tool-count wall on the 7B (tool-schema bloat degrades selection). So the builder is exposed via explicit
**commands** instead (like `/ask`), which need no tool-selection:
- **`/build <description>`** — `build_web_app.invoke(...)`: generate → write → serve (static) → reply
  with the LAN URL.
- **`/doc <description>`** — writes the document on the **local** model (personal content stays local),
  renders a PDF (or `.docx` if "word"/"docx" is mentioned), and sends it via `bot.send_document`.

The `build_web_app`/`make_document`/`serve_project`/`list_projects` `@tool`s still exist (invoked by the
commands + covered by tests) — they're just **not in `ALL_TOOLS`**. Conversational access ("Mochi, build
me a page") awaits **dynamic per-turn tool binding** (bind a small relevant subset per message) — the
proper fix for the growing tool count, deferred to its own effort. For now the persona points the user
to `/build` and `/doc`.

## Config (`app/config.py`, `.env.example`)
`builder_port_base=8100`, `builder_sandbox_timeout=120`, `builder_npm_timeout=300`, `builder_fs_deny=
True`, `cloudflared_path`. Deps: `reportlab`, `python-docx`.

## Safety (constitution)
Scrubbed env is the hard guarantee (no secrets in the child); `sandbox-exec` denies the secret files
(best-effort, verified working); no builder tool reads OAuth tokens/the DB. Public exposure only on
explicit request (tunnel, step 3). Docker = the full-isolation end-state (Mac mini).

## Verify (no phone)
- `PYTHONPATH=. uv run pytest tests/test_builder.py -v` (10): path-safety (`../` blocked), env-scrub,
  timeout, cwd-jail, PDF/docx render, `build_web_app` (fake codegen+serve) writes files + returns URL,
  framework scaffolds-without-serving, `make_document` queues a real PDF.
- `PYTHONPATH=. uv run python scripts/verify_phase4b.py` (7/7): the real integration above.
- **Live (phone):** landing page → LAN link loads; PDF plan → file arrives.

## Deferred → steps 2/3 + later
- Framework serving (Vite dev), cloudflared tunnel, iterate-on-build-error, Docker isolation (mini),
  mobile apps (Phase 8), persistent deploys.
