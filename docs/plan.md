# mailbrief - approved implementation plan

Personal email briefing assistant. Reads corporate mailboxes over IMAP, stores mail
locally in a plain greppable format, classifies it, and produces morning and
end-of-day briefings written by Claude.

**Status:** design approved 2026-08-25. No code written yet. Build starts at Stage 1.

---

## 1. Confirmed environment facts

These were probed, not assumed. Do not re-probe unless something fails.

### Mail server (account 1)

Probed 2026-08-25 against `example.com` / `imap.example.com`.

```
DNS:   example.com, imap.example.com, webmail.example.com  ->  203.0.113.10
MX:    imap.example.com (preference 0)

Port 993   OPEN   implicit TLS
Port 143   OPEN   advertises STARTTLS
Port 465   closed
Port 587   closed

TLS-VERIFY:  OK (default chain validation passed)
TLS-PROTO:   Tls13
CERT-SUBJECT: CN=*.example.com
CERT-ISSUER:  CN=YR1, O=Let's Encrypt, C=US
CERT-EXPIRES: 2026-10-15
CERT-SAN:     DNS Name=*.example.com, DNS Name=example.com

GREETING: * OK [CAPABILITY IMAP4rev1 LOGIN-REFERRALS ID ENABLE IDLE SASL-IR
          LITERAL+ AUTH=PLAIN AUTH=LOGIN] IMAP server ready.
STATUS:   a1 OK Pre-login capabilities listed, post-login capabilities have more.
```

Conclusions:

- IMAP is directly reachable. No browser automation against a webmail UI. Ever.
- Certificate validates cleanly, so `ssl.create_default_context()` works with
  verification ON. No certificate-skip workarounds are needed or permitted.
- Auth is PLAIN / LOGIN only. No OAuth on this host. Username and password.
- Server advertises IDLE. A push watcher is possible later but is explicitly out
  of scope: a long-lived daemon is harder to repair than a script that exits.
- Post-login capabilities are unknown and the design must not depend on any
  extension. State tracking uses UIDVALIDITY plus highest-seen UID, which is
  plain RFC 3501 and works on any server, including the future M365 account.

### Workstation

```
Python        3.13.7  at C:\Users\you\AppData\Local\Programs\Python\Python313\python.exe
stdlib check  imaplib, email, ssl, sqlite3, json  all import OK
keyring       NOT INSTALLED - install approved by user, not yet done
schtasks      C:\Windows\System32\schtasks.exe
Timezone      system local (UTC+03:00), no DST
Briefing folder   C:\path\to\briefings  (Sync ENABLED)
Vault folders Clippings, First Vault, Meeting Notes, to-do
Daily notes   core plugin enabled but unconfigured (no daily-notes.json)
C:\path\to\mailbrief  did not exist before this plan
```

---

## 2. Decisions taken

| Topic | Decision | Rationale |
|---|---|---|
| Transport | IMAP over implicit TLS, `imap.example.com:993` | MX points at `imap.example.com`, so it is the canonical mail name and survives a web-hosting move. Cert SAN covers it. |
| Username | `you@example.com` | common email convention. Confirm at Stage 1, do not assume. |
| Credentials | Windows Credential Manager via `keyring` | User's choice. Alternative was hand-rolled DPAPI ctypes. |
| Folders read | INBOX and Sent | Sent is what makes "waiting on my reply" trustworthy. |
| Poll schedule | Twice daily, 07:00 and 18:00 local | User's choice. |
| Briefing engine | Deterministic packet + `/brief` skill in Claude Code | Zero API cost. Packet is a stable interface, so an unattended API path can be added later without touching the fetcher. |
| Briefing language | English, regardless of source language | |
| Attachments | Metadata only: filename, MIME type, size | No downloads. |
| Retention | 90 days of raw mail, pruned by a maintenance command | |
| Timezone | Store UTC, render in system local time | Machine is UTC+03. No hardcoded zone to go stale. |
| Briefing output | `C:\path\to\briefings\Email Briefings\YYYY-MM-DD Morning.md` and `... Evening.md` | Separate dated files. Appending to the daily note risks clobbering hand-written content. |
| Phone sync | Accepted by user | Briefings carry sender names and subjects off this machine. User confirmed this is fine. |
| Account 2 | Microsoft 365, does not exist yet | See section 12. |

---

## 3. Architecture

Strictly one-directional. Nothing upstream knows anything about what is downstream.

```
config.toml + Windows Credential Manager
      |
      v
   auth  ->  imap client  (EXAMINE, read-only)
      |
      v
   raw store  (JSONL + body .txt + sqlite index)
      |
      v
   parse  ->  classify  ->  thread assembly
      |
      v
   briefing packet  (.md, deterministic, no AI)
      |
      v
   /brief skill in Claude Code  ->  briefing folder
```

The packet is the contract between collection and analysis. Changing the briefing
format must never require touching the fetcher. Adding a mailbox must never require
touching anything above the config line.

---

## 4. File layout

### Code - `C:\path\to\mailbrief\` (git-able, contains no secrets)

```
README.md
CLAUDE.md
config.template.toml           committed, placeholder values only
.gitignore
docs/plan.md                   this file
mailbrief/
  __init__.py
  __main__.py                  CLI entry: python -m mailbrief <command>
  config.py                    load and validate; errors name the fix
  auth.py                      PasswordAuth | OAuth2Auth   <- the M365 seam
  imapclient.py                connect, EXAMINE, fetch by UID
  store.py                     append-only, idempotent writes
  parse.py                     MIME, charsets, HTML, quote stripping
  classify.py                  Direct / CC / Bulk
  threads.py                   thread assembly, reply-owed detection
  packet.py                    render the briefing packet
  prune.py                     retention enforcement
tests/
  test_parse.py
  test_classify.py
  test_threads.py
  test_readonly.py             greps source for mutating IMAP verbs
  fixtures/                    real messages from the mailbox, sanitized
.claude/skills/brief/SKILL.md  the /brief command
```

### Data - `%USERPROFILE%\.mailbrief\` (never inside any repo)

```
config.toml                        real config: host, addresses, allowlists
state.sqlite3                      UIDVALIDITY, highest UID, dedupe index
accounts/primary/messages.jsonl     one JSON object per message
accounts/primary/bodies/<uid>.txt   cleaned body text
packets/YYYY-MM-DD-morning.md
logs/mailbrief.log
```

`python -m mailbrief config path` prints the config location, so nobody has to
remember it at 8am.

---

## 5. State and idempotency

Per account, per folder, sqlite holds `uidvalidity` and `highest_uid`.

Each run issues `UID SEARCH UID <highest_uid+1>:*`. If `UIDVALIDITY` has changed the
server has renumbered the mailbox, so the run does a full resync with `highest_uid`
reset to zero; deduplication on `Message-ID` means nothing is stored twice.

Write order matters. Append the JSONL record and the body file first, then commit the
state row. A crash between the two means the message is refetched on the next run and
the dedupe index discards it. Never lose, never duplicate.

The JSONL is the source of truth for content. sqlite is a rebuildable index.
`python -m mailbrief reindex` regenerates sqlite from the JSONL. If the database is
corrupt at 8am, that is the single command that fixes it.

---

## 6. Handling real-world email mess

| Problem | Handling |
|---|---|
| MIME multipart | `EmailMessage.walk()`; prefer `text/plain`, fall back to `text/html` |
| quoted-printable, base64 | `part.get_payload(decode=True)`; stdlib decodes transfer encoding |
| RFC 2047 encoded headers | `email.header.decode_header` + `make_header`, wrapped so it cannot raise |
| non-UTF-8 charsets | declared charset first; on `LookupError` or `UnicodeDecodeError` fall back cp1251, then cp1253, then latin-1 with `errors='replace'`. **Record which charset was actually used** so bad guesses are visible |
| HTML-only bodies | stdlib `html.parser` subclass: drop script and style, block-level tags become newlines, unescape entities, collapse whitespace. No bs4, no lxml |
| long quoted reply chains | strip trailing `>` blocks and the usual separators: `On ... wrote:`, `-----Original Message-----`, a long underscore rule, `Sent from my`. **Record the trimmed character count**; nothing is discarded silently |

Every file Python writes is UTF-8 without BOM.

---

## 7. Classification

Evaluated in order. First match wins.

**Bulk / automated** if any of:

- `List-Id`, `List-Unsubscribe`, or `List-Post` present
- `Precedence: bulk | list | junk`
- `Auto-Submitted` present and not `no`
- `X-Auto-Response-Suppress` present
- `Return-Path: <>` (null sender: bounce or auto-reply)
- sender localpart matches `no-?reply|donotreply|notifications?|mailer-daemon|postmaster|bounce|alerts?`
- sender domain listed in the config `bulk_domains` list

**Direct** if one of my addresses appears in `To:` AND total To+Cc recipients is at
most `direct_max_recipients` (default 5).

**CC / FYI** otherwise: address in `Cc:` only, or in `To:` on a large distribution,
or in neither (BCC, alias, catch-all delivery).

### Where these signals will be wrong

Stated up front, because pretending otherwise is how the tool loses trust.

- A colleague who sends a genuine request from a `noreply` address is filed as bulk.
  Mitigated by a `never_bulk_senders` allowlist in config.
- Vendors sending real one-to-one mail through a marketing platform carry
  `List-Unsubscribe` and are filed as bulk. Same allowlist.
- A human message to a twelve-person team where the user is the one who must act is
  filed CC. **No header can detect this.**
- BCC and alias delivery leave the user's address nowhere in the visible headers.
  Mitigated by also checking `Delivered-To`, `X-Delivered-To`, `X-Envelope-To`, and
  `X-Original-To`; the server usually sets one of them.
- Matching is always on the address, never the display name, so encoded-word Greek or
  Russian display names cannot break it.

Because of case three: **the rule-based label is a hint recorded in the packet, not a
verdict.** The packet retains full recipient lists, and the briefing skill is
explicitly permitted to overrule the label. That is what keeps the analysis Claude's
rather than a keyword rule's.

### Address discovery

The user declined to enumerate their addresses. Do not guess. After Stage 2,
`python -m mailbrief whoami` scans delivery headers across everything collected and
prints a ranked list of addresses that actually deliver to this mailbox. The user
confirms; the confirmed list goes into config. Evidence, not assumption.

Known seed: `you@example.com`.

---

## 8. Thread assembly

Key on the `References` chain root, falling back to `In-Reply-To`, falling back to
normalized subject plus participant-set overlap. Subject normalization strips `Re:`,
`RE:`, `Fwd:`, `FW:`, `Re[2]:`, and the Greek `Απ:` / `ΑΠ:`.

Sent-folder messages join the same threads.

- `awaiting_my_reply` - the last message in the thread is inbound and no Sent message
  references it. This single field is the entire justification for reading Sent.
- `i_replied_at` - timestamp of the user's most recent Sent message in the thread.

---

## 9. Briefing packet

One markdown file per briefing window. Fully deterministic, no AI involved.

Per thread: participants, subject, message count, first and last timestamps,
classification counts, `awaiting_my_reply`, and the cleaned body of each message with
quoted tails trimmed. Bulk is reduced to a compact table of sender, subject, and
count, with no bodies.

**Size control.** If the packet exceeds its configured character budget, bulk bodies
are dropped entirely, CC/FYI bodies are truncated, and Direct bodies are always kept
in full. The packet header states exactly what was trimmed. Degradation is visible,
never silent. This is why nobody needs to know the mail volume in advance.

---

## 10. The /brief skill

`.claude/skills/brief/SKILL.md`, invoked as `/brief morning` or `/brief eod`.

1. Run the collector (so a manual run is never stale).
2. Read the current packet.
3. Write the briefing into the briefing folder.

Rules the skill must enforce:

- One entry per thread. Never one entry per message.
- Per item: sender and topic on one line; why it matters to this user specifically;
  urgency **with the evidence for that call** quoted from the mail - a stated
  deadline, an explicit ask, an escalation, or a second chase.
- A concrete next step.
- Bulk gets a single short roll-up paragraph, not per-item treatment.
- **Do not invent urgency.** With no stated deadline, explicit ask, escalation, or
  repeat chase, assign none. If nothing is urgent, say "Nothing urgent today."

Morning briefing answers: what landed overnight, what needs me today, what is waiting
on my reply, what can wait. "Overnight" means since the previous evening run.

End-of-day briefing answers: what happened today grouped by thread or topic, who
wanted what from me, what I owe people, what is still open, what I pick up tomorrow.

---

## 11. Scheduling

Two `schtasks` entries running the **collector only**, at 07:00 and 18:00 local time.
The briefing stays a manual `/brief` command. If `/brief` runs and the collector has
not, the skill runs it first.

---

## 12. Account 2 - Microsoft 365

Does not exist yet. The seam is built now; the implementation is not.

**Microsoft disabled basic authentication for IMAP in Exchange Online.** Password auth
will not work. The M365 account requires:

- An Entra ID app registration with the delegated `IMAP.AccessAsUser.All` permission
  on Office 365 Exchange Online
- A one-time consent flow (device code suits a CLI tool), then a refresh token stored
  in Credential Manager
- IMAP not disabled at tenant or mailbox level

Two prerequisites for the user to confirm with their IT before this work starts:
whether non-admin users may register apps in the tenant, and whether IMAP is enabled
on that mailbox. Many corporate tenants block both.

Design consequence, applied from day one: `auth.py` exposes `PasswordAuth` (LOGIN)
and `OAuth2Auth` (XOAUTH2), selected by a config field. Credential Manager stores a
password for one and a refresh token for the other. Everything downstream of "give me
an authenticated IMAP connection" is identical.

`msal` (Microsoft's own OAuth library) is pre-approved for that work and must not be
installed before it starts.

---

## 13. Build stages and verification

Prove each stage against the real mailbox before starting the next. Show real output.
Never mock data. Exit code zero is not evidence.

| Stage | Command | Evidence required |
|---|---|---|
| 1 connect | `python -m mailbrief check` | server, TLS version, certificate, post-login capabilities, folder list with message counts. No message content. |
| 2 fetch | `python -m mailbrief fetch --limit 20 --dry-run` | uid, date, from, subject that it *would* store, writing nothing. Then a real fetch, plus the measured messages-per-day figure. |
| 3 parse | `python -m mailbrief show <uid>` | decoded headers, detected charset, body text, characters trimmed. Must be run against a Greek message and an HTML-only message. |
| 4 classify | `python -m mailbrief classify --explain` | every message with its label AND the header that triggered it. User reviews for wrong calls; tune the allowlists. |
| 5 brief | `/brief morning` | the real briefing in the briefing folder. |

Tests use stdlib `unittest` against fixtures saved from real mail, sanitized.
`test_readonly.py` greps the source for `STORE`, `COPY`, `EXPUNGE`, `APPEND`, and
`MOVE` as IMAP commands and fails if a mutating verb ever appears.

---

## 14. Dependencies

**`keyring`** - the only third-party package. Approved by the user. Stores the
mailbox password in Windows Credential Manager. The alternative was hand-written
DPAPI ctypes glue, which is security-sensitive code the user would have to trust.

**`msal`** - pre-approved for the M365 account only, when that work starts.
Justification at that time: implementing OAuth2 device-code and refresh against
Entra ID by hand is a large amount of security-sensitive code, and msal is
Microsoft's own.

Everything else is stdlib: `imaplib`, `email`, `ssl`, `sqlite3`, `tomllib`,
`html.parser`, `unittest`, `json`, `getpass`.

---

## 15. Security rules

- Folders are opened with `EXAMINE`, not a writable `SELECT`. Read-only is enforced at
  the protocol level and again by `test_readonly.py`.
- No sending, no deleting, no moving, no marking read. Anywhere. Ever.
- **The password is never seen by Claude.** The user runs
  `python -m mailbrief store-credential`, which prompts with hidden input via
  `getpass` and writes straight to Credential Manager. It is never a CLI argument,
  never in config, never in logs, never in briefing output, never in an error message.
  The logger carries a redaction filter.
- TLS verification stays ON, confirmed working against the real certificate.
- Real config and all mail data live outside the repo. `.gitignore` enforces it.

---

## 16. Explicitly not building

No IDLE daemon. No web UI. No sending or drafting replies. No attachment downloads.
No full-text search index - JSONL plus ripgrep covers it.

Nothing in the collector may reference a specific mail provider, webmail product, or the user's address.
Those belong in config only.
