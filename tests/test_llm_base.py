"""Regression tests for the robust JSON extraction (§5.3, review-driven).

Pins the MED finding that `extract_json_value` scanned only the *first* balanced
`{...}`/`[...]` and gave up: chatty output like `Note [x] {"issues": []}` parsed
the non-JSON `[x]`, failed, and never reached the valid object after it. The
extractor now scans every balanced candidate in order and returns the first that
parses.

Stdlib `unittest`; offline.
"""
from __future__ import annotations

import unittest

from gchat_agent.llm.base import extract_json, extract_json_value


class ExtractJsonScanAllTest(unittest.TestCase):
    """The fallback tries *every* balanced candidate, not just the first."""

    def test_non_json_bracket_group_before_object(self) -> None:
        # The regression: first balanced value is `[x]` (invalid), the real
        # payload is the object after it. Old code raised ValueError here.
        out = extract_json_value('Note [x] {"issues": []}')
        self.assertEqual(out, {"issues": []})

    def test_object_extracted_via_object_helper(self) -> None:
        out = extract_json('see [TODO] then {"a": 1, "b": 2}')
        self.assertEqual(out, {"a": 1, "b": 2})

    def test_bracketed_prose_before_array(self) -> None:
        # Two non-JSON bracket groups, then the real array — all must be tried.
        out = extract_json_value("prefix [x][y] then [1, 2, 3]")
        self.assertEqual(out, [1, 2, 3])

    def test_fast_path_plain_object_still_works(self) -> None:
        self.assertEqual(extract_json_value('{"ok": true}'), {"ok": True})

    def test_fenced_json_still_extracted(self) -> None:
        out = extract_json_value('```json\n{"issues": [{"id": 1}]}\n```')
        self.assertEqual(out, {"issues": [{"id": 1}]})

    def test_brackets_inside_strings_do_not_break_scan(self) -> None:
        # The `]` lives inside a string literal; depth tracking must ignore it.
        out = extract_json_value('chatter {"text": "a [b] c", "n": 1}')
        self.assertEqual(out, {"text": "a [b] c", "n": 1})

    def test_no_json_value_raises(self) -> None:
        with self.assertRaises(ValueError):
            extract_json_value("no json here at all")

    def test_found_but_unparseable_raises_with_context(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            extract_json_value("here: {not: valid}")
        self.assertIn("none parsed", str(ctx.exception))

    def test_object_helper_rejects_top_level_array(self) -> None:
        # extract_json demands an object; a top-level array is an error (the
        # detection path uses extract_json_value instead).
        with self.assertRaises(ValueError):
            extract_json("[1, 2, 3]")


if __name__ == "__main__":
    unittest.main()
