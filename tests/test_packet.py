"""Briefing packet tests (docs/plan.md section 9).

Messages are written through the real Store (save_message -> save_parsed ->
save_classified, exactly the production write order) into a temp dir, so the
packet stage reads the same JSONL + raw/ + bodies/ layout the real collector
produces. Labels come from the JSONL - the packet never re-classifies.
"""

from __future__ import annotations

import datetime
import pathlib
import tempfile
import unittest
from email.utils import format_datetime

from mailbrief import packet, parse
from mailbrief.config import Account, Config
from mailbrief.store import Store

ME = "me@example.com"
CONFIG = Config(
    accounts=(),
    bulk_domains=(),
    never_bulk_senders=(),
    direct_max_recipients=5,
    packet_max_chars=40000,
    addresses=(ME,),
)
NOW = "2026-09-01T07:05:00+00:00"
SINCE = "2026-08-31T18:00:00+00:00"  # previous evening run, UTC


def _msg(
    subject: str,
    mid: str,
    *,
    date: str = "2026-09-01T06:00:00+00:00",
    from_: str = "A <a@x.com>",
    to: str = "me@example.com, a@x.com",
    cc: str | None = None,
    references: str | None = None,
    in_reply_to: str | None = None,
) -> bytes:
    # The parser reads the Date header via email.utils.parsedate_to_datetime,
    # which wants RFC 2822 - the real mailbox format. Tests pass ISO and
    # convert, so expectations stay readable.
    date = format_datetime(datetime.datetime.fromisoformat(date))
    headers = [
        f"From: {from_}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Message-ID: <{mid}>",
        f"Date: {date}",
    ]
    if references:
        headers.append(f"References: {references}")
    if in_reply_to:
        headers.append(f"In-Reply-To: {in_reply_to}")
    if cc:
        headers.append(f"Cc: {cc}")
    return ("\r\n".join(headers) + "\r\n\r\nraw body\r\n").encode()


class _TempStore:
    """A real Store in a temp dir; records go through the production writers."""

    def __init__(self, tmp: pathlib.Path) -> None:
        account = Account(
            name="primary", host="h", port=993, username="u",
            auth="password", folders=("INBOX", "INBOX.Sent"),
        )
        self.store = Store(account, tmp)

    def put(
        self,
        folder: str,
        uid: int,
        raw: bytes,
        body: str,
        label: str,
        evidence: str,
    ) -> None:
        st = self.store
        rec = parse.envelope(raw, uid, None)  # the production envelope record
        rec["account"] = st.account.name
        rec["folder"] = folder
        rec["fetched_at"] = NOW
        rec["file"] = f"bodies/{uid}.txt"
        st.save_message(folder, uid, rec, raw, 1)
        st.save_parsed(
            folder, uid, body, raw,
            {"parsed_at": NOW, "charset": "utf-8", "declared_charset": "utf-8",
             "body_html": False, "body_chars": len(body), "trimmed": 0,
             "raw_file": f"raw/{uid}.txt"},
        )
        if label is not None:
            st.save_classified(
                folder, uid,
                {"label": label, "label_evidence": evidence, "classified_at": NOW},
            )


def _packet(store: _TempStore, since: str | None = SINCE, window: str = "morning"):
    return packet.build_packet(CONFIG, since, window, [store.store], now=NOW)


class BuildTests(unittest.TestCase):
    def _store(self):
        return _TempStore(pathlib.Path(tempfile.mkdtemp()))

    def test_counts_and_window(self):
        t = self._store()
        t.put("INBOX", 1, _msg("Hello", "one@x.com"), "hi there", "direct",
              "me@example.com in To, To+Cc 2 <= 5")
        t.put("INBOX", 2, _msg("Old", "old@x.com", date="2026-08-31T10:00:00+00:00"),
              "old body", "direct", "ev")
        data = _packet(t)
        self.assertEqual(data["new_counts"]["total"], 1)
        self.assertEqual(data["new_counts"]["direct"], 1)
        self.assertEqual(data["window"], "morning")
        self.assertEqual(data["generated_at"], NOW)
        self.assertEqual(data["since"], SINCE)

    def test_thread_has_metadata(self):
        t = self._store()
        t.put("INBOX", 1, _msg("Status", "root@x.com"), "first body", "direct", "ev1")
        t.put("INBOX", 2, _msg("Re: Status", "child@x.com", date="2026-09-01T06:30:00+00:00",
                               references="<root@x.com>", in_reply_to="<root@x.com>"),
              "second body", "cc", "me@example.com in Cc only")
        data = _packet(t)
        self.assertEqual(data["thread_count"], 1)
        th = data["threads"][0]
        self.assertEqual(th["subject"], "Re: Status")
        self.assertEqual(th["count"], 2)
        self.assertEqual(th["first"], "2026-09-01T06:00:00+00:00")
        self.assertEqual(th["last"], "2026-09-01T06:30:00+00:00")
        self.assertEqual(th["labels"], {"direct": 1, "cc": 1, "bulk": 0})
        self.assertTrue(th["awaiting_my_reply"])
        self.assertIsNone(th["i_replied_at"])
        self.assertIn("a@x.com", th["participants"])
        self.assertEqual(len(th["messages"]), 2)
        self.assertEqual(th["messages"][0]["body"], "first body")
        self.assertEqual(th["messages"][1]["label"], "cc")
        self.assertEqual(th["messages"][1]["evidence"], "me@example.com in Cc only")

    def test_sent_message_in_thread(self):
        t = self._store()
        t.put("INBOX", 1, _msg("Status", "root@x.com"), "inbound", "direct", "ev")
        t.put("INBOX.Sent", 2, _msg("Re: Status", "myreply@x.com",
                                    date="2026-09-01T07:00:00+00:00",
                                    references="<root@x.com>",
                                    in_reply_to="<root@x.com>"),
              "my words", "direct", "n/a")
        data = _packet(t)
        th = data["threads"][0]
        self.assertFalse(th["awaiting_my_reply"])
        self.assertEqual(th["i_replied_at"], "2026-09-01T07:00:00+00:00")
        self.assertTrue(th["messages"][1]["sent"])

    def test_earlier_messages_counted(self):
        t = self._store()
        t.put("INBOX", 1, _msg("Status", "root@x.com", date="2026-08-31T06:00:00+00:00"),
              "old", "direct", "ev")
        t.put("INBOX", 2, _msg("Re: Status", "new@x.com", date="2026-09-01T19:00:00+00:00",
                               references="<root@x.com>", in_reply_to="<root@x.com>"),
              "new", "direct", "ev")
        data = _packet(t)
        th = data["threads"][0]
        self.assertEqual(th["earlier"], 1)
        self.assertEqual(len(th["messages"]), 1)

    def test_bulk_goes_to_table_not_thread(self):
        t = self._store()
        t.put("INBOX", 1, _msg("Newsletter", "n@x.com", date="2026-09-01T19:00:00+00:00"),
              "news body", "bulk", "List-Id present")
        t.put("INBOX", 2, _msg("Newsletter", "n2@x.com", date="2026-09-01T19:10:00+00:00"),
              "news body 2", "bulk", "List-Id present")
        data = _packet(t)
        self.assertEqual(data["thread_count"], 0)
        self.assertEqual(len(data["bulk_rows"]), 1)
        self.assertEqual(data["bulk_rows"][0]["count"], 2)
        self.assertEqual(data["bulk_rows"][0]["subject"], "Newsletter")

    def test_sent_message_not_in_bulk_table(self):
        t = self._store()
        t.put("INBOX.Sent", 1, _msg("Newsletter", "s@x.com", date="2026-09-01T19:00:00+00:00"),
              "sent body", "bulk", "List-Id present")
        data = _packet(t)
        self.assertEqual(data["bulk_rows"], [])

    def test_unclassified_message_reported(self):
        t = self._store()
        t.put("INBOX", 1, _msg("Status", "u@x.com", date="2026-09-01T19:00:00+00:00"),
              "unlabelled", None, None)  # never classified
        data = _packet(t)
        self.assertEqual(data["new_counts"]["unclassified"], 1)
        self.assertIn("unclassified", data["threads"][0]["messages"][0]["label"])

    def test_no_since_means_everything(self):
        t = self._store()
        t.put("INBOX", 1, _msg("Old", "o@x.com", date="2026-08-01T06:00:00+00:00"),
              "old", "direct", "ev")
        data = _packet(t, since=None)
        self.assertEqual(data["new_counts"]["total"], 1)


class RenderTests(unittest.TestCase):
    def _data(self, store: _TempStore, **kw):
        return _packet(store, **kw)

    def test_header_and_sections(self):
        t = _TempStore(pathlib.Path(tempfile.mkdtemp()))
        t.put("INBOX", 1, _msg("Status", "root@x.com"), "first body", "direct", "ev1")
        text = packet.render(self._data(t), 40000)
        self.assertIn("# Email briefing packet - Morning", text)
        self.assertIn("generated:", text)
        self.assertIn("1 new message", text)
        self.assertIn("## Threads", text)
        self.assertIn("Status", text)
        self.assertIn("first body", text)
        self.assertIn("direct", text)
        self.assertIn("ev1", text)
        self.assertIn("awaiting my reply", text)
        self.assertIn("Trims: none", text)

    def test_bulk_table_rendered_without_bodies(self):
        t = _TempStore(pathlib.Path(tempfile.mkdtemp()))
        t.put("INBOX", 1, _msg("Newsletter", "n@x.com", date="2026-09-01T19:00:00+00:00"),
              "SECRETBODY", "bulk", "List-Id present")
        text = packet.render(self._data(t), 40000)
        self.assertIn("## Bulk", text)
        self.assertIn("Newsletter", text)
        self.assertNotIn("SECRETBODY", text)

    def test_sent_message_marker_no_body(self):
        t = _TempStore(pathlib.Path(tempfile.mkdtemp()))
        t.put("INBOX", 1, _msg("Status", "root@x.com"), "inbound", "direct", "ev")
        t.put("INBOX.Sent", 2, _msg("Re: Status", "myreply@x.com",
                                    date="2026-09-01T07:00:00+00:00",
                                    in_reply_to="<root@x.com>"),
              "MYWORDS", "direct", "n/a")
        text = packet.render(self._data(t), 40000)
        self.assertIn("(sent)", text)
        self.assertNotIn("MYWORDS", text)
        self.assertIn("you replied", text)

    def test_earlier_note_rendered(self):
        t = _TempStore(pathlib.Path(tempfile.mkdtemp()))
        t.put("INBOX", 1, _msg("Status", "root@x.com", date="2026-08-31T06:00:00+00:00"),
              "old", "direct", "ev")
        t.put("INBOX", 2, _msg("Re: Status", "new@x.com", date="2026-09-01T19:00:00+00:00",
                               references="<root@x.com>", in_reply_to="<root@x.com>"),
              "new", "direct", "ev")
        text = packet.render(self._data(t), 40000)
        self.assertIn("1 earlier message", text)

    def test_threads_newest_activity_first(self):
        t = _TempStore(pathlib.Path(tempfile.mkdtemp()))
        t.put("INBOX", 1, _msg("Old thread", "old@x.com", date="2026-09-01T06:00:00+00:00"),
              "old", "direct", "ev")
        t.put("INBOX", 2, _msg("New thread", "new@x.com", date="2026-09-01T19:30:00+00:00"),
              "new", "direct", "ev")
        text = packet.render(self._data(t), 40000)
        self.assertLess(text.index("New thread"), text.index("Old thread"))

    def test_cc_bodies_truncated_when_over_budget(self):
        t = _TempStore(pathlib.Path(tempfile.mkdtemp()))
        t.put("INBOX", 1, _msg("Status", "d@x.com"), "D" * 100, "direct", "ev")
        t.put("INBOX", 2, _msg("Re: Status", "c@x.com", date="2026-09-01T06:30:00+00:00",
                               references="<d@x.com>", in_reply_to="<d@x.com>",
                               cc="me@example.com", to="a@x.com"),
              "C" * 3000, "cc", "me@example.com in Cc only")
        text = packet.render(self._data(t), 3000)
        self.assertIn("truncated to 1500", text)
        self.assertIn("D" * 100, text)       # direct kept full
        self.assertNotIn("C" * 3000, text)   # cc truncated
        self.assertIn("C" * 1500, text)

    def test_cc_bodies_dropped_when_still_over(self):
        t = _TempStore(pathlib.Path(tempfile.mkdtemp()))
        t.put("INBOX", 1, _msg("Status", "d@x.com"), "D" * 100, "direct", "ev")
        t.put("INBOX", 2, _msg("Re: Status", "c@x.com", date="2026-09-01T06:30:00+00:00",
                               references="<d@x.com>", in_reply_to="<d@x.com>",
                               cc="me@example.com", to="a@x.com"),
              "C" * 3000, "cc", "me@example.com in Cc only")
        text = packet.render(self._data(t), 1200)
        self.assertIn("dropped", text)
        self.assertNotIn("C" * 1500, text)
        self.assertIn("D" * 100, text)

    def test_direct_always_kept_even_over_budget(self):
        t = _TempStore(pathlib.Path(tempfile.mkdtemp()))
        t.put("INBOX", 1, _msg("Direct", "d@x.com"), "D" * 300, "direct", "ev")
        text = packet.render(self._data(t), 200)
        self.assertIn("D" * 300, text)
        self.assertIn("over budget", text)


if __name__ == "__main__":
    unittest.main()
