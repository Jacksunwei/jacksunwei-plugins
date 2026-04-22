# jacksunwei-plugins

Claude Code plugin marketplace curated by Wei (Jack) Sun.

## Why this exists

Gemini's `google_search` grounding is one of the best ways to give an LLM live, cited web results — fast, broad coverage, and source URLs come back in the response. This plugin exposes that as an MCP `web_search` tool so Claude Code can call it directly.

## Plugins in this marketplace

| Plugin                               | Description                                                                |
| ------------------------------------ | -------------------------------------------------------------------------- |
| [`gemini-web`](./plugins/gemini-web) | Gemini-powered web tools: `web_search` (via `google_search` grounding) and `summarize_pages` (via `url_context`) |

## Install (Claude Code)

```bash
/plugin marketplace add jacksunwei/jacksunwei-plugins
/plugin install gemini-web@jacksunwei-plugins
```

## Prerequisites

1. **`uv`** on `PATH` — [install](https://docs.astral.sh/uv/getting-started/installation/). The plugin runs the server with `uv run --script`, which auto-installs Python deps from PEP 723 inline metadata on first run.
2. **Auth.** Pick one path; the `google-genai` SDK auto-detects which from your env.

   **Gemini API key (simplest, individual users):**
   ```bash
   export GOOGLE_API_KEY=your-key   # https://aistudio.google.com/apikey
   ```

   **Vertex AI + ADC (enterprise / Google-internal):**
   ```bash
   gcloud auth application-default login
   gcloud auth application-default set-quota-project YOUR_PROJECT_ID
   export GOOGLE_GENAI_USE_VERTEXAI=true
   export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
   # Vertex AI API must be enabled on the project.
   ```

## Configuration

| Variable                    | Default                          | Notes                                                                                                                              |
| --------------------------- | -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `GOOGLE_GENAI_USE_VERTEXAI` | _(unset)_                        | `true` selects Vertex AI mode                                                                                                      |
| `GOOGLE_CLOUD_PROJECT`      | _(required for Vertex)_          | GCP project ID for Vertex billing                                                                                                  |
| `GOOGLE_CLOUD_LOCATION`     | `us-central1`                    | Vertex region                                                                                                                      |
| `GOOGLE_API_KEY`            | _(required for Gemini API mode)_ | API key from AI Studio                                                                                                             |
| `GEMINI_WEB_MODEL`          | `gemini-flash-latest`            | Any Gemini model that supports `google_search` grounding. Pin to a specific version (e.g. `gemini-2.5-flash`) for reproducibility. |

## Usage

Once installed, Claude Code exposes a `web_search` tool. Ask Claude things like:

> Search the web for the latest changes in Vertex AI Gemini grounding pricing.

Claude calls the tool; the server queries Gemini with `google_search` enabled and returns the grounded answer plus source URLs.

## License

Apache 2.0 — see [LICENSE](./LICENSE).
