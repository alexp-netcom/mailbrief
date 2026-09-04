"""Message parsing.

Stage 2 builds the envelope: uid, Message-ID, UTC date, From, Subject.
Stage 3 adds body cleaning: MIME walk (text/plain preferred, text/html
fallback), a charset fallback ladder, HTML-to-text conversion with stdlib
html.parser only, and trailing quote-chain stripping. Every cleaning step
records its evidence (which charset, how many characters trimmed) so a bad
call is visible, never silent.

Every function here is wrapped so it cannot raise; a bad message degrades to
recorded evidence, not a crash.
"""

from __future__ import annotations

import datetime
import email
import html
import html.parser
import imaplib
import re
from email import policy
from email.utils import getaddresses, parsedate_to_datetime


def _safe_get(msg, name: str) -> str:
    try:
        value = msg.get(name, "")
    except Exception:
        return ""
    return value if isinstance(value, str) else str(value)


def _date_utc(msg, internaldate_raw: str | None) -> str | None:
    """Best-effort UTC ISO timestamp: Date header first, INTERNALDATE second.

    A missing or unparsable Date header is common; INTERNALDATE is the
    server's arrival stamp and covers that case. If neither yields a timezone,
    the timestamp is treated as UTC and the record is still honest - it names
    a source in the caller's metadata when one exists.
    """
    date_header = _safe_get(msg, "Date")
    if date_header:
        try:
            dt = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            dt = None
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(datetime.timezone.utc).isoformat()
    if internaldate_raw:
        try:
            # imaplib parses INTERNALDATE into a local-time struct_time.
            t = imaplib.Internaldate2tuple(internaldate_raw)
            dt = datetime.datetime(*t[:6])
        except Exception:
            dt = None
        if dt is not None:
            return dt.replace(tzinfo=datetime.timezone.utc).isoformat()
    return None


def _format_from(from_header: str) -> str:
    """'Name <addr>' or the raw header; never empty."""
    if not from_header:
        return "(no From header)"
    try:
        pairs = getaddresses([from_header])
    except Exception:
        pairs = []
    for name, addr in pairs:
        if name and addr:
            return f"{name} <{addr}>"
        if addr:
            return addr
        if name:
            return name
    return from_header


def envelope(raw: bytes, uid: int, internaldate_raw: str | None) -> dict:
    """Best-effort envelope record for one message. Never raises.

    raw may be the full message or just the header section; both carry the
    headers this needs.
    """
    try:
        msg = email.message_from_bytes(raw, policy=policy.default)
    except Exception:
        msg = None
    if msg is None:
        return {
            "uid": uid,
            "message_id": None,
            "date": None,
            "from": "(unparsable message)",
            "subject": "(unparsable message)",
        }
    message_id = _safe_get(msg, "Message-ID").strip() or None
    from_header = _safe_get(msg, "From")
    subject = _safe_get(msg, "Subject").strip() or "(no subject)"
    return {
        "uid": uid,
        "message_id": message_id,
        "date": _date_utc(msg, internaldate_raw),
        "from": _format_from(from_header),
        "subject": subject,
    }


# ---------------------------------------------------------------------------
# Stage 3: body cleaning
# ---------------------------------------------------------------------------


def _best_text_part(msg):
    """(part, subtype) preferring text/plain over text/html. Never raises."""
    plain = html_part = None
    try:
        for part in msg.walk():
            try:
                ctype = part.get_content_type()
                subtype = ctype.split("/")[-1].lower()
            except Exception:
                continue
            if ctype == "text/plain" and plain is None:
                plain = part
                if html_part:
                    break
            elif ctype == "text/html" and html_part is None:
                html_part = part
                if plain:
                    break
    except Exception:
        pass
    if plain is not None:
        return plain, "plain"
    if html_part is not None:
        return html_part, "html"
    return None, None


def _decode_bytes(payload: bytes, declared: str | None) -> tuple[str, str]:
    """Charset ladder: declared -> cp1251 -> cp1253 -> latin-1 (with replace).

    Returns (text, charset_used). The last rung cannot fail, so a body always
    decodes; the recorded charset makes a wrong guess visible.
    """
    candidates = [declared] if declared else []
    candidates += ["cp1251", "cp1253", "latin-1"]
    for cs in candidates[:-1]:
        try:
            return payload.decode(cs), cs
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode(candidates[-1], errors="replace"), candidates[-1]


class _HtmlToText(html.parser.HTMLParser):
    """HTML -> plain text: drop script/style, block tags become newlines."""

    _BLOCK = frozenset(
        {
            "p", "br", "div", "li", "ul", "ol", "tr", "td", "th", "table",
            "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section",
            "article", "hr", "pre", "dl", "dt", "dd", "header", "footer",
        }
    )
    _SKIP = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs) -> None:
        t = tag.lower()
        if t in self._SKIP:
            self._skip_depth += 1
        elif t in self._BLOCK:
            self._out.append("\n")

    def handle_endtag(self, tag) -> None:
        t = tag.lower()
        if t in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif t in self._BLOCK:
            self._out.append("\n")

    def handle_data(self, data) -> None:
        if self._skip_depth == 0:
            self._out.append(data)


def _html_to_text(source: str) -> str:
    """Convert an HTML body to plain text. Never raises."""
    try:
        p = _HtmlToText()
        p.feed(source)
        p.close()
    except Exception:
        return ""
    text = html.unescape("".join(p._out))
    text = re.sub(r"[ \t\f\v\r]+", " ", text)  # collapse horizontal whitespace
    text = re.sub(r"[ \t]+(?=\n)", "", text)   # trailing spaces on lines
    text = re.sub(r"\n[ \t]+", "\n", text)     # leading spaces on lines
    text = re.sub(r"\n{3,}", "\n\n", text)     # collapse blank runs
    return text.strip()


_TAIL_HEADER_RE = re.compile(r"^(?:From|Sent|To|Cc|Bcc|Subject|Date|Reply-To):\s")
_TAIL_SEP_RES = [
    re.compile(r"^On\s+.+\s+wrote:$"),
    re.compile(r"^-----Original Message-----$"),
    re.compile(r"^_{20,}$"),
    re.compile(r"^Sent from my\b"),
]


def strip_quotes(text: str) -> tuple[str, int]:
    """Trim a trailing quoted chain; return (kept_text, chars_trimmed).

    Walks from the end and drops quoted (`>`) lines, the usual separators
    (``On ... wrote:``, ``-----Original Message-----``, a long underscore
    line, ``Sent from my``) and the small header block that follows a
    separator. Stops at the first ordinary line, so a message with no quoted
    tail is untouched and trimmed == 0.
    """
    before = len(text)
    lines = text.split("\n")
    i = len(lines)
    while i > 0:
        s = lines[i - 1].strip()
        if s == "" or s.startswith(">"):
            i -= 1
            continue
        if _TAIL_HEADER_RE.match(s) or any(r.search(s) for r in _TAIL_SEP_RES):
            i -= 1
            continue
        break
    kept = "\n".join(lines[:i]).strip()
    return kept, before - len(kept)


def decode_body(raw: bytes) -> dict:
    """Extract and clean the body of a raw message. Never raises.

    Returns:
      found            a text/plain or text/html part existed and decoded
      html             the part was text/html (HTML->text pass ran)
      declared_charset the Content-Type charset the message declared
      charset          the charset actually used after the fallback ladder
      text             cleaned body, quoted tail trimmed
      trimmed          characters removed by quote stripping
    """
    result = {
        "found": False,
        "html": False,
        "declared_charset": None,
        "charset": None,
        "text": "",
        "trimmed": 0,
    }
    try:
        msg = email.message_from_bytes(raw, policy=policy.default)
    except Exception:
        return result
    part, subtype = _best_text_part(msg)
    if part is None:
        return result
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    try:
        declared = part.get_content_charset()
    except Exception:
        declared = None
    result["declared_charset"] = declared
    text = None
    if isinstance(payload, str):
        # No transfer encoding; the parser already decoded it.
        text = payload.lstrip("\ufeff")
        result["charset"] = declared
    elif isinstance(payload, (bytes, bytearray)):
        text, used = _decode_bytes(bytes(payload), declared)
        if text.startswith("\ufeff"):
            text = text[1:]
        result["charset"] = used
    if text is None:
        return result  # unreadable payload; found stays False, evidence kept
    text = text.replace("\r\n", "\n").replace("\r", "\n")  # normalize endings
    result["found"] = True
    result["html"] = subtype == "html"
    if result["html"]:
        text = _html_to_text(text)
    text, trimmed = strip_quotes(text)
    result["text"] = text
    result["trimmed"] = trimmed
    return result
