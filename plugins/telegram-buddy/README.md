# telegram-buddy

Approve Claude Code permission prompts from Telegram, so a long-running session can keep moving while you're away from
the terminal.

## How to use it

Once installed, the daily loop is two commands.

When you're about to step away from the terminal:

```
/telegram-buddy:on
```

(or just say to Claude: *"Enable Telegram approvals."*)

Now any tool call that *would have prompted you* in the terminal gets sent to your Telegram chat instead — tap
**Approve** or **Deny** on your phone, and Claude continues. Calls already covered by your `permissions.allow` rules
keep running silently; your phone only buzzes for things Claude would have actually asked you about.

When you're back:

```
/telegram-buddy:off
```

(or *"Disable Telegram approvals."*)

Future prompts go back to the terminal. That's the whole loop. Check current state with `/telegram-buddy:status`.

### Typical user scenarios

**1. Step away mid-task.** The dishwasher's beeping, the kid needs pickup, the coffee run can't wait — and Claude is
halfway through a long refactor. Flip on Telegram approvals, walk out the door. When Claude hits the next `git push` or
`rm`, your phone buzzes; tap **Approve** from the sidewalk and the run keeps moving instead of stalling on an empty
terminal until you're back.

**2. Read without breaking flow.** Claude is mid-stream printing a wall of analysis you actually want to absorb. The
moment it hits a permission prompt, the terminal yanks your focus into a modal and your reading flow shatters. With
Telegram on, the prompt diverts to your phone — thumb-tap **Approve**, eyes never leave the scrollback.

## Why this exists

The official `telegram` plugin's `claude/channel/permission` capability (and Claude Code's `auto` mode) require the
direct Anthropic API — on Vertex AI / Bedrock / Foundry / 3rd-party models, Claude Code drops them silently.
telegram-buddy uses a `PermissionRequest` HTTP hook instead: works on any backend, fires only when Claude Code would
have prompted you, so allowlisted calls stay silent.

It also fits a different session shape. The official plugin extends the whole session to Telegram — input, output, and
approvals all reachable from the phone in parallel with the terminal. telegram-buddy mirrors only the permission modal,
so your scrollback and editor stay the sole I/O surface.

## Setup

### 1. Create a Telegram bot

Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow the prompts → save the HTTP API token.

### 2. Install the plugin

From the [`jacksunwei-plugins`](../..) marketplace:

```bash
/plugin marketplace add jacksunwei/jacksunwei-plugins
/plugin install telegram-buddy@jacksunwei-plugins
```

Claude Code prompts for two values during install:

- **Telegram Bot Token** — paste the token from BotFather. Stored in your macOS Keychain (or
  `~/.claude/.credentials.json` on Linux/Windows); never written to disk in plain text.
- **Telegram Chat ID** — your numeric Telegram user ID. Get it by messaging [@userinfobot](https://t.me/userinfobot) —
  copy the `Id` value it returns.

To re-enter or change either later: `/plugin list` → telegram-buddy → Configure options.

> Heads-up: Claude Code v2.1.84 has a known bug where the install prompts can be skipped. If you don't see them, use the
> `Configure options` flow above to set the values.

### 3. Open the bot's chat

DM your bot once (any message) — Telegram requires you to initiate before a bot can message you back.

## Tool reference

The plugin exposes three MCP tools. Each has a slash command wrapper for quick use; you can also call the tool by its
full name or just ask Claude in plain language.

| Slash command            | MCP tool           | Direct MCP name                                               | What it does                                                                                                |
| ------------------------ | ------------------ | ------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `/telegram-buddy:on`     | `enable_telegram`  | `mcp__plugin_telegram-buddy_telegram-buddy__enable_telegram`  | Bind `127.0.0.1:8787`, start the Telegram poller. Sends to the chat ID from your install/Configure options. |
| `/telegram-buddy:off`    | `disable_telegram` | `mcp__plugin_telegram-buddy_telegram-buddy__disable_telegram` | Stop the listener and poller. Future prompts go back to the terminal.                                       |
| `/telegram-buddy:status` | `status`           | `mcp__plugin_telegram-buddy_telegram-buddy__status`           | Show enabled/disabled, chat ID, port, pending count, decided count.                                         |

Tip: add the three MCP names to `permissions.allow` in `~/.claude/settings.json` so the slash commands run silently
instead of prompting you to approve each invocation.

## What the Telegram message looks like

```
🔧 Bash [a1b2c3]
git push origin main
cwd: /Users/you/your-repo
[✅ Approve]  [❌ Deny]
```

Tap a button. The bubble updates to show ✅ Approved or ❌ Denied, the keyboard disappears, and Claude proceeds (or the
call is blocked).

## Edge cases

- **Telegram timeout** (no tap within 8 hours) → hook returns no decision, Claude falls back to the local prompt. Note:
  the originating Claude Code session is blocked the whole time.
- **Phone offline / Telegram unreachable** → same as timeout.
- **Two sessions try to enable** → the second one errors cleanly: `Could not bind port 8787...`. Disable in the other
  session first.
- **Forgot to disable before closing the session** → the MCP server is killed when Claude Code exits, which frees the
  port automatically.

## Configuration

The bot token and chat ID are set via the plugin's install prompts (see Setup). To reconfigure later: `/plugin list` →
telegram-buddy → Configure options.

For **standalone testing** outside Claude Code, the server also honors these env vars as fallbacks:

| Variable                 | Notes                                                       |
| ------------------------ | ----------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`     | Bot token.                                                  |
| `TELEGRAM_BUDDY_CHAT_ID` | Chat ID for `enable_telegram` to send approval messages to. |

## How it works

1. **Permission-only hook**: the plugin declares a `PermissionRequest` HTTP hook (in `plugin.json`) that fires *only*
   when Claude Code is about to prompt you — calls already covered by your `permissions.allow` rules are auto-approved
   upstream and never reach the hook.
1. **On-demand bridge**: the MCP server only binds port 8787 when you call `enable_telegram`. When disabled, the port is
   unbound, the hook gets connection-refused, and Claude Code falls back to its normal local prompt.
1. **Telegram round-trip**: while enabled, the server forwards each prompt as an inline-keyboard message to your chat;
   the button callback resolves the pending request and the hook returns `decision: {behavior: "allow" | "deny"}` to
   Claude Code.

## Limitations

- Only one Claude Code session can hold the listener at a time. A second session calling `enable_telegram` errors out
  cleanly — disable in the other first.
- Inline-keyboard buttons in DMs only. Don't add the bot to groups.
- If Telegram times out (no tap within 8 hours), the hook returns no decision and Claude Code falls back to its local
  prompt.

## License

Apache 2.0 — see [LICENSE](../../LICENSE).
