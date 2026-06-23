"""Tests for call/dm_resolve.py — the DM URL normalizer + the callee-name picker.

These cover the PURE, import-light helpers only (no browser): `normalize_dm_url`,
`_space_id`, and `pick_callee_name`. Playwright is imported lazily inside
`resolve_callee_name`, so importing the module here never drags the heavy
browser/audio graph and the suite stays hermetic.

`call/` is a flat-dir script tree (not a package), so we add it to sys.path the
same way its own entry scripts do.
"""
from __future__ import annotations

import os
import sys
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "call"))

import dm_resolve  # noqa: E402


class NormalizeDmUrlTest(unittest.TestCase):
    """All accepted destination forms reduce to the standalone DM deep link."""

    _CANON = "https://chat.google.com/u/0/app/chat/qtotjoAAAAE"

    def test_full_url_passes_through_unchanged(self) -> None:
        # An exact address-bar URL (any u/<n>) must be returned verbatim.
        for url in (
            self._CANON,
            "https://chat.google.com/u/3/app/chat/qtotjoAAAAE",
            "http://chat.google.com/u/0/app/chat/qtotjoAAAAE?foo=bar",
        ):
            self.assertEqual(dm_resolve.normalize_dm_url(url), url)

    def test_short_forms_build_the_deep_link(self) -> None:
        for raw in (
            "qtotjoAAAAE",
            "chat/qtotjoAAAAE",
            "app/chat/qtotjoAAAAE",
            "spaces/qtotjoAAAAE",
            "  spaces/qtotjoAAAAE  ",
            "spaces/qtotjoAAAAE/",
        ):
            self.assertEqual(dm_resolve.normalize_dm_url(raw), self._CANON, raw)

    def test_authuser_index_is_honored(self) -> None:
        self.assertEqual(
            dm_resolve.normalize_dm_url("qtotjoAAAAE", authuser=2),
            "https://chat.google.com/u/2/app/chat/qtotjoAAAAE",
        )

    def test_space_id_extracts_from_every_form(self) -> None:
        for raw in (
            "qtotjoAAAAE",
            "chat/qtotjoAAAAE",
            "spaces/qtotjoAAAAE",
            self._CANON,
            "https://chat.google.com/u/0/app/chat/qtotjoAAAAE?x=1#frag",
        ):
            self.assertEqual(dm_resolve._space_id(raw), "qtotjoAAAAE", raw)


class PickCalleeNameTest(unittest.TestCase):
    """Pick the partner's name from the two scraped page signals."""

    def test_region_aria_label_wins(self) -> None:
        # The clean region label is preferred over the decorated title.
        self.assertEqual(
            dm_resolve.pick_callee_name("Duc Tran Trong", "Duc Tran Trong - Chat"),
            "Duc Tran Trong",
        )

    def test_title_fallback_strips_chat_decoration(self) -> None:
        self.assertEqual(
            dm_resolve.pick_callee_name(None, "Duc Tran Trong - Chat"),
            "Duc Tran Trong",
        )
        self.assertEqual(
            dm_resolve.pick_callee_name("", "Jane Doe - Google Chat"),
            "Jane Doe",
        )

    def test_title_strips_leading_unread_count(self) -> None:
        self.assertEqual(
            dm_resolve.pick_callee_name(None, "(7) Duc Tran Trong - Chat"),
            "Duc Tran Trong",
        )

    def test_generic_labels_are_rejected(self) -> None:
        # A bare "Chat"/"Google Chat" (conversation not yet rendered) yields nothing.
        self.assertIsNone(dm_resolve.pick_callee_name("Chat", "Google Chat"))
        self.assertIsNone(dm_resolve.pick_callee_name(None, "Chat"))
        self.assertIsNone(dm_resolve.pick_callee_name(None, None))

    def test_signed_in_account_label_is_not_picked(self) -> None:
        # The caller's own "Google Account: …" label must never become the callee.
        self.assertIsNone(
            dm_resolve.pick_callee_name(
                "Google Account: Tran Duc (mikmikb26@gmail.com)", "Google Chat")
        )

    def test_region_falls_through_to_title_when_generic(self) -> None:
        # A generic region label is skipped in favor of a real title name.
        self.assertEqual(
            dm_resolve.pick_callee_name("Chat", "Duc Tran Trong - Chat"),
            "Duc Tran Trong",
        )


if __name__ == "__main__":
    unittest.main()
