# Setting up prism-mcp

This guide takes you from a fresh clone to a running MCP server wired into
Cursor. It targets **internal Nutanix engineers** (the defaults assume
Nutanix Artifactory / Canaveral and the Figma org).

The whole flow is: **clone → install → credentials → trust TLS → download
models → run & verify → wire into Cursor.**

---

## 1. Prerequisites

- **Python 3.11+**.
- **[`uv`](https://docs.astral.sh/uv/)** — install with
  `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- **Nutanix VPN access** (the Artifactory + Figma hosts are internal).
- **Artifactory credentials** — your `JFROG_EMAIL` + `JFROG_API_KEY`
  (from Artifactory → *Edit Profile* → *Generate API Key*), or a
  pre-encoded `JFROG_AUTH`.
- **A Figma personal access token** with `file:read` scope (only needed
  for `map_figma_node` / `map_figma_tree`), from
  <https://www.figma.com/settings>.

## 2. Clone to `~/autoprism`

Clone to a stable, known location so the Cursor config path (step 8) is
predictable. The canonical location is `~/autoprism`:

```bash
git clone <this-repo-url> ~/autoprism
cd ~/autoprism
```

Any absolute path works — just reuse the same one in step 8.

## 3. Install dependencies

```bash
uv sync
```

This creates `.venv/` and installs the runtime + dev dependencies pinned in
`uv.lock`. No Node, npm, or browser downloads are required — this server is
pure Python.

## 4. Configure credentials

Copy the contract file and fill in what you need:

```bash
cp .env.example .env
```

Edit `.env`:

- `FIGMA_TOKEN=` — required for the Figma tools.
- Artifactory auth — **either** `JFROG_EMAIL` + `JFROG_API_KEY` **or**
  `JFROG_AUTH` (base64 of `email:apikey`).

`.env` is git-ignored. The server reads these on startup; you can also inline
them into your Cursor MCP config (step 8) instead of using `.env`.

## 5. Trust the internal Artifactory TLS chain

Canaveral Artifactory is signed by an internal root CA that lives in the
macOS keychain but not in Python's `certifi` bundle. Build a PEM bundle once:

```bash
bash scripts/build_canaveral_ca_bundle.sh
# writes ~/.cache/prism-mcp/canaveral-ca-bundle.pem
```

Then point the server at it (add to `.env` or the Cursor config):

```bash
PRISM_MCP_CA_BUNDLE=~/.cache/prism-mcp/canaveral-ca-bundle.pem
```

Last-resort escape hatch (known-internal host only):
`PRISM_MCP_INSECURE_TLS=1`. If both are set, the CA bundle wins.

## 6. Download the models

The example-search and node-mapping tools use two ONNX models, downloaded
automatically by `fastembed` **on first use** (cached under
`~/.cache/fastembed/`, a few hundred MB total):

- Encoder: `jinaai/jina-embeddings-v2-base-code`
- Reranker: `Xenova/ms-marco-MiniLM-L-12-v2`

You don't need a separate download step — the verify run in step 7 calls
`search_examples`, which warms both models (and the per-version example
corpus) so the first real query in Cursor is fast. Just make sure you're
online (on VPN) the first time.

## 7. Run and verify

Run the bundled smoke check (loads `.env`, spawns the server, does the MCP
handshake, and exercises `get_library_meta`, `search_examples`, and
`search_entities`):

```bash
set -a ; source .env ; set +a
uv run python scripts/verify_server.py
```

A healthy first run reports `7 tools`, a resolved `get_library_meta`
version, and non-empty search hits. (First run is slower while the tarball
downloads/indexes and the models cache.)

To start the server by hand on stdio (what Cursor does for you):

```bash
uv run prism-mcp
```

## 8. Wire into Cursor

Add an entry to `~/.cursor/mcp.json` (use the **absolute** path to `uv` from
`which uv` — Cursor spawns servers with a minimal PATH):

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

Restart Cursor, open the MCP settings, and confirm `prism-mcp` shows **7
tools**. The repo also ships the `figma-page-to-prism` Cursor skill
(`.cursor/skills/figma-page-to-prism/`) that orchestrates the page-level
flow on top of `map_figma_tree`.

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `spawn uv ENOENT` in Cursor | Use the absolute path to `uv` in `command`. |
| `SELF_SIGNED_CERT_IN_CHAIN` / TLS handshake failure | Set `PRISM_MCP_CA_BUNDLE` (step 5). |
| `LibraryError: ... connect to the Nutanix VPN` | Connect to VPN, or set `JFROG_EMAIL`/`JFROG_API_KEY`. With a populated cache the server runs offline (`from_cache=true`). |
| Figma tool returns `[missing_token]` / `[invalid_token]` | Set/refresh `FIGMA_TOKEN`. |
| First `search_examples` is slow | Expected — models + corpus warm on first use (step 6). |

The server logs to **stderr** (stdout is reserved for MCP JSON-RPC framing),
so redirect stderr to a file when debugging:
`uv run prism-mcp 2> /tmp/prism-mcp.log`.
