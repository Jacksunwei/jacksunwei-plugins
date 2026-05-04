# Wei (Jack) Sun's Claude Code Plugins

A small, opinionated marketplace of Claude Code plugins by Wei (Jack) Sun.

## Install

```bash
/plugin marketplace add Jacksunwei/jacksunwei-plugins
/plugin install gemini-web@jacksunwei-plugins
```

## Plugins

### Gemini Web

![Search, summarize, and generate — three Gemini-powered tools for Claude Code](./plugins/gemini-web/docs/hero.png)

Drop-in upgrades to Claude Code's built-in WebSearch and WebFetch — broader coverage, one-shot multi-URL synthesis —
plus image generation Claude Code doesn't ship at all. Especially useful on Bedrock or Vertex Anthropic backends that
don't ship a built-in WebSearch.

| Tool              | What it does                                                                                                                                   |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `web_search`      | Google Search via Gemini's `google_search` grounding — synthesized answer with cited source URLs.                                              |
| `summarize_pages` | Fetch and synthesize up to 20 URLs in one call — HTML, PDF, JSON, plain text, images (≤34 MB each).                                            |
| `generate_image`  | Text-to-image, image editing, and multi-image fusion via Gemini's Nano Banana model (`gemini-3.1-flash-image-preview`), saved to your project. |

See [`plugins/gemini-web/README.md`](./plugins/gemini-web/README.md) for usage examples, configuration, and auth
details.

### Telegram Buddy

Graduated to its own standalone repo — see
[Jacksunwei/claude-telegram-buddy](https://github.com/Jacksunwei/claude-telegram-buddy) for the latest version.

## Prerequisites

[`uv`](https://docs.astral.sh/uv/) is required — install via
[their docs](https://docs.astral.sh/uv/getting-started/installation/).

## License

Apache 2.0 — see [LICENSE](./LICENSE).
