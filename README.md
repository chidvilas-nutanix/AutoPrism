# prism-mcp

A local Python MCP server that exposes the Nutanix internal React component
library — `@nutanix-ui/prism-reactjs` — to LLM clients (Cursor, Claude
Desktop, etc.) so they can generate correct, non-deprecated, type-safe
component code.

The server is a **knowledge + mapping** engine: it downloads and indexes the
published Prism package, ranks components/hooks/managers/utils/tokens for a
query, retrieves real usage examples, and maps Figma designs onto concrete
Prism components. It does **not** build or validate code — your agent
(Cursor) runs `tsc` / `eslint` / tests in its own loop.

> New here? Jump to **[Installation](#installation)** below — the flow is
> **clone → credentials & certificates → install (deps, models, library) →
> use.** [`SETUP.md`](SETUP.md) has the same flow with extra troubleshooting.

## What it does

- **Library acquisition** — fetches the `@nutanix-ui/prism-reactjs` tarball
  from Nutanix Artifactory (Basic auth, ETag short-circuit on the per-version
  manifest, SRI + sha1 integrity check), atomic-swap cache at
  `~/.cache/prism-mcp/<version>/`, and offline fallback to the last cached
  version. A background loop re-polls once per day.
- **Indexing** — parses `.d.ts` (components, hooks, managers, utils),
  `examples.md`, and `.less` design tokens into a typed entity index.
- **Search** — BM25 lexical ranking (`search_entities`) and a hybrid
  semantic pipeline for example code (`search_examples`: BM25 + Jina v2
  dense embeddings + Reciprocal Rank Fusion + a cross-encoder reranker).
- **Figma → Prism mapping** — `map_figma_node` ranks Prism components for a
  single node (with related components, matching tokens, imitation examples,
  and a11y guidance); `map_figma_tree` walks a whole Figma page URL into a
  pruned layout tree + an ordered agenda of mapped regions.

## Installation

The server installs in four stages — **clone → credentials & certificates →
install (deps, models, library) → use.** It is pure Python: no Node, npm, or
browser downloads. It targets **internal Nutanix engineers** (the defaults
assume Nutanix Artifactory / Canaveral and the Figma org).

### Requirements

- **Python 3.11+**.
- **[`uv`](https://docs.astral.sh/uv/)** — install with
  `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- **Nutanix VPN access** — the Artifactory and Figma hosts are internal.
- **Artifactory credentials** — `JFROG_EMAIL` + `JFROG_API_KEY`
  (Artifactory → *Edit Profile* → *Generate API Key*), or a pre-encoded
  `JFROG_AUTH` (base64 of `email:apikey`).
- **A Figma personal access token** (`file:read` scope) for the Figma tools,
  from <https://www.figma.com/settings>.

### 1. Clone to `~/autoprism`

Clone the repo to a stable, known location so the Cursor config path is
predictable. The canonical location is `~/autoprism`:

```bash
git clone <this-repo-url> ~/autoprism
cd ~/autoprism
```

Any absolute path works — just reuse the same one in the Cursor config
(stage 4). The rest of this guide assumes `~/autoprism`
(i.e. `/Users/<you>/autoprism`).

### 2. Feed credentials and trust the TLS chain

Copy the committed env contract and fill in what you need:

```bash
cp .env.example .env
```

Edit `.env`:

- `FIGMA_TOKEN=` — required for `map_figma_node` / `map_figma_tree`.
- Artifactory auth — **either** `JFROG_EMAIL` + `JFROG_API_KEY` **or**
  `JFROG_AUTH`.

`.env` is git-ignored. The server reads these on cold start; you can also
inline them into the Cursor config (stage 4) instead of using `.env`.

**Trust the internal Artifactory CA.** Canaveral Artifactory presents a TLS
chain rooted at the internal `Canaveral - Root CA`, which lives in the macOS
System keychain but **not** in Python's `certifi` bundle — so without
configuring trust Python rejects the handshake with
`SELF_SIGNED_CERT_IN_CHAIN`. Build a PEM bundle once and point the server at
it:

```bash
bash scripts/build_canaveral_ca_bundle.sh
# writes ~/.cache/prism-mcp/canaveral-ca-bundle.pem
```

Then add it to `.env` (or the Cursor config):

```bash
PRISM_MCP_CA_BUNDLE=~/.cache/prism-mcp/canaveral-ca-bundle.pem
```

| Env var | Effect |
|---|---|
| `PRISM_MCP_CA_BUNDLE=/path/to/bundle.pem` | Recommended. Passed to httpx as `verify=<path>`; the bundle must contain the Canaveral roots. On Linux, point it at `/etc/ssl/certs/ca-certificates.crt` (with the Canaveral roots installed via `update-ca-certificates`). |
| `PRISM_MCP_INSECURE_TLS=1` | Last-resort escape hatch. Disables TLS verification entirely; only use against the known-internal registry host. The server logs a `WARNING` whenever it's enabled. |

If both are set, the CA bundle wins (explicit trust beats opt-out).

### 3. Install the MCP (dependencies, models, library)

Install the Python runtime + dev dependencies into a local `.venv`:

```bash
uv sync
```

There is **no separate `install` command** for the models and library — they
are fetched lazily on first use and cached on disk:

- **Library** — the `@nutanix-ui/prism-reactjs` tarball is downloaded from
  Artifactory and indexed on the first tool call, then cached at
  `~/.cache/prism-mcp/<version>/` (atomic-swap; a daily background loop
  re-polls).
- **Models** — `search_examples` and the Figma mappers use two ONNX models
  (`jinaai/jina-embeddings-v2-base-code` +
  `Xenova/ms-marco-MiniLM-L-12-v2`), downloaded automatically by `fastembed`
  to `~/.cache/fastembed/` (a few hundred MB total) on first use.

Warm both now — and verify the install — with the bundled smoke check. It
spawns the server, does the MCP handshake, and exercises `get_library_meta`
(acquires + indexes the library) and `search_examples` (downloads + warms
both models and the per-version example corpus):

```bash
set -a ; source .env ; set +a
uv run python scripts/verify_server.py
```

Be online (on VPN) the first time. A healthy first run prints `7 tools`, a
resolved version, and non-empty search hits (it is slower while the tarball
indexes and the models cache):

```
id=2 tools/list      7 tools: ['echo', 'get_library_meta', ...]
id=3 get_library_meta
   package_name: @nutanix-ui/prism-reactjs
   version: 2.54.0
   from_cache: False
id=4 search_examples version=2.54.0  hits=5
   - Button               Primary button with onClick handler
id=5 search_entities query='modal dialog' hits=5
   - Modal           type=component  score=5.502 why=['dialog', 'modal']
   ...
```

The caches survive restarts. On cold start without VPN the server falls back
to the last cached version (`from_cache=true`); when the cache is also empty
it raises a `LibraryError` whose message tells you to connect to the Nutanix
VPN.

### 4. Use it — wire into Cursor

Add an entry to `~/.cursor/mcp.json`. Use the **absolute** path to `uv` (run
`which uv`) and the clone path from stage 1 — Cursor spawns MCP servers with
a minimal PATH that excludes `~/.local/bin` and `/opt/homebrew/bin`, so a
bare `"command": "uv"` fails with `spawn uv ENOENT`:

```json
{
  "mcpServers": {
    "prism-mcp": {
      "command": "/Users/<you>/.local/bin/uv",
      "args": ["run", "--project", "/Users/<you>/autoprism", "prism-mcp"],
      "env": {
        "JFROG_EMAIL": "you@nutanix.com",
        "JFROG_API_KEY": "<your-jfrog-api-key>",
        "FIGMA_TOKEN": "<your-figma-personal-access-token>",
        "PRISM_MCP_CA_BUNDLE": "/Users/<you>/.cache/prism-mcp/canaveral-ca-bundle.pem"
      }
    }
  }
}
```

Restart Cursor, open the MCP settings, and confirm `prism-mcp` shows
**7 tools**. Claude Desktop's `claude_desktop_config.json` uses the same
shape and the same absolute-path gotcha. To run the server by hand on stdio
(what Cursor does for you):

```bash
uv run prism-mcp
```

The server logs to **stderr** and reserves stdout for the MCP JSON-RPC
framing, so redirect stderr when debugging:
`uv run prism-mcp 2> /tmp/prism-mcp.log`.

Once wired up, drive page-level Figma → Prism conversion with the
[`figma-page-to-prism`](.cursor/skills/figma-page-to-prism/SKILL.md) Cursor
skill (it orchestrates input → gather → map → plan → compose → validate →
report on top of `map_figma_tree`).

## The seven MCP tools

| Tool | Purpose |
|---|---|
| `echo` | Liveness probe; returns a fixed string. |
| `get_library_meta` | Resolved version, cache path, source URL, indexed-at. |
| `search_entities` | BM25-ranked matches for a prose query (`why_matched` included). |
| `search_examples` | Hybrid (BM25 + dense + reranker) example-code retrieval. |
| `get_entity` | Full record (props, signature, examples, import path). |
| `map_figma_node` | Rank Prism components for one Figma node (+ tokens, examples, a11y, related). |
| `map_figma_tree` | Page-level walker: Figma node URL → pruned layout tree + agenda of mapped regions. |

## Figma page → Prism (page-level mapping)

In addition to per-node mapping (`map_figma_node`), the server exposes
`map_figma_tree` for **page-level** Figma → Prism conversion. Given a Figma
node URL it parses the URL, fetches the subtree via the Figma REST API,
applies a noise filter + routing layer + pattern detector, and returns a
structured mapping containing a pruned `layout_tree`, an `agenda` of
`MappedRegion`s with ranked Prism candidates, and a per-reason
`dropped_summary`.

The response is **lean by default** — slim agenda rows (chosen component +
one-line description + top-3 `{name, score}`) so it doesn't flood the LLM's
context. Drill into a single region with `map_figma_node`, or pass
`response_detail="full"` for the complete payload (full candidates,
examples, a11y, the full `dropped` audit). The
[`figma-page-to-prism`](.cursor/skills/figma-page-to-prism/SKILL.md) Cursor
skill orchestrates the full flow (input → gather → map → plan → compose →
validate → report) on top of this tool.

`map_figma_tree` requires `FIGMA_TOKEN` (a Figma personal access token with
`file:read` scope, generated at <https://www.figma.com/settings>). Put it in
`.env` alongside the JFROG credentials; the server reads it at call time. The
fetcher caches Figma responses to `~/.cache/prism-mcp/figma/` with a 1-hour
TTL and retries transient 429/5xx/timeout errors three times with
exponential backoff.

## Repository layout

| Path | Purpose |
|---|---|
| `src/prism_mcp/` | Server source. The `prism-mcp` script lives here. |
| `tests/` | Pytest suite. `pytest -q` is the fast inner loop. |
| `scripts/` | Operator helpers: `verify_server.py`, `build_canaveral_ca_bundle.sh`. |
| `.cursor/skills/figma-page-to-prism/` | The page-level mapping skill (ships with the repo). |
| `SETUP.md` / `.env.example` | First-time setup + the env-var contract. |
| `.github/workflows/ci.yml` | CI: lint + tests on every push. |

## Development

The must-pass loop (also run in CI):

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q
```

## Project conventions

- Python style: 80-char lines, 4-space indent, parameterized logging only
  (no f-strings in logger calls). Enforced by `ruff` (config in
  `pyproject.toml`).
- Dependency management uses `uv`. `requirements.txt` is kept in sync as a
  read-only export for tooling that doesn't speak `uv`/`pyproject.toml`.
- Never commit credentials. Artifactory auth is supplied via env vars
  (`JFROG_EMAIL`/`JFROG_API_KEY` or `JFROG_AUTH`) at runtime only.

## CI

`.github/workflows/ci.yml` runs the must-pass loop above on every push and
pull request. It is defined entirely in `pyproject.toml`, so any runner that
can execute `uv run ruff check`, `uv run ruff format --check`, and
`uv run pytest -q` is sufficient.
