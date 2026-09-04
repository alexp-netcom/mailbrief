"""Stage 3 parser tests.

Fixtures are synthetic messages with realistic MIME shapes (a Greek
ISO-8859-7 message, an HTML-only newsletter, a calendar invite). No real
names, addresses, or domains appear anywhere in them.
"""

from __future__ import annotations

import base64
import pathlib
import unittest

from mailbrief import parse

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _raw_message(body_bytes: bytes, ctype: str = "text/plain", charset: str = "utf-8") -> bytes:
    """A minimal single-part message with a base64-encoded body, so
    get_payload(decode=True) always returns bytes regardless of the charset."""
    head = (
        b"From: sender@example.com\r\n"
        b"To: user@example.com\r\n"
        b"Subject: test\r\n"
        b"MIME-Version: 1.0\r\n"
    )
    head += f"Content-Type: {ctype}; charset={charset}\r\n".encode()
    head += b"Content-Transfer-Encoding: base64\r\n\r\n"
    return head + base64.b64encode(body_bytes)


def _multipart(parts: list[bytes]) -> bytes:
    """A multipart/alternative message whose parts are base64-encoded."""
    boundary = b"----=testboundary"
    out = [
        b"From: sender@example.com\r\n",
        b"To: user@example.com\r\n",
        b"Subject: multipart\r\n",
        b"MIME-Version: 1.0\r\n",
        b"Content-Type: multipart/alternative; boundary=\"----=testboundary\"\r\n\r\n",
    ]
    for part in parts:
        out += [b"--" + boundary + b"\r\n", part, b"\r\n"]
    out.append(b"--" + boundary + b"--\r\n")
    return b"".join(out)


class FixtureTests(unittest.TestCase):
    """Real messages (sanitized): the parser must handle real-world shapes."""

    def test_greek_iso_8859_7(self) -> None:
        d = parse.decode_body(_fixture("greek_iso88597.eml"))
        self.assertTrue(d["found"])
        self.assertFalse(d["html"])
        self.assertEqual(d["charset"], "iso-8859-7")
        self.assertEqual(d["declared_charset"], "iso-8859-7")
        self.assertIn("Καλημέρα σας", d["text"])  # decoded Greek
        self.assertGreater(len(d["text"]), 1000)

    def test_html_only_utf8(self) -> None:
        d = parse.decode_body(_fixture("html_only_utf8.eml"))
        self.assertTrue(d["found"])
        self.assertTrue(d["html"])
        self.assertEqual(d["charset"], "utf-8")
        self.assertIn("Monthly update", d["text"])
        self.assertNotIn("<", d["text"])  # HTML tags gone

    def test_calendar_only_has_no_text_body(self) -> None:
        d = parse.decode_body(_fixture("calendar_only.eml"))
        self.assertFalse(d["found"])
        self.assertEqual(d["text"], "")

    def test_envelope_still_works_on_fixtures(self) -> None:
        e = parse.envelope(_fixture("greek_iso88597.eml"), 7, None)
        self.assertEqual(e["uid"], 7)
        self.assertEqual(e["subject"], "Μηνιαία αναφορά")  # RFC 2047 decoded
        self.assertIn("example.com", e["from"])


class CharsetTests(unittest.TestCase):
    def test_declared_charset_used(self) -> None:
        body = "Καλημέρα".encode("iso-8859-7")
        d = parse.decode_body(_raw_message(body, charset="iso-8859-7"))
        self.assertEqual(d["charset"], "iso-8859-7")
        self.assertEqual(d["text"], "Καλημέρα")

    def test_bogus_declared_falls_to_cp1251(self) -> None:
        body = b"\xc0\xc1"  # valid cp1251, gibberish elsewhere
        d = parse.decode_body(_raw_message(body, charset="x-bogus"))
        self.assertEqual(d["charset"], "cp1251")
        self.assertEqual(d["declared_charset"], "x-bogus")

    def test_last_rung_never_raises(self) -> None:
        # 0x98 is undefined in both cp1251 and cp1253, so the ladder ends at
        # latin-1 with errors='replace' - it can never raise.
        body = b"\x00\xff\xfe garbage \x98\xaa"
        d = parse.decode_body(_raw_message(body, charset="x-bogus"))
        self.assertEqual(d["charset"], "latin-1")
        self.assertEqual(d["text"], body.decode("latin-1", errors="replace"))


class HtmlTests(unittest.TestCase):
    def test_prefers_plain_over_html(self) -> None:
        plain = _raw_message(b"plain words", ctype="text/plain")
        html_part = _raw_message(b"<b>html words</b>", ctype="text/html")
        d = parse.decode_body(_multipart([plain, html_part]))
        self.assertTrue(d["found"])
        self.assertFalse(d["html"])
        self.assertEqual(d["text"], "plain words")

    def test_script_and_style_dropped(self) -> None:
        html_bytes = b"<html><style>body{display:none}</style><p>Hello</p>" \
            b"<script>alert('x')</script><p>World</p></html>"
        d = parse.decode_body(_raw_message(html_bytes, ctype="text/html"))
        self.assertTrue(d["html"])
        self.assertNotIn("display", d["text"])
        self.assertNotIn("alert", d["text"])
        self.assertIn("Hello", d["text"])
        self.assertIn("World", d["text"])

    def test_entities_unescaped_and_whitespace_collapsed(self) -> None:
        html_bytes = b"<p>a &amp; b</p><p>c</p><p>   spaced   </p>"
        d = parse.decode_body(_raw_message(html_bytes, ctype="text/html"))
        self.assertEqual(d["text"], "a & b\n\nc\n\nspaced")


class QuoteStripTests(unittest.TestCase):
    def test_gt_block(self) -> None:
        kept, trimmed = parse.strip_quotes("hello\n\n> quoted\n> more")
        self.assertEqual(kept, "hello")
        self.assertEqual(trimmed, len("hello\n\n> quoted\n> more") - len("hello"))

    def test_on_wrote(self) -> None:
        text = "reply text\n\nOn Fri, Aug 28, 2026 at 1:40 PM S wrote:\n\n> old"
        kept, trimmed = parse.strip_quotes(text)
        self.assertEqual(kept, "reply text")
        self.assertGreater(trimmed, 0)

    def test_original_message_block(self) -> None:
        text = "my reply\n\n-----Original Message-----\nFrom: a@example.com\n" \
            "Sent: Mon, 1 Sep 2026\nTo: user@example.com\nSubject: old\n\n> quoted"
        kept, trimmed = parse.strip_quotes(text)
        self.assertEqual(kept, "my reply")
        self.assertGreater(trimmed, 0)

    def test_underscore_rule(self) -> None:
        text = "body\n\n________________________________\n\n> quoted"
        kept, trimmed = parse.strip_quotes(text)
        self.assertEqual(kept, "body")
        self.assertGreater(trimmed, 0)

    def test_sent_from_my(self) -> None:
        kept, trimmed = parse.strip_quotes("ok\n\nSent from my iPhone")
        self.assertEqual(kept, "ok")
        self.assertGreater(trimmed, 0)

    def test_no_tail_untouched(self) -> None:
        kept, trimmed = parse.strip_quotes("plain body\nsecond line")
        self.assertEqual(kept, "plain body\nsecond line")
        self.assertEqual(trimmed, 0)

    def test_all_quoted_becomes_empty(self) -> None:
        kept, trimmed = parse.strip_quotes("> a\n> b\n\n")
        self.assertEqual(kept, "")
        self.assertGreater(trimmed, 0)


class RobustnessTests(unittest.TestCase):
    def test_decode_never_raises_on_garbage(self) -> None:
        for garbage in (b"", b"\xff\xfe\x00", b"not a message at all", b"<html><broken"):
            d = parse.decode_body(garbage)
            self.assertIsInstance(d, dict)
            self.assertIn("text", d)

    def test_envelope_never_raises_on_garbage(self) -> None:
        for garbage in (b"", b"\xff\xfe", b"\x00" * 10):
            e = parse.envelope(garbage, 1, None)
            self.assertIsInstance(e, dict)

    def test_crlf_normalized(self) -> None:
        body = b"line one\r\nline two\r\n"
        d = parse.decode_body(_raw_message(body))
        self.assertEqual(d["text"], "line one\nline two")


if __name__ == "__main__":
    unittest.main()
