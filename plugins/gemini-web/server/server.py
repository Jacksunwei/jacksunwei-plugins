#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-genai>=1.73.0",
#   "httpx>=0.27.0",
#   "mcp[cli]>=1.27.0",
# ]
# ///
# Copyright 2026 Wei (Jack) Sun
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""MCP server providing Gemini-powered web tools.

Currently exposes:
  - web_search: web search via Gemini's google_search grounding
  - summarize_pages: summarize one or more pages via Gemini's url_context tool
  - generate_image: text-to-image via Gemini's "Nano Banana" image model

Auth is resolved by the google-genai SDK from the environment:
  - Vertex AI mode: GOOGLE_GENAI_USE_VERTEXAI=true + GOOGLE_CLOUD_PROJECT
    (+ optional GOOGLE_CLOUD_LOCATION, defaults to us-central1) with ADC
  - Gemini API mode: GOOGLE_API_KEY=<key>

Plugin-specific env:
  GEMINI_WEB_MCP_MODEL        Model ID for web_search / summarize_pages
                              (default: gemini-flash-latest). Must support
                              both google_search grounding and url_context.
  GEMINI_WEB_MCP_IMAGE_MODEL  Model ID for generate_image
                              (default: gemini-3.1-flash-image-preview, a.k.a.
                              Nano Banana 2). Must support image output.
"""

import mimetypes
import os
import tempfile
import time
from pathlib import Path

import httpx
from google import genai
from google.genai import types
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("gemini-web")

MODEL = os.environ.get("GEMINI_WEB_MCP_MODEL", "gemini-flash-latest")
IMAGE_MODEL = os.environ.get(
    "GEMINI_WEB_MCP_IMAGE_MODEL", "gemini-3.1-flash-image-preview"
)

client = genai.Client()


@mcp.tool()
async def web_search(query: str) -> str:
  """Search the web for current information, returning a synthesized answer with cited sources.

  Use this for any task that needs fresh web information — news, current events,
  latest releases, recent prices, "what's the latest...", "look up...", or any
  question whose answer might have changed since the model's training cutoff.
  Powered by Google Search via Gemini's `google_search` grounding, so coverage
  matches Google's index.

  Args:
    query: A natural-language question or keywords describing what to search for.

  Returns:
    Markdown text: a synthesized answer to the query, followed by a `Sources:`
    section listing the cited URLs.
  """
  response = await client.aio.models.generate_content(
      model=MODEL,
      contents=f"Search the web and provide detailed results for: {query}",
      config=types.GenerateContentConfig(
          tools=[types.Tool(google_search=types.GoogleSearch())]
      ),
  )

  parts = []
  if response.text:
    parts.append(response.text)

  candidate = response.candidates[0] if response.candidates else None
  grounding = candidate.grounding_metadata if candidate else None
  chunks = grounding.grounding_chunks if grounding else None
  if chunks:
    parts.append("\nSources:")
    for chunk in chunks:
      if chunk.web:
        parts.append(f"- [{chunk.web.title}]({chunk.web.uri})")

  return "\n".join(parts) if parts else "No results found."


@mcp.tool()
async def summarize_pages(urls: list[str], focus: str | None = None) -> str:
  """Summarize one or more web pages by URL using Gemini's URL Context tool.

  The model fetches each URL (HTML, PDF, JSON, plain text, or images up to
  34 MB each, max 20 URLs per call) and returns a synthesized summary. With
  multiple URLs the model can compare, contrast, or consolidate across them —
  phrase `focus` accordingly (e.g. "diff the API changes" or "extract the
  pricing tier from each").

  Public URLs only: no localhost, login-gated, paywalled pages, YouTube,
  Google Workspace docs, or other private content.

  Args:
    urls: One or more page URLs to summarize.
    focus: Optional aspect to focus the summary on (e.g. "performance numbers",
      "breaking changes"). Omit for a general summary.

  Returns:
    Markdown text: the summary, followed by a `Sources:` section listing each
    URL and its retrieval status.
  """
  if not urls:
    return "No URLs provided."

  url_lines = "\n".join(f"- {u}" for u in urls)
  focus_clause = f" Focus on: {focus}." if focus else ""
  page_word = "page" if len(urls) == 1 else "pages"
  prompt = f"Summarize the following {page_word}.{focus_clause}\n{url_lines}"

  response = await client.aio.models.generate_content(
      model=MODEL,
      contents=prompt,
      config=types.GenerateContentConfig(
          tools=[types.Tool(url_context=types.UrlContext())]
      ),
  )

  parts = []
  if response.text:
    parts.append(response.text)

  candidate = response.candidates[0] if response.candidates else None
  url_meta = candidate.url_context_metadata if candidate else None
  entries = url_meta.url_metadata if url_meta else None
  if entries:
    parts.append("\nSources:")
    for entry in entries:
      url = entry.retrieved_url or "(unknown URL)"
      status = entry.url_retrieval_status or "UNKNOWN"
      parts.append(f"- {url} ({status})")

  return "\n".join(parts) if parts else "No results found."


async def _load_image_parts(refs: list[str]) -> list[types.Part]:
  """Load each ref (absolute path or http(s):// URL) into an inline image Part."""
  parts: list[types.Part] = []
  http: httpx.AsyncClient | None = None
  try:
    for ref in refs:
      if ref.startswith(("http://", "https://")):
        if http is None:
          http = httpx.AsyncClient(follow_redirects=True, timeout=30)
        resp = await http.get(ref)
        resp.raise_for_status()
        mime = (
            (resp.headers.get("content-type") or "application/octet-stream")
            .split(";")[0]
            .strip()
        )
        parts.append(types.Part.from_bytes(data=resp.content, mime_type=mime))
        continue
      path = Path(ref).expanduser()
      if not path.is_absolute():
        raise ValueError(
            "input_images entry must be an absolute file path or http(s):// "
            f"URL (got: {ref})"
        )
      data = path.read_bytes()
      mime, _ = mimetypes.guess_type(str(path))
      parts.append(
          types.Part.from_bytes(data=data, mime_type=mime or "application/octet-stream")
      )
  finally:
    if http is not None:
      await http.aclose()
  return parts


@mcp.tool()
async def generate_image(
    prompt: str,
    input_images: list[str] | None = None,
    output_path: str | None = None,
    aspect_ratio: str | None = None,
    image_size: str | None = None,
) -> str:
  """Generate, edit, or compose an image with Gemini's "Nano Banana" image model.

  Defaults to `gemini-3.1-flash-image-preview` (Nano Banana 2) — a native
  multimodal image model with strong prompt adherence, in-image text
  rendering, and up to 4K output. Every image carries an invisible SynthID
  watermark.

  The mode is driven by the prompt and the presence of `input_images`:
    - text only → generate from scratch
    - prompt + 1 image → edit, restyle, or transform that image
    - prompt + multiple images → blend, fuse, or use as subject/style
      references

  Be specific in the prompt: subject, style, composition, lighting, camera
  angle, and any literal text to render.

  Args:
    prompt: Natural-language description of the desired output.
    input_images: Optional reference images. Each entry is either an
      absolute filesystem path (`~` is expanded) or a public `http(s)://`
      URL. Relative paths are rejected. Pass screenshots, prior
      `generate_image` outputs, or web image URLs.
    output_path: Optional absolute file path to write the image to. Parent
      directories are created if missing. If omitted, saves to the OS
      temp directory with a timestamped filename. Relative paths are
      rejected.
    aspect_ratio: Optional output aspect ratio, e.g. `"1:1"`, `"16:9"`,
      `"9:16"`, `"4:3"`, `"3:4"`, `"21:9"`. Omit to let the model choose.
    image_size: Optional output resolution tier: `"1K"` (default), `"2K"`,
      or `"4K"`. Omit for the model default.

  Returns:
    Markdown text: the absolute path of the saved image, plus any
    commentary text the model returned alongside the image.
  """
  resolved_out: Path | None = None
  if output_path:
    resolved_out = Path(output_path).expanduser()
    if not resolved_out.is_absolute():
      raise ValueError(f"output_path must be absolute (got: {output_path})")

  contents: list = await _load_image_parts(input_images or [])
  contents.append(prompt)

  image_config = None
  if aspect_ratio or image_size:
    image_config = types.ImageConfig(
        aspect_ratio=aspect_ratio,
        image_size=image_size,
    )

  response = await client.aio.models.generate_content(
      model=IMAGE_MODEL,
      contents=contents,
      config=types.GenerateContentConfig(
          response_modalities=["IMAGE"],
          image_config=image_config,
      ),
  )

  candidate = response.candidates[0] if response.candidates else None
  parts = (candidate.content.parts if candidate and candidate.content else []) or []

  image_bytes = None
  mime_type = "image/png"
  text_parts = []
  for part in parts:
    if part.inline_data and part.inline_data.data:
      image_bytes = part.inline_data.data
      if part.inline_data.mime_type:
        mime_type = part.inline_data.mime_type
    elif part.text:
      text_parts.append(part.text)

  if not image_bytes:
    note = "\n".join(text_parts).strip()
    return f"No image returned. Model said: {note}" if note else "No image returned."

  ext = mime_type.rsplit("/", 1)[-1] if "/" in mime_type else "png"
  if resolved_out is not None:
    resolved_out.parent.mkdir(parents=True, exist_ok=True)
    out_path = resolved_out
  else:
    out_path = Path(tempfile.gettempdir()) / f"gemini-image-{int(time.time())}.{ext}"

  out_path.write_bytes(image_bytes)

  out = [f"Saved image: {out_path}"]
  if text_parts:
    out.append("")
    out.append("\n".join(text_parts).strip())
  return "\n".join(out)


if __name__ == "__main__":
  mcp.run()
