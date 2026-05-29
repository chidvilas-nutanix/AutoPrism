# prism-mcp

A local Python MCP server that exposes the Nutanix internal React component
library — `@nutanix-ui/prism-reactjs` — to LLM clients (Cursor, Claude
Desktop, etc.) so they can generate correct, non-deprecated, type-safe
component code.

Status: **Slices 1–8 shipped — v1 feature-complete** — see
[`docs/prd/prism-reactjs-mcp-server.md`](docs/prd/prism-reactjs-mcp-server.md)
for the full plan. The current build ships:

- **Slice 1**: `uv`-managed Python project, ruff + pytest, CI green, stdio `echo` tool.
- **Slice 2**: Artifactory acquisition with Basic auth, ETag short-
  circuit on the `<base>/<pkg>/latest` manifest endpoint (we don't
  pull the full registry document — for Prism that's ~46MB; the
  per-version manifest is a few KB), SRI + sha1 integrity verification,
  atomic-swap cache at `~/.cache/prism-mcp/<version>/`, offline fallback
  to last cached version, `get_library_meta` tool.
- **Slice 3**: bracket-aware `.d.ts` parser + `examples.md` splitter, per-
  subcomponent walk over `lib/components/v2/`, in-memory `Index` with
  `list_entities` and `get_entity` tools.
- **Slice 4**: BM25 ranking over a synthetic doc per entity
  (``name + type + category + summary + example-titles``), camelCase-aware
  tokenizer with light English-suffix stripping (`trapping` → `trap`),
  `search_entities` tool returning `score` + `why_matched`.
- **Slice 5**: function + class parsers extending the `.d.ts` parser,
  walkers for hooks (`lib/hooks/use*.d.ts`), managers (`lib/managers/
  *Manager.d.ts`), and utils (`lib/utils/**/*.d.ts`); all four entity
  types appear in `list_entities` / `search_entities`.
- **Slice 6**: LESS extractor for `src/styles/v2/*.less` producing
  `token` entities with `name`, `value`, `category`, `source_file`;
  category is inferred from filename (Colors → color, Z-Index → z-index,
  etc.).
- **Slice 7**: `Library.refresh()` returns a structured `RefreshOutcome`
  (`swapped` / `not_modified` / `offline`) and a `RefreshLoop` asyncio
  driver runs it once per day. The loop is wired into FastMCP's
  lifespan so every running server cold-starts with one poll and then
  ticks daily. Index swaps are atomic: we build the new `Index` into a
  local variable before publishing both `_meta` and `_index` together,
  so any in-flight tool call sees a consistent snapshot.
- **Slice 8**: registry transport failures (DNS, TLS, connection,
  timeout) are wrapped as `RegistryError` so the existing cache
  fallback fires for *all* "offline" causes, not just non-2xx HTTP
  responses. Cached fallback logs a warning naming the cached version;
  cold-start-no-cache raises `LibraryError` with an explicit
  "Connect to the Nutanix VPN (or set JFROG_EMAIL/JFROG_API_KEY)"
  hint. `get_library_meta` surfaces `from_cache=true` so the LLM
  client can detect degraded mode.

## Requirements

- Python 3.11+ (the project uses 3.12 by default via `uv`).
- [`uv`](https://docs.astral.sh/uv/) for dependency and venv management.

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
initialize handshake, calls `get_library_meta`, `list_entities`,
and `search_entities` for "modal dialog", then prints a short
summary. Useful when wiring a new laptop:

```bash
set -a ; source .env ; set +a
uv run python scripts/verify_server.py
```

A healthy first run prints something like:

```
id=3 get_library_meta
   package_name: @nutanix-ui/prism-reactjs
   version: 2.53.0
   from_cache: False
id=4 list_entities   version=2.53.0  components=331
id=5 search_entities query='modal dialog' hits=5
   - Modal           type=component  score=5.502 why=['dialog', 'modal']
   - ConfirmModal    type=component  score=5.455 why=['dialog', 'modal']
   ...
```

The server exposes five MCP tools:

| Tool | Purpose |
|---|---|
| `echo` | Liveness probe; returns a fixed string. |
| `get_library_meta` | Resolved version, cache path, source URL, indexed-at. |
| `list_entities` | All indexed entities, optionally filtered by `type`. |
| `search_entities` | BM25-ranked matches for a prose query (`why_matched` included). |
| `get_entity` | Full record (props, signature, examples, import path). |

The cache survives restarts. On cold start without VPN, the server
falls back to the last cached version with `from_cache=true`; when
the cache is also empty, the server raises a `LibraryError` whose
message tells the operator to connect to the Nutanix VPN.

### Figma page → Prism (page-level mapping)

In addition to per-node mapping (`map_figma_node`), the server
exposes `map_figma_tree` for **page-level** Figma → Prism
conversion. Given a Figma node URL it parses the URL, fetches
the subtree via the Figma REST API, applies a 7-pass noise
filter + routing layer + pattern detector, and returns a
structured `FigmaTreeMapping` containing a pruned `layout_tree`,
an `agenda` of `MappedRegion`s with ranked Prism candidates,
and a `dropped` audit trail. The
[`figma-page-to-prism`](.cursor/skills/figma-page-to-prism/SKILL.md)
Cursor skill orchestrates the full Phase A–H flow (input →
gather → map → plan → compose → write → validate → report) on
top of this tool. See
[`docs/figma-page-to-prism-plan.md`](docs/figma-page-to-prism-plan.md)
for the design rationale, the routing table, the pattern
catalogue, and worked examples.

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
| `docs/prd/` | Product requirements doc; the destination document. |
| `prism-ui-prism-reactjs-lib-master/` | Read-only upstream library. Never edit. |
| `.github/workflows/ci.yml` | CI: lint + tests on every push. |

## Project conventions

- Python style follows
  [`.cursor/rules/language/ntnx-python-standards.mdc`](.cursor/rules/language/ntnx-python-standards.mdc)
  (80-char lines, 4-space indent, parameterized logging only).
- Dependency management uses `uv`. `requirements.txt` is kept in sync as a
  read-only export for tooling that doesn't speak `uv`/`pyproject.toml`.
- Never commit credentials. Artifactory auth is supplied via env vars
  (`JFROG_EMAIL`/`JFROG_API_KEY` or `JFROG_AUTH`) at runtime only.

## CI

`.github/workflows/ci.yml` runs on every push and pull request. It's the
default; swap to whichever CI host your team uses — the must-pass loop is
defined entirely in `pyproject.toml`, so any runner that can execute
`uv run ruff check`, `uv run ruff format --check`, and `uv run pytest -q`
is sufficient.
