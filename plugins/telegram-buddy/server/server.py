#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp[cli]>=1.27.0",
#   "aiohttp>=3.9",
#   "python-telegram-bot>=21.0",
#   "claude-agent-sdk>=0.1.0",
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
import html
import json
import os
import secrets
import tempfile

from aiohttp import ClientSession, web
from claude_agent_sdk.types import HookJSONOutput, PermissionRequestHookInput
from mcp.server.fastmcp import FastMCP
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

PORT = 8787
HOOK_TIMEOUT_S = (
    28700  # leaves headroom under the 28800s (8h) hook timeout in plugin.json
)
# Crude file logging for diagnosing hook deliveries when MCP server stderr
# is not easily reachable. Append-only; harmless if the file grows. Lives
# in the system temp dir (tempfile.gettempdir() honors $TMPDIR — on macOS
# that's a per-user /var/folders/.../T/ path, not the shared /tmp). Per-PID
# so multiple concurrent MCP servers (one per Claude Code session) don't
# trample each other's diagnostics.
LOG_PATH = os.path.join(tempfile.gettempdir(), f"telegram-buddy.{os.getpid()}.log")


def _log(msg: str) -> None:
  try:
    with open(LOG_PATH, "a") as f:
      f.write(msg.rstrip() + "\n")
  except Exception:
    pass


# Cap each interpolated field. Long attacker payloads otherwise push the
# Approve/Deny buttons off-screen on mobile.
MAX_FIELD_LEN = 1024

mcp = FastMCP("telegram-buddy")

state: dict = {
    "enabled": False,
    "chat_id": None,
    # The Claude Code session_id that called enable_telegram. Hook payloads
    # carry session_id; we filter so only the owning session's tool calls get
    # routed through Telegram. Other sessions get a silent local prompt
    # fallback (see S3 in the design table).
    "owner_session_id": None,
    "http_runner": None,
    "tg_app": None,
    # request_id -> {"future", "text" (HTML), "message_id", "input_key"}
    "pending": {},
    "decided": 0,
}


def _load_token() -> str | None:
  return os.environ.get("TELEGRAM_BOT_TOKEN") or None


def _esc(value, max_len: int = MAX_FIELD_LEN) -> str:
  """HTML-escape a value, truncating to bound message length.

  Critical: every interpolated field MUST go through this. Telegram parses
  the message body as HTML, and an unescaped attacker-controlled field
  (tool_name, command, file_path, cwd, etc.) could otherwise close a tag,
  inject formatting, and spoof a different request to the operator.
  """
  s = str(value)
  if len(s) > max_len:
    s = s[:max_len] + "…[truncated]"
  return html.escape(s)


def _format_request(payload: PermissionRequestHookInput, request_id: str) -> str:
  tool = payload.get("tool_name", "?")
  inp = payload.get("tool_input") or {}
  cwd = payload.get("cwd", "?")
  preview = ""
  if isinstance(inp, dict):
    if "command" in inp:
      preview = f"\n<pre>{_esc(inp['command'])}</pre>"
    elif "file_path" in inp:
      preview = f"\n<code>{_esc(inp['file_path'])}</code>"
    elif "url" in inp:
      preview = f"\n{_esc(inp['url'])}"
  # request_id is hex from secrets.token_hex — no escape needed.
  return (
      f"🔧 <b>{_esc(tool)}</b> <code>[{request_id}]</code>"
      f"{preview}\n"
      f"<i>cwd</i>: <code>{_esc(cwd)}</code>"
  )


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
  # Re-send the original HTML source so the formatting persists on edit.
  # q.message.text is plain text and would lose it. The suffix is a literal
  # safe string (✅/❌/⏰) so no escaping needed; if you ever interpolate
  # user content into it, run it through _esc().
  prior = entry["text"] if entry else None
  if prior is not None:
    try:
      await q.edit_message_text(text=f"{prior}\n\n{suffix}", parse_mode="HTML")
    except Exception:
      pass


def _input_key(tool_name: str, tool_input) -> str:
  """Stable key for matching a PermissionRequest pending entry to a later
  PostToolUse event for the same tool call.

  Claude Code does not surface a stable tool_use_id in the PermissionRequest
  payload, so we key on (tool_name, an identifying slice of tool_input).
  Whole-input JSON would be brittle — PostToolUse can carry extra/normalized
  fields that PermissionRequest doesn't (description, normalized whitespace,
  etc.). Pick one identifying field per known tool; fall back to tool_name
  alone for unknown tools.
  """
  if not isinstance(tool_input, dict):
    return tool_name
  for field in ("command", "file_path", "url"):
    if field in tool_input:
      try:
        return f"{tool_name}|{field}={json.dumps(tool_input[field], default=str)}"
      except Exception:
        return f"{tool_name}|{field}={tool_input[field]!r}"
  return tool_name


async def _handle_posttooluse(request: web.Request) -> web.Response:
  """Cleanup endpoint for PostToolUse events.

  Fires after a tool actually runs (regardless of how the permission was
  granted: Telegram tap, local prompt, or auto-allow). For each matching
  pending entry, edits the Telegram message to 'Handled in terminal' and
  resolves the still-open PermissionRequest hook so it stops blocking.
  """
  try:
    payload = await request.json()
  except Exception as e:
    _log(f"posttooluse: failed to parse json: {e}")
    return web.json_response({})
  caller = payload.get("session_id")
  if caller != state["owner_session_id"]:
    # S3: not our session — silently ignore so this hook doesn't disturb
    # other Claude Code sessions running in parallel.
    return web.json_response({})
  tool_name = payload.get("tool_name", "?")
  key = _input_key(tool_name, payload.get("tool_input") or {})
  pending_keys = [e.get("input_key") for e in state["pending"].values()]
  _log(
      f"posttooluse: tool={tool_name} key={key!r} "
      f"pending={len(state['pending'])} pending_keys={pending_keys}"
  )
  for rid, entry in list(state["pending"].items()):
    if entry.get("input_key") != key:
      continue
    _log(f"posttooluse: matched rid={rid}")
    await _edit_telegram(rid, "🤝 Resolved without Telegram")
    fut = entry.get("future")
    if fut and not fut.done():
      # 'ask' → empty hook response → Claude Code uses whatever decision the
      # local flow already made. The response is moot since the tool ran.
      fut.set_result("ask")
    break  # one match is enough; if there are duplicates, later events drain them
  return web.json_response({})


async def _edit_telegram(rid: str, suffix: str) -> None:
  """Append a status suffix to the pending Telegram message for `rid`.

  Used from outside the callback handler (e.g. when Claude Code dropped
  the HTTP request because the operator decided locally).
  """
  entry = state["pending"].get(rid)
  tg_app = state["tg_app"]
  if not entry or tg_app is None or state["chat_id"] is None:
    return
  prior = entry.get("text")
  message_id = entry.get("message_id")
  if prior is None or message_id is None:
    return
  try:
    await tg_app.bot.edit_message_text(
        chat_id=state["chat_id"],
        message_id=message_id,
        text=f"{prior}\n\n{suffix}",
        parse_mode="HTML",
    )
  except Exception:
    pass


async def _handle_approve(request: web.Request) -> web.Response:
  # Untyped at the wire (any process on localhost can POST), but the structure
  # we expect is PermissionRequestHookInput; .get()s tolerate missing fields.
  payload: PermissionRequestHookInput = await request.json()
  caller = payload.get("session_id")
  if caller != state["owner_session_id"]:
    # S3: not our session — return empty hook output so Claude Code falls
    # back to its default permission flow (i.e., local prompt for the
    # non-owner session). Non-owner sessions never see Telegram.
    return web.json_response({})
  rid = secrets.token_hex(3)
  fut: asyncio.Future = asyncio.get_event_loop().create_future()
  text = _format_request(payload, rid)

  keyboard = InlineKeyboardMarkup(
      [
          [
              InlineKeyboardButton("✅ Approve", callback_data=f"a:{rid}"),
              InlineKeyboardButton("❌ Deny", callback_data=f"d:{rid}"),
          ]
      ]
  )
  try:
    sent = await state["tg_app"].bot.send_message(
        chat_id=state["chat_id"],
        text=text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )
  except Exception:
    # Empty body → no hook decision → Claude Code falls back to its local prompt.
    return web.json_response({})

  state["pending"][rid] = {
      "future": fut,
      "text": text,
      "message_id": sent.message_id,
      "input_key": _input_key(
          payload.get("tool_name", "?"), payload.get("tool_input") or {}
      ),
  }

  # Wait for Telegram tap, the PostToolUse cleanup (operator answered the
  # local prompt and the tool ran), or the 8h hook timeout. The cleanup
  # handler resolves the future with 'ask', which falls through to an empty
  # hook response — by then the tool has already run, so the response is
  # discarded by Claude Code.
  try:
    decision = await asyncio.wait_for(fut, timeout=HOOK_TIMEOUT_S)
  except asyncio.TimeoutError:
    decision = "ask"
  finally:
    state["pending"].pop(rid, None)

  state["decided"] += 1
  return web.json_response(_hook_response(decision))


def _hook_response(decision: str) -> HookJSONOutput | dict:
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


async def _handle_who(_request: web.Request) -> web.Response:
  """Returns the identity of whoever currently owns the bridge.

  Used by other Claude Code sessions' enable_telegram (S5 take-over flow)
  to learn who has the listener before requesting handover.
  """
  return web.json_response(
      {
          "owner_session_id": state["owner_session_id"],
          "pid": os.getpid(),
      }
  )


async def _handle_release(_request: web.Request) -> web.Response:
  """Handover endpoint (S5): another session is taking over.

  Schedules listener shutdown after the response flies so the requester
  can immediately retry bind. The caller is trusted (localhost-only,
  same-uid).
  """

  async def shutdown_after_response():
    await asyncio.sleep(0.1)  # let the OK response fly first
    await _shutdown_listener()

  asyncio.create_task(shutdown_after_response())
  return web.json_response({"ok": True})


async def _shutdown_listener() -> None:
  """Common teardown: stops Telegram poller, closes HTTP listener,
  resolves pending Futures with 'ask', clears state. Used by both
  disable_telegram and the /release handover."""
  if not state["enabled"]:
    return
  for entry in list(state["pending"].values()):
    fut = entry["future"]
    if not fut.done():
      fut.set_result("ask")
  state["pending"].clear()

  tg = state["tg_app"]
  try:
    if tg is not None and tg.updater is not None:
      await tg.updater.stop()
    if tg is not None:
      await tg.stop()
      await tg.shutdown()
  except Exception:
    pass

  if state["http_runner"] is not None:
    await state["http_runner"].cleanup()

  state.update(
      enabled=False,
      chat_id=None,
      owner_session_id=None,
      http_runner=None,
      tg_app=None,
  )


async def _ensure_port_free(_session_id: str) -> str | None:
  """If port PORT is bound by another listener, ask it to release (S5).

  Returns None on success (port is free or was freed). Returns an error
  string if the existing listener doesn't respond to /release.
  """
  base = f"http://127.0.0.1:{PORT}"
  try:
    async with ClientSession() as client:
      # /who is purely informational — log who we're displacing.
      try:
        async with client.get(f"{base}/who", timeout=2) as resp:
          info = await resp.json()
          _log(
              f"ensure_port_free: existing listener pid={info.get('pid')}"
              f" owner={info.get('owner_session_id')}"
          )
      except Exception:
        # No listener responding on /who → port is probably free or held by
        # something that isn't us. Either way, let the bind try report.
        return None
      try:
        async with client.post(f"{base}/release", timeout=2) as resp:
          await resp.read()
      except Exception as e:
        return f"Existing listener didn't accept /release: {e}"
  except Exception as e:
    return f"Could not contact existing listener: {e}"

  # Wait for the port to actually free (the previous listener does shutdown
  # asynchronously; OS may also leave the socket in TIME_WAIT briefly).
  for _ in range(40):  # up to 2s
    try:
      _, writer = await asyncio.open_connection("127.0.0.1", PORT)
      writer.close()
      await writer.wait_closed()
    except (ConnectionRefusedError, OSError):
      return None  # port is free
    await asyncio.sleep(0.05)
  return f"Port {PORT} still in use after take-over request"


@mcp.tool()
async def enable_telegram(session_id: str) -> str:
  """Start routing this Claude Code session's permission prompts to Telegram.

  Multi-session safe: only THIS session's tool calls (matched by session_id
  in the hook payload) get routed; other sessions on the same machine fall
  back to local prompts. If another session already holds the bridge, this
  call requests handover via the /release endpoint and takes over.

  The destination chat is taken from the TELEGRAM_BUDDY_CHAT_ID env var,
  which inside Claude Code is populated by the userConfig prompt at install
  time (managed via /plugin → telegram-buddy → Configure options).

  Args:
    session_id: The current Claude Code session_id. The /telegram-buddy:on
      slash command supplies this via ${CLAUDE_SESSION_ID} substitution.
      Without it, hooks have no way to know whose session a tool call
      belongs to and would route everything indiscriminately.

  Returns:
    Status string.
  """
  if state["enabled"] and state["owner_session_id"] == session_id:
    return f"Already enabled (chat_id={state['chat_id']})."
  if state["enabled"] and state["owner_session_id"] != session_id:
    return (
        "This MCP server is already bound to a different session. "
        "Call disable_telegram first to clear local state."
    )

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

  # S5 take-over: if port is held by another listener, ask it to release.
  takeover_err = await _ensure_port_free(session_id)
  if takeover_err:
    return f"Take-over failed: {takeover_err}"

  app = web.Application()
  app.router.add_post("/approve", _handle_approve)
  app.router.add_post("/posttooluse", _handle_posttooluse)
  app.router.add_get("/who", _handle_who)
  app.router.add_post("/release", _handle_release)
  runner = web.AppRunner(app)
  await runner.setup()
  site = web.TCPSite(runner, "127.0.0.1", PORT)
  try:
    await site.start()
  except OSError as e:
    await runner.cleanup()
    return f"Could not bind port {PORT}: {e}."

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
      owner_session_id=session_id,
      http_runner=runner,
      tg_app=tg_app,
  )
  return (
      f"Enabled. Approvals route to chat {chat_id}. " f"Listener on 127.0.0.1:{PORT}."
  )


@mcp.tool()
async def disable_telegram(session_id: str) -> str:
  """Stop routing approvals to Telegram for THIS session.

  Behavior depends on local state vs. the listener's actual owner (S7):
    - Local state says we own the bridge AND we are the owner → normal
      shutdown.
    - Local state says we own the bridge but the listener disagrees (we
      were taken over) → silently clear local state.
    - Local state says we don't own anything but a listener exists → ping
      it; if it claims us, send /release; otherwise tell the user it
      belongs to a different session.
    - Nothing local, no listener → "Not enabled."

  Args:
    session_id: The current Claude Code session_id (supplied by the
      /telegram-buddy:off slash command via ${CLAUDE_SESSION_ID}).
  """
  # Case (a): we are the listener owner. Normal shutdown.
  if state["enabled"] and state["owner_session_id"] == session_id:
    await _shutdown_listener()
    return "Disabled. Hooks will fall back to local prompts."

  # Case (b): local state inconsistent — we think we're enabled but for
  # another session. Treat as "we were taken over" and clear local state.
  if state["enabled"] and state["owner_session_id"] != session_id:
    state.update(
        enabled=False,
        chat_id=None,
        owner_session_id=None,
        http_runner=None,
        tg_app=None,
    )
    return "Local state cleared (was bound to a different session)."

  # Case (c): nothing locally — but maybe a stale registration with the
  # active listener? Check.
  base = f"http://127.0.0.1:{PORT}"
  try:
    async with ClientSession() as client:
      async with client.get(f"{base}/who", timeout=2) as resp:
        info = await resp.json()
      owner = info.get("owner_session_id")
      if owner == session_id:
        # Stale: listener thinks we own it but our state says otherwise.
        # Send /release so it cleans up.
        async with client.post(f"{base}/release", timeout=2) as r:
          await r.read()
        return "Released stale listener registration."
      preview = (owner[:8] + "…") if owner else "?"
      return (
          f"Not your bridge to disable; the active listener is owned by "
          f"another session ({preview})."
      )
  except Exception:
    return "Not enabled. Nothing to do."


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
