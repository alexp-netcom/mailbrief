"""Telegram briefing bot tests (docs/plan-telegram.md).

The HTTP layer is faked end to end: FakeTransport plays api.telegram.org with
queued getUpdates responses and captured sendMessage params. No network, no
token in tests - the token is only ever in the API URL the real transport
builds. The claude subprocess is faked the same way: a runner callable that
writes the briefing file, exactly what the real `claude -p` run leaves behind.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import tempfile
import time
import unittest

from mailbrief import telegram
from mailbrief.config import Config, ConfigError, parse

LIMIT = telegram.CHUNK_LIMIT


def make_update(update_id: int, chat_id: str, text: str | None = None) -> dict:
    """One getUpdates result object, shaped like the real Bot API."""
    update: dict = {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id, "first_name": "Test User"}, "message_id": update_id},
    }
    if text is not None:
        update["message"]["text"] = text
    return update


class FakeTransport:
    """In-memory Telegram Bot API. Queue getUpdates responses per call."""

    def __init__(self) -> None:
        self.update_batches: list[list[dict]] = []
        self.sent: list[dict] = []  # sendMessage params, in order
        self.get_updates_calls: list[dict] = []  # params per getUpdates call

    def __call__(self, base_url: str, token: str, method: str, params: dict) -> dict:
        if method == "getUpdates":
            self.get_updates_calls.append(params)
            return {"ok": True, "result": self.update_batches.pop(0) if self.update_batches else []}
        if method == "sendMessage":
            self.sent.append(params)
            return {"ok": True, "result": {"message_id": len(self.sent)}}
        raise AssertionError(f"unexpected method {method}")


class TestChunking(unittest.TestCase):
    def test_short_text_is_one_chunk(self) -> None:
        self.assertEqual(telegram.chunk_text("hello"), ["hello"])

    def test_empty_text_is_no_chunks(self) -> None:
        self.assertEqual(telegram.chunk_text(""), [])

    def test_splits_at_paragraph_boundaries_and_reconstructs_exactly(self) -> None:
        paras = [f"para{i}\nsecond line {i}" for i in range(1, 8)]
        text = "\n\n".join(paras)
        chunks = telegram.chunk_text(text, limit=40)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 40)
        self.assertEqual("".join(chunks), text)

    def test_hard_splits_one_oversized_paragraph_and_reconstructs_exactly(self) -> None:
        text = "word " * 3000  # 15000 chars, no blank lines
        chunks = telegram.chunk_text(text, limit=1000)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 1000)
        self.assertEqual("".join(chunks), text)

    def test_exact_limit_boundary(self) -> None:
        at = "a" * LIMIT
        over = at + "b"
        self.assertEqual(telegram.chunk_text(at), [at])
        self.assertEqual(telegram.chunk_text(over), [at, "b"])


class TestCommandParsing(unittest.TestCase):
    def test_parses_both_windows(self) -> None:
        self.assertEqual(telegram.command_of("brief morning"), "morning")
        self.assertEqual(telegram.command_of("brief eod"), "eod")

    def test_case_and_whitespace_insensitive(self) -> None:
        self.assertEqual(telegram.command_of("  Brief   Morning "), "morning")

    def test_non_commands_are_none(self) -> None:
        self.assertEqual(telegram.command_of("hello"), None)
        self.assertEqual(telegram.command_of(""), None)
        self.assertEqual(telegram.command_of(None), None)


class _PollBase(unittest.TestCase):
    """Shared fixture: temp data dir + briefing dir, fake transport."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = pathlib.Path(self.tmp.name) / "data"
        self.briefing = pathlib.Path(self.tmp.name) / "briefing"
        self.transport = FakeTransport()
        self.chats = frozenset({"111"})
        self.today = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")

    def briefing_path(self, window: str) -> pathlib.Path:
        name = "Morning" if window == "morning" else "Evening"
        return self.briefing / f"{self.today} {name}.md"

    def poll(self, *, token="t", extra=None) -> telegram.PollResult:
        kwargs = {
            "token": token,
            "chat_ids": self.chats,
            "data_dir": self.data_dir,
            "transport": self.transport,
            "briefing_dir": self.briefing,
        }
        if extra:
            kwargs.update(extra)
        return telegram.run_poll(**kwargs)

    def queue_one(self, update_id: int, chat_id: str, text: str | None = None) -> None:
        self.transport.update_batches.append([make_update(update_id, chat_id, text)])


class TestRunPoll(_PollBase):
    def test_dispatches_morning_briefing_sends_chunks_and_reply(self) -> None:
        text = "\n\n".join(f"paragraph number {i} with enough words" for i in range(200))
        seen = {}

        def fake_claude(window: str) -> int:
            seen["window"] = window
            path = self.briefing_path(window)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return 0

        self.queue_one(7, "111", "brief morning")
        result = self.poll(extra={"claude_runner": fake_claude, "max_waits": 5})

        self.assertEqual(seen["window"], "morning")
        self.assertEqual(result.briefings, 1)
        self.assertGreater(len(self.transport.sent), 2)  # chunks + confirmation
        sent_text = "".join(m["text"] for m in self.transport.sent)
        self.assertIn(text, sent_text)
        for m in self.transport.sent:
            self.assertLessEqual(len(m["text"]), LIMIT)
        self.assertTrue(self.transport.sent[-1]["text"].startswith("briefing sent"))
        # offset persisted: next poll must not reprocess update 7
        self.assertEqual(telegram.read_offset(self.data_dir), 8)

    def test_ignores_chat_outside_allowlist(self) -> None:
        self.queue_one(7, "999", "brief morning")
        result = self.poll()
        self.assertEqual(result.briefings, 0)
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(telegram.read_offset(self.data_dir), 8)  # consumed silently

    def test_unknown_command_from_allowed_chat_gets_usage(self) -> None:
        self.queue_one(7, "111", "hello there")
        self.poll()
        self.assertEqual(len(self.transport.sent), 1)
        self.assertIn("brief morning", self.transport.sent[0]["text"])
        self.assertIn("brief eod", self.transport.sent[0]["text"])
        self.assertEqual(telegram.read_offset(self.data_dir), 8)

    def test_message_without_text_is_skipped(self) -> None:
        self.queue_one(7, "111")  # photo/sticker: no text field
        self.poll()
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(telegram.read_offset(self.data_dir), 8)

    def test_lock_busy_replies_and_still_consumes_update(self) -> None:
        telegram.acquire_lock(self.data_dir)  # simulate the in-flight run
        try:
            self.queue_one(7, "111", "brief eod")
            self.poll()
        finally:
            telegram.release_lock(self.data_dir)
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(self.transport.sent[0]["text"], telegram.BUSY_REPLY)
        self.assertEqual(telegram.read_offset(self.data_dir), 8)

    def test_stale_lock_is_taken_over(self) -> None:
        lock = telegram.acquire_lock(self.data_dir)
        old = datetime.datetime.now() - datetime.timedelta(hours=2)
        import os

        os.utime(lock, (old.timestamp(), old.timestamp()))
        self.queue_one(7, "111", "brief morning")

        def fake_claude(window: str) -> int:
            path = self.briefing_path(window)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("stale lock beaten", encoding="utf-8")
            return 0

        result = self.poll(extra={"claude_runner": fake_claude, "max_waits": 5})
        self.assertEqual(result.briefings, 1)
        telegram.release_lock(self.data_dir)

    def test_claude_failure_replies_with_the_fix(self) -> None:
        self.queue_one(7, "111", "brief morning")
        self.poll(extra={"claude_runner": lambda window: 2})
        self.assertEqual(len(self.transport.sent), 1)
        self.assertIn("claude", self.transport.sent[0]["text"])
        self.assertIn("/brief", self.transport.sent[0]["text"])

    def test_missing_briefing_file_replies_with_the_fix(self) -> None:
        waits = []

        def fake_wait(seconds: float) -> None:
            waits.append(seconds)

        self.queue_one(7, "111", "brief eod")
        self.poll(
            extra={
                "claude_runner": lambda window: 0,
                "wait": fake_wait,
                "max_waits": 3,
            }
        )
        self.assertEqual(len(self.transport.sent), 1)
        self.assertIn("did not appear", self.transport.sent[0]["text"])
        self.assertEqual(len(waits), 3)

    def test_fresh_file_within_clock_grace_is_accepted(self) -> None:
        # Windows NTFS mtimes can lag the precise wall clock by up to ~16 ms
        # (tick-quantized); the freshness check must tolerate that skew
        # without letting a truly stale briefing through.
        self.queue_one(7, "111", "brief eod")

        def fake_claude(window: str) -> int:
            path = self.briefing_path(window)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fresh but clock-skewed", encoding="utf-8")
            skew = time.time() - 5
            os.utime(path, (skew, skew))
            return 0

        result = self.poll(
            extra={"claude_runner": fake_claude, "wait": lambda s: None, "max_waits": 2}
        )
        self.assertEqual(result.briefings, 1)

    def test_stale_briefing_file_from_earlier_today_is_not_sent(self) -> None:
        path = self.briefing_path("morning")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old briefing from this morning", encoding="utf-8")
        old = datetime.datetime.now() - datetime.timedelta(hours=1)
        import os

        os.utime(path, (old.timestamp(), old.timestamp()))

        self.queue_one(7, "111", "brief morning")
        self.poll(
            extra={
                "claude_runner": lambda window: 0,
                "wait": lambda s: None,
                "max_waits": 2,
            }
        )
        self.assertEqual(len(self.transport.sent), 1)
        self.assertIn("did not appear", self.transport.sent[0]["text"])

    def test_offset_persisted_between_polls(self) -> None:
        self.queue_one(7, "111", "brief morning")
        self.poll(extra={"claude_runner": lambda window: 1})  # fails, but consumed
        before = len(self.transport.sent)  # the failure reply above
        # a later poll with the same update in the queue must not re-run it
        self.transport.update_batches.append([make_update(7, "111", "brief morning")])
        result = self.poll()
        self.assertEqual(result.updates, 1)
        self.assertEqual(result.commands, 0)
        self.assertEqual(len(self.transport.sent), before)

    def test_offset_reads_zero_without_state_file(self) -> None:
        self.assertEqual(telegram.read_offset(self.data_dir), 0)


class TestWhoami(unittest.TestCase):
    def test_lists_distinct_chats_with_names(self) -> None:
        t = FakeTransport()
        t.update_batches.append(
            [
                make_update(1, "111"),
                make_update(2, "111"),
                {
                    "update_id": 3,
                    "message": {
                        "chat": {"id": "222", "first_name": "Bob", "username": "bob"},
                        "text": "hi",
                    },
                },
            ]
        )
        chats = telegram.whoami("token", transport=t)
        self.assertEqual([c["id"] for c in chats], ["111", "222"])
        self.assertEqual(chats[1]["username"], "bob")

    def test_empty_updates_list_nothing(self) -> None:
        t = FakeTransport()
        self.assertEqual(telegram.whoami("token", transport=t), [])


class TestConfigTelegram(unittest.TestCase):
    def test_chat_ids_parsed_above_accounts(self) -> None:
        conf = parse(
            {
                "telegram_chat_ids": [111, "222"],
                "accounts": [{"name": "a", "host": "h", "username": "u"}],
            },
            pathlib.Path("config.toml"),
        )
        self.assertEqual(conf.telegram_chat_ids, ("111", "222"))

    def test_chat_ids_absorbed_into_account_is_an_error(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse(
                {
                    "accounts": [
                        {
                            "name": "a",
                            "host": "h",
                            "username": "u",
                            "telegram_chat_ids": [111],
                        }
                    ],
                },
                pathlib.Path("config.toml"),
            )
        self.assertIn("ABOVE [[accounts]]", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
