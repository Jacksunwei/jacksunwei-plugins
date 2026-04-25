#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp[cli]>=1.27.0",
#   "aiohttp>=3.9",
#   "python-telegram-bot>=21.0",
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
"""MCP server that routes Claude Code permission prompts to Telegram.

Tools:
  - enable_telegram(chat_id?): bind 127.0.0.1:8787 + start Telegram poll
  - disable_telegram(): stop both
  - status(): report current state

The plugin also declares a PreToolUse HTTP hook (in plugin.json) that POSTs
every matched tool call to http://localhost:8787/approve. While "enabled",
this server relays each request to Telegram as an inline-keyboard message
and resolves the response with the user's tap. While "disabled", the port is
unbound and Claude Code falls back to its local prompt.

Bot token discovery (in order):
  1. CLAUDE_PLUGIN_OPTION_TELEGRAM_BOT_TOKEN — set by the plugin's userConfig
     prompt at install time (stored in macOS Keychain).
  2. TELEGRAM_BOT_TOKEN env var — for standalone testing outside Claude Code.
  3. ~/.claude/channels/claude-approve/.env (KEY=VALUE format) — fallback.
"""

import asyncio
import os
import secrets
from pathlib import Path

from aiohttp import web
from mcp.server.fastmcp import FastMCP
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

PORT = 8787
ENV_FILE = Path.home() / ".claude" / "channels" / "claude-approve" / ".env"
HOOK_TIMEOUT_S = 290  # leaves headroom under the 300s hook timeout in plugin.json

mcp = FastMCP("claude-approve")

state: dict = {
  "enabled": False,
  "chat_id": None,
  "http_runner": None,
  "tg_app": None,
  "pending": {},  # request_id -> asyncio.Future[str]
  "decided": 0,
}


def _load_token() -> str | None:
  for var in ("CLAUDE_PLUGIN_OPTION_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"):
    tok = os.environ.get(var)
    if tok:
      return tok
  if not ENV_FILE.exists():
    return None
  for line in ENV_FILE.read_text().splitlines():
    line = line.strip()
    if line.startswith("TELEGRAM_BOT_TOKEN="):
      return line.split("=", 1)[1].strip()
  return None


def _format_request(payload: dict, request_id: str) -> str:
  tool = payload.get("tool_name", "?")
  inp = payload.get("tool_input") or {}
  cwd = payload.get("cwd", "?")
  preview = ""
  if isinstance(inp, dict):
    if "command" in inp:
      preview = f"\n```\n{inp['command']}\n```"
    elif "file_path" in inp:
      preview = f"\n`{inp['file_path']}`"
    elif "url" in inp:
      preview = f"\n{inp['url']}"
  return f"🔧 *{tool}* `[{request_id}]`{preview}\n_cwd_: `{cwd}`"


async def _on_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
  q = update.callback_query
  if not q or not q.data:
    return
  await q.answer()
  # Only the chat owner can decide. Defensive even though the inline keyboard
  # is only sent to chat_id and Telegram doesn't allow forwarding it intact.
  if state["chat_id"] and q.from_user and str(q.from_user.id) != str(state["chat_id"]):
    return
  try:
    action, rid = q.data.split(":", 1)
  except ValueError:
    return
  fut = state["pending"].get(rid)
  if fut and not fut.done():
    decision = "allow" if action == "a" else "deny"
    fut.set_result(decision)
    suffix = "✅ Approved" if decision == "allow" else "❌ Denied"
  else:
    suffix = "⏰ Expired"
  # callback_query.message is MaybeInaccessibleMessage — only Message has .text.
  prior = getattr(q.message, "text", None) if q.message else None
  if prior is not None:
    try:
      await q.edit_message_text(text=f"{prior}\n\n{suffix}")
    except Exception:
      pass


async def _handle_approve(request: web.Request) -> web.Response:
  payload = await request.json()
  rid = secrets.token_hex(3)
  fut: asyncio.Future = asyncio.get_event_loop().create_future()
  state["pending"][rid] = fut

  text = _format_request(payload, rid)
  keyboard = InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Approve", callback_data=f"a:{rid}"),
    InlineKeyboardButton("❌ Deny", callback_data=f"d:{rid}"),
  ]])
  try:
    await state["tg_app"].bot.send_message(
      chat_id=state["chat_id"],
      text=text,
      reply_markup=keyboard,
      parse_mode="Markdown",
    )
  except Exception as e:
    state["pending"].pop(rid, None)
    return web.json_response({
      "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": f"claude-approve: telegram send failed: {e}",
      }
    })

  try:
    decision = await asyncio.wait_for(fut, timeout=HOOK_TIMEOUT_S)
  except asyncio.TimeoutError:
    decision = "ask"
  finally:
    state["pending"].pop(rid, None)

  state["decided"] += 1
  return web.json_response({
    "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "permissionDecision": decision,
      "permissionDecisionReason": f"claude-approve via Telegram → {decision}",
    }
  })


@mcp.tool()
async def enable_telegram(chat_id: str | None = None) -> str:
  """Start routing Claude Code permission prompts to Telegram.

  Binds 127.0.0.1:8787 and starts a Telegram long-poll. While enabled, every
  PreToolUse hook for Bash/Edit/Write/WebFetch is relayed to your phone as
  an inline-keyboard message; tap Approve or Deny to decide.

  Args:
    chat_id: Telegram chat ID for approval messages. For DMs, this is your
      Telegram user ID (message @userinfobot to find yours). Falls back to
      the chat_id you set at plugin install time, then to the
      CLAUDE_APPROVE_CHAT_ID env var.

  Returns:
    Status string. Errors if port is already bound (another session holds
    it), no bot token is configured, or no chat_id is available.
  """
  if state["enabled"]:
    return f"Already enabled (chat_id={state['chat_id']}). Call disable_telegram first."

  chat_id = (
    chat_id
    or os.environ.get("CLAUDE_PLUGIN_OPTION_CHAT_ID")
    or os.environ.get("CLAUDE_APPROVE_CHAT_ID")
  )
  if not chat_id:
    return (
      "No chat_id. Reconfigure the plugin (`/plugin` → claude-approve → "
      "Configure options) or pass an explicit chat_id."
    )

  token = _load_token()
  if not token:
    return (
      "No bot token. Reconfigure the plugin (`/plugin` → claude-approve → "
      "Configure options) to set the Telegram Bot Token, or set the "
      "TELEGRAM_BOT_TOKEN env var for standalone testing."
    )

  app = web.Application()
  app.router.add_post("/approve", _handle_approve)
  runner = web.AppRunner(app)
  await runner.setup()
  site = web.TCPSite(runner, "127.0.0.1", PORT)
  try:
    await site.start()
  except OSError as e:
    await runner.cleanup()
    return f"Could not bind port {PORT}: {e}. Likely another session holds it."

  tg_app = Application.builder().token(token).build()
  tg_app.add_handler(CallbackQueryHandler(_on_callback))
  await tg_app.initialize()
  await tg_app.start()
  updater = tg_app.updater
  if updater is None:
    await tg_app.shutdown()
    await runner.cleanup()
    return "Telegram Application has no updater (unexpected)."
  await updater.start_polling(
    drop_pending_updates=True,
    allowed_updates=["callback_query"],
  )

  state.update(
    enabled=True,
    chat_id=str(chat_id),
    http_runner=runner,
    tg_app=tg_app,
  )
  return f"Enabled. Approvals route to chat {chat_id}. Listener on 127.0.0.1:{PORT}."


@mcp.tool()
async def disable_telegram() -> str:
  """Stop routing approvals to Telegram. Future hook calls fall back to local prompts."""
  if not state["enabled"]:
    return "Not enabled."

  for fut in list(state["pending"].values()):
    if not fut.done():
      fut.set_result("ask")
  state["pending"].clear()

  tg = state["tg_app"]
  try:
    if tg.updater is not None:
      await tg.updater.stop()
    await tg.stop()
    await tg.shutdown()
  except Exception:
    pass

  await state["http_runner"].cleanup()

  state.update(enabled=False, chat_id=None, http_runner=None, tg_app=None)
  return "Disabled. Hooks will fall back to local prompts."


@mcp.tool()
async def status() -> str:
  """Report whether Telegram routing is enabled and basic counters."""
  return (
    f"enabled={state['enabled']} "
    f"chat_id={state['chat_id']} "
    f"port={PORT} "
    f"pending={len(state['pending'])} "
    f"decided={state['decided']}"
  )


if __name__ == "__main__":
  mcp.run()
