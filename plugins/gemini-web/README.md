# Gemini Web - Claude Code Plugin

![Search, summarize, and generate — three Gemini-powered tools for Claude Code](docs/hero.png)

**Real Google Search, multi-page summaries, and Nano Banana image generation — inside Claude Code.**

Three Gemini-powered MCP tools for Claude Code:

- **`web_search`** — real Google Search via Gemini's grounding, with cited source URLs.
- **`summarize_pages`** — fetch and synthesize up to 20 URLs in a single call (HTML, PDF, JSON, images — up to 34 MB each).
- **`generate_image`** — text-to-image, image editing, and multi-image fusion via Gemini's "Nano Banana" model, saved to disk.

Drop-in upgrades to Claude Code's built-in WebSearch and WebFetch — broader coverage, one-shot multi-URL synthesis — plus image generation Claude Code doesn't ship at all. Especially useful on Bedrock or Vertex Anthropic backends that don't ship a built-in WebSearch.

## Usage

Just ask Claude. A few examples:

**`web_search`:**

> Use Gemini to research all the major image-generation models and compare pros and cons.

**`summarize_pages`:**

> Summarize key changes of the paper in \<url>.

**`generate_image`:**

> Generate an image of a retro 8-bit banana floating in space, save it as `/Users/me/proj/assets/hero.png`.

> Take `/Users/me/Downloads/sketch.png` and turn it into a watercolor painting.

## Install

From the [`jacksunwei-plugins`](../..) marketplace:

```bash
/plugin marketplace add jacksunwei/jacksunwei-plugins
/plugin install gemini-web@jacksunwei-plugins
```

### Configure

**First time:** Claude Code prompts you for the fields below right after `/plugin install`. Fill in the API key (the rest can stay blank for defaults).

**Later:** to change any setting, run `/plugin`, select **gemini-web**, and edit its config.

| Field                            | Default                          | Notes                                                                                                                                              |
| -------------------------------- | -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Gemini API key**               | _none_                           | Your [AI Studio key](https://aistudio.google.com/apikey). Stored in your system keychain.                                                          |
| **Search / summarization model** | `gemini-flash-latest`            | Used by `web_search` and `summarize_pages`. Must support both `google_search` grounding and the `url_context` tool.                                |
| **Image generation model**       | `gemini-3.1-flash-image-preview` | Nano Banana 2. Override to `gemini-2.5-flash-image` (GA Nano Banana) or `gemini-3-pro-image-preview` (Nano Banana Pro).                            |

> Need Vertex AI or env-var auth instead? See [Advanced: env-var auth](#advanced-env-var-auth) below.

## Advanced: env-var auth

If you can't (or don't want to) use the plugin UI for the API key — for example you're on Vertex AI, sharing settings across machines, or scripting installs — leave the **Gemini API key** field blank and set env vars instead. The `google-genai` SDK auto-selects the auth path from your environment:

**Gemini API key (individual users):**

```bash
export GOOGLE_API_KEY=your-key   # https://aistudio.google.com/apikey
export GOOGLE_GENAI_USE_VERTEXAI=false   # only if previously set to true
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

If both the **Gemini API key** plugin field and `GOOGLE_*` env vars are set, the plugin field wins.

## License

Apache 2.0 — see [LICENSE](../../LICENSE).
