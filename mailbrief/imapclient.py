"""Read-only IMAP client.

Folders are opened with EXAMINE, never a writable SELECT. Nothing here may
send, delete, move, or mark mail. tests/test_readonly.py enforces this by
grepping the source for the mutating IMAP verbs.
"""

from __future__ import annotations

import base64
import imaplib
import re
import ssl
from dataclasses import dataclass

from .auth import AuthError
from .config import Account


class ImapError(Exception):
    pass


def _decode_mutf7(name: str) -> str:
    """Decode IMAP modified UTF-7 (RFC 3501 section 5.1.3) for display."""
    if "&" not in name:
        return name
    out: list[str] = []
    i = 0
    n = len(name)
    while i < n:
        c = name[i]
        if c != "&":
            out.append(c)
            i += 1
            continue
        end = name.find("-", i + 1)
        if end == -1:
            out.append(name[i:])
            break
        seg = name[i + 1 : end]
        if seg == "":
            out.append("&")
            i = end + 1
            continue
        try:
            b64 = seg.replace(",", "/")
            pad = "=" * ((4 - len(b64) % 4) % 4)
            raw = base64.b64decode(b64 + pad)
            if len(raw) % 2:
                raw += b"\x00"
            out.append(raw.decode("utf-16-be"))
        except Exception:
            out.append(name[i : end + 1])
        i = end + 1
    return "".join(out)


def _parse_list_name(line: str) -> str | None:
    """Extract the mailbox name from a LIST response line."""
    m = re.match(r'^\([^)]*\)\s+"[^"]*"\s+(.+)$', line)
    if not m:
        return None
    rest = m.group(1)
    if not rest.startswith('"'):
        return rest
    parts: list[str] = []
    i = 1
    while i < len(rest):
        ch = rest[i]
        if ch == "\\" and i + 1 < len(rest):
            parts.append(rest[i + 1])
            i += 2
        elif ch == '"':
            return "".join(parts)
        else:
            parts.append(ch)
            i += 1
    return None


def certificate_summary(cert: dict) -> str:
    subject = dict(x[0] for x in cert["subject"])
    issuer = dict(x[0] for x in cert["issuer"])
    san = ", ".join(entry for _, entry in cert.get("subjectAltName", []))
    return (
        f"CN={subject.get('commonName', '?')}, issuer CN={issuer.get('commonName', '?')}, "
        f"expires {cert.get('notAfter', '?')}, SAN={san or '?'}"
    )


@dataclass
class FolderInfo:
    name: str
    count: int | None
    error: str | None


class ImapClient:
    def __init__(self, account: Account) -> None:
        self.account = account
        self.client: imaplib.IMAP4_SSL | None = None
        self.tls_version: str | None = None
        self.certificate: dict | None = None

    def connect(self) -> None:
        ctx = ssl.create_default_context()  # verification ON, nothing skipped
        try:
            self.client = imaplib.IMAP4_SSL(self.account.host, self.account.port, ssl_context=ctx)
        except Exception as e:
            raise ImapError(
                f"cannot reach {self.account.host}:{self.account.port}: {e}. "
                "Check host and port in config.toml."
            ) from e
        self.tls_version = self.client.sock.version()
        self.certificate = self.client.sock.getpeercert()

    def welcome(self) -> str:
        return self.client.welcome.decode("utf-8", "replace")

    def login(self, auth) -> None:
        try:
            auth.login(self.client)
        except AuthError:
            raise
        except self.client.error as e:
            raise ImapError(
                f"login failed for {self.account.username}: {e}. "
                "Check the username in config.toml and the stored credential."
            ) from e

    def post_login_capabilities(self) -> tuple[str, ...]:
        typ, data = self.client.capability()
        if typ != "OK":
            raise ImapError(f"CAPABILITY failed: {data}")
        return tuple(x for x in data[0].decode("utf-8", "replace").split(" ") if x)

    def folders(self) -> list[str]:
        typ, data = self.client.list()
        if typ != "OK":
            raise ImapError(f"LIST failed: {data}")
        names = []
        for line in data:
            text = line.decode("utf-8", "replace")
            parsed = _parse_list_name(text)
            if parsed is not None:
                names.append(_decode_mutf7(parsed))
        return names

    def select_folder(self, folder: str) -> tuple[int, int, int]:
        """EXAMINE folder; returns (exists, uidvalidity, uidnext).

        Raises ImapError if the folder cannot be opened or no UIDVALIDITY is
        returned - without UIDVALIDITY, state tracking would be unsafe.
        """
        try:
            typ, data = self.client.select(folder, readonly=True)  # sends EXAMINE
        except self.client.error as e:
            raise ImapError(f"cannot open {folder!r}: {e}") from e
        if typ != "OK":
            raise ImapError(f"EXAMINE {folder!r} failed: {data}")
        try:
            exists = int(data[0])
        except (TypeError, ValueError):
            exists = 0
        uidvalidity = self._untagged_int("UIDVALIDITY")
        if uidvalidity is None:
            raise ImapError(
                f"EXAMINE {folder!r} returned no UIDVALIDITY; cannot track state safely"
            )
        return exists, uidvalidity, self._untagged_int("UIDNEXT") or 0

    def _untagged_int(self, key: str) -> int | None:
        resp = self.client.untagged_responses.get(key, [])
        if not resp:
            return None
        try:
            return int(resp[-1])
        except (TypeError, ValueError):
            return None

    def search_uids(self, start: int) -> list[int]:
        """UID SEARCH UID <start>:* - ascending uids, empty list if none.

        Two traps on this Dovecot server, both verified live:
        - A bare set like ``UID SEARCH <start>:*`` is a *sequence-number*
          set, not a UID set (RFC 3501). When the sequence count is below
          <start> it returns empty and new mail is silently missed. The
          ``UID`` key prefix is required to search by UID.
        - ``UID SEARCH UID <start>:*`` returns the last existing uid as a
          stale hit when <start> exceeds the maximum - refetched and deduped
          every run unless we drop anything below <start> here.
        """
        typ, data = self.client.uid("search", None, f"UID {start}:*")
        if typ != "OK":
            raise ImapError(f"UID SEARCH failed: {data}")
        if not data or not data[0]:
            return []
        try:
            return [int(x) for x in data[0].split() if int(x) >= start]
        except ValueError as e:
            raise ImapError(f"UID SEARCH returned garbage: {data[0]!r}") from e

    def fetch_headers(self, uids: list[int]) -> list[tuple[int, bytes, str | None]]:
        """One batched UID FETCH of (INTERNALDATE BODY.PEEK[HEADER]).

        Returns (uid, header_bytes, internaldate_raw) per message. BODY.PEEK
        never sets the Seen flag, and EXAMINE would not allow it anyway.
        """
        if not uids:
            return []
        chunk = ",".join(str(u) for u in uids)
        typ, data = self.client.uid("fetch", chunk, "(UID INTERNALDATE BODY.PEEK[HEADER])")
        if typ != "OK":
            raise ImapError(f"UID FETCH failed: {data}")
        out: list[tuple[int, bytes, str | None]] = []
        for item in data:
            if not isinstance(item, tuple):
                continue
            line = item[0].decode("utf-8", "replace")
            m = re.search(r"\bUID (\d+)", line)
            if not m:
                continue
            uid = int(m.group(1))
            internaldate = None
            m2 = re.search(r'INTERNALDATE "([^"]+)"', line)
            if m2:
                internaldate = m2.group(1)
            out.append((uid, item[1], internaldate))
        return out

    def fetch_raws(self, uids: list[int]) -> dict[int, bytes]:
        """One batched UID FETCH of BODY.PEEK[]; {uid: raw_message_bytes}.

        Batching matters: the mail server answers each message with a separate
        round trip, so fetching one message at a time costs minutes for a few
        dozen messages.
        """
        if not uids:
            return {}
        chunk = ",".join(str(u) for u in uids)
        typ, data = self.client.uid("fetch", chunk, "(UID BODY.PEEK[])")
        if typ != "OK":
            raise ImapError(f"UID FETCH failed: {data}")
        out: dict[int, bytes] = {}
        for item in data:
            if not isinstance(item, tuple):
                continue
            line = item[0].decode("utf-8", "replace")
            m = re.search(r"\bUID (\d+)", line)
            if m and isinstance(item[1], bytes):
                out[int(m.group(1))] = item[1]
        return out

    def examine_count(self, folder: str) -> tuple[int | None, str | None]:
        """EXAMINE a folder and return (message count, error)."""
        try:
            typ, data = self.client.select(folder, readonly=True)  # sends EXAMINE
        except self.client.error as e:
            return None, str(e)
        if typ != "OK":
            return None, str(data)
        try:
            return int(data[0]), None
        except (TypeError, ValueError):
            return None, str(data)

    def disconnect(self) -> None:
        try:
            self.client.logout()
        except Exception:
            pass
