# jacksunwei-plugins

A small, opinionated marketplace of Claude Code plugins by Wei (Jack) Sun. Each plugin is single-file Python that runs
via [`uv`](https://docs.astral.sh/uv/) — minimal install footprint, works on any Claude Code backend (direct Anthropic
API, Vertex AI, Bedrock, third-party Anthropic providers).

## Plugins

| Plugin                                        | What it does                                                                                                                      |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| [`gemini-web`](./plugins/gemini-web/)         | **Real Google Search inside Claude Code, with cited sources** — via Gemini's `google_search` grounding + multi-URL summarization. |
| [`telegram-buddy`](./plugins/telegram-buddy/) | **Approve Claude Code permission prompts from your phone** — routes prompts to a Telegram chat while you're away from the desk.   |

## Install

```bash
/plugin marketplace add jacksunwei/jacksunwei-plugins
/plugin install gemini-web@jacksunwei-plugins
/plugin install telegram-buddy@jacksunwei-plugins
```

See each plugin's README for auth, configuration, and usage details.

## Prerequisites

**[`uv`](https://docs.astral.sh/uv/getting-started/installation/)** on `PATH`. Plugins run their MCP servers with
`uv run --script`, which auto-installs Python deps from PEP 723 inline metadata on first launch — no separate
`requirements.txt` or virtualenv to manage.

Plugin-specific prereqs (API keys, OAuth, tokens) are in each plugin's README.

## License

Apache 2.0 — see [LICENSE](./LICENSE).
