"""Stage 4 classifier tests.

Messages here are minimal header-only synthetics - the classifier reads only
headers, and each test must pin down exactly one trigger. Addresses are
placeholders; no real-world data in here. Rules come from docs/plan.md
section 7, evaluated in order: bulk -> direct -> cc. First match wins.
"""

from __future__ import annotations

import email
import pathlib
import unittest
from email import policy

from mailbrief import classify
from mailbrief.config import Config

ME = "me@example.com"
CONFIG = Config(
    accounts=(),
    bulk_domains=("bulksender.com",),
    never_bulk_senders=("noreply@colleague.com",),
    direct_max_recipients=5,
    packet_max_chars=40000,
    addresses=(ME,),
)


def _msg(headers: dict[str, str]) -> email.message.Message:
    """A message with exactly the given headers."""
    data = "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
    return email.message_from_bytes(data.encode(), policy=policy.default)


def _result(msg: email.message.Message) -> dict:
    return classify.classify(msg, CONFIG)


def _label(msg: email.message.Message) -> str:
    return _result(msg)["label"]


class BulkTests(unittest.TestCase):
    def test_list_unsubscribe_is_bulk(self) -> None:
        msg = _msg(
            {
                "From": "marketing@example.com",
                "To": ME,
                "Subject": "newsletter",
                "List-Unsubscribe": "<mailto:unsub@example.com>",
            }
        )
        r = _result(msg)
        self.assertEqual(r["label"], "bulk")
        self.assertIn("List-Unsubscribe", r["evidence"])

    def test_list_id_is_bulk(self) -> None:
        r = _result(_msg({"From": "a@example.com", "To": ME, "List-Id": "list.example.com"}))
        self.assertEqual(r["label"], "bulk")
        self.assertIn("List-Id", r["evidence"])

    def test_precedence_bulk_is_bulk(self) -> None:
        r = _result(_msg({"From": "a@example.com", "To": ME, "Precedence": "bulk"}))
        self.assertEqual(r["label"], "bulk")
        self.assertIn("Precedence", r["evidence"])

    def test_precedence_list_and_junk_are_bulk(self) -> None:
        for value in ("list", "junk"):
            r = _result(_msg({"From": "a@example.com", "To": ME, "Precedence": value}))
            self.assertEqual(r["label"], "bulk", value)

    def test_auto_submitted_present_is_bulk(self) -> None:
        r = _result(_msg({"From": "a@example.com", "To": ME, "Auto-Submitted": "auto-replied"}))
        self.assertEqual(r["label"], "bulk")
        self.assertIn("Auto-Submitted", r["evidence"])

    def test_auto_submitted_no_is_not_bulk_by_this_rule(self) -> None:
        r = _result(_msg({"From": "a@example.com", "To": ME, "Auto-Submitted": "no"}))
        self.assertEqual(r["label"], "direct")

    def test_x_auto_response_suppress_is_bulk(self) -> None:
        r = _result(_msg({"From": "a@example.com", "To": ME, "X-Auto-Response-Suppress": "All"}))
        self.assertEqual(r["label"], "bulk")
        self.assertIn("X-Auto-Response-Suppress", r["evidence"])

    def test_null_return_path_is_bulk(self) -> None:
        r = _result(_msg({"From": "a@example.com", "To": ME, "Return-Path": "<>"}))
        self.assertEqual(r["label"], "bulk")
        self.assertIn("Return-Path", r["evidence"])

    def test_missing_return_path_is_not_bulk(self) -> None:
        # No Return-Path header at all must not count as a null sender.
        r = _result(_msg({"From": "a@example.com", "To": ME}))
        self.assertEqual(r["label"], "direct")

    def test_automated_localparts_are_bulk(self) -> None:
        for local in (
            "no-reply", "noreply", "donotreply", "notifications", "notification",
            "mailer-daemon", "postmaster", "bounce", "alerts", "alert",
        ):
            msg = _msg({"From": f"{local}@example.com", "To": ME})
            r = _result(msg)
            self.assertEqual(r["label"], "bulk", local)
            self.assertIn(local, r["evidence"])

    def test_sender_domain_in_bulk_domains_is_bulk(self) -> None:
        r = _result(_msg({"From": "news@bulksender.com", "To": ME}))
        self.assertEqual(r["label"], "bulk")
        self.assertIn("bulksender.com", r["evidence"])

    def test_never_bulk_sender_overrides_bulk_rules(self) -> None:
        # A colleague's genuine request sent from a noreply address.
        msg = _msg({"From": "noreply@colleague.com", "To": ME, "Cc": "other@example.com"})
        self.assertEqual(_label(msg), "direct")

    def test_never_bulk_sender_matches_address_not_name(self) -> None:
        msg = _msg(
            {
                "From": "Noreply Desk <noreply@colleague.com>",
                "To": ME,
                "Cc": "other@example.com",
            }
        )
        self.assertEqual(_label(msg), "direct")


class DirectTests(unittest.TestCase):
    def test_user_in_small_to_is_direct(self) -> None:
        msg = _msg({"From": "alice@example.com", "To": ME, "Cc": "bob@example.com"})
        r = _result(msg)
        self.assertEqual(r["label"], "direct")
        self.assertIn("To+Cc 2 <= 5", r["evidence"])
        self.assertIn(ME, r["evidence"])

    def test_exactly_max_recipients_is_direct(self) -> None:
        to = [ME] + [f"p{i}@example.com" for i in range(4)]  # 5 total
        msg = _msg({"From": "alice@example.com", "To": ", ".join(to)})
        self.assertEqual(_label(msg), "direct")

    def test_over_max_recipients_is_cc(self) -> None:
        to = [ME] + [f"p{i}@example.com" for i in range(5)]  # 6 total
        msg = _msg({"From": "alice@example.com", "To": ", ".join(to)})
        r = _result(msg)
        self.assertEqual(r["label"], "cc")
        self.assertIn("6 > 5", r["evidence"])

    def test_match_on_address_not_display_name(self) -> None:
        msg = _msg({"From": "alice@example.com", "To": "Me The User <me@example.com>"})
        self.assertEqual(_label(msg), "direct")

    def test_address_match_is_case_insensitive(self) -> None:
        msg = _msg({"From": "alice@example.com", "To": "Me@Example.COM"})
        self.assertEqual(_label(msg), "direct")


class CcTests(unittest.TestCase):
    def test_user_in_cc_only_is_cc(self) -> None:
        msg = _msg({"From": "alice@example.com", "To": "bob@example.com", "Cc": ME})
        r = _result(msg)
        self.assertEqual(r["label"], "cc")
        self.assertIn("Cc", r["evidence"])

    def test_user_in_delivery_header_only_is_cc(self) -> None:
        msg = _msg({"From": "alice@example.com", "To": "bob@example.com", "Delivered-To": ME})
        r = _result(msg)
        self.assertEqual(r["label"], "cc")
        self.assertIn("Delivered-To", r["evidence"])

    def test_other_delivery_headers_count(self) -> None:
        for header in ("X-Delivered-To", "X-Envelope-To", "X-Original-To"):
            msg = _msg({"From": "alice@example.com", "To": "bob@example.com", header: ME})
            r = _result(msg)
            self.assertEqual(r["label"], "cc", header)
            self.assertIn(header, r["evidence"])

    def test_user_nowhere_is_cc(self) -> None:
        msg = _msg({"From": "alice@example.com", "To": "bob@example.com"})
        r = _result(msg)
        self.assertEqual(r["label"], "cc")
        self.assertIn("not in any visible header", r["evidence"])

    def test_no_to_header_is_cc(self) -> None:
        msg = _msg({"From": "alice@example.com"})
        self.assertEqual(_label(msg), "cc")


class RobustnessTests(unittest.TestCase):
    def test_empty_message_never_raises(self) -> None:
        for data in (b"", b"garbage", b"\xff\xfe"):
            msg = email.message_from_bytes(data, policy=policy.default)
            r = classify.classify(msg, CONFIG)
            self.assertIsInstance(r, dict)
            self.assertIn("label", r)
            self.assertIn("evidence", r)


if __name__ == "__main__":
    unittest.main()
