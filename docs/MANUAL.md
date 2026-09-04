# mailbrief — the daily guide

What to do each day, in plain terms. For setup and the full command reference,
see the README.

## Getting your briefing

Three ways — use whichever is convenient:

- **From your phone (Telegram):** send `brief morning` or `brief eod` to your
  bot. The briefing arrives in chat within a minute.
- **At the computer (Claude Code):** open Claude Code in the project folder and
  type `/brief morning` or `/brief eod`.
- **One command:** `python -m mailbrief brief` — collects, prepares, and opens
  the briefing (written prose if Claude Code is installed, otherwise a plain
  digest).

## What the two briefings answer

- **Morning** — what landed overnight, what needs you today, what's waiting on
  your reply, what can wait.
- **End of day** — what happened today, who wanted what from you, what you owe,
  what's still open, what to pick up tomorrow.

Briefings are in English, plain text. If nothing is urgent, the briefing says
"Nothing urgent today." — that's a statement, not a bug.

## How the mail gets there

Two scheduled jobs run each day (07:00 and 18:00) to fetch new mail and prepare
it, and the Telegram poll runs every minute. You don't need to wait for the
schedule: `/brief` and the Telegram command fetch the latest mail themselves, so
the briefing is never built from stale data.

## Where things live

- Mail data and settings are in `%USERPROFILE%\.mailbrief\` — never in the
  project folder, never in git.
- The mailbox password lives in Windows Credential Manager. It is never printed
  or stored as text.
- Finished briefings are saved as dated Markdown files in the folder you set as
  `briefing_dir`.

## When something looks wrong

| Symptom | What it means / what to do |
|---|---|
| "0 new messages" | Usually nothing new since the last run. If you expected mail, run `/brief` again. |
| Connection / login error | Wrong server or password in config, or no internet. Run `python -m mailbrief check`. |
| Bot doesn't reply | Your chat id isn't in the allowlist, the token isn't stored, or the poll isn't scheduled. See the README's Telegram section. |
| "awaiting my reply: yes" on everything | A hint, not a fact — the tool can't always see your sent mail. Trust what the email itself says. |

## FAQ

### Does `/brief` fetch mail again, or read what the scheduled runs fetched?

It fetches again, first, every time. `/brief` never builds a briefing from stale
data — it runs the collector first, then reads the packet. If the scheduled runs
already stored everything, the extra fetch finds nothing new and costs a few
seconds.

### Then why fetch automatically at all, if `/brief` fetches anyway?

So mail is never lost even if you skip briefings for days, and so each briefing
is fast (the store is warm, only a little is new). The scheduled job is free to
run unattended; a briefing spends Claude, so it only runs when you ask.

### Does the once-a-minute Telegram poll slow my computer down?

No. Each tick starts a small program that makes one instant check with Telegram
and exits — under a second, mostly network wait, near-zero CPU. The heavy part
(Claude) runs only when you send a `brief` command.

### What is the SQLite database for?

Bookkeeping, not storage. It remembers how far each mailbox was read (so a run
never re-fetches everything) and which messages were seen before (so a crash
never stores a duplicate). Mail content lives in plain text files.

### If I skip a day, is that mail lost? Do the two briefings overlap?

Nothing is lost. Each briefing covers everything since the previous briefing run
of either kind. A message appears in exactly one briefing — the window it landed
in. Morning and evening never double-report the same mail.

### Which AI model writes my briefing?

None is fixed by the program. The briefing is written by whichever Claude Code
model you are running — your current Claude Code session, or the default `claude`
command for the Telegram path. Everything upstream (fetch, clean, classify,
thread) is deterministic code and never varies.
