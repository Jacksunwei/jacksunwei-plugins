# claude-approve

Approve Claude Code permission prompts from Telegram, so a long-running session can keep moving while you're away from the terminal.

## How to use it

Once installed, the daily loop is two commands.

When you're about to step away from the terminal:

```
/claude-approve:on
```

(or just say to Claude: *"Enable Telegram approvals."*)

Now any tool call that *would have prompted you* in the terminal gets sent to your Telegram chat instead — tap **Approve** or **Deny** on your phone, and Claude continues. Calls already covered by your `permissions.allow` rules keep running silently; your phone only buzzes for things Claude would have actually asked you about.

When you're back:

```
/claude-approve:off
```

(or *"Disable Telegram approvals."*)

Future prompts go back to the terminal. That's the whole loop. Check current state with `/claude-approve:status`.

## Why this exists

Claude Code's built-in `auto` permission mode and the official `telegram` plugin's `claude/channel/permission` capability both require the direct Anthropic API. On Vertex AI / Bedrock / Foundry, Claude Code silently drops the relevant client-side capabilities. This plugin works around that with a `PermissionRequest` HTTP hook that doesn't depend on those capabilities — and only fires when Claude Code would have prompted you anyway, so allowlisted calls run silently.

## Setup

### 1. Create a Telegram bot

Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow the prompts → save the HTTP API token.

### 2. Install the plugin

From the [`jacksunwei-plugins`](../..) marketplace:

```bash
/plugin marketplace add jacksunwei/jacksunwei-plugins
/plugin install claude-approve@jacksunwei-plugins
```

Claude Code prompts for two values during install:

- **Telegram Bot Token** — paste the token from BotFather. Stored in your macOS Keychain (or `~/.claude/.credentials.json` on Linux/Windows); never written to disk in plain text.
- **Telegram Chat ID** — your numeric Telegram user ID. Get it by messaging [@userinfobot](https://t.me/userinfobot) — copy the `Id` value it returns.

To re-enter or change either later: `/plugin list` → claude-approve → Configure options.

> Heads-up: Claude Code v2.1.84 has a known bug where the install prompts can be skipped. If you don't see them, use the `Configure options` flow above to set the values.

### 3. Open the bot's chat

DM your bot once (any message) — Telegram requires you to initiate before a bot can message you back.

## Tool reference

The plugin exposes three MCP tools. Each has a slash command wrapper for quick use; you can also call the tool by its full name or just ask Claude in plain language.

| Slash command            | MCP tool           | Direct MCP name                                               | What it does                                                                                            |
| ------------------------ | ------------------ | ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `/claude-approve:on`     | `enable_telegram`  | `mcp__plugin_claude-approve_claude-approve__enable_telegram`  | Bind `127.0.0.1:8787`, start the Telegram poller. Optional `chat_id` arg overrides the install default. |
| `/claude-approve:off`    | `disable_telegram` | `mcp__plugin_claude-approve_claude-approve__disable_telegram` | Stop the listener and poller. Future prompts go back to the terminal.                                   |
| `/claude-approve:status` | `status`           | `mcp__plugin_claude-approve_claude-approve__status`           | Show enabled/disabled, chat ID, port, pending count, decided count.                                     |

Tip: add the three MCP names to `permissions.allow` in `~/.claude/settings.json` so the slash commands run silently instead of prompting you to approve each invocation.

## What the Telegram message looks like

```
🔧 Bash [a1b2c3]
git push origin main
cwd: /Users/you/your-repo
[✅ Approve]  [❌ Deny]
```

Tap a button. The bubble updates to show ✅ Approved or ❌ Denied, the keyboard disappears, and Claude proceeds (or the call is blocked).

## Edge cases

- **Telegram timeout** (no tap within 5 minutes) → hook returns no decision, Claude falls back to the local prompt.
- **Phone offline / Telegram unreachable** → same as timeout.
- **Two sessions try to enable** → the second one errors cleanly: `Could not bind port 8787...`. Disable in the other session first.
- **Forgot to disable before closing the session** → the MCP server is killed when Claude Code exits, which frees the port automatically.

## Configuration

The bot token and chat ID are set via the plugin's install prompts (see Setup). To reconfigure later: `/plugin list` → claude-approve → Configure options.

For **standalone testing** outside Claude Code, the server also honors these env vars as fallbacks:

| Variable                 | Notes                                                      |
| ------------------------ | ---------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`     | Bot token.                                                 |
| `CLAUDE_APPROVE_CHAT_ID` | Default chat ID for `enable_telegram` if no arg is passed. |

## How it works

1. **Permission-only hook**: the plugin declares a `PermissionRequest` HTTP hook (in `plugin.json`) that fires *only* when Claude Code is about to prompt you — calls already covered by your `permissions.allow` rules are auto-approved upstream and never reach the hook.
1. **On-demand bridge**: the MCP server only binds port 8787 when you call `enable_telegram`. When disabled, the port is unbound, the hook gets connection-refused, and Claude Code falls back to its normal local prompt.
1. **Telegram round-trip**: while enabled, the server forwards each prompt as an inline-keyboard message to your chat; the button callback resolves the pending request and the hook returns `decision: {behavior: "allow" | "deny"}` to Claude Code.

## Limitations

- Only one Claude Code session can hold the listener at a time. A second session calling `enable_telegram` errors out cleanly — disable in the other first.
- Inline-keyboard buttons in DMs only. Don't add the bot to groups.
- If Telegram times out (no tap within 5 minutes), the hook returns no decision and Claude Code falls back to its local prompt.

## License

Apache 2.0 — see [LICENSE](../../LICENSE).
