# gemini-web

Gemini-powered web tools for Claude Code, exposed via MCP.

- **`web_search`** — answers a question using Gemini's `google_search` grounding, with source URLs.
- **`summarize_pages`** — fetches one or more URLs via Gemini's `url_context` tool and returns a synthesized summary, optionally focused on an aspect you specify.

Both tools are thin wrappers over Gemini's built-in grounding tools — fast, broad coverage, real source URLs come back in the response.

## Install

From the [`jacksunwei-plugins`](../..) marketplace:

```bash
/plugin marketplace add jacksunwei/jacksunwei-plugins
/plugin install gemini-web@jacksunwei-plugins
```

## Auth

Pick one path; the `google-genai` SDK auto-detects which from your env.

**Gemini API key (simplest, individual users):**
```bash
export GOOGLE_API_KEY=your-key   # https://aistudio.google.com/apikey
export GOOGLE_GENAI_USE_VERTEXAI=false
export GEMINI_WEB_MCP_MODEL=gemini-flash-latest   # optional; override to use a different Gemini model
```

**Vertex AI + ADC (enterprise / Google-internal):**
```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
export GOOGLE_GENAI_USE_VERTEXAI=true
export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
export GOOGLE_CLOUD_LOCATION=us-central1   # any Vertex region
export GEMINI_WEB_MCP_MODEL=gemini-flash-latest   # optional; override to use a different Gemini model
# Vertex AI API must be enabled on the project.
```

## Usage

Once installed, ask Claude things like:

> Search the web for the latest changes in Vertex AI Gemini grounding pricing.

> Summarize https://ai.google.dev/gemini-api/docs/url-context, focused on the size and content-type limits.

> Compare these two release notes and highlight what's new: \<url1> \<url2>

`summarize_pages` accepts up to 20 URLs per call and handles HTML, PDF, JSON, plain text, and images.

## License

Apache 2.0 — see [LICENSE](../../LICENSE).
