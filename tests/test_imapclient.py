"""Regression tests for search_uids UID-range semantics.

The live bug: a bare set in ``UID SEARCH <start>:*`` is interpreted by the
Dovecot server as a *sequence-number* set, so when the sequence count was
below <start> it returned empty and new mail was silently skipped. The search
must carry the ``UID`` key prefix, and results below <start> must be dropped
(because Dovecot returns the last existing uid as a stale hit when <start>
exceeds the maximum).
"""

from __future__ import annotations

import unittest

from mailbrief.imapclient import ImapClient, ImapError


class _FakeIMAP:
    """Minimal stand-in for the real imaplib connection."""

    def __init__(self, resp: bytes) -> None:
        self.resp = resp
        self.calls: list[tuple] = []

    def uid(self, *args: object) -> tuple[str, list[bytes]]:
        self.calls.append(args)
        return "OK", [self.resp]


class TestSearchUids(unittest.TestCase):
    def _client(self, resp: bytes) -> tuple[ImapClient, _FakeIMAP]:
        imap = _FakeIMAP(resp)
        client = ImapClient(account=None)  # type: ignore[arg-type]
        client.client = imap  # type: ignore[assignment]
        return client, imap

    def test_sends_uid_prefixed_range(self) -> None:
        client, imap = self._client(b"5796 5797")
        self.assertEqual(client.search_uids(5796), [5796, 5797])
        self.assertEqual(imap.calls[0], ("search", None, "UID 5796:*"))

    def test_returns_empty_when_none_above_start(self) -> None:
        # Dovecot quirk: UID SEARCH UID <start>:* with <start> past the max
        # returns the last existing uid as a stale hit. It must be filtered.
        client, imap = self._client(b"5797")
        self.assertEqual(client.search_uids(5798), [])
        self.assertEqual(imap.calls[0], ("search", None, "UID 5798:*"))

    def test_returns_empty_on_no_results(self) -> None:
        client, _ = self._client(b"")
        self.assertEqual(client.search_uids(5796), [])

    def test_raises_imap_error_on_bad_response(self) -> None:
        imap = _FakeIMAP(b"")
        imap.uid = lambda *a: ("NO", [b"failed"])  # type: ignore[method-assign]
        client = ImapClient(account=None)  # type: ignore[arg-type]
        client.client = imap  # type: ignore[assignment]
        with self.assertRaises(ImapError):
            client.search_uids(5796)


if __name__ == "__main__":
    unittest.main()
