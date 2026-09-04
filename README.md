# mailbrief

Personal email briefing assistant. Reads mailboxes **read-only** over IMAP,
stores mail locally in a plain greppable format, classifies it, and produces
morning and end-of-day briefing packets.

The prose is written by **Claude Code** (the deterministic "packet" is the raw
material; Claude turns it into readable prose). Everything upstream — fetch,
parse, classify, thread — is plain Python with no AI.

mailbrief only **reads** mail. It can never send, delete, move, or mark anything
read.

## How it works

```
IMAP (read-only, EXAMINE)
  -> local store (plain text + JSONL + sqlite dedupe index)
  -> parse (charsets, HTML-to-text, quoted-reply stripping)
  -> classify (direct / cc / bulk, with the triggering header kept as evidence)
  -> threads (with "awaiting your reply")
  -> packet (deterministic Markdown, no AI)
  -> Claude Code /brief skill -> prose briefing
        -> terminal, the briefing_dir folder, and/or Telegram
```

The packet is the contract between the two halves: changing the briefing format
never requires touching the fetcher, and adding a mailbox is a config edit, not
a code change.

## Claude Code — how the prose is written

mailbrief's own output is the **packet**: a deterministic Markdown file of
threads, labels, and bodies. The prose is written by **Claude Code**, following
a skill file shipped with this repo: `.claude/skills/brief/SKILL.md`.

That skill does four things every time it runs:

1. `python -m mailbrief collect` — fetch the latest mail.
2. `python -m mailbrief packet --window morning|eod` — regenerate the packet.
3. Read the packet.
4. Write the prose briefing to `<briefing_dir>/YYYY-MM-DD Morning.md` (or
   `Evening.md`).

The skill also encodes the writing rules (one entry per thread, urgency only
with quoted evidence, "Nothing urgent today." when nothing is urgent). Edit that
file to change the style.

**To use it yourself:**

1. Install Claude Code (`npm install -g @anthropic-ai/claude-code`, which needs
   Node.js, or the native installer) and sign in to your account.
2. Open Claude Code in this project's folder.
3. Type `/brief morning` or `/brief eod`.

Claude reads the skill, runs the collector + packet, and writes the prose. There
is no API key and no code to change — it reuses whichever Claude Code account is
signed in on the machine. `briefing_dir` must be set in config so the skill
knows where to write.

**The Telegram path runs the same thing headless.** When you send `brief morning`
to the bot, the poll launches `claude -p` with the same skill, waits for the
briefing file to appear in `briefing_dir`, and sends it back to your chat. So the
only requirements for Telegram prose are: Claude Code installed and signed in,
and `briefing_dir` set in config.

## Ways to get the briefing

1. **Telegram (phone).** Send `brief morning` or `brief eod` to your bot; the
   full briefing arrives in chat. No computer needed.
2. **Terminal.** Run `/brief morning` or `/brief eod` in Claude Code from this
   repo.
3. **A folder.** The finished briefing is saved as a dated Markdown file in
   `briefing_dir` — a folder you choose.

## Prerequisites

| Thing | Notes |
|---|---|
| Windows 10/11 | uses `schtasks`, `pythonw`, Windows Credential Manager |
| Python 3.11+ | on PATH |
| `keyring` | the only third-party package; stores the password + bot token |
| Claude Code CLI | required for the prose briefing; sign in to your own account |
| An IMAP mailbox | must allow password login over IMAP |
| Telegram bot | only for the phone-triggered briefing |

Note: the mailbox must support password login over IMAP (most providers do). If
your provider issues an app password for third-party clients, use that in place
of your normal password.

## Setup

One time, about 5 minutes. Step 3 is a double-click — no terminal needed.

1. **Copy the template and fill in your details.**
   - Copy `config.template.toml` to `config.toml` (in this same folder).
   - Open `config.toml` in Notepad and replace the example values with yours.
     The ones that matter most are under `[[accounts]]`: `host` (your mail
     server) and `username` (your email address). Every line has a comment
     saying what it is.

2. **Put your mailbox password in a file.** Create `password.txt` in this
   folder and put the password on the first line. (If your provider issues an
   "app password" for third-party clients, use that instead of your normal
   password.) Skip this step if your mailbox needs no password.

3. **Run the setup script.** Double-click `setup.bat` — or, if you prefer a
   terminal, run `python setup.py`. It installs the one dependency (`keyring`),
   creates the folders it needs, moves `config.toml` and `password.txt` into
   place, and deletes `password.txt`.

4. **Done.** To confirm it works, run:
   ```
   python -m mailbrief check
   ```
   You should see your folders listed with message counts.

That's it. **Daily routine:** in Claude Code, type `/brief morning` or
`/brief eod` — that command is a Claude Code *skill*, so it exists only inside
Claude Code. Without Claude Code, run `python -m mailbrief brief` instead (it
collects new mail and produces the digest on its own; with Claude Code
installed it also writes the prose). Over Telegram, message `brief morning` to
your bot (after the Telegram setup below).

## Configuration

`config.toml` lives at `%USERPROFILE%\.mailbrief\config.toml` (run
`python -m mailbrief config` to print it). Top-level keys must stay **above**
`[[accounts]]` — TOML absorbs anything written after a `[[accounts]]` header:

```toml
addresses = ["you@example.com"]     # your own addresses (from `whoami`)
bulk_domains = []                   # sender domains always treated as bulk
never_bulk_senders = []             # addresses never treated as bulk
direct_max_recipients = 5           # To+Cc ceiling for "direct"
packet_max_chars = 40000            # packet character budget
telegram_chat_ids = []              # chats allowed to command the bot
briefing_dir = "C:/path/to/briefings"   # where the finished briefing is written

[[accounts]]
name = "primary"
host = "imap.example.com"
port = 993
username = "you@example.com"
auth = "password"
folders = ["INBOX", "Sent"]
```

## Telegram — the phone briefing

1. Create a bot with [@BotFather](https://t.me/BotFather) in Telegram; copy the
   token.
2. Store the token (a file is the reliable path — getpass hangs on some consoles):
   ```
   python -m mailbrief store-telegram-token --file C:\path\to\token.txt
   ```
3. Message your bot once, then discover your chat id:
   ```
   python -m mailbrief telegram-whoami
   ```
   Put the printed id into `telegram_chat_ids` in `config.toml`.
4. Schedule the poll (see below), or run one poll manually to test:
   ```
   python -m mailbrief telegram
   ```
5. Send `brief morning` (plain text, no slash) to the bot. Within a minute the
   poll picks it up and spawns Claude, which runs the collector + packet and
   writes the prose briefing into `briefing_dir`; the bot reads it back and
   sends it to you — chunked at Telegram's 4096-char limit.

The allowlist (`telegram_chat_ids`) is the only gate: any other chat is ignored.
A lock file prevents overlapping runs; a run still in flight replies
"briefing already running".

## Scheduling

The tool is a set of scripts that exit, not a long-lived daemon. Schedule them
with Windows Task Scheduler (`schtasks`). Use the full path to `python.exe` /
`pythonw.exe` — PATH is not reliable under Task Scheduler.

**Collector** — twice a day (07:00 and 18:00), keeps the store warm:

```
schtasks /Create /TN "Mailbrief morning collect" /SC DAILY /ST 07:00 /TR "cmd /c (cd /d C:\path\to\mailbrief && C:\Python313\python.exe -m mailbrief collect) >> %USERPROFILE%\.mailbrief\logs\collect-0700.out.txt 2>&1"
schtasks /Create /TN "Mailbrief evening collect" /SC DAILY /ST 18:00 /TR "cmd /c (cd /d C:\path\to\mailbrief && C:\Python313\python.exe -m mailbrief collect) >> %USERPROFILE%\.mailbrief\logs\collect-1800.out.txt 2>&1"
```

**Telegram poll** — every minute (`.pyw` runs under pythonw, no console window):

```
schtasks /Create /TN "Mailbrief telegram poll" /SC MINUTE /MO 1 /TR "C:\Python313\pythonw.exe C:\path\to\mailbrief\mailbrief\telegram_poll.pyw"
```

The collector tasks are optional in the sense that `/brief` and the Telegram
poll fetch first anyway — but they make briefings fast and ensure no mail is
lost if you skip a few days.

## Commands

- `brief [--window morning|eod]` — one-command briefing: collect + packet, plus Claude prose if available
- `config` — print the config location
- `store-credential [account] [--file PATH]` — store the mailbox password
- `check` — connect to every account: TLS, cert, capabilities, folder counts
- `fetch [--account NAME] [--folder F] [--limit N] [--dry-run]`
- `collect [--account NAME]` — fetch → parse → classify
- `parse [--account NAME]` — clean stored bodies
- `show UID` — one message: headers, charset, cleaned body
- `classify [--account NAME] [--explain]` — label messages, showing evidence
- `whoami` — rank the addresses that deliver to this mailbox
- `threads [--account NAME]` — thread assembly debug view
- `packet [--window morning|eod] [--since ISO] [--no-cutoff] [--out FILE]`
- `telegram` — poll Telegram once for briefing commands
- `telegram-whoami` — list chat ids that have messaged the bot
- `store-telegram-token [--file PATH]` — store the bot token

## Customizing

- **Schedule** — the Task Scheduler triggers above.
- **Recipients / "what's yours"** — `addresses`.
- **What counts as bulk** — `bulk_domains`, `never_bulk_senders`.
- **Digest size** — `packet_max_chars` (over budget, cc/bulk bodies trim first;
  direct bodies are always kept; the header states what was cut).
- **Where the briefing lands** — `briefing_dir`.
- **Who may command the bot** — `telegram_chat_ids`.
- **The prose style / structure** — `.claude/skills/brief/SKILL.md`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `check` fails to connect / login | Wrong host/username in config, no password stored, or no internet. Fix config, re-run `store-credential`. |
| Briefing says "0 new messages" | Usually genuinely nothing new. Run `/brief` again (it re-collects first). |
| Briefing empty-wrong but you have mail | Suspect fetch: `python -m mailbrief fetch --dry-run`. |
| Bot doesn't reply | `telegram_chat_ids` missing your id, token not stored, or the poll task isn't scheduled. Run `python -m mailbrief telegram` and check `logs/telegram-poll.out.txt`. |
| Bot replies "briefing already running" | A previous run is still in flight; wait, or delete the stale lock at `%USERPROFILE%\.mailbrief\locks\telegram.lock`. |
| Bot replies "briefing failed" / "did not appear" | The Claude step failed. Check `logs/telegram-claude.out.txt`, or run `/brief` locally. |

## Security

- Mailboxes are opened with `EXAMINE` only. `STORE`, `COPY`, `EXPUNGE`,
  `APPEND`, and `MOVE` never appear in source; `tests/test_readonly.py` enforces
  it.
- The password and bot token live in Windows Credential Manager via `keyring`,
  never in source, config, logs, CLI args, or briefing output. The logger has a
  redaction filter.
- TLS verification stays on.
- Mail data and the real config live in `%USERPROFILE%\.mailbrief\`, never in
  the repo.
- Sending a briefing to Telegram carries sender names and subjects to a third
  party (Telegram). Enable it only if you're OK with that.

## Development

```
python -m unittest discover -s tests
```

113 tests (parser, classifier, threads, packet, Telegram bot, read-only guard),
no network, no credentials. See `docs/plan.md` (design) and
`docs/plan-telegram.md` (Telegram design) for the full rationale.
