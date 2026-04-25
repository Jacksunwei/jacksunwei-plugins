# gemini-web

**Real Google Search inside Claude Code, with cited sources.**

A Claude Code plugin that exposes two of Gemini's grounding tools as MCP tools — `web_search` (Google Search index,
synthesized answer + source URLs) and `summarize_pages` (fetch up to 20 URLs in one call, get back a focused synthesis).
Drop-in replacements for Claude Code's built-in WebSearch and WebFetch with broader coverage and a one-shot multi-URL
summary path.

## When you'd want this

**Fresh facts, with sources.** *"What's the latest version of X?"* *"Did Y change pricing this week?"* Native WebSearch
works, but Gemini's `google_search` grounding hits the actual Google index and returns the URLs it cited — verifiable
answers, not vibes.

**Multi-URL synthesis in one call.** Comparing two release notes? Reading a doc set? `summarize_pages` takes up to 20
URLs in a single call (HTML, PDF, JSON, plain text, images — up to 34 MB each) and returns one synthesized answer
instead of you watching Claude WebFetch them serially and stitching the results.

**Reading without breaking flow.** *"Summarize this blog post"* → one tool call, one response. No copy-pasting URLs into
the chat, no waiting for sequential fetches.

## Install

From the [`jacksunwei-plugins`](../..) marketplace:

```bash
/plugin marketplace add jacksunwei/jacksunwei-plugins
/plugin install gemini-web@jacksunwei-plugins
```

## Auth — pick one

The `google-genai` SDK auto-selects the path from your env vars; this plugin contains no auth code of its own.

**Gemini API key (simplest, individual users):**

```bash
export GOOGLE_API_KEY=your-key   # https://aistudio.google.com/apikey
export GOOGLE_GENAI_USE_VERTEXAI=false   # only if you've previously set it to true
```

**Vertex AI + ADC (enterprise / Google-internal):**

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
export GOOGLE_GENAI_USE_VERTEXAI=true
export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
export GOOGLE_CLOUD_LOCATION=us-central1
# Vertex AI API must be enabled on the project.
```

Optional: `export GEMINI_WEB_MCP_MODEL=gemini-2.5-flash` (default `gemini-flash-latest`). The model **must support both
`google_search` grounding and the `url_context` tool** — not all Gemini variants do.

## Usage

Just ask Claude. Examples:

> Search the web for the latest changes in Vertex AI Gemini grounding pricing.

> Summarize https://ai.google.dev/gemini-api/docs/url-context, focused on the size and content-type limits.

> Compare these two release notes and highlight what's new: \<url1> \<url2>

> Pull the API spec from https://example.com/api-docs.pdf and tell me what authentication options it supports.

## Tool reference

| MCP tool          | What it does                                                                                                                   |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `web_search`      | Search Google via Gemini's `google_search` grounding. Returns a markdown answer followed by a `Sources:` list of cited URLs.   |
| `summarize_pages` | Fetch and synthesize up to 20 URLs in a single Gemini call. Handles HTML, PDF, JSON, plain text, images. Optional `focus` arg. |

## Why this and not Claude Code's built-in WebSearch / WebFetch

**Index coverage.** Gemini's grounding hits the live Google index, which is broader and fresher for most queries than
Anthropic's built-in WebSearch. If Claude ever shrugs at a query, try the same words through `web_search`.

**One-shot multi-URL summarization.** WebFetch fetches one URL at a time, leaving Claude to stitch results across
multiple invocations. `summarize_pages` does fetch + synthesize in a single Gemini call with built-in
PDF/HTML/JSON/image handling — fewer round-trips, better cross-document reasoning.

**Cited sources.** Every `web_search` response ends with a `Sources:` list, so you can verify Gemini's claims rather
than trust them.

**Backend independent.** Works on AI Studio (one API key) or Vertex AI (enterprise auth). Useful if you've standardized
on one provider for billing or quota reasons — and especially if you're driving Claude Code with a 3rd-party / Bedrock /
Vertex Anthropic model that doesn't ship a built-in WebSearch tool, so this fills the gap rather than competing with
one.

## License

Apache 2.0 — see [LICENSE](../../LICENSE).
