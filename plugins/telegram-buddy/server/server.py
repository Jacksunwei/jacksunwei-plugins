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
  - enable_telegram(): bind 127.0.0.1:52891 + start Telegram poll
  - disable_telegram(): stop both
  - status(): report current state

The plugin declares a PermissionRequest HTTP hook (in plugin.json) that POSTs
to http://localhost:52891/approve only when Claude Code is about to prompt the
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
from dataclasses import dataclass

from aiohttp import ClientSession, web
from claude_agent_sdk.types import HookJSONOutput, PermissionRequestHookInput
from mcp.server.fastmcp import FastMCP
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

PORT = 52891
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


# ---------- Message rendering ----------

# Cap each interpolated field. Long attacker payloads otherwise push the
# Approve/Deny buttons off-screen on mobile.
MAX_FIELD_LEN = 1024

# Suffix labels appended to the original Telegram message body once a request
# resolves. All literal-safe (✅/❌/⏰/🤝 + ASCII); if you ever interpolate
# user content into one of these, route it through _esc() first.
SUFFIX_APPROVED = "✅ Approved"
SUFFIX_DENIED = "❌ Denied"
SUFFIX_EXPIRED = "⏰ Expired"
SUFFIX_RESOLVED_LOCALLY = "🤝 Resolved without Telegram"


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


# ---------- Bridge state ----------


@dataclass
class PendingApproval:
  """A PermissionRequest awaiting resolution.

  - future: resolved by the Telegram callback (allow/deny), the PostToolUse
    cleanup (ask), or the 8h timeout.
  - text: the original HTML body, kept around so we can re-render it with a
    status suffix on edit (Telegram only exposes plain text via callbacks).
  - message_id: Telegram message we sent for this request, needed to edit it.
  - input_key: stable matching key (see _input_key) for cross-referencing
    against the later PostToolUse event for the same tool call.
  """

  future: asyncio.Future
  text: str
  message_id: int
  input_key: str


class TelegramBridge:
  """Owns the listener + Telegram poller + pending-request map.

  Single instance per process. MCP tools and HTTP handlers are thin wrappers
  that delegate here so the state and lifecycle stay in one place.
  """

  def __init__(self) -> None:
    self.enabled: bool = False
    self.chat_id: str | None = None
    # The Claude Code session_id that called enable_telegram. Hook payloads
    # carry session_id; we filter so only the owning session's tool calls get
    # routed through Telegram. Other sessions get a silent local prompt
    # fallback (see S3 in the design table).
    self.owner_session_id: str | None = None
    self.http_runner: web.AppRunner | None = None
    self.tg_app: Application | None = None
    self.pending: dict[str, PendingApproval] = {}
    self.decided: int = 0
    # Telegram polling lifecycle. Distinct from `enabled` (which only tracks
    # the HTTP listener) because the Telegram bot can take seconds to start
    # polling — Telegram allows one getUpdates consumer per token, and after
    # a take-over the previous owner's long-poll can hold the slot for ~30s.
    # Values: "idle" | "starting" | "active" | "failed".
    self.polling_state: str = "idle"
    self.polling_error: str | None = None

  # ---- Telegram callback ----

  async def on_callback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data:
      return
    await q.answer()
    # Only the chat owner can decide. Defensive even though the inline keyboard
    # is only sent to chat_id and Telegram doesn't allow forwarding it intact.
    if self.chat_id and q.from_user and str(q.from_user.id) != str(self.chat_id):
      return
    try:
      action, rid = q.data.split(":", 1)
    except ValueError:
      return
    entry = self.pending.get(rid)
    if entry and not entry.future.done():
      decision = "allow" if action == "a" else "deny"
      entry.future.set_result(decision)
      suffix = SUFFIX_APPROVED if decision == "allow" else SUFFIX_DENIED
    else:
      suffix = SUFFIX_EXPIRED
    # Re-send the original HTML source so the formatting persists on edit.
    # q.message.text is plain text and would lose it.
    prior = entry.text if entry else None
    if prior is not None:
      try:
        await q.edit_message_text(text=f"{prior}\n\n{suffix}", parse_mode="HTML")
      except Exception:
        pass

  # ---- HTTP handlers ----

  async def handle_approve(self, request: web.Request) -> web.Response:
    # Untyped at the wire (any process on localhost can POST), but the structure
    # we expect is PermissionRequestHookInput; .get()s tolerate missing fields.
    payload: PermissionRequestHookInput = await request.json()
    caller = payload.get("session_id")
    if caller != self.owner_session_id:
      # S3: not our session — return empty hook output so Claude Code falls
      # back to its default permission flow (i.e., local prompt for the
      # non-owner session). Non-owner sessions never see Telegram.
      return web.json_response({})
    if self.tg_app is None or self.chat_id is None:
      # Should be unreachable while the listener is bound (enable() sets both
      # before site.start()), but narrows types for the rest of this handler.
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
      sent = await self.tg_app.bot.send_message(
          chat_id=self.chat_id,
          text=text,
          reply_markup=keyboard,
          parse_mode="HTML",
      )
    except Exception:
      # Empty body → no hook decision → Claude Code falls back to its local prompt.
      return web.json_response({})

    self.pending[rid] = PendingApproval(
        future=fut,
        text=text,
        message_id=sent.message_id,
        input_key=_input_key(
            payload.get("tool_name", "?"), payload.get("tool_input") or {}
        ),
    )

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
      self.pending.pop(rid, None)

    self.decided += 1
    return web.json_response(_hook_response(decision))

  async def handle_posttooluse(self, request: web.Request) -> web.Response:
    """Cleanup endpoint for PostToolUse events.

    Fires after a tool actually runs (regardless of how the permission was
    granted: Telegram tap, local prompt, or auto-allow). For each matching
    pending entry, edits the Telegram message to 'Resolved without Telegram'
    and resolves the still-open PermissionRequest hook so it stops blocking.
    """
    try:
      payload = await request.json()
    except Exception as e:
      _log(f"posttooluse: failed to parse json: {e}")
      return web.json_response({})
    caller = payload.get("session_id")
    if caller != self.owner_session_id:
      # S3: not our session — silently ignore so this hook doesn't disturb
      # other Claude Code sessions running in parallel.
      return web.json_response({})
    tool_name = payload.get("tool_name", "?")
    key = _input_key(tool_name, payload.get("tool_input") or {})
    pending_keys = [e.input_key for e in self.pending.values()]
    _log(
        f"posttooluse: tool={tool_name} key={key!r} "
        f"pending={len(self.pending)} pending_keys={pending_keys}"
    )
    for rid, entry in list(self.pending.items()):
      if entry.input_key != key:
        continue
      _log(f"posttooluse: matched rid={rid}")
      await self._edit_message(rid, SUFFIX_RESOLVED_LOCALLY)
      if not entry.future.done():
        # 'ask' → empty hook response → Claude Code uses whatever decision the
        # local flow already made. The response is moot since the tool ran.
        entry.future.set_result("ask")
      break  # one match is enough; if there are duplicates, later events drain them
    return web.json_response({})

  async def handle_who(self, _request: web.Request) -> web.Response:
    """Returns the identity of whoever currently owns the bridge.

    Used by other Claude Code sessions' enable_telegram (S5 take-over flow)
    to learn who has the listener before requesting handover.
    """
    return web.json_response(
        {
            "owner_session_id": self.owner_session_id,
            "pid": os.getpid(),
        }
    )

  async def handle_release(self, _request: web.Request) -> web.Response:
    """Handover endpoint (S5): another session is taking over.

    Schedules listener shutdown after the response flies so the requester
    can immediately retry bind. The caller is trusted (localhost-only,
    same-uid).
    """

    async def shutdown_after_response():
      await asyncio.sleep(0.1)  # let the OK response fly first
      await self._shutdown()

    asyncio.create_task(shutdown_after_response())
    return web.json_response({"ok": True})

  # ---- Lifecycle (called by MCP tools) ----

  async def enable(self, session_id: str) -> str:
    if self.enabled and self.owner_session_id == session_id:
      return f"Already enabled (chat_id={self.chat_id})."
    if self.enabled and self.owner_session_id != session_id:
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

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
      return (
          "No bot token. Reconfigure the plugin (`/plugin` → telegram-buddy → "
          "Configure options) to set the Telegram Bot Token, or set the "
          "TELEGRAM_BOT_TOKEN env var for standalone testing."
      )

    # S5 take-over: if port is held by another listener, ask it to release.
    takeover_err = await self._ensure_port_free()
    if takeover_err:
      return f"Take-over failed: {takeover_err}"

    app = web.Application()
    app.router.add_post("/approve", self.handle_approve)
    app.router.add_post("/posttooluse", self.handle_posttooluse)
    app.router.add_get("/who", self.handle_who)
    app.router.add_post("/release", self.handle_release)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    try:
      await site.start()
    except OSError as e:
      await runner.cleanup()
      return f"Could not bind port {PORT}: {e}."

    # Mark enabled and store HTTP listener immediately so the new owner is
    # visible via /who right away. Telegram polling starts in a background
    # task — Telegram allows one getUpdates consumer per token, so after a
    # take-over we may hit 409 Conflict for up to ~30s while the previous
    # owner's long-poll drains. The retry loop handles that.
    self.enabled = True
    self.chat_id = str(chat_id)
    self.owner_session_id = session_id
    self.http_runner = runner
    self.polling_state = "starting"
    self.polling_error = None
    asyncio.create_task(self._start_polling_with_retry(token))

    return (
        f"Enabled. Approvals route to chat {chat_id}. "
        f"Listener on 127.0.0.1:{PORT}. Telegram polling starting in "
        f"background — check status."
    )

  async def _start_polling_with_retry(self, token: str, max_attempts: int = 12) -> None:
    """Start the Telegram bot, retrying on 409 Conflict.

    Telegram permits exactly one getUpdates consumer per token. After a
    take-over the previous owner's long-poll can hold the slot for up to
    ~30s before its task notices the stop signal. We retry start_polling
    with backoff, capped, until either we succeed or give up.
    """
    tg_app = Application.builder().token(token).build()
    tg_app.add_handler(CallbackQueryHandler(self.on_callback))
    try:
      await tg_app.initialize()
      await tg_app.start()
    except Exception as e:
      _log(f"polling: bot bootstrap failed: {e}")
      self.polling_state = "failed"
      self.polling_error = str(e)
      return
    self.tg_app = tg_app

    updater = tg_app.updater
    if updater is None:
      self.polling_state = "failed"
      self.polling_error = "Telegram Application has no updater"
      return

    for attempt in range(1, max_attempts + 1):
      try:
        await updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["callback_query"],
        )
        self.polling_state = "active"
        self.polling_error = None
        _log(f"polling: started on attempt {attempt}")
        return
      except Exception as e:
        msg = str(e)
        is_conflict = "409" in msg or "Conflict" in msg
        if not is_conflict:
          _log(f"polling: non-409 failure on attempt {attempt}: {e}")
          self.polling_state = "failed"
          self.polling_error = msg
          return
        delay = min(0.5 + 0.5 * attempt, 5.0)
        _log(f"polling: 409 on attempt {attempt}, retrying in {delay:.1f}s")
        await asyncio.sleep(delay)

    self.polling_state = "failed"
    self.polling_error = (
        f"409 Conflict persisted after {max_attempts} attempts — another "
        "instance is still polling on this token."
    )
    _log(self.polling_error)

  async def disable(self, session_id: str) -> str:
    # Case (a): we are the listener owner. Normal shutdown.
    if self.enabled and self.owner_session_id == session_id:
      await self._shutdown()
      return "Disabled. Hooks will fall back to local prompts."

    # Case (b): local state inconsistent — we think we're enabled but for
    # another session. Treat as "we were taken over" and clear local state.
    if self.enabled and self.owner_session_id != session_id:
      self._clear()
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

  def status_string(self) -> str:
    """Local-only status — what THIS MCP server's bridge instance knows."""
    parts = [
        f"enabled={self.enabled}",
        f"polling={self.polling_state}",
        f"chat_id={self.chat_id}",
        f"port={PORT}",
        f"pending={len(self.pending)}",
        f"decided={self.decided}",
    ]
    if self.polling_error:
      parts.append(f"polling_error={self.polling_error!r}")
    return " ".join(parts)

  async def status_with_listener(self, current_session_id: str | None) -> str:
    """Local status + a probe of /who to report the actual listener owner.

    Useful when the caller's session may not be the owner — the local fields
    reflect THIS MCP server, but the listener might be held by a different
    session that took over via S5.
    """
    base = self.status_string()
    listener: str
    try:
      async with ClientSession() as client:
        async with client.get(f"http://127.0.0.1:{PORT}/who", timeout=2) as resp:
          info = await resp.json()
      owner = info.get("owner_session_id")
      pid = info.get("pid")
      if not owner:
        mine = "no-owner"
      elif current_session_id and owner == current_session_id:
        mine = "yes"
      else:
        mine = "no"
      preview = (owner[:8] + "…") if owner else "?"
      listener = f"listener=up listener_pid={pid} listener_owner={preview} mine={mine}"
    except Exception:
      listener = "listener=down"
    return f"{base} | {listener}"

  # ---- Internal helpers ----

  async def _edit_message(self, rid: str, suffix: str) -> None:
    """Append a status suffix to the pending Telegram message for `rid`.

    Used from outside the callback handler (e.g. when Claude Code dropped
    the HTTP request because the operator decided locally).
    """
    entry = self.pending.get(rid)
    if not entry or self.tg_app is None or self.chat_id is None:
      return
    try:
      await self.tg_app.bot.edit_message_text(
          chat_id=self.chat_id,
          message_id=entry.message_id,
          text=f"{entry.text}\n\n{suffix}",
          parse_mode="HTML",
      )
    except Exception:
      pass

  async def _shutdown(self) -> None:
    """Free the port first, then tear down Telegram in the background.

    The Telegram updater's long-poll can hold the connection for ~30s
    waiting for getUpdates to return, which previously blocked the HTTP
    runner cleanup and made take-over (S5) flaky. Closing the listener
    first lets the new owner bind immediately; the bot teardown trails
    asynchronously.
    """
    if not self.enabled:
      return
    for entry in list(self.pending.values()):
      if not entry.future.done():
        entry.future.set_result("ask")
    self.pending.clear()

    runner = self.http_runner
    tg = self.tg_app
    self._clear()  # mark disabled before any awaits

    # Free the port FIRST so the take-over requester can bind right away.
    if runner is not None:
      try:
        await runner.cleanup()
      except Exception:
        pass

    # Bot teardown can be slow (long-poll). Background it so /release returns
    # quickly and we don't block the take-over.
    if tg is not None:

      async def _tg_shutdown():
        try:
          if tg.updater is not None:
            await tg.updater.stop()
          await tg.stop()
          await tg.shutdown()
        except Exception:
          pass

      asyncio.create_task(_tg_shutdown())

  def _clear(self) -> None:
    self.enabled = False
    self.chat_id = None
    self.owner_session_id = None
    self.http_runner = None
    self.tg_app = None
    self.polling_state = "idle"
    self.polling_error = None

  async def _ensure_port_free(self) -> str | None:
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


# ---------- MCP wiring ----------

mcp = FastMCP("telegram-buddy")
_bridge = TelegramBridge()


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
  return await _bridge.enable(session_id)


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
  return await _bridge.disable(session_id)


@mcp.tool()
async def status(session_id: str | None = None) -> str:
  """Report local bridge state plus a probe of who actually owns the listener.

  The local fields (enabled / polling / chat_id / pending / decided) reflect
  THIS MCP server's state. The trailing `listener=...` segment is the result
  of a live GET /who against 127.0.0.1:52891, so it shows the actual current
  owner even if our local state is stale (e.g. we were taken over by another
  session). If `session_id` is supplied (the /telegram-buddy:status slash
  command passes it via ${CLAUDE_SESSION_ID}), `mine=yes/no` tells you at a
  glance whether the bridge belongs to your session.
  """
  return await _bridge.status_with_listener(session_id)


if __name__ == "__main__":
  mcp.run()
