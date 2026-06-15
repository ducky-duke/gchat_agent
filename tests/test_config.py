"""Tests for the stdlib `.env` parser in `gchat_agent.config`.

Focus: `_clean_value` inline-comment / quoting semantics, with a regression
test for the empty-value-plus-inline-comment case (`KEY=   # note`) that once
leaked the comment text through as the value — e.g. `POLL_BACKFILL_SINCE`'s
comment reached the Chat API as a `createTime >` filter and returned HTTP 400.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from gchat_agent.config import _clean_value, load_config


class CleanValueTest(unittest.TestCase):
    def test_empty_value_with_inline_comment_is_empty(self) -> None:
        # The regression: whitespace then `#` => empty value, not the comment.
        self.assertEqual(
            _clean_value("             # empty = start at startup; RFC-3339 ..."),
            "",
        )
        self.assertEqual(_clean_value("\t# Phase 2 only"), "")

    def test_leading_hash_literal_survives(self) -> None:
        # No leading whitespace => `#fff` is a literal value (e.g. a hex color).
        self.assertEqual(_clean_value("#fff"), "#fff")

    def test_trailing_inline_comment_stripped(self) -> None:
        self.assertEqual(_clean_value("8080                # Phase 2 only"), "8080")
        self.assertEqual(_clean_value("value # note"), "value")

    def test_hash_without_preceding_space_is_literal(self) -> None:
        self.assertEqual(_clean_value("http://x#frag"), "http://x#frag")

    def test_quoted_value_taken_verbatim(self) -> None:
        self.assertEqual(_clean_value('"a # b"'), "a # b")
        self.assertEqual(_clean_value("'c'  trailing"), "c")

    def test_blank_and_whitespace_only(self) -> None:
        self.assertEqual(_clean_value(""), "")
        self.assertEqual(_clean_value("   "), "")


class LoadConfigEnvFileTest(unittest.TestCase):
    def test_empty_value_with_comment_loads_as_empty(self) -> None:
        body = (
            "POLL_BACKFILL_SINCE=             # empty = no backfill; RFC-3339 to backfill\n"
            "WEBHOOK_AUTH_AUDIENCE=           # Phase 2 only\n"
            "GOOGLE_SPACE=spaces/AAQA123      # the live space\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write(body)
            path = fh.name
        try:
            cfg = load_config(env_file=path)
        finally:
            os.unlink(path)
        self.assertEqual(cfg.POLL_BACKFILL_SINCE, "")
        self.assertEqual(cfg.WEBHOOK_AUTH_AUDIENCE, "")
        self.assertEqual(cfg.GOOGLE_SPACE, "spaces/AAQA123")


if __name__ == "__main__":
    unittest.main()
