# CLAUDE.md - mailbrief

Personal email briefing assistant. IMAP collector, classifier, and Claude-authored
morning and end-of-day briefings.

**Read `docs/plan.md` first.** It is the approved design and it records the confirmed
server facts, so you do not need to re-probe the mail server. Design was approved
2026-08-25.

**Then read `docs/HANDOFF.md`.** It records build state: Stage 1 (connect) is
proven, Stage 2 (fetch) is next, plus machine quirks and current config facts.

## Hard rules

### Read-only against every mailbox

Open folders with `EXAMINE`, never a writable `SELECT`. No sending, no deleting, no
moving, no marking read, no drafts. The IMAP commands `STORE`, `COPY`, `EXPUNGE`,
`APPEND`, and `MOVE` must never appear in this codebase; `tests/test_readonly.py`
enforces that by grepping the source.

### Credentials

Never in source, config, logs, briefing output, CLI arguments, or error messages.

The mailbox password lives in Windows Credential Manager and is read at runtime via
`keyring`. The user stores it themselves with `python -m mailbrief store-credential`,
which prompts through `getpass`. You never see it, type it, echo it, or ask for it in
chat. The logger carries a redaction filter.

### Dependencies

Python stdlib only, with one approved exception: `keyring`. Anything else requires
asking first and justifying why the stdlib cannot do it. `msal` is pre-approved for
the Microsoft 365 account when that work begins, and not before.

### Data location

Code lives in this repo. Data lives in `%USERPROFILE%\.mailbrief\`. Raw mail, the real
config, state, and logs never enter the repo, and `.gitignore` must keep it that way.

### Encoding

Every file is UTF-8 without BOM. Python writes with `encoding='utf-8'`. Do not use
PowerShell `Set-Content` to write project files: in PS 5.1 it emits a BOM.

## Working style

- Prove each stage against the real mailbox before starting the next: connect, fetch,
  parse, classify, brief. Show actual output from the actual mailbox, never mock data.
- Exit code zero is not evidence. Do not claim a stage works without showing what it
  produced.
- Every failure must print a message that names what to fix.
- Explicit config beats convention. A few plain readable files beat an elegant
  abstraction. Optimise for being repairable at 8am.
- Classification labels are hints for the briefing, not verdicts. Always keep the
  evidence (which header fired) in the output so wrong calls are visible.
- Never invent urgency in a briefing. No stated deadline, explicit ask, escalation, or
  repeat chase means no urgency. If nothing is urgent, say so.

## Scope discipline

Adding a mailbox is a config edit, never a code change. Nothing in the collector may
reference a specific webmail product, mail provider, or the user's email address. Those belong in config.

Changing the briefing format must never require touching the fetcher. The briefing
packet is the contract between the two halves.
