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
  - enable_telegram(): bind 127.0.0.1:8787 + start Telegram poll
  - disable_telegram(): stop both
  - status(): report current state

The plugin declares a PermissionRequest HTTP hook (in plugin.json) that POSTs
to http://localhost:8787/approve only when Claude Code is about to prompt the
user — i.e., calls already auto-approved by the allowlist run silently and
never reach this server. While "enabled", the server relays each prompt to
Telegram as an inline-keyboard message and resolves with the user's tap.
While "disabled", the port is unbound and Claude Code falls back to its
local prompt.

Bot token discovery: TELEGRAM_BOT_TOKEN env var. Inside Claude Code, this is
populated by plugin.json's mcpServers.env from the userConfig prompt at
install (stored in macOS Keychain). For standalone testing outside Claude
Code, set it manually before launching the server.

Note: userConfig values are NOT auto-injected as CLAUDE_PLUGIN_OPTION_<KEY>
into MCP servers (only into hook commands). They reach the MCP server only
via explicit ${user_config.KEY} substitution in the manifest's mcpServers.env.
"""

import asyncio
import os
import secrets

from aiohttp import web
from mcp.server.fastmcp import FastMCP
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

PORT = 8787
HOOK_TIMEOUT_S = 290  # leaves headroom under the 300s hook timeout in plugin.json

mcp = FastMCP("telegram-buddy")

state: dict = {
    "enabled": False,
    "chat_id": None,
    "http_runner": None,
    "tg_app": None,
    "pending": {},  # request_id -> {"future": Future[str], "text": markdown source}
    "decided": 0,
}


def _load_token() -> str | None:
  return os.environ.get("TELEGRAM_BOT_TOKEN") or None


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
  entry = state["pending"].get(rid)
  if entry and not entry["future"].done():
    decision = "allow" if action == "a" else "deny"
    entry["future"].set_result(decision)
    suffix = "✅ Approved" if decision == "allow" else "❌ Denied"
  else:
    suffix = "⏰ Expired"
  # Re-send the original markdown source so the codeblock keeps rendering on
  # edit. q.message.text is plain text and would lose the formatting.
  prior = entry["text"] if entry else None
  if prior is not None:
    try:
      await q.edit_message_text(text=f"{prior}\n\n{suffix}", parse_mode="Markdown")
    except Exception:
      pass


async def _handle_approve(request: web.Request) -> web.Response:
  payload = await request.json()
  rid = secrets.token_hex(3)
  fut: asyncio.Future = asyncio.get_event_loop().create_future()
  text = _format_request(payload, rid)
  state["pending"][rid] = {"future": fut, "text": text}

  keyboard = InlineKeyboardMarkup(
      [
          [
              InlineKeyboardButton("✅ Approve", callback_data=f"a:{rid}"),
              InlineKeyboardButton("❌ Deny", callback_data=f"d:{rid}"),
          ]
      ]
  )
  try:
    await state["tg_app"].bot.send_message(
        chat_id=state["chat_id"],
        text=text,
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
  except Exception:
    state["pending"].pop(rid, None)
    # Empty body → no hook decision → Claude Code falls back to its local prompt.
    return web.json_response({})

  try:
    decision = await asyncio.wait_for(fut, timeout=HOOK_TIMEOUT_S)
  except asyncio.TimeoutError:
    decision = "ask"
  finally:
    state["pending"].pop(rid, None)

  state["decided"] += 1
  return web.json_response(_hook_response(decision))


def _hook_response(decision: str) -> dict:
  """Shape a PermissionRequest hook response.

  - 'allow' / 'deny' → structured decision that Claude Code applies directly.
  - anything else (e.g. 'ask' from a timeout) → empty object, so Claude Code
    falls back to its local prompt. PermissionRequest's decision field only
    accepts allow/deny; there is no 'ask' or 'defer' to send back.
  """
  if decision == "allow":
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
  if decision == "deny":
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "deny", "message": "Denied via Telegram"},
        }
    }
  return {}


@mcp.tool()
async def enable_telegram() -> str:
  """Start routing Claude Code permission prompts to Telegram.

  Binds 127.0.0.1:8787 and starts a Telegram long-poll. While enabled, any
  tool call that *would have prompted you* (i.e., not auto-allowed by your
  permissions allowlist) is relayed to your phone as an inline-keyboard
  message; tap Approve or Deny to decide. Allowlisted calls run silently.

  The destination chat is taken from the TELEGRAM_BUDDY_CHAT_ID env var,
  which inside Claude Code is populated by the userConfig prompt at install
  time (managed via /plugin → telegram-buddy → Configure options).

  Returns:
    Status string. Errors if port is already bound (another session holds
    it), no bot token is configured, or no chat_id is available.
  """
  if state["enabled"]:
    return f"Already enabled (chat_id={state['chat_id']}). Call disable_telegram first."

  chat_id = os.environ.get("TELEGRAM_BUDDY_CHAT_ID")
  if not chat_id:
    return (
        "No chat_id. Reconfigure the plugin (`/plugin` → telegram-buddy → "
        "Configure options) to set the Telegram Chat ID."
    )

  token = _load_token()
  if not token:
    return (
        "No bot token. Reconfigure the plugin (`/plugin` → telegram-buddy → "
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

  for entry in list(state["pending"].values()):
    fut = entry["future"]
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
