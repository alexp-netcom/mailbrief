"""Append-only local store for fetched mail.

Data layout under %USERPROFILE%\\.mailbrief\\ (never in the repo):

  state.sqlite3                        folder state + dedupe index
  accounts/<account>/messages.jsonl    one JSON object per message, append-only
  accounts/<account>/bodies/<uid>.txt  raw message while unparsed; the cleaned
                                       body text once `parse` has run
  accounts/<account>/raw/<uid>.txt     raw message kept after parse (byte-exact)

Fetch write order per message: body file, then the JSONL record, then the
sqlite row. A crash between the two means the next run refetches the message
and the dedupe index discards the duplicate. Never lose, never duplicate.

`parse` write order: raw/ file, then the cleaned body, then the JSONL record
update. A crash between the two leaves raw/ in place, so the next parse run
reads the true raw from raw/ and redoes the same cleaning - idempotent.

The JSONL is the source of truth for content. sqlite is a rebuildable index;
`python -m mailbrief reindex` (a later stage) regenerates it from the JSONL.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sqlite3

from .config import Account

_SCHEMA = """
CREATE TABLE IF NOT EXISTS folder_state (
    account      TEXT NOT NULL,
    folder       TEXT NOT NULL,
    uidvalidity  INTEGER NOT NULL,
    highest_uid  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account, folder)
);
CREATE TABLE IF NOT EXISTS messages (
    account    TEXT NOT NULL,
    folder     TEXT NOT NULL,
    uid        INTEGER NOT NULL,
    message_id TEXT,
    PRIMARY KEY (account, folder, uid),
    UNIQUE (account, folder, message_id)
);
"""


class StoreError(Exception):
    pass


class Store:
    """Per-account append-only store. Reads never create any file."""

    def __init__(self, account: Account, data_dir: pathlib.Path) -> None:
        self.account = account
        self.data_dir = pathlib.Path(data_dir)
        self.root = self.data_dir / "accounts" / account.name
        self.db_path = self.data_dir / "state.sqlite3"
        self._jsonl = self.root / "messages.jsonl"

    # -- reads (no files are created) --

    def folder_state(self, folder: str) -> tuple[int, int]:
        """(uidvalidity, highest_uid) for this folder; (0, 0) if never fetched."""
        if not self.db_path.exists():
            return (0, 0)
        with self._connect() as conn:
            try:
                row = conn.execute(
                    "SELECT uidvalidity, highest_uid FROM folder_state "
                    "WHERE account=? AND folder=?",
                    (self.account.name, folder),
                ).fetchone()
            except sqlite3.Error as e:
                raise self._db_error(e) from e
        if row is None:
            return (0, 0)
        return (int(row[0]), int(row[1]))

    def stored(self, folder: str, uid: int, message_id: str | None, uid_reliable: bool) -> bool:
        """True if this message is already in the index.

        uid_reliable is False during a resync after a UIDVALIDITY change, when
        uids are meaningless and only the Message-ID dedupe counts.
        """
        if not self.db_path.exists():
            return False
        with self._connect() as conn:
            try:
                if uid_reliable:
                    row = conn.execute(
                        "SELECT 1 FROM messages WHERE account=? AND folder=? AND uid=?",
                        (self.account.name, folder, uid),
                    ).fetchone()
                    if row:
                        return True
                if message_id:
                    row = conn.execute(
                        "SELECT 1 FROM messages WHERE account=? AND folder=? AND message_id=?",
                        (self.account.name, folder, message_id),
                    ).fetchone()
                    return row is not None
            except sqlite3.Error as e:
                raise self._db_error(e) from e
        return False

    def records(self) -> tuple[list[dict], int]:
        """All JSONL records (file order) plus the corrupt-line count."""
        if not self._jsonl.exists():
            return [], 0
        recs = []
        corrupt = 0
        with open(self._jsonl, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    corrupt += 1
        return recs, corrupt

    def day_counts(self, folder: str) -> tuple[list[tuple[str, int]], int]:
        """((date, count), corrupt_lines) per UTC day, newest first, from the JSONL."""
        if not self._jsonl.exists():
            return [], 0
        counts: dict[str, int] = {}
        corrupt = 0
        with open(self._jsonl, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    corrupt += 1
                    continue
                if rec.get("folder") != folder:
                    continue
                day = (rec.get("date") or "")[:10]
                if len(day) == 10:
                    counts[day] = counts.get(day, 0) + 1
        return sorted(counts.items(), reverse=True), corrupt

    # -- writes --

    def save_message(
        self, folder: str, uid: int, record: dict, raw: bytes, uidvalidity: int
    ) -> str | None:
        """Persist one message; return a warning string, or None on success.

        Order: body file, JSONL record, then the sqlite state row - the commit
        point. The body file is written atomically (tmp + rename).
        """
        self._ensure_layout()
        body = self.root / "bodies" / f"{uid}.txt"
        record.setdefault("file", f"bodies/{uid}.txt")
        tmp = body.with_name(body.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(raw.decode("utf-8", "surrogateescape"))
        os.replace(tmp, body)
        with open(self._jsonl, "a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO messages (account, folder, uid, message_id) "
                    "VALUES (?, ?, ?, ?)",
                    (self.account.name, folder, uid, record.get("message_id")),
                )
                conn.execute(
                    "INSERT INTO folder_state (account, folder, uidvalidity, highest_uid) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(account, folder) DO UPDATE SET "
                    "uidvalidity=excluded.uidvalidity, "
                    "highest_uid=MAX(highest_uid, excluded.highest_uid)",
                    (self.account.name, folder, uidvalidity, uid),
                )
        except sqlite3.IntegrityError:
            # A concurrent run already stored this Message-ID. The message is
            # on disk; it is just not stored twice. State is committed by the
            # other run; the next run resumes from its highest UID.
            return (
                f"uid {uid}: Message-ID already in index (concurrent run?); "
                "not stored twice"
            )
        except sqlite3.Error as e:
            raise self._db_error(e) from e
        return None

    def save_parsed(
        self, folder: str, uid: int, body_text: str, raw_bytes: bytes, fields: dict
    ) -> None:
        """Stage 3: keep the raw message, replace the body file with cleaned text.

        Order: raw/<uid>.txt first, then bodies/<uid>.txt, then the JSONL
        record update. A crash after the first step leaves raw/ in place, so
        the next parse run reads the true raw from raw/ and redoes the same
        cleaning - never clean an already-cleaned body.
        """
        self._ensure_layout()
        raw_dir = self.root / "raw"
        raw_dir.mkdir(exist_ok=True)
        raw_path = raw_dir / f"{uid}.txt"
        tmp = raw_path.with_name(raw_path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(raw_bytes.decode("utf-8", "surrogateescape"))
        os.replace(tmp, raw_path)
        body = self.root / "bodies" / f"{uid}.txt"
        tmp = body.with_name(body.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(body_text)
        os.replace(tmp, body)
        self._update_record(folder, uid, fields)

    def save_classified(self, folder: str, uid: int, fields: dict) -> None:
        """Stage 4: merge label + evidence into one JSONL record (atomic rewrite)."""
        self._update_record(folder, uid, fields)

    def _update_record(self, folder: str, uid: int, fields: dict) -> None:
        """Merge fields into one JSONL record (atomic rewrite). Never loses data."""
        if not self._jsonl.exists():
            raise StoreError(
                f"messages.jsonl missing at {self._jsonl}. Fix: run fetch first."
            )
        tmp = self._jsonl.with_name(self._jsonl.name + ".tmp")
        found = False
        with open(self._jsonl, encoding="utf-8", newline="") as src, open(
            tmp, "w", encoding="utf-8", newline=""
        ) as out:
            for line in src:
                s = line.rstrip("\n")
                if not s.strip():
                    out.write(line)
                    continue
                try:
                    rec = json.loads(s)
                except json.JSONDecodeError:
                    out.write(line)  # corrupt lines survive verbatim
                    continue
                if rec.get("folder") == folder and rec.get("uid") == uid:
                    rec.update(fields)
                    found = True
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if not found:
            tmp.unlink()
            raise StoreError(
                f"no message with uid {uid} in folder {folder!r} of "
                f"{self._jsonl}. Fix: refetch."
            )
        os.replace(tmp, self._jsonl)

    # -- internals --

    @contextlib.contextmanager
    def _connect(self):
        """A context manager; the connection is closed on exit, never leaked."""
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            yield conn
        finally:
            conn.close()

    def _ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "bodies").mkdir(exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _db_error(self, e: sqlite3.Error) -> StoreError:
        return StoreError(
            f"state database {self.db_path} is unreadable: {e}. "
            f"Fix: delete {self.db_path} and fetch again - the JSONL is "
            "untouched (reindex regenerates the database in a later stage)."
        )
