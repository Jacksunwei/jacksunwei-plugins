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
  - enable_telegram(): subscribe THIS session; bind 127.0.0.1:52891 if free
  - disable_telegram(): unsubscribe; shut down listener if no subscribers left
  - status(): report local role (host/standby/off) and listener state

Multi-tenant model: subscriptions are persisted as one-file-per-session
sentinels under tempfile.gettempdir()/telegram-buddy/sessions/. Whichever
Claude Code session's MCP server binds the port becomes the "host" and routes
every PermissionRequest whose session_id has a sentinel. Other subscribed
sessions stand by — their MCP servers run a heartbeat task that retries the
bind whenever the host is gone, so failover is automatic. All subscribed
sessions share the same chat_id (from userConfig — one per user install).

The plugin declares a PermissionRequest HTTP hook (in plugin.json) that POSTs
to http://localhost:52891/approve only when Claude Code is about to prompt the
user — i.e., calls already auto-approved by the allowlist run silently and
never reach this server. While a host is up, the server relays each prompt
to Telegram as an inline-keyboard message and resolves with the user's tap.
While no host is bound, curl gets connection refused and Claude Code falls
back to its local prompt.

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
# Standby retry-bind cadence. Trades failover latency vs. wakeup cost; ~30s
# means a dead host is replaced within 30s on average for any opted-in
# session that was already standby.
HEARTBEAT_INTERVAL_S = 30

# Crude file logging for diagnosing hook deliveries when MCP server stderr
# is not easily reachable. Append-only; harmless if the file grows. Lives
# in the system temp dir (tempfile.gettempdir() honors $TMPDIR — on macOS
# that's a per-user /var/folders/.../T/ path, not the shared /tmp). Per-PID
# so multiple concurrent MCP servers (one per Claude Code session) don't
# trample each other's diagnostics.
LOG_PATH = os.path.join(tempfile.gettempdir(), f"telegram-buddy.{os.getpid()}.log")

# Subscription sentinel directory. One empty file per opted-in session named
# for its session_id. Source of truth for "should the host route this
# session's PermissionRequest?" — survives the host MCP server dying, so a
# new host elected via bind-race instantly knows the full subscriber set.
SUBSCRIPTION_DIR = os.path.join(tempfile.gettempdir(), "telegram-buddy", "sessions")


def _log(msg: str) -> None:
  try:
    with open(LOG_PATH, "a") as f:
      f.write(msg.rstrip() + "\n")
  except Exception:
    pass


# ---------- Subscription sentinels ----------


def _sentinel_path(session_id: str) -> str:
  return os.path.join(SUBSCRIPTION_DIR, session_id)


def _add_subscription(session_id: str) -> None:
  os.makedirs(SUBSCRIPTION_DIR, exist_ok=True)
  # Empty file; presence is the entire signal.
  open(_sentinel_path(session_id), "w").close()


def _remove_subscription(session_id: str) -> None:
  try:
    os.remove(_sentinel_path(session_id))
  except FileNotFoundError:
    pass
  except OSError as e:
    _log(f"sentinel: remove({session_id}) failed: {e}")


def _is_subscribed(session_id: str | None) -> bool:
  if not session_id:
    return False
  return os.path.exists(_sentinel_path(session_id))


def _subscriber_count() -> int:
  try:
    return len(os.listdir(SUBSCRIPTION_DIR))
  except FileNotFoundError:
    return 0


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

  Multi-tenant model: any opted-in session may become the "host" by winning
  the bind race for PORT. The host routes every PermissionRequest whose
  session_id has a sentinel file under SUBSCRIPTION_DIR. Non-host opted-in
  sessions act as standby — their MCP servers run a heartbeat task that
  retries the bind whenever the host is gone, so failover is automatic.
  """

  def __init__(self) -> None:
    # `enabled` now means "this process holds the listener" (i.e. is host).
    # A standby is opted-in (own_session_id set) but not enabled.
    self.enabled: bool = False
    self.chat_id: str | None = None
    # The Claude Code session_id this MCP process is serving. Set in enable(),
    # cleared in disable(). Each MCP server is per-session, so this is at most
    # one value per process. None = this process never opted in (or already
    # disabled). Independent of host status: a standby has own_session_id
    # set but enabled=False.
    self.own_session_id: str | None = None
    self.http_runner: web.AppRunner | None = None
    self.tg_app: Application | None = None
    self.pending: dict[str, PendingApproval] = {}
    self.decided: int = 0
    # Telegram polling lifecycle. Distinct from `enabled` (which only tracks
    # the HTTP listener) because the Telegram bot can take seconds to start
    # polling — Telegram allows one getUpdates consumer per token, and after
    # a host swap the previous host's long-poll can hold the slot for ~30s.
    # Values: "idle" | "starting" | "active" | "failed".
    self.polling_state: str = "idle"
    self.polling_error: str | None = None
    # Background task that retries bind() on a tick while we're standby.
    # None until enable() runs as standby; cleared on promotion or disable.
    self.heartbeat_task: asyncio.Task | None = None

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
    if not _is_subscribed(caller):
      # Not opted in (no sentinel file for this session_id). Empty body →
      # Claude Code falls back to the local prompt for this session. The
      # caller may be a session that never enabled, or one that disabled but
      # whose hook is still configured.
      return web.json_response({})
    if self.tg_app is None or self.chat_id is None:
      # Should be unreachable while the listener is bound (we set both before
      # site.start()), but narrows types for the rest of this handler.
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
    if not _is_subscribed(caller):
      # Not opted in — nothing to clean up; the hook fired for an unrelated
      # session (or one that disabled before this PostToolUse arrived).
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
    """Identity of whoever currently holds the listener.

    Used by other sessions' status / heartbeat probes to confirm the bridge
    is up and which Claude Code session is hosting it.
    """
    return web.json_response(
        {
            "host_session_id": self.own_session_id,
            "pid": os.getpid(),
            "subscribers": _subscriber_count(),
        }
    )

  # ---- Lifecycle (called by MCP tools) ----

  async def enable(self, session_id: str) -> str:
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

    _add_subscription(session_id)
    self.own_session_id = session_id

    if self.enabled:
      return (
          f"Already enabled (host). chat_id={self.chat_id} port={PORT} "
          f"subscribers={_subscriber_count()}."
      )

    if await self._try_become_host(token):
      return (
          f"Enabled (host). Approvals route to chat {chat_id}. "
          f"Listener on 127.0.0.1:{PORT}. Telegram polling starting in "
          f"background — check status."
      )

    # Port held by another MCP server — that host will see our sentinel and
    # route our prompts. We start a heartbeat so we promote ourselves if the
    # current host exits.
    self._ensure_heartbeat(token)
    return (
        f"Enabled (standby). Existing host on 127.0.0.1:{PORT} routes our "
        f"prompts; we'll take over within ~{HEARTBEAT_INTERVAL_S}s if it exits."
    )

  async def _try_become_host(self, token: str) -> bool:
    """Try to bind PORT and become the host. Returns True on success."""
    chat_id = os.environ.get("TELEGRAM_BUDDY_CHAT_ID")
    if not chat_id:
      return False
    app = web.Application()
    app.router.add_post("/approve", self.handle_approve)
    app.router.add_post("/posttooluse", self.handle_posttooluse)
    app.router.add_get("/who", self.handle_who)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    try:
      await site.start()
    except OSError as e:
      await runner.cleanup()
      _log(f"bind: port held: {e}")
      return False

    self.enabled = True
    self.chat_id = str(chat_id)
    self.http_runner = runner
    self.polling_state = "starting"
    self.polling_error = None
    asyncio.create_task(self._start_polling_with_retry(token))
    # If we were running a heartbeat as standby, it's no longer needed.
    self._stop_heartbeat()
    return True

  def _ensure_heartbeat(self, token: str) -> None:
    """Start the standby heartbeat if not already running."""
    if self.heartbeat_task and not self.heartbeat_task.done():
      return
    self.heartbeat_task = asyncio.create_task(self._heartbeat_loop(token))

  def _stop_heartbeat(self) -> None:
    if self.heartbeat_task and not self.heartbeat_task.done():
      self.heartbeat_task.cancel()
    self.heartbeat_task = None

  async def _heartbeat_loop(self, token: str) -> None:
    """Probe /who on a tick; if the listener is gone, race to bind it.

    OS-level bind() is the election mechanism — exactly one process can hold
    the port, so the standby that races first wins and the rest see EADDRINUSE
    and stay standby. The losing standbys remain on the heartbeat for the
    next round.
    """
    base = f"http://127.0.0.1:{PORT}"
    while self.own_session_id is not None and not self.enabled:
      try:
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)
      except asyncio.CancelledError:
        return
      if self.own_session_id is None or self.enabled:
        return
      try:
        async with ClientSession() as client:
          async with client.get(f"{base}/who", timeout=2) as resp:
            await resp.read()
        continue  # listener responding → stay standby
      except Exception:
        pass  # listener is down → try to promote
      if await self._try_become_host(token):
        _log("heartbeat: promoted to host")
        return

  async def _start_polling_with_retry(self, token: str, max_attempts: int = 12) -> None:
    """Start the Telegram bot, retrying on 409 Conflict.

    Telegram permits exactly one getUpdates consumer per token. After a host
    swap the previous host's long-poll can hold the slot for up to ~30s
    before its task notices the stop signal. We retry start_polling with
    backoff, capped, until either we succeed or give up.
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
    _remove_subscription(session_id)
    if self.own_session_id != session_id:
      # Caller is asking us to drop a sentinel that doesn't belong to this
      # MCP process. The sentinel is gone (above), which is the only state
      # we own for that session_id. The host (if any) will simply stop
      # routing for it on its next request.
      return f"Sentinel for {session_id[:8]}… removed."

    self.own_session_id = None
    self._stop_heartbeat()

    if self.enabled and _subscriber_count() == 0:
      # Last subscriber bowed out and we're the host — nothing left to serve.
      await self._shutdown()
      return "Disabled. Listener shut down (no remaining subscribers)."
    if self.enabled:
      # Other sessions still subscribed; keep hosting on their behalf until
      # this MCP process exits or another disable empties the dir.
      return (
          f"Disabled for this session. Listener stays up serving "
          f"{_subscriber_count()} other subscriber(s)."
      )
    return "Disabled."

  def status_string(self, current_session_id: str | None = None) -> str:
    """Local-only status — what THIS MCP server's bridge instance knows."""
    if self.enabled:
      role = "host"
    elif self.own_session_id is not None:
      role = "standby"
    else:
      role = "off"
    parts = [
        f"role={role}",
        f"subscribed={_is_subscribed(current_session_id or self.own_session_id)}",
        f"polling={self.polling_state}",
        f"chat_id={self.chat_id}",
        f"port={PORT}",
        f"subscribers={_subscriber_count()}",
        f"pending={len(self.pending)}",
        f"decided={self.decided}",
    ]
    if self.polling_error:
      parts.append(f"polling_error={self.polling_error!r}")
    return " ".join(parts)

  async def status_with_listener(self, current_session_id: str | None) -> str:
    """Local status + a probe of /who to report the actual listener host.

    The local fields reflect THIS MCP server. The listener might be held by
    a different session's MCP server, in which case we're a standby — /who
    tells us who actually has the port.
    """
    base = self.status_string(current_session_id)
    listener: str
    try:
      async with ClientSession() as client:
        async with client.get(f"http://127.0.0.1:{PORT}/who", timeout=2) as resp:
          info = await resp.json()
      host = info.get("host_session_id")
      pid = info.get("pid")
      if not host:
        mine = "no-host"
      elif current_session_id and host == current_session_id:
        mine = "yes"
      else:
        mine = "no"
      preview = (host[:8] + "…") if host else "?"
      listener = f"listener=up listener_pid={pid} listener_host={preview} mine={mine}"
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
    waiting for getUpdates to return. Closing the listener first frees the
    port so a standby's heartbeat can promote without waiting on the bot
    teardown, which trails asynchronously.
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

    # Free the port FIRST so a standby's next heartbeat can bind right away.
    if runner is not None:
      try:
        await runner.cleanup()
      except Exception:
        pass

    # Bot teardown can be slow (long-poll). Background it so we don't block
    # callers waiting on disable() / process exit.
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
    """Reset listener-related fields. Does NOT touch own_session_id /
    heartbeat — those reflect this process's *subscription*, which is
    independent of whether we currently host the listener (a host that loses
    its bind via _shutdown is still a subscriber until disable() runs).
    """
    self.enabled = False
    self.chat_id = None
    self.http_runner = None
    self.tg_app = None
    self.polling_state = "idle"
    self.polling_error = None


# ---------- MCP wiring ----------

mcp = FastMCP("telegram-buddy")
_bridge = TelegramBridge()


@mcp.tool()
async def enable_telegram(session_id: str) -> str:
  """Subscribe this Claude Code session to Telegram approval routing.

  Multi-tenant: any number of sessions can subscribe simultaneously. Whichever
  session's MCP server happens to bind 127.0.0.1:PORT first becomes the
  "host" and routes every subscribed session's PermissionRequest to Telegram.
  The other subscribed sessions stand by and auto-promote (via a 30s heartbeat
  + bind race) if the host's process exits.

  Subscription is recorded as a sentinel file under tempfile.gettempdir()/
  telegram-buddy/sessions/<session_id>; the host reads this dir per request
  to decide whether to route.

  Destination chat comes from the TELEGRAM_BUDDY_CHAT_ID env var, populated
  by the userConfig prompt at install (managed via /plugin → telegram-buddy
  → Configure options). All subscribers share the same chat — there's only
  one user per install.

  Args:
    session_id: Current Claude Code session_id (supplied by
      /telegram-buddy:on via ${CLAUDE_SESSION_ID}). Used as the sentinel
      filename and matched against hook payloads' session_id at routing time.
  """
  return await _bridge.enable(session_id)


@mcp.tool()
async def disable_telegram(session_id: str) -> str:
  """Unsubscribe THIS session from Telegram approval routing.

  Removes the sentinel file. If we're the host AND no other subscribers
  remain, the listener shuts down. If other subscribers exist, we keep
  hosting on their behalf until our MCP process exits or another disable
  empties the dir.

  Args:
    session_id: Current Claude Code session_id (supplied by
      /telegram-buddy:off via ${CLAUDE_SESSION_ID}).
  """
  return await _bridge.disable(session_id)


@mcp.tool()
async def status(session_id: str | None = None) -> str:
  """Report local bridge state plus a probe of the actual listener host.

  Local fields (role / subscribed / polling / chat_id / port / subscribers /
  pending / decided) reflect THIS MCP server. The trailing `listener=...`
  segment is a live GET /who against 127.0.0.1:PORT, so it shows the actual
  current host even if we're a standby. With `session_id` supplied, `mine=
  yes/no` tells you whether your session is the one hosting.
  """
  return await _bridge.status_with_listener(session_id)


if __name__ == "__main__":
  mcp.run()
