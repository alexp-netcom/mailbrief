"""Enforce the read-only rule at the source level.

The IMAP commands that mutate mail must never appear in the package. Mailboxes
are opened with EXAMINE, and that is the only access mode allowed.
"""

from __future__ import annotations

import pathlib
import unittest

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent / "mailbrief"
FORBIDDEN_VERBS = ("STORE", "COPY", "EXPUNGE", "APPEND", "MOVE")


class TestReadOnly(unittest.TestCase):
    def test_no_mutating_imap_verbs_in_source(self) -> None:
        offenders = []
        for py in sorted(PACKAGE_DIR.rglob("*.py")):
            text = py.read_text(encoding="utf-8")
            for verb in FORBIDDEN_VERBS:
                if verb in text:
                    offenders.append(f"{py.name}: {verb}")
        self.assertEqual(
            offenders,
            [],
            "mutating IMAP verbs found in package source: " + ", ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
