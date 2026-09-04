"""Thread assembly tests (docs/plan.md section 8).

Messages here are synthetic but shaped like real mail: every message has a
From/To/Subject/Message-ID/Date, and reply chains carry References or
In-Reply-To exactly as real clients emit them. The real mailbox currently
has no Sent messages, so the Sent-side behaviour (`awaiting_my_reply`,
`i_replied_at`) is pinned by these synthetics only.

Keying rules under test, first match wins:
  References chain root -> In-Reply-To -> normalized subject + participant
  overlap. Subject normalization strips Re:, RE:, Fwd:, FW:, Re[n]:, and the
  Greek "Απ:" / "ΑΠ:".
"""

from __future__ import annotations

import unittest

from mailbrief import threads


def _raw(
    subject: str,
    mid: str,
    *,
    references: str | None = None,
    in_reply_to: str | None = None,
    to: str = "me@example.com, a@x.com",
    cc: str | None = None,
    date: str = "2026-09-01T06:00:00+00:00",
    from_: str = "A <a@x.com>",
) -> bytes:
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
    return ("\r\n".join(headers) + "\r\n\r\nbody text\r\n").encode()


def _rec(
    uid: int,
    mid: str,
    date: str,
    subject: str,
    folder: str = "INBOX",
) -> dict:
    """A JSONL-shaped record, as store.py writes it."""
    return {
        "uid": uid,
        "message_id": mid,
        "date": date,
        "from": "A <a@x.com>",
        "subject": subject,
        "account": "primary",
        "folder": folder,
        "file": f"bodies/{uid}.txt",
    }


def _assemble(records: list[dict], raws: dict[int, bytes]) -> list[threads.Thread]:
    return threads.assemble(records, lambda uid: raws.get(uid), my_addresses=("me@example.com",))


class NormalizeSubjectTests(unittest.TestCase):
    def test_strips_re_case_insensitive(self):
        self.assertEqual(threads.normalize_subject("Re: Status update"), "status update")

    def test_strips_repeated_prefixes(self):
        self.assertEqual(threads.normalize_subject("RE: Re: Fwd: Status"), "status")

    def test_strips_indexed_re(self):
        self.assertEqual(threads.normalize_subject("Re[2]: Status"), "status")
        self.assertEqual(threads.normalize_subject("Re[12]: Status"), "status")

    def test_strips_greek_prefixes(self):
        self.assertEqual(threads.normalize_subject("Απ: Καλημέρα"), "καλημέρα")
        self.assertEqual(threads.normalize_subject("ΑΠ: Καλημέρα"), "καλημέρα")

    def test_untouched_subject_kept(self):
        self.assertEqual(threads.normalize_subject("Status update"), "status update")
        self.assertEqual(threads.normalize_subject("(no subject)"), "(no subject)")

    def test_no_prefix_only_colon_not_stripped(self):
        # "FW:" is a prefix; a plain colon inside the subject is not.
        self.assertEqual(threads.normalize_subject("Meeting: agenda"), "meeting: agenda")


class MessageIdTests(unittest.TestCase):
    def test_ids_strip_brackets_and_lowercase(self):
        self.assertEqual(threads.message_id_key("<ABC@X.com>"), "abc@x.com")

    def test_empty_reference_header_yields_no_ids(self):
        self.assertEqual(threads.message_ids(""), [])
        self.assertEqual(threads.message_ids(None), [])


class KeyingTests(unittest.TestCase):
    def test_references_root_is_key(self):
        raws = {
            1: _raw("Thread", "root@x.com", references="<root@x.com> <child@x.com>"),
        }
        t = _assemble([_rec(1, "root@x.com", "2026-09-01T06:00:00+00:00", "Thread")], raws)[0]
        self.assertEqual(t.key, "root@x.com")

    def test_in_reply_to_fallback_when_no_references(self):
        raws = {1: _raw("Thread", "child@x.com", in_reply_to="<ROOT@X.com>")}
        t = _assemble([_rec(1, "child@x.com", "2026-09-01T06:00:00+00:00", "Thread")], raws)[0]
        self.assertEqual(t.key, "root@x.com")

    def test_references_wins_over_in_reply_to(self):
        raws = {
            1: _raw(
                "Thread", "mid@x.com",
                references="<root@x.com>", in_reply_to="<other@x.com>",
            )
        }
        t = _assemble([_rec(1, "mid@x.com", "2026-09-01T06:00:00+00:00", "Thread")], raws)[0]
        self.assertEqual(t.key, "root@x.com")

    def test_subject_participants_key_when_no_ids(self):
        # Same sender, same subject, overlapping recipients (a@x in both) =
        # one conversation. The user's own address is in every message and is
        # excluded from the overlap check.
        raws = {
            1: _raw("Status", "one@x.com", to="me@example.com, a@x.com"),
            2: _raw("Status", "two@x.com", to="me@example.com, a@x.com"),
        }
        recs = [
            _rec(1, "one@x.com", "2026-09-01T06:00:00+00:00", "Status"),
            _rec(2, "two@x.com", "2026-09-01T06:10:00+00:00", "Status"),
        ]
        ts = _assemble(recs, raws)
        self.assertEqual(len(ts), 1, "shared subject + overlapping participants = one thread")

    def test_disjoint_participants_stay_separate(self):
        # Different senders, same subject, disjoint recipients = two threads.
        raws = {
            1: _raw("Status", "one@x.com", to="me@example.com, a@x.com",
                    from_="B <b@x.com>"),
            2: _raw("Status", "two@x.com", to="me@example.com, c@x.com",
                    from_="C <c@x.com>"),
        }
        recs = [
            _rec(1, "one@x.com", "2026-09-01T06:00:00+00:00", "Status"),
            _rec(2, "two@x.com", "2026-09-01T06:10:00+00:00", "Status"),
        ]
        ts = _assemble(recs, raws)
        self.assertEqual(len(ts), 2, "same subject, disjoint participants = separate threads")

    def test_own_address_alone_does_not_join(self):
        # Two senders who each wrote to me only, same subject: separate
        # threads. The user's own address is not a shared participant.
        raws = {
            1: _raw("Status", "one@x.com", to="me@example.com",
                    from_="B <b@x.com>"),
            2: _raw("Status", "two@x.com", to="me@example.com",
                    from_="C <c@x.com>"),
        }
        recs = [
            _rec(1, "one@x.com", "2026-09-01T06:00:00+00:00", "Status"),
            _rec(2, "two@x.com", "2026-09-01T06:10:00+00:00", "Status"),
        ]
        ts = _assemble(recs, raws)
        self.assertEqual(len(ts), 2)

    def test_reply_joins_chain_by_reference_not_subject(self):
        # A reply references the root even when its subject differs.
        raws = {
            1: _raw("Original title", "root@x.com"),
            2: _raw("Re: changed title", "child@x.com",
                    references="<root@x.com>", in_reply_to="<root@x.com>"),
        }
        recs = [
            _rec(1, "root@x.com", "2026-09-01T06:00:00+00:00", "Original title"),
            _rec(2, "child@x.com", "2026-09-01T06:30:00+00:00", "Re: changed title"),
        ]
        ts = _assemble(recs, raws)
        self.assertEqual(len(ts), 1)

    def test_missing_raw_falls_back_to_subject(self):
        # No raw file: no references known, no participants known. Two
        # messages with the same normalized subject still join.
        recs = [
            _rec(1, "one@x.com", "2026-09-01T06:00:00+00:00", "Status"),
            _rec(2, "two@x.com", "2026-09-01T06:10:00+00:00", "Re: Status"),
        ]
        ts = _assemble(recs, {})
        self.assertEqual(len(ts), 1)


class ThreadOrderingTests(unittest.TestCase):
    def test_messages_sorted_by_date(self):
        raws = {
            1: _raw("Status", "root@x.com", date="2026-09-01T06:00:00+00:00"),
            2: _raw("Status", "later@x.com", date="2026-09-01T08:00:00+00:00",
                    references="<root@x.com>", in_reply_to="<root@x.com>"),
        }
        recs = [
            _rec(1, "root@x.com", "2026-09-01T06:00:00+00:00", "Status"),
            _rec(2, "later@x.com", "2026-09-01T08:00:00+00:00", "Status"),
        ]
        t = _assemble(recs, raws)[0]
        self.assertEqual([m["uid"] for m in t.messages], [1, 2])

    def test_threads_sorted_by_last_activity_descending(self):
        raws = {
            1: _raw("Old", "old@x.com", date="2026-08-31T06:00:00+00:00"),
            2: _raw("New", "new@x.com", date="2026-09-01T06:00:00+00:00"),
        }
        recs = [
            _rec(1, "old@x.com", "2026-08-31T06:00:00+00:00", "Old"),
            _rec(2, "new@x.com", "2026-09-01T06:00:00+00:00", "New"),
        ]
        ts = _assemble(recs, raws)
        self.assertEqual([t.messages[0]["uid"] for t in ts], [2, 1])


class AwaitingReplyTests(unittest.TestCase):
    def test_awaiting_true_when_last_inbound_no_sent(self):
        raws = {
            1: _raw("Status", "root@x.com"),
            2: _raw("Re: Status", "child@x.com",
                    references="<root@x.com>", in_reply_to="<root@x.com>",
                    date="2026-09-01T07:00:00+00:00"),
        }
        recs = [
            _rec(1, "root@x.com", "2026-09-01T06:00:00+00:00", "Status"),
            _rec(2, "child@x.com", "2026-09-01T07:00:00+00:00", "Re: Status"),
        ]
        t = _assemble(recs, raws)[0]
        self.assertTrue(t.awaiting_my_reply)
        self.assertIsNone(t.i_replied_at)

    def test_awaiting_false_when_last_is_sent_reply(self):
        raws = {
            1: _raw("Status", "root@x.com"),
            2: _raw("Re: Status", "child@x.com",
                    references="<root@x.com>", in_reply_to="<root@x.com>",
                    date="2026-09-01T07:00:00+00:00"),
            3: _raw("Re: Status", "myreply@x.com",
                    references="<root@x.com> <child@x.com>",
                    in_reply_to="<child@x.com>",
                    date="2026-09-01T08:00:00+00:00"),
        }
        recs = [
            _rec(1, "root@x.com", "2026-09-01T06:00:00+00:00", "Status"),
            _rec(2, "child@x.com", "2026-09-01T07:00:00+00:00", "Re: Status"),
            _rec(3, "myreply@x.com", "2026-09-01T08:00:00+00:00", "Re: Status",
                 folder="INBOX.Sent"),
        ]
        t = _assemble(recs, raws)[0]
        self.assertFalse(t.awaiting_my_reply)
        self.assertEqual(t.i_replied_at, "2026-09-01T08:00:00+00:00")

    def test_awaiting_true_when_sent_exists_but_does_not_reference_last(self):
        # User replied to the first message; a NEW inbound arrived after that.
        # The Sent message references the earlier one, not the new one, so the
        # user still owes a reply to the new arrival.
        raws = {
            1: _raw("Status", "root@x.com", date="2026-09-01T06:00:00+00:00"),
            2: _raw("Re: Status", "myreply@x.com", in_reply_to="<root@x.com>",
                    date="2026-09-01T07:00:00+00:00"),
            3: _raw("Re: Status", "followup@x.com", in_reply_to="<root@x.com>",
                    references="<root@x.com>", date="2026-09-01T08:00:00+00:00"),
        }
        recs = [
            _rec(1, "root@x.com", "2026-09-01T06:00:00+00:00", "Status"),
            _rec(2, "myreply@x.com", "2026-09-01T07:00:00+00:00", "Re: Status",
                 folder="INBOX.Sent"),
            _rec(3, "followup@x.com", "2026-09-01T08:00:00+00:00", "Re: Status"),
        ]
        t = _assemble(recs, raws)[0]
        self.assertTrue(t.awaiting_my_reply)
        self.assertEqual(t.i_replied_at, "2026-09-01T07:00:00+00:00")

    def test_awaiting_false_when_sent_references_last_despite_date_skew(self):
        # Literal plan wording: "no Sent message references it". A Sent reply
        # that references the last inbound (clock skew aside) clears the flag.
        raws = {
            1: _raw("Status", "root@x.com", date="2026-09-01T06:00:00+00:00"),
            2: _raw("Re: Status", "followup@x.com", in_reply_to="<root@x.com>",
                    references="<root@x.com>", date="2026-09-01T07:00:00+00:00"),
            3: _raw("Re: Status", "myreply@x.com", in_reply_to="<followup@x.com>",
                    references="<root@x.com> <followup@x.com>",
                    date="2026-09-01T06:59:00+00:00"),
        }
        recs = [
            _rec(1, "root@x.com", "2026-09-01T06:00:00+00:00", "Status"),
            _rec(2, "followup@x.com", "2026-09-01T07:00:00+00:00", "Re: Status"),
            _rec(3, "myreply@x.com", "2026-09-01T06:59:00+00:00", "Re: Status",
                 folder="INBOX.Sent"),
        ]
        t = _assemble(recs, raws)[0]
        self.assertFalse(t.awaiting_my_reply)
        self.assertEqual(t.i_replied_at, "2026-09-01T06:59:00+00:00")


class SentFolderTests(unittest.TestCase):
    def test_sent_folders_recognized(self):
        for folder in ("INBOX.Sent", "Sent", "INBOX/Sent"):
            with self.subTest(folder=folder):
                self.assertTrue(threads.is_sent_folder(folder))

    def test_inbox_not_sent(self):
        self.assertFalse(threads.is_sent_folder("INBOX"))


if __name__ == "__main__":
    unittest.main()
