"""Thread assembly (docs/plan.md section 8).

Group messages into conversation threads. Two linking mechanisms:

  1. Message-ID edges. A message's In-Reply-To (and every Message-ID in its
     References chain) that matches a stored message's Message-ID links the
     two. Components of the resulting graph are threads. This is what the
     plan's "key on the References chain root, falling back to In-Reply-To"
     means in practice, and it is what attaches the chain's root message
     (which carries no References of its own) to its children.
  2. Subject + participant overlap, only for messages with no Message-ID
     links at all (no References/In-Reply-To, and no stored parent found).
     Subject normalization strips Re:, RE:, Fwd:, FW:, Re[n]: and the Greek
     "Απ:" / "ΑΠ:". Two messages join when their participant sets overlap
     after the user's own addresses are removed - the user's address is in
     every received mail and would otherwise merge everything.

Sent-folder messages join the same threads. Two fields matter for the
briefing:

  awaiting_my_reply  the last message in the thread is inbound AND no Sent
                     message in the thread references it (its Message-ID
                     appears in a Sent message's References/In-Reply-To).
                     This single field is why the collector reads Sent.
  i_replied_at       timestamp of the user's most recent Sent message.

Threads assemble over ALL stored records every run - never over a subset -
because the Sent side of a thread can predate the current briefing window.
The packet stage filters to its window afterwards.

Headers that are not in the JSONL envelope (References, In-Reply-To, To, Cc)
are read from the byte-exact raw/<uid>.txt file, the same pattern classify
uses. A missing raw file degrades to the subject fallback, never a crash.
"""

from __future__ import annotations

import email
import re
from dataclasses import dataclass, field
from email import policy
from email.utils import getaddresses

# A reply prefix: Re:, Re[2]:, Fwd:, FW:, Απ:, ΑΠ: (case-insensitive).
_PREFIX_RE = re.compile(r"^(?:re|fw|fwd|απ)(?:\[\d+\])?:", re.IGNORECASE)


def normalize_subject(subject: str | None) -> str:
    """Lowercased subject with reply/fwd prefixes stripped, repeatedly.

    Used for thread keying only; the packet shows the original subject.
    """
    s = (subject or "").strip()
    while True:
        stripped = _PREFIX_RE.sub("", s).strip()
        if stripped == s:
            return s.lower()
        s = stripped


def message_id_key(value: str | None) -> str:
    """One Message-ID normalized for comparison: lowercase, no brackets/space."""
    if not value:
        return ""
    return re.sub(r"[\s<>]", "", value).lower()


def message_ids(header: str | None) -> list[str]:
    """All Message-IDs in a References/In-Reply-To header, normalized, in order."""
    if not header:
        return []
    return [message_id_key(m) for m in re.findall(r"<[^>]*>|\S+", header)]


def is_sent_folder(folder: str | None) -> bool:
    """True for a sent-mail folder (Sent, INBOX.Sent, INBOX/Sent, ...)."""
    return any(seg.lower() == "sent" for seg in re.split(r"[./]", folder or ""))


def _participants(msg) -> list[str]:
    """Lowercased, deduplicated, sorted From+To+Cc addresses. Never raises."""
    out: set[str] = set()
    for header in ("From", "To", "Cc"):
        try:
            value = msg.get(header)
        except Exception:
            continue
        if not value:
            continue
        try:
            pairs = getaddresses([value])
        except Exception:
            continue
        for _name, addr in pairs:
            if addr:
                out.add(addr.lower())
    return sorted(out)


def _enrich(rec: dict, raw: bytes | None) -> dict:
    """Record + headers read from raw: references, in_reply_to, participants."""
    m = dict(rec)
    m["references"] = []
    m["in_reply_to"] = []
    m["participants"] = []
    m["sent"] = is_sent_folder(rec.get("folder"))
    if not raw:
        return m
    try:
        msg = email.message_from_bytes(raw, policy=policy.default)
    except Exception:
        return m
    try:
        m["references"] = message_ids(msg.get("References"))
        m["in_reply_to"] = message_ids(msg.get("In-Reply-To"))
        m["participants"] = _participants(msg)
    except Exception:
        pass  # broken headers degrade to the subject fallback
    return m


@dataclass
class Thread:
    key: str
    messages: list[dict]
    awaiting_my_reply: bool = False
    i_replied_at: str | None = None
    participants: list[str] = field(default_factory=list)

    @property
    def last_date(self) -> str | None:
        return self.messages[-1].get("date") if self.messages else None


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _overlap(a: list[str], b: list[str], mine: set[str]) -> bool:
    """Participant sets overlap after removing the user's own addresses.

    An empty set (participants unknown - missing raw) overlaps with
    everything, so a bare record still lands somewhere.
    """
    x = [p for p in a if p not in mine]
    y = [p for p in b if p not in mine]
    return not x or not y or bool(set(x) & set(y))


def _join_by_subject(msgs: list[dict], mine: set[str]) -> list[list[dict]]:
    """Union-find over messages sharing one normalized subject."""
    n = len(msgs)
    uf = _UnionFind(n)
    for i in range(n):
        for j in range(i + 1, n):
            if _overlap(msgs[i]["participants"], msgs[j]["participants"], mine):
                uf.union(i, j)
    groups: dict[int, list[dict]] = {}
    for i, m in enumerate(msgs):
        groups.setdefault(uf.find(i), []).append(m)
    return list(groups.values())


def _component_key(msgs: list[dict]) -> str:
    """Deterministic thread key: earliest message's chain root, else its
    Message-ID, else the subject+participants fallback."""
    first = msgs[0]
    if first["references"]:
        return first["references"][0]
    if first["in_reply_to"]:
        return first["in_reply_to"][0]
    if first.get("message_id"):
        return "mid|" + message_id_key(first["message_id"])
    parts = sorted({p for m in msgs for p in m["participants"]})
    return f"sbj|{normalize_subject(first.get('subject'))}|{'|'.join(parts)}"


def assemble(
    records: list[dict], raw_for, my_addresses: tuple[str, ...] = ()
) -> list[Thread]:
    """Group records into threads. Never raises; missing raw degrades.

    raw_for(uid) -> raw bytes of that message, or None. my_addresses are the
    user's own addresses (config `addresses`), excluded from the subject
    fallback's participant-overlap check. Threads come back sorted by last
    activity (newest thread first); messages within a thread are sorted by
    date.
    """
    mine = {a.lower() for a in my_addresses}
    enriched = [_enrich(rec, raw_for(rec.get("uid"))) for rec in records]

    # Pass 1: union-find over Message-ID edges (In-Reply-To and References
    # entries that match a stored message).
    n = len(enriched)
    by_mid: dict[str, int] = {}
    for i, m in enumerate(enriched):
        mid = message_id_key(m.get("message_id"))
        if mid and mid not in by_mid:
            by_mid[mid] = i
    uf = _UnionFind(n)
    for i, m in enumerate(enriched):
        for rid in m["in_reply_to"] + m["references"]:
            j = by_mid.get(rid)
            if j is not None and j != i:
                uf.union(i, j)

    id_comps: dict[int, list[dict]] = {}
    for i, m in enumerate(enriched):
        id_comps.setdefault(uf.find(i), []).append(m)
    for msgs in id_comps.values():
        msgs.sort(key=lambda m: (m.get("date") or "", m.get("uid") or 0))

    # Pass 2: messages with no Message-ID links at all join by normalized
    # subject + participant overlap.
    final: list[list[dict]] = []
    singles: list[dict] = []
    for msgs in id_comps.values():
        if len(msgs) == 1 and not msgs[0]["references"] and not msgs[0]["in_reply_to"]:
            singles.append(msgs[0])
        else:
            final.append(msgs)
    if singles:
        subject_groups: dict[str, list[dict]] = {}
        for m in singles:
            subject_groups.setdefault(normalize_subject(m.get("subject")), []).append(m)
        for msgs in subject_groups.values():
            for group in _join_by_subject(msgs, mine):
                group.sort(key=lambda m: (m.get("date") or "", m.get("uid") or 0))
                final.append(group)

    threads = []
    for msgs in final:
        sent = [m for m in msgs if m["sent"]]
        referenced_by_sent = {
            mid for m in sent for mid in m["references"] + m["in_reply_to"]
        }
        last = msgs[-1]
        last_id = message_id_key(last.get("message_id"))
        awaiting = (not last["sent"]) and (last_id not in referenced_by_sent)
        sent_dates = [m["date"] for m in sent if m.get("date")]
        i_replied_at = max(sent_dates) if sent_dates else None
        participants = sorted({p for m in msgs for p in m["participants"]})
        threads.append(
            Thread(
                key=_component_key(msgs),
                messages=msgs,
                awaiting_my_reply=awaiting,
                i_replied_at=i_replied_at,
                participants=participants,
            )
        )

    threads.sort(
        key=lambda t: (
            t.last_date or "",
            t.key,
        ),
        reverse=True,
    )
    return threads
