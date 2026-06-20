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

> New here? Start with **[`SETUP.md`](SETUP.md)** for the clone → credentials
> → models → run walkthrough.

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

## Requirements

- Python 3.11+.
- [`uv`](https://docs.astral.sh/uv/) for dependency and venv management.
- Nutanix Artifactory credentials (for the library fetch) and, for the Figma
  tools, a `FIGMA_TOKEN`. See [`SETUP.md`](SETUP.md) and
  [`.env.example`](.env.example).

## Quickstart

```bash
# Install runtime + dev dependencies into a local .venv
uv sync

# Run the must-pass loop (linter, formatter check, tests)
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q

# Start the MCP server on stdio (this is what Cursor does for you)
uv run prism-mcp
```

The server logs to stderr and reserves stdout for the MCP JSON-RPC framing,
so you can pipe stderr to a file while a client speaks to stdout.

### Hooking up to Cursor

Add an entry to your Cursor MCP config (`~/.cursor/mcp.json`) pointing at the
script entry point. Inline the env vars the server needs:

```json
{
  "mcpServers": {
    "prism-mcp": {
      "command": "/Users/<you>/.local/bin/uv",
      "args": ["run", "--project", "/absolute/path/to/this/repo", "prism-mcp"],
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

Use the **absolute** path to `uv` (run `which uv` to find it). Cursor
spawns MCP servers with a minimal PATH that does not include
`~/.local/bin` or `/opt/homebrew/bin`, so a bare `"command": "uv"` will
fail with `spawn uv ENOENT`. Claude Desktop's
`claude_desktop_config.json` uses the same shape and has the same
gotcha.

The server needs Artifactory credentials to fetch the published tarball.
Set one of:

```bash
export JFROG_AUTH="<base64 of email:apikey>"          # preferred
# or
export JFROG_EMAIL="you@nutanix.com"
export JFROG_API_KEY="…"
```

### TLS to internal Artifactory

Canaveral Artifactory presents a TLS chain rooted at the internal
`Canaveral - Root CA` certificate. That root is in the macOS System
keychain on Nutanix laptops but **not** in certifi's Mozilla NSS bundle,
so without configuring trust Python will reject the handshake with
`SELF_SIGNED_CERT_IN_CHAIN`.

Two knobs cover both cases:

| Env var | Effect |
|---|---|
| `PRISM_MCP_CA_BUNDLE=/path/to/bundle.pem` | Recommended. Passed to httpx as `verify=<path>`; the bundle must contain Canaveral roots. Run `scripts/build_canaveral_ca_bundle.sh` to extract them from the macOS keychain into `~/.cache/prism-mcp/canaveral-ca-bundle.pem`. |
| `PRISM_MCP_INSECURE_TLS=1` | Escape hatch. Disables TLS verification entirely. Only use against a known-internal host (the registry URL you configured); the server logs a `WARNING` whenever it's enabled. |

If both are set, the CA bundle wins (explicit trust beats opt-out).

### One-shot sanity check

`scripts/verify_server.py` spawns the server, performs the MCP
initialize handshake, calls `get_library_meta`, `search_examples`,
and `search_entities` for "modal dialog", then prints a short
summary. Useful when wiring a new laptop:

```bash
set -a ; source .env ; set +a
uv run python scripts/verify_server.py
```

A healthy first run prints something like:

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
   - ConfirmModal    type=component  score=5.455 why=['dialog', 'modal']
   ...
```

The server exposes **seven** MCP tools:

| Tool | Purpose |
|---|---|
| `echo` | Liveness probe; returns a fixed string. |
| `get_library_meta` | Resolved version, cache path, source URL, indexed-at. |
| `search_entities` | BM25-ranked matches for a prose query (`why_matched` included). |
| `search_examples` | Hybrid (BM25 + dense + reranker) example-code retrieval. |
| `get_entity` | Full record (props, signature, examples, import path). |
| `map_figma_node` | Rank Prism components for one Figma node (+ tokens, examples, a11y, related). |
| `map_figma_tree` | Page-level walker: Figma node URL → pruned layout tree + agenda of mapped regions. |

The cache survives restarts. On cold start without VPN, the server
falls back to the last cached version with `from_cache=true`; when
the cache is also empty, the server raises a `LibraryError` whose
message tells the operator to connect to the Nutanix VPN.

### Figma page → Prism (page-level mapping)

In addition to per-node mapping (`map_figma_node`), the server
exposes `map_figma_tree` for **page-level** Figma → Prism
conversion. Given a Figma node URL it parses the URL, fetches
the subtree via the Figma REST API, applies a noise filter +
routing layer + pattern detector, and returns a structured
`FigmaTreeMapping` containing a pruned `layout_tree`, an `agenda`
of `MappedRegion`s with ranked Prism candidates, and a `dropped`
audit trail. The
[`figma-page-to-prism`](.cursor/skills/figma-page-to-prism/SKILL.md)
Cursor skill orchestrates the full flow (input → gather → map →
plan → compose → validate → report) on top of this tool.

`map_figma_tree` requires `FIGMA_TOKEN` (a Figma personal
access token with `file:read` scope, generated at
<https://www.figma.com/settings>). Put it in `.env` alongside
the JFROG credentials; the server reads it at call time. The
fetcher caches Figma responses to `~/.cache/prism-mcp/figma/`
with a 1-hour TTL and retries transient 429/5xx/timeout errors
three times with exponential backoff.

## Repository layout

| Path | Purpose |
|---|---|
| `src/prism_mcp/` | Server source. The `prism-mcp` script lives here. |
| `tests/` | Pytest suite. `pytest -q` is the fast inner loop. |
| `scripts/` | Operator helpers: `verify_server.py`, `build_canaveral_ca_bundle.sh`. |
| `.cursor/skills/figma-page-to-prism/` | The page-level mapping skill (ships with the repo). |
| `SETUP.md` / `.env.example` | First-time setup + the env-var contract. |
| `.github/workflows/ci.yml` | CI: lint + tests on every push. |

## Project conventions

- Python style: 80-char lines, 4-space indent, parameterized logging only
  (no f-strings in logger calls). Enforced by `ruff` (config in
  `pyproject.toml`).
- Dependency management uses `uv`. `requirements.txt` is kept in sync as a
  read-only export for tooling that doesn't speak `uv`/`pyproject.toml`.
- Never commit credentials. Artifactory auth is supplied via env vars
  (`JFROG_EMAIL`/`JFROG_API_KEY` or `JFROG_AUTH`) at runtime only.

## CI

`.github/workflows/ci.yml` runs on every push and pull request. The
must-pass loop is defined entirely in `pyproject.toml`, so any runner that
can execute `uv run ruff check`, `uv run ruff format --check`, and
`uv run pytest -q` is sufficient.
