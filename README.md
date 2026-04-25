# jacksunwei-plugins

Claude Code plugin marketplace curated by Wei (Jack) Sun.

## Plugins

| Plugin                                       | Description                                                                                                      |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| [`gemini-web`](./plugins/gemini-web)         | Gemini-powered web tools: `web_search` (via `google_search` grounding) and `summarize_pages` (via `url_context`) |
| [`telegram-buddy`](./plugins/telegram-buddy) | Approve Claude Code permission prompts from Telegram while away from the terminal                                |

See each plugin's README for auth, configuration, and usage details.

## Install the marketplace

```bash
/plugin marketplace add jacksunwei/jacksunwei-plugins
```

Then install individual plugins:

```bash
/plugin install <plugin-name>@jacksunwei-plugins
```

## Prerequisites

**`uv`** on `PATH` — [install](https://docs.astral.sh/uv/getting-started/installation/). Plugins in this marketplace run
their MCP servers with `uv run --script`, which auto-installs Python deps from PEP 723 inline metadata on first run.

Plugin-specific prerequisites (auth, env vars, API access) are documented in each plugin's README.

## License

Apache 2.0 — see [LICENSE](./LICENSE).
