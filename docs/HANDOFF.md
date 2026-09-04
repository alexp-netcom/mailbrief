# HANDOFF - build state for the next session

State as of 2026-09-02. **All five stages complete and proven against the
real mailbox: connect, fetch, parse, classify, and Stage 5 - the `/brief`
skill plus scheduling (plan sections 10-11).** Stage 6 is now complete too:
the Telegram briefing bot (`docs/plan-telegram.md`, approved 2026-09-01,
built and proven 2026-09-02). The full approved design is `docs/plan.md`;
this file records what happened since it was written.

## What exists

```
mailbrief/            package: config, auth, imapclient, parse, classify,
                      threads, packet, store, log, telegram, __main__
config.template.toml  placeholders only, committed
.gitignore            repo holds no data, ever
README.md             commands + hard rules
tests/test_readonly.py  greps source for STORE/COPY/EXPUNGE/APPEND/MOVE - passes
tests/test_parse.py      stage 3 parser tests (20 tests) - passes
tests/test_classify.py   stage 4 classifier tests (24 tests) - passes
tests/test_threads.py    stage 5 thread tests (24 tests) - passes
tests/test_packet.py     stage 5 packet tests (16 tests) - passes
tests/test_telegram.py   stage 6 telegram bot tests (24 tests) - passes
tests/fixtures/          3 synthetic messages - committed
.claude/skills/brief/    the /brief skill (stage 5)
docs/plan.md          the approved design
docs/plan-telegram.md the approved Telegram bot design (stage 6)
docs/MANUAL.md        plain-terms user manual (daily routine, troubleshooting)
docs/HANDOFF.md       this file
```

109 tests total, all green (`python -m unittest discover -s tests`), plus
test_readonly.py. The store leaked one sqlite connection per read
(ResourceWarnings in tests): `store._connect` is now a context manager that
closes on exit. No behaviour change; fetch/parse/classify untouched.

Real config lives at `%USERPROFILE%\.mailbrief\config.toml` (never in repo).
Classifier hints are TOP-LEVEL keys ABOVE `[[accounts]]` (TOML table
absorption - see below). Current contents:

```toml
addresses = ["you@example.com"]   # confirmed via `whoami`, 2026-09-01
bulk_domains = []
never_bulk_senders = []
direct_max_recipients = 5
packet_max_chars = 40000

[[accounts]]
name = "primary"
host = "imap.example.com"
port = 993
username = "you@example.com"
auth = "password"
folders = ["INBOX", "INBOX.Sent"]
```

Credential is stored in Windows Credential Manager (keyring, service
"mailbrief", username = the mailbox address). Never ask for it, never print it.

## Stage 5 evidence (real mailbox, 2026-09-01)

1. `python -m mailbrief threads` - 47 threads over 88 messages. Real chains
   joined correctly: 4 consecutive "Refresh failed"
   messages keyed by a shared Message-ID (their
   References/In-Reply-To chains are real), an out-of-office auto-reply
   thread joined 2 messages across senders, a weekly report joined 2.
   Rootless singletons key as
   `[mid|<their own Message-ID>]`. All messages show "awaiting my reply"
   because **INBOX.Sent is empty on the server** - the Sent-side logic is
   pinned by tests only until mail lands there.
2. `python -m mailbrief packet` (first run, no cutoff) - wrote
   `packets/2026-09-01-morning.md`: 88 messages (31 direct, 23 cc, 34 bulk),
   31 threads, 34 bulk in a compact sender/subject/count table (17 rows).
   Over the 40000 budget, cc/fyi bodies were dropped with visible markers
   and direct bodies kept full; the header states it: "still over budget:
   direct bodies kept full per plan section 9...". A realistic single-window
   packet will be far smaller than 119K chars - this run deliberately had no
   cutoff, so it covered the whole store.
3. Window mechanics proven: second `packet` run used the stored cutoff and
   reported `0 new messages`; `--since 2026-09-01T15:00` (system local time)
   converted to UTC and picked exactly the 2 messages after 12:00 UTC;
   `--no-cutoff` re-includes everything.
4. `--no-cutoff` overrides `--since` (fixed after the first run did the
   opposite of its help text).

### Design decisions made in this stage (read before touching the code)

- **Threads assemble over ALL stored messages every run**, never a window.
  The packet filters to its window afterwards. Otherwise the Sent side of a
  thread started before the window would never join, and `awaiting_my_reply`
  would be wrong.
- **Message-ID edges, not "key on the References root".** A literal
  "references root -> in-reply-to -> subject" keying leaves the chain's root
  message (which carries no References) stranded: children key on the root's
  Message-ID, the root keys on its subject. Instead: union-find over edges
  (each message's In-Reply-To and References entries that match a stored
  message's Message-ID), then subject+participant overlap only for messages
  with no Message-ID links at all. Plan section 8's wording is satisfied in
  effect: chains group under their root.
- **The user's own addresses are excluded from the subject-fallback
  participant overlap.** Your address is in every received mail; counted, it
  merges every same-subject message into one thread. Participants for the
  overlap check are From+To+Cc minus config `addresses`.
- **Sent messages render as one-line markers, no body.** The briefing needs
  what others wrote; the user's own words add nothing and cost budget.
- **Bulk-only threads get no thread block.** Bulk in the window goes to the
  table only (no bodies). A thread with both bulk and direct/cc messages
  shows the bulk in the table and the rest in the block.
- **Packet window**: `--since` (local ISO, converted to UTC) or the previous
  run's cutoff in `packets/last_cutoff.txt`. That file is written at the end
  of every packet run - which is how "overnight" means "since the previous
  evening run".
- **Trims ladder** per plan section 9: over `packet_max_chars`, cc/fyi
  bodies truncate to 1500 chars, then drop entirely; direct bodies always
  stay full; if still over (pathological), the header says so with sizes and
  names the fix (raise packet_max_chars or narrow the window).
- Bulk table groups by (sender, subject) with a count - one row per repeated
  notification, e.g. a repeated alert collapsed to 6 rows.

### Machine quirk learned the hard way (continues)

- **TOML table absorption**: top-level config keys placed after a
  `[[accounts]]` header are silently absorbed into the account table. Keep
  classifier keys ABOVE `[[accounts]]`; config.py now errors on this mistake.
- **email.utils.parsedate_to_datetime rejects ISO 8601 dates.** Real-mail
  Date headers are RFC 2822, so production is unaffected, but test fixtures
  must write `Tue, 01 Sep 2026 06:00:00 +0000` - test_packet converts ISO to
  RFC 2822 via `format_datetime` before building the message.
- **SQLite `with conn:` commits but does not close.** store.py now closes
  via a contextmanager; keep that pattern in new code.

## Verify everything still works

```
python -m mailbrief check
python tests/test_readonly.py
python -m unittest discover -s tests
python -m mailbrief fetch --dry-run
python -m mailbrief classify --explain
python -m mailbrief threads
python -m mailbrief packet --no-cutoff
python -m mailbrief show 5776    # Greek, direct
python -m mailbrief show 5794    # HTML-only, bulk
```

All must exit 0 and print clean output. Note: INBOX has new folders on the
server (INBOX.Archive, INBOX.spam, INBOX.Trash, INBOX.Junk, INBOX.Drafts) -
only INBOX and INBOX.Sent are configured, which is fine. INBOX.Sent is
empty on the server (0 messages), so `awaiting_my_reply` has no Sent-side
evidence yet.

## Data layout on disk (under %USERPROFILE%\.mailbrief\)

- `state.sqlite3` - `folder_state` (uidvalidity, highest_uid per
  account/folder) + `messages` (uid, message_id; unique on message_id =
  dedupe index).
- `accounts/<name>/messages.jsonl` - one JSON object per message:
  uid, message_id, date (UTC ISO), from, subject, account, folder,
  fetched_at, file (relative body path). After parse: parsed_at, charset,
  declared_charset, body_html, body_chars, trimmed, raw_file. After
  classify: label, label_evidence, classified_at.
- `accounts/<name>/bodies/<uid>.txt` - raw message until parse; cleaned body
  text after. Written atomically (tmp+rename) at every stage.
- `accounts/<name>/raw/<uid>.txt` - raw message kept after parse, byte-exact
  (written via surrogateescape, reads back byte-exact). Classify reads full
  headers (To, Cc, Delivered-To, X-*) from here - the JSONL envelope does not
  store delivery headers. Threads also read References/In-Reply-To/To/Cc
  from here.
- `packets/YYYY-MM-DD-morning.md` / `-eod.md` - rendered packets.
- `packets/last_cutoff.txt` - UTC ISO cutoff of the last packet run.
- `logs/mailbrief.log`.

Known latent issue, not fixed: raw/<uid>.txt is keyed by UID only, but UIDs
are per-folder - if two configured folders ever contain the same UID number,
the raw files collide. Single-folder mail hides it. Fix when it bites (a
folder segment in the raw filename), or refactor raw to <folder>-<uid>.txt.

Fetch write order per message: body file, JSONL record, sqlite row (commit
point). Parse write order: raw/ file, cleaned body, JSONL record update. If a
parse crashes between raw/ and the record, the next parse run reads the true
raw from raw/ and redoes the same cleaning - never clean an already-cleaned
body. JSONL is the source of truth; sqlite is rebuildable.

### Why fetch is fast

Bodies are fetched in ONE batched `UID FETCH ... (UID BODY.PEEK[])` call, not
one round trip per message. Headers in one batched `(UID INTERNALDATE
BODY.PEEK[HEADER])` call. Keep it batched.

### Bug fixed in stage 2 and re-fixed in stage 7: UID-range search semantics

Two traps, both verified live against the real server, and the first "fix"
was the cause of the second bug:

- **Dovecot stale-hit quirk** (stage 2, 2026-09-01): `UID SEARCH UID <n>:*`
  returns the **last** existing uid even when `<n>` exceeds the max - a stale
  message re-found and deduped each run (the old "1 already present" line).
- **Bare set = sequence numbers** (stage 7, 2026-09-02, LIVE): the stage-2 fix
  switched to `UID SEARCH <n>:*`, claiming it was the RFC 3501 form. Wrong: a
  bare set in `UID SEARCH` is a *sequence-number* set, not a UID set. Once the
  mailbox held fewer than `<n>` messages, it returned an empty list and new
  mail was silently skipped - the 2026-09-02 morning run reported
  `new since highest_uid=5795: 0` while UIDs 5796-5797 sat on the server
  unseen. The morning and EOD briefings both came out empty-wrong.

Correct form, now in `imapclient.search_uids`: send `UID SEARCH UID <n>:*`
**and** drop any returned uid below `<n>` (kills the stale-hit quirk). Verified
live 2026-09-02: the two missed messages fetched immediately after the fix.
Regression tests: tests/test_imapclient.py. When the briefing is empty but the
user reports mail, suspect this before anything else.

## Machine quirks learned the hard way

- **getpass hangs** in this user's consoles AND in the Claude Code `!` shell.
  `store-credential` has a `--file` fallback: put the password on line 1 of a
  text file, the tool imports it into Credential Manager and deletes the file.
  The password value is never printed, logged, or handled by Claude.
- **The `!` shell is Git Bash on Windows**: backslashes in paths get eaten.
  Use forward slashes: `! python -m mailbrief store-credential --file C:/Users/you/.mailbrief/credential.txt`.
- **Console codepage is cp1251** (system locale): Greek subjects print as
  `?????` unless stdout is forced to UTF-8. `__main__` does
  `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` - keep it.
  PowerShell `Get-Content` ALSO misreads Python's UTF-8 files as cp1251
  (mojibake). Read JSONL via `python -X utf8` or `Get-Content -Encoding UTF8`.
- Folder names from LIST are modified UTF-7; `imapclient._decode_mutf7`
  decodes them for display, and imaplib re-encodes on the way back in.
- keyring 25.7.0, WinVaultKeyring backend - verified working.
- All project files UTF-8 without BOM (Write tool does this; never PowerShell
  Set-Content on project files). Bash double-quote escapes eat backslashes -
  when replacing characters in files via `python -c`, use `chr()` instead of
  typed escapes.
- The mail server is slow per round trip (remote host); one batched
  fetch call is fine, never loop per-message fetches.
- Bash one-liners with `python -c "..."`: JSON `\\` handling eats backslashes
  in double-quoted heredocs. Prefer `chr(0x5C)` over typed `\\`.
- PowerShell parses a whole command before running it: a heredoc syntax error
  anywhere in the line means NONE of it runs (stage 4: config edit silently
  did not happen). Validate writes by re-reading the file afterwards.

## Stage 5 done - the /brief skill and scheduling

`.claude/skills/brief/SKILL.md` exists, invoked as `/brief morning` or
`/brief eod`. Built TDD-style: a baseline subagent ran the briefing without
the skill (RED - it worked, but guessed the window `--since` by hand), then
with the skill (GREEN - full compliance). The skill's procedure:

1. `python -m mailbrief collect` - the whole pipeline (see below).
2. `python -m mailbrief packet --window morning|eod` - ALWAYS regenerate,
   never read yesterday's packet file. Default window start = previous
   run's cutoff; never guess `--since`.
3. Read the packet (Read tool, not PowerShell Get-Content - cp1251).
4. Write `C:\path\to\briefings\Email Briefings\YYYY-MM-DD Morning.md` or
   `... Evening.md` (folder is created if missing; UTF-8 no BOM).

Stage 5 evidence (real mailbox, 2026-09-01): `C:\path\to\briefings\Email
Briefings\2026-09-01 Morning.md` and `2026-09-01 Evening.md` exist, both
written from real collected mail. The evening briefing covered 14 new
messages across 9 threads; it overruled `awaiting my reply: yes` where the
mail said the ask targets someone else (cc), quoted Greek evidence for the
urgency calls, and stated "Nothing urgent today." with no invented deadlines.
The morning briefing (written by the RED run) covered 6 overnight messages.

**The skill only appears in the skill list after a fresh Claude Code
session** - a subagent spawned right after creation did not see it in the
Skill tool listing (it found and followed SKILL.md by path instead). Restart
Claude Code to pick `/brief` up in the command list.

### New command: `collect` (added in this stage)

`python -m mailbrief collect [--account NAME]` runs fetch -> parse ->
classify in order and stops on the first failure (exit 0 on success). Added
because a three-command `&&` chain exceeded schtasks' 261-char `/TR` limit,
and one command is a better "run the collector" primitive for the skill too.
Each stage gets a minimal argparse.Namespace with its own flags' defaults -
**stages return 0 on success, so collect tests `!= 0`, never truthiness**
(two bugs caught by running it, not by tests).

### Scheduling (plan section 11) - created and proven

Two tasks, collector only (no packet, no briefing - those stay manual):

- `Mailbrief morning collect` - daily 07:00
- `Mailbrief evening collect` - daily 18:00

Both run `cmd /c (cd /d C:\path\to\mailbrief && <python> -m mailbrief collect)
>> %USERPROFILE%\.mailbrief\logs\collect-0700|1800.out.txt 2>&1` with the
full python path (PATH is not reliable in that context). Proven by
`schtasks /Run` on both: full collect output landed in each log, login via
keyring works under Task Scheduler. Started "Ready", first real fire at
07:00/18:00 tomorrow. Task Scheduler's 261-char `/TR` limit is why the
command is short - keep it that way.

### Test bookkeeping left behind

During skill testing the packet cutoff was reset a few times; it now reads
2026-09-01T19:25Z (the eod packet run). Next morning `/brief` will correctly
see the overnight window.

Also still to build (later stages): `reindex` command (regenerate sqlite
from JSONL), `prune` (90-day retention). The Telegram bot proposal became
Stage 6 and is done - see below.

## Stage 6 done - the Telegram briefing bot (docs/plan-telegram.md)

Approved 2026-09-01, built and proven 2026-09-02. User sends `brief morning`
or `brief eod` (plain text, no slash) to the bot from the phone; within a
minute the scheduled poll picks it up, spawns `claude -p` with the /brief
skill, reads the briefing file back from the briefing folder and sends it to the chat
chunked at 4096 chars.

**Live evidence (2026-09-02 00:46):** phone message `Brief eod` ->
schtasks poll -> claude exited 0 -> briefing written to
`C:\path\to\briefings\Email Briefings\2026-09-02 Evening.md` -> bot sent
`# Email Briefing — Evening, 2026-09-02 ...` -> confirmation
`briefing sent (1 messages)`. Both landed in the user's Telegram chat.

Setup facts:

- Bot token in Windows Credential Manager (keyring service "mailbrief",
  username "telegram-bot-token"), imported via
  `python -m mailbrief store-telegram-token --file <path>` (deletes the
  file). Registered in the log redaction filter in `_setup_logging`.
- Allowlist: `telegram_chat_ids = ["<your-chat-id>"]` in the real config,
  above `[[accounts]]` (absorption applies). Discover via
  `python -m mailbrief telegram-whoami`.
- Scheduled task `Mailbrief telegram poll`: every 1 minute,
  StartWhenAvailable=true, Hidden=true, InteractiveToken, no stored
  password. User changed the interval from the initial 5-minute answer to
  1 minute on 2026-09-02. Command: `<pythonw.exe>
  C:\path\to\mailbrief\mailbrief\telegram_poll.pyw` - **no cmd wrapper, no
  console**. The first version used `cmd /c (... python -m mailbrief
  telegram) >> log` and flashed a window every run even with Hidden=true
  in the task XML; cmd spawns its own console. `telegram_poll.pyw`
  redirects stdout/stderr to `logs/telegram-poll.out.txt` itself because
  pythonw has no stdout (print would crash). The claude spawn also passes
  `CREATE_NO_WINDOW` for the same reason.
- schtasks `/Change /RI 1` prints an empty-password warning; the task
  still runs (InteractiveToken, same model as the collector tasks).

### Design decisions made in this stage (read before touching the code)

- **Consume on read, before dispatch.** The update offset is written the
  moment updates are read, not after the run. A briefing run outlives the
  1-minute poll interval; an overlapping poll must not re-run the command.
  If the run then crashes, the command is lost - the user re-sends, which
  is cheaper than a duplicate claude run.
- **`claude -p` needs two things to run headless, both non-negotiable:**
  `--allowedTools` with exactly `Read,Write,Bash(python -m mailbrief
  collect:*),Bash(python -m mailbrief packet:*),Bash(python -m mailbrief
  config:*)` - without it the skill's first collect blocks and the run
  dies with nobody to approve - and `--add-dir C:\path\to\briefings` - without
  it the Write call to the vault is denied and claude QUIETLY prints the
  briefing instead of saving it (rc 0, no file: the bot then waits 10
  minutes and replies "did not appear"). Both proven by live failures.
- **Lock file** `locks/telegram.lock` (pid + start time): a second command
  while a briefing is in flight gets "briefing already running". Locks
  older than 15 minutes are taken over (a briefing takes ~2-5 min of
  claude + up to ~10 min of file waiting; a younger lock is a legitimate
  overlap, an older one is an orphan). Killed dev runs leave orphans -
  TaskStop on Windows is a hard kill, no `finally` runs.
- **Freshness check has a 30-second grace** (`FRESH_FILE_GRACE`): Windows
  NTFS mtimes are tick-quantized and can lag the precise wall clock
  (`time.time()` uses GetSystemTimePreciseAsFileTime) by up to ~16 ms, so
  a file written milliseconds after the run started can stat older. Flaked
  ~1 run in 10 before the grace; pinned by
  `test_fresh_file_within_clock_grace_is_accepted` and a 30-loop stress run.
- **shutil.which("claude") finds the npm shim** `%APPDATA%\Roaming\npm\
  claude.CMD` on this machine; the .cmd branch spawns via `shell=True`.
  Both branches proven live.
- The spawned claude prints `[claude-code:unrecognized_model]` for the
  inherited model env - harmless, it runs anyway.
- Bot replies for a briefing file that never appears name the file and the
  fix (`run /brief locally in Claude Code, or check logs/
  telegram-claude.out.txt`). All sends are logged to mailbrief.log with an
  80-char prefix (never the token).

### New data files (under %USERPROFILE%\.mailbrief\)

- `telegram_offset.txt` - last processed update_id + 1 (consume-on-read).
- `locks/telegram.lock` - held only while a briefing run is in flight.
- `logs/telegram-poll.out.txt` - schtasks poll stdout (per-run summary).
- `logs/telegram-claude.out.txt` - claude -p stdout (append mode).

## Hard rules still in force

- EXAMINE only. The five mutating verbs never appear in source (test enforces).
- Credentials: never in source, config, logs, CLI args, error messages, or
  briefing output. Logger has a redaction filter; new secrets must be
  registered via `log.register_secret`.
- Stdlib only + keyring. `msal` pre-approved but only when M365 work starts.
- Nothing in the collector may reference a specific mail provider, example.com, or the user's
  address - those live in config only.
- Prove each stage against the real mailbox, show actual output, exit code
  zero is not evidence.
