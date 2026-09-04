---
name: brief
description: Use when the user asks for the daily email briefing in this project — `/brief morning`, `/brief eod`, "morning briefing", "end-of-day briefing", "daily briefing". Produces the prose briefing from the deterministic packet.
---

# /brief - daily email briefing

## Overview

The last stage of mailbrief: deterministic packet in, prose briefing out, into the
briefing folder. The skill runs the collector first, so a manual run is never stale.

## Windows

- `/brief morning` - answers: what landed overnight, what needs me today, what is
  waiting on my reply, what can wait.
- `/brief eod` - answers: what happened today grouped by thread/topic, who wanted
  what from me, what I owe people, what is still open, what I pick up tomorrow.

Each writes one dated file into the vault. Both follow the same procedure.

## Procedure

1. **Collect, from the repo root (this project's folder):**
   `python -m mailbrief collect`
   (runs fetch -> parse -> classify in order). If it fails, stop: print the
   failure, name the fix, write no briefing.
2. **Regenerate the packet - always.** Even if today's packet file already exists,
   it is stale by definition:
   `python -m mailbrief packet --window morning|eod` (match the invocation).
   The default window start is the previous packet run's cutoff
   (`packets/last_cutoff.txt`) - that is the contract for "overnight" and "today".
   Never guess a `--since` value. If the header says "(beginning of store)", the
   window covers everything stored (first run) - say so in the briefing.
3. **Read the packet.** `python -m mailbrief config` prints the data dir; the
   packet is `packets/YYYY-MM-DD-<window>.md` under it. Read it with the Read
   tool - PowerShell `Get-Content` misreads UTF-8 as cp1251.
4. **Write the briefing** to `<briefing_dir>/YYYY-MM-DD Morning.md` (morning) or
   `... Evening.md` (eod), where `briefing_dir` is the `briefing_dir` value in
   `%USERPROFILE%\.mailbrief\config.toml`. Create the folder if missing. UTF-8
   without BOM. Never edit the packet - it is data, not a draft.

## Reading the packet

- Header: window, counts (direct/cc/bulk/unclassified), `Trims:` line - it states
  what the budget cut; do not silently rely on cut bodies.
- `## Bulk`: sender/subject/count table -> one roll-up paragraph, never per-item.
- `## Threads`: one block per thread - `### subject - N messages - awaiting my
  reply: ...`, participants, first/last, labels in window, then per message
  `**from** - date - label (evidence)` plus body. `**you** (sent)` lines are the
  user's own sent messages: one-line context, no body.
- `awaiting my reply` and the labels are hints, not verdicts. Overrule them with
  what the mail itself says, and keep the quoted evidence visible.

## Writing rules (docs/plan.md section 10 - non-negotiable)

- One entry per thread. Never one entry per message.
- Per item: sender and topic on one line; why it matters to this user
  specifically; urgency with the evidence quoted from the mail - a stated
  deadline, an explicit ask, an escalation, or a second chase; a concrete next
  step.
- Bulk gets a single short roll-up paragraph, not per-item treatment.
- Do not invent urgency. No stated deadline, no explicit ask, no escalation, no
  repeat chase -> no urgency. If nothing is urgent, write "Nothing urgent today."
- English, regardless of source language. Quote urgency evidence in its source
  language where that matters.

## Common mistakes

| Mistake | Fix |
|---|---|
| Skipping the collector because a packet file exists | Always collect, always regenerate the packet |
| Running only part of the pipeline | `python -m mailbrief collect` runs all three stages in order |
| One entry per message | Group by thread block |
| "Urgent" without a quote | Urgency only with evidence quoted from the mail |
| Trusting `awaiting my reply: yes` | Hint only - the Sent folder is often empty; check the mail |
| Writing the briefing into the repo | Briefing goes to `briefing_dir`; the packet stays in the data dir |

When done, report the briefing file path and show its text.
