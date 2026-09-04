# Telegram briefing bot - design

Approved by the user 2026-09-01 (answers recorded in "Open questions"
below). User drives the briefing from Telegram: sends "brief morning" or
"brief eod" to the bot, gets the full briefing text back in chat. Local
`/brief` in Claude Code keeps working unchanged.

## Goal

- User sends `brief morning` / `brief eod` to a Telegram bot.
- The bot triggers the existing `/brief` skill (Claude-authored briefing),
  then sends the resulting briefing text back to the user's chat.
- Delivery is the full briefing text, split into multiple messages when
  over Telegram's 4096-char limit. Plain text, no parse_mode (markdown
  escaping mangles Greek text; not worth it).
- Nothing else. No scheduled auto-send, no buttons, no attachments.

## Design decisions

| Topic | Decision | Rationale |
|---|---|---|
| Bot loop | One-shot poll, scheduled every 1 minute via schtasks | Project rule: a long-lived daemon is harder to repair than a script that exits. Latency ≤ ~60s, plus a manual `python -m mailbrief telegram` for immediate use. |
| Briefing author | Bot spawns `claude -p` (headless Claude Code) with a prompt that loads the brief skill | Keeps "briefing written by Claude" and the deterministic packet contract, zero API cost. The machine already has Claude Code logged in. |
| Getting the text back | Bot waits for claude to exit, then reads `<briefing_dir>/YYYY-MM-DD Morning.md` / `Evening.md` | Deterministic path, no parsing of claude's stdout. the folder copy is a side effect, matching manual `/brief`. |
| Bot token | Windows Credential Manager via keyring, service "mailbrief", username "telegram-bot-token". Registered in the log redaction filter | Same rule as the mailbox password: never in repo, config, logs, or CLI args. |
| Who may command | Chat-ID allowlist in config.toml (`telegram_chat_ids = [...]`), discovered by a `telegram-whoami` command (getUpdates) | Not secret, but gates control: any other chat is ignored. |
| Concurrency | Lock file while a briefing run is in flight; new command while locked replies "briefing already running" | schtasks runs can overlap if a run outlives the poll interval (claude -p can take minutes). |
| Spawning claude | `subprocess.run` with cwd `c:\repos\mailbrief`, prompt: "Follow .claude/skills/brief/SKILL.md exactly and produce the <morning|eod> briefing." stdout to logs/telegram-claude.out.txt | cwd matters (skill paths are repo-relative); output logged for debugging. |
| Headless permissions | `--allowedTools` grants exactly Read, Write, and `Bash(python -m mailbrief collect|packet|config:*)` | `claude -p` has nobody to approve tool calls; without this the skill's first `collect` blocks and the run dies. Anything outside the grant still asks and blocks. |
| Briefing write path | `--add-dir <briefing_dir>` on the spawn (`briefing_dir` from config) | The briefing lands outside the repo; without `--add-dir`, `-p` denies the Write call and quietly prints the briefing instead of saving it. |
| Fallback if claude fails | Reply with the failure and the fix (e.g. "claude unavailable - run /brief locally") | Every failure names what to fix, per project style. |

## Data flow

```
Telegram message "brief morning"
   -> schtasks "Mailbrief telegram poll" (every minute)
   -> mailbrief telegram: getUpdates(offset) -> chat id in allowlist?
       yes -> lock free?
           yes -> spawn: claude -p "follow brief skill, morning"
                  wait for exit
                  read briefing file (poll up to ~10 min for it to appear)
                  sendMessage (chunked at 4096)
                  reply "briefing sent" + first chunk order
           no  -> reply "briefing already running"
       no  -> ignore silently
   -> exits
```

Ordering: messages arrive in order (Telegram sequence numbers); each chunk
is sent in order, so the briefing reads correctly.

## Files

```
mailbrief/telegram.py    poll once: getUpdates, dispatch, sendMessage (chunked)
mailbrief/__main__.py    telegram, telegram-whoami commands
config.template.toml     telegram_chat_ids = [] placeholder
tests/test_telegram.py   chunking, dispatch, allowlist - with a fake HTTP transport
```

Stdlib only: `urllib.request` against api.telegram.org. No new packages.

## Security

- Token never in source, config, logs, CLI args, or error messages. Logger
  redaction via `log.register_secret` (the existing mechanism).
- Chat-ID allowlist is the only gate for triggering runs. Bot ignores
  everything else silently (no info leak about the system).
- Sending briefings to Telegram carries sender names and subjects to
  Telegram (third party). Equivalent exposure was already accepted for
  a synced folder; this extends it to Telegram.
- The bot runs as the user's session (same schtasks interactive model as
  the collector). It inherits the mailbox credential access via keyring;
  it never sees or prints any credential.

## Testing

- `tests/test_telegram.py`: chunking at 4096 with paragraph-aware splits,
  command dispatch, allowlist rejection, lock-file behavior, chunk order.
  HTTP layer faked (no network, no token in tests).
- Live proof (stage style, real mailbox): create bot via BotFather, store
  token, `telegram-whoami`, allowlist own chat, send `brief morning` from
  the phone, show the briefing arriving in Telegram.

## Open questions - resolved 2026-09-01

1. Bot token storage: **--file fallback** approved (token on line 1 of a
   temp file, `store-credential`-style command imports it into Credential
   Manager and deletes the file; getpass hangs in this machine's
   consoles).
2. Poll interval: **every 1 minute** via schtasks + StartWhenAvailable
   (user changed from the initial 5-minute choice on 2026-09-02).
3. `claude -p` cost: **accepted** (same cost as a manual /brief run).

## Explicitly out of scope

- No scheduled auto-send of briefings (still manual trigger, now from
  Telegram or Claude Code).
- No message buttons / inline keyboards / attachments.
- No multi-user support (single-user allowlist only).
