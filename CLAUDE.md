# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

This repo is a **Claude Code plugin marketplace** (`jacksunwei-plugins`). It is not a single application — it is a registry of plugins, each of which ships its own MCP server. Currently it hosts one plugin: `gemini-web` (an MCP `web_search` tool backed by Gemini's `google_search` grounding).

## Architecture: how a plugin is wired together

A plugin is defined by **three coordinating files**. Editing any one in isolation will usually break installation:

1. **`.claude-plugin/marketplace.json`** (repo root) — the marketplace manifest. Lists each plugin's `name`, `source` (path to plugin dir), and `description`. Claude Code reads this when a user runs `/plugin marketplace add`.
2. **`plugins/<name>/.claude-plugin/plugin.json`** — the per-plugin manifest. Declares `mcpServers`, where the `command` + `args` tell Claude Code how to launch the server. Use `${CLAUDE_PLUGIN_ROOT}` to reference paths inside the plugin directory (e.g. `${CLAUDE_PLUGIN_ROOT}/server/server.py`) — never hardcode absolute paths.
3. **`plugins/<name>/server/server.py`** — the MCP server itself. Uses **PEP 723 inline script metadata** (the `# /// script` block at the top) so `uv run --script` auto-installs Python deps on first launch. There is no `pyproject.toml` or `requirements.txt` per plugin — dependencies live inside the script file.

When adding a new plugin, all three files must be created and the marketplace manifest's `plugins` array updated.

## Auth model (gemini-web)

The `google-genai` SDK auto-selects the auth path from env vars — the server itself contains no auth logic:

- `GOOGLE_API_KEY` set → Gemini API mode (individual users / AI Studio key).
- `GOOGLE_GENAI_USE_VERTEXAI=true` + `GOOGLE_CLOUD_PROJECT` + ADC → Vertex AI mode (enterprise / Google-internal).

Model is `GEMINI_WEB_MCP_MODEL` (default `gemini-flash-latest`). The model **must support `google_search` grounding** — not all Gemini models do.

## Common commands

```bash
# Run the MCP server standalone (for smoke-testing; serves stdio MCP protocol)
uv run --script plugins/gemini-web/server/server.py

# Install this marketplace locally for end-to-end testing in Claude Code
/plugin marketplace add /Users/jacksun/Github/gemini-mcp
/plugin install gemini-web@jacksunwei-plugins
```

There is no test suite, linter config, or build step in this repo. Validate changes by:
1. Running the server standalone to catch import/syntax errors.
2. Installing the marketplace locally and exercising the tool from Claude Code.

## Conventions

- **Python indentation: 2 spaces** (see `server.py`). This is unusual for Python — match it.
- **Apache 2.0 header** on every Python source file (copyright `Wei (Jack) Sun`).
- Keep dependencies in the PEP 723 block, **not** in a separate requirements file. The whole point of the layout is single-file deployability via `uv`.
- Plugin descriptions in `marketplace.json`, `plugin.json`, and `README.md` should stay in sync — the marketplace table in the README is hand-maintained.
