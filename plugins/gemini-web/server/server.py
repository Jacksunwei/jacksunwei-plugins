#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-genai>=1.73.0",
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

Auth is resolved by the google-genai SDK from the environment:
  - Vertex AI mode: GOOGLE_GENAI_USE_VERTEXAI=true + GOOGLE_CLOUD_PROJECT
    (+ optional GOOGLE_CLOUD_LOCATION, defaults to us-central1) with ADC
  - Gemini API mode: GOOGLE_API_KEY=<key>

Plugin-specific env:
  GEMINI_WEB_MODEL     Model ID (default: gemini-flash-latest)
"""

import os

from google import genai
from google.genai import types
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("gemini-web")

MODEL = os.environ.get("GEMINI_WEB_MODEL", "gemini-flash-latest")

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


if __name__ == "__main__":
  mcp.run()
