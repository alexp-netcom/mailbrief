"""Briefing packet (docs/plan.md section 9).

One deterministic markdown file per briefing window. No AI, no random order:
threads sort by last activity, messages by date, bulk rows by count then
sender, participants alphabetically.

Per thread: participants, subject, message count, first and last timestamps,
classification counts, awaiting_my_reply, i_replied_at, and the cleaned body
of each inbound message in the window (sent messages are one-line markers -
the briefing needs what others wrote, not the user's own words). Threads
assemble over ALL stored mail; only the window's messages show bodies.
Messages before the window are counted ("N earlier"), never hidden silently.

Bulk is reduced to a compact table of sender, subject, count - no bodies.

Size control (plan section 9): over packet_max_chars, CC/FYI bodies are
truncated to 1500 chars, then dropped entirely. Direct bodies are always
kept in full. The header states exactly what was trimmed - degradation is
visible, never silent.

The briefing window: `since` is the UTC ISO cutoff. The CLI defaults it to
the previous run's cutoff (last_cutoff.txt), which is how "overnight" means
"since the previous evening run".
"""

from __future__ import annotations

import collections
import datetime
import pathlib

from . import threads

_CC_TRUNCATE = 1500
_TRUNCATE_MARKER = "\n\n…[truncated to 1500 chars - packet over budget]"
_DROPPED_MARKER = "\n…[cc/fyi body dropped - packet over budget]"


def build_packet(conf, since: str | None, window: str, stores, now: str | None = None) -> dict:
    """Gather the window's messages into a render-ready structure. Pure data.

    Reads the JSONL records (labels and evidence already there - never
    re-classifies), raw/ for threading headers, bodies/ for cleaned text.
    since is a UTC ISO cutoff or None for everything.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    total = 0
    counts = collections.Counter()
    bulk_rows: dict[tuple[str, str], int] = collections.Counter()
    thread_blocks: list[dict] = []

    for store in stores:
        recs, _corrupt = store.records()
        account = store.account.name

        def raw_for(uid, store=store):
            path = store.root / "raw" / f"{uid}.txt"
            try:
                return path.read_bytes() if path.exists() else None
            except OSError:
                return None

        def body_for(uid, store=store):
            path = store.root / "bodies" / f"{uid}.txt"
            try:
                return path.read_text(encoding="utf-8", errors="replace") if path.exists() else None
            except OSError:
                return None

        for t in threads.assemble(recs, raw_for, my_addresses=conf.addresses):
            in_window = [
                m for m in t.messages
                if since is None or ((m.get("date") or m.get("fetched_at") or "") >= since)
            ]
            bulk_win = [m for m in in_window if not m["sent"] and m.get("label") == "bulk"]
            keep_win = [m for m in in_window if not m["sent"] and m.get("label") != "bulk"]
            total += len(in_window)
            for m in in_window:
                if not m["sent"]:
                    counts[m.get("label") or "unclassified"] += 1
            for m in bulk_win:
                bulk_rows[(m.get("from") or "?", m.get("subject") or "?")] += 1
            if not keep_win:
                # Only bulk and/or sent activity in the window: bulk goes to
                # the compact table (no bodies); sent-only adds nothing the
                # briefing can use. No thread block.
                continue
            labels = {
                "direct": sum(1 for m in keep_win if m.get("label") == "direct"),
                "cc": sum(1 for m in keep_win if m.get("label") == "cc"),
                "bulk": len(bulk_win),
            }
            non_sent = [m for m in t.messages if not m["sent"]]
            dates = [m["date"] for m in t.messages if m.get("date")]
            subject = (non_sent[-1].get("subject") if non_sent else t.messages[-1].get("subject")) or "?"
            entries = []
            for m in in_window:
                if not m["sent"] and m.get("label") == "bulk":
                    continue  # bulk is in the table, never in a thread block
                entries.append(
                    {
                        "date": m.get("date"),
                        "from": m.get("from") or "?",
                        "folder": m.get("folder") or "?",
                        "sent": m["sent"],
                        "label": (m.get("label") or "unclassified") if not m["sent"] else None,
                        "evidence": (m.get("label_evidence") or "no label - run classify")
                            if not m["sent"] else None,
                        "body": None if m["sent"] else body_for(m.get("uid")),
                    }
                )
            thread_blocks.append(
                {
                    "account": account,
                    "subject": subject,
                    "count": len(t.messages),
                    "first": min(dates) if dates else None,
                    "last": max(dates) if dates else None,
                    "labels": labels,
                    "awaiting_my_reply": t.awaiting_my_reply,
                    "i_replied_at": t.i_replied_at,
                    "earlier": len(t.messages) - len(in_window),
                    "participants": t.participants,
                    "messages": entries,
                }
            )

    thread_blocks.sort(key=lambda b: (b["last"] or "", b["subject"]), reverse=True)
    return {
        "window": window,
        "title": "Morning" if window == "morning" else "End-of-day",
        "generated_at": now,
        "since": since,
        "new_counts": {
            "total": total,
            "direct": counts["direct"],
            "cc": counts["cc"],
            "bulk": counts["bulk"],
            "unclassified": counts["unclassified"],
        },
        "thread_count": len(thread_blocks),
        "bulk_rows": [
            {"sender": sender, "subject": subject, "count": n}
            for (sender, subject), n in sorted(
                bulk_rows.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])
            )
        ],
        "threads": thread_blocks,
    }


def _local(date_iso: str | None) -> str:
    """UTC ISO -> local time for display. Raw string if it cannot parse."""
    if not date_iso:
        return "?"
    try:
        dt = datetime.datetime.fromisoformat(date_iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return date_iso


def _render_thread(block: dict, truncate_cc: bool, drop_cc: bool) -> list[str]:
    lines = []
    counts = block["labels"]
    label_str = (
        f"{counts['direct']} direct, {counts['cc']} cc, {counts['bulk']} bulk"
    )
    awaiting = "awaiting my reply: yes" if block["awaiting_my_reply"] else (
        "awaiting my reply: no - you replied "
        + _local(block["i_replied_at"])
        if block["i_replied_at"]
        else "awaiting my reply: no"
    )
    earlier = (
        f" - {block['earlier']} earlier message" + ("s" if block["earlier"] != 1 else "")
        + " not in window"
        if block["earlier"]
        else ""
    )
    lines.append(
        f"### {block['subject']} - {block['count']} messages - {awaiting}"
    )
    lines.append(
        f"account: {block['account']} - participants: "
        + ", ".join(block["participants"]) or "participants: (none)"
    )
    lines.append(
        f"first: {_local(block['first'])} - last: {_local(block['last'])}"
        f" - labels in window: {label_str}{earlier}"
    )
    for m in block["messages"]:
        lines.append("---")
        if m["sent"]:
            lines.append(f"**you** (sent) - {_local(m['date'])} - folder {m['folder']}")
            continue
        lines.append(
            f"**{m['from']}** - {_local(m['date'])} - {m['label']} ({m['evidence']})"
        )
        body = m["body"]
        if body is None:
            lines.append("[body file missing - fix: run 'python -m mailbrief parse']")
        elif drop_cc and m["label"] == "cc":
            lines.append(_DROPPED_MARKER)
        elif truncate_cc and m["label"] == "cc":
            lines.append(body[:_CC_TRUNCATE] + _TRUNCATE_MARKER)
        else:
            lines.append(body)
    return lines


def render(data: dict, budget: int) -> str:
    """Render the packet to markdown, applying the plan's size ladder.

    Over budget: CC/FYI bodies truncated to 1500 chars, then dropped
    entirely. Direct bodies are always kept in full. The header states what
    happened; if still over after the ladder, it says so instead of hiding.
    """
    counts = data["new_counts"]
    plural = "s" if counts["total"] != 1 else ""
    head = [
        f"# Email briefing packet - {data['title']}",
        "",
        f"window: since {data['since'] or '(beginning of store)'} - "
        f"{counts['total']} new message{plural} "
        f"({counts['direct']} direct, {counts['cc']} cc, {counts['bulk']} bulk, "
        f"{counts['unclassified']} unclassified)",
        f"generated: {data['generated_at']}",
        f"threads: {data['thread_count']}, bulk senders: {len(data['bulk_rows'])}",
    ]

    trims: list[str] = []
    sections: list[str] = []

    def render_body(truncate_cc: bool, drop_cc: bool) -> list[str]:
        out = list(head)
        if data["bulk_rows"]:
            total_bulk = sum(r["count"] for r in data["bulk_rows"])
            out += [
                "",
                f"## Bulk ({total_bulk} messages, {len(data['bulk_rows'])} senders)",
                "",
                "| Sender | Subject | Count |",
                "|---|---|---|",
            ]
            out += [f"| {r['sender']} | {r['subject']} | {r['count']} |" for r in data["bulk_rows"]]
        out += ["", "## Threads"]
        for i, block in enumerate(data["threads"], start=1):
            out.append("")
            out += _render_thread(block, truncate_cc, drop_cc)
        return out

    full = render_body(False, False)
    size = sum(len(line) for line in full)
    if size <= budget:
        trims = ["none"]
    else:
        truncated = render_body(True, False)
        if sum(len(l) for l in truncated) <= budget:
            trims = [f"cc/fyi bodies truncated to {_CC_TRUNCATE} chars (over {budget} budget)"]
            full = truncated
        else:
            dropped = render_body(True, True)
            if sum(len(l) for l in dropped) <= budget:
                trims = [
                    f"cc/fyi bodies truncated to {_CC_TRUNCATE} chars, then dropped entirely "
                    f"(still over {budget} budget)"
                ]
                full = dropped
            else:
                trims = [
                    "still over budget: direct bodies kept full per plan section 9; "
                    f"increase packet_max_chars or narrow the window (now {sum(len(l) for l in dropped)} > {budget})"
                ]
                full = dropped

    # insert the Trims line after the "threads:" line
    out = []
    inserted = False
    for line in full:
        if not inserted and line.startswith("threads:"):
            out.append(line)
            out.append("Trims: " + "; ".join(trims))
            inserted = True
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def default_since(data_dir: pathlib.Path) -> str | None:
    """The previous packet run's cutoff (UTC ISO), or None on first run."""
    path = data_dir / "packets" / "last_cutoff.txt"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def save_cutoff(data_dir: pathlib.Path, iso: str) -> None:
    """Record this run's cutoff for the next window. Never loses a packet."""
    path = data_dir / "packets" / "last_cutoff.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(iso + "\n", encoding="utf-8")
    tmp.replace(path)
