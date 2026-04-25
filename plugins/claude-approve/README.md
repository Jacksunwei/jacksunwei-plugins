# claude-approve

Approve Claude Code permission prompts from Telegram, so a long-running session can keep moving while you're away from the terminal.

When **enabled**, every `Bash` / `Edit` / `Write` / `WebFetch` call sends an inline-keyboard message to your Telegram chat; tap **Approve** or **Deny** to decide. Claude Code's local prompt is replaced by your tap.

When **disabled** (the default), nothing happens — the hook gets connection-refused and Claude Code falls back to its normal local prompt. That's the "back at desk" mode.

## Why this exists

Claude Code's built-in `auto` permission mode and the official `telegram` plugin's `claude/channel/permission` capability both require the direct Anthropic API. On Vertex AI / Bedrock / Foundry, Claude Code silently drops the relevant client-side capabilities. This plugin works around that with a `PreToolUse` HTTP hook that doesn't depend on those capabilities.

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

## Usage

When you're about to step away, ask Claude:

> Enable Telegram approvals.

Claude calls `enable_telegram(chat_id=...)`. From then on, every matched tool call buzzes your phone.

When you're back:

> Disable Telegram approvals.

Claude calls `disable_telegram()`. Future tool calls prompt locally again.

## Configuration

The bot token and chat ID are set via the plugin's install prompts (see Setup). To reconfigure later: `/plugin list` → claude-approve → Configure options.

For **standalone testing** outside Claude Code, the server also honors these env vars as fallbacks:

| Variable                 | Notes                                                                                       |
| ------------------------ | ------------------------------------------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`     | Bot token. Also read from `~/.claude/channels/claude-approve/.env` (key=value) as a final fallback. |
| `CLAUDE_APPROVE_CHAT_ID` | Default chat ID for `enable_telegram` if no arg is passed.                                  |

## How it works

1. **Always-on hook**: the plugin declares a `PreToolUse` HTTP hook (in `plugin.json`) that POSTs every matched tool call to `http://localhost:8787/approve`.
2. **On-demand bridge**: the MCP server only binds port 8787 when you call `enable_telegram`. When disabled, the port is unbound, the hook gets connection-refused, and Claude Code falls back to its normal prompt.
3. **Telegram round-trip**: while enabled, the server forwards each request as an inline-keyboard message to your chat; the button callback resolves the pending request and the hook returns the decision JSON to Claude Code.

## Limitations

- Only one Claude Code session can hold the listener at a time. A second session calling `enable_telegram` errors out cleanly — disable in the other first.
- Inline-keyboard buttons in DMs only. Don't add the bot to groups.
- Reads (`Read`, `Grep`, `Glob`) are intentionally not intercepted, so your phone doesn't buzz on every file open.

## License

Apache 2.0 — see [LICENSE](../../LICENSE).
