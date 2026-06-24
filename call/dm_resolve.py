#!/usr/bin/env python3
"""dm_resolve.py — resolve a Chat DM destination + the partner's display name.

Two responsibilities, both import-light (stdlib only at module load; Playwright is
imported LAZILY inside :func:`resolve_callee_name`, so importing this module never
drags the heavy browser/audio graph):

  * :func:`normalize_dm_url` — accept ANY of a full Chat URL, ``spaces/<id>``,
    ``chat/<id>`` / ``app/chat/<id>``, or a bare ``<id>`` and return the standalone
    Chat deep link ``https://chat.google.com/u/<authuser>/app/chat/<id>``. A full
    http(s) URL passes through unchanged.
  * :func:`resolve_callee_name` — read the DM partner's DISPLAY NAME from the
    signed-in caller browser already showing that DM.

Why the browser, not the REST API: under USER OAuth the Chat REST API populates a
``User`` object's ``name`` and ``type`` only — NOT ``displayName`` (documented on
both ``Message.sender`` and ``Membership.member``; see
``docs/google_chat/.../spaces.messages.md.txt`` /
``.../spaces.members.md.txt``). ``displayName`` is filled only under APP/service-
account auth, which this project deliberately does not use (personal Gmail + user
OAuth). The Chat WEB UI renders the name regardless, and the caller Brave is already
navigating to the DM — so the only reliable name source here is the rendered page.

So ``gemini_call.py`` / ``call_apigw.sh`` can be handed just a DM link (or even a
bare space id) and figure out who they are calling — no ``--callee`` needed.
"""
from __future__ import annotations

import os
import re
import time
from typing import Callable

# document.title / region aria-labels that are NOT a person's name — never pick one.
_GENERIC_NAMES = frozenset({
    "", "chat", "google chat", "home", "main", "mentions", "starred",
    "search chat", "threads", "spaces", "direct messages",
})

# Read the conversation partner's name out of the live DM page. Two signals, both
# observed on the standalone Chat app (chat.google.com/u/<n>/app/chat/<id>):
#   region = the role="main" (else role="region") aria-label → the partner's name,
#            clean and undecorated (e.g. "Duc Tran Trong").
#   title  = document.title → "<Name> - Chat" (the decoration is stripped in Python).
# The signed-in account shows under a SEPARATE "Google Account: …" aria-label, which
# pick_callee_name() filters out, so we never mistake the caller for the callee.
_NAME_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  let region = '';
  for (const sel of ['[role="main"]', '[role="region"]']) {
    for (const e of document.querySelectorAll(sel)) {
      const a = clean(e.getAttribute('aria-label'));
      if (a) { region = a; break; }
    }
    if (region) break;
  }
  return { region, title: clean(document.title) };
}
"""


def _space_id(value: str) -> str:
    """Bare space id from a full URL or any accepted short form
    (``spaces/<id>`` / ``chat/<id>`` / ``app/chat/<id>`` / ``<id>``)."""
    v = (value or "").strip()
    m = re.search(r"/app/chat/([^/?#]+)", v)
    if m:
        return m.group(1)
    v = re.sub(r"^(app/)?chat/", "", v)
    if v.startswith("spaces/"):
        v = v[len("spaces/"):]
    return v.strip("/")


def env_value(repo_root: str, *keys: str) -> "str | None":
    """First non-empty value among ``keys`` from the environment, else parsed out of
    ``<repo_root>/.env``. Returns ``None`` when none is set anywhere.

    The .env parse mirrors the hard-won rule (see MEMORY.md / gemini_voice): split on
    the FIRST ``=``, strip a matching surrounding quote, and drop a trailing
    `` # comment`` only when the value is UNQUOTED. Used to source the call
    destination from config (GOOGLE_CHAT_REPORT_SPACE) instead of hardcoding it."""
    for k in keys:
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip()
    env_path = os.path.join(repo_root, ".env")
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() not in keys:
                    continue
                v = v.strip()
                if v[:1] in ("'", '"') and v[-1:] == v[:1] and len(v) >= 2:
                    v = v[1:-1]
                else:
                    v = v.split(" #", 1)[0].strip()
                if v:
                    return v
    except OSError:
        pass
    return None


def normalize_dm_url(value: str, authuser: int = 0) -> str:
    """Return the standalone Chat deep link for ``value``.

    Accepts a full http(s) URL (returned UNCHANGED, so an already-exact address-bar
    URL still works), ``spaces/<id>``, ``chat/<id>`` / ``app/chat/<id>``, or a bare
    ``<id>``. ``authuser`` is the ``u/<n>`` account index (0 in the single-account
    caller profile)."""
    v = (value or "").strip()
    if v.startswith("http://") or v.startswith("https://"):
        return v
    return f"https://chat.google.com/u/{authuser}/app/chat/{_space_id(v)}"


def pick_callee_name(region: "str | None", title: "str | None") -> "str | None":
    """Pick the partner's name from the two scraped signals (see ``_NAME_JS``).

    Prefer the clean region aria-label; fall back to the document title minus its
    ``- Chat`` / ``- Google Chat`` decoration and any leading ``(N)`` unread count.
    Returns ``None`` when neither yields a real name (a generic UI label, empty, or
    the signed-in ``Google Account: …`` label)."""
    cands: list[str] = []
    if region:
        cands.append(" ".join(region.split()))
    if title:
        t = " ".join(title.split())
        t = re.sub(r"^\(\d+\)\s*", "", t)
        t = re.sub(r"\s*[-–—]\s*Google Chat$", "", t, flags=re.I)
        t = re.sub(r"\s*[-–—]\s*Chat$", "", t, flags=re.I)
        cands.append(t.strip())
    for c in cands:
        low = c.lower()
        if c and low not in _GENERIC_NAMES and not low.startswith("google account"):
            return c
    return None


def resolve_callee_name(
    port: int,
    url: str,
    *,
    timeout_s: float = 12.0,
    log: Callable[[str], object] = print,
) -> "str | None":
    """Best-effort: read the DM partner's display name from the caller browser on
    CDP ``port``, already signed in and showing the DM at ``url``.

    Polls the rendered page until a real name appears or ``timeout_s`` elapses.
    NEVER raises — any import/connect/eval failure returns ``None`` (the caller then
    falls back to a generic label). Needs NO new OAuth scope: the name comes from the
    rendered UI, not the REST API (which hides ``displayName`` under user auth)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        log(f"  ⚠️  playwright unavailable ({exc}); cannot resolve the callee name")
        return None
    want = _space_id(url)
    cdp = f"http://127.0.0.1:{port}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp, timeout=30_000)
            ctx = browser.contexts[0] if browser.contexts else None
            if ctx is None:
                return None
            # Prefer the page already on the target space; else any Chat page.
            page = next(
                (p for p in ctx.pages if want and want in (p.url or "")), None)
            page = page or next(
                (p for p in ctx.pages if "chat.google.com" in (p.url or "")), None)
            page = page or (ctx.pages[0] if ctx.pages else None)
            if page is None:
                return None
            if want and want not in (page.url or ""):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                except Exception:  # noqa: BLE001
                    pass
            deadline = time.time() + timeout_s
            while True:
                try:
                    data = page.evaluate(_NAME_JS) or {}
                except Exception:  # noqa: BLE001
                    data = {}
                name = pick_callee_name(data.get("region"), data.get("title"))
                if name or time.time() >= deadline:
                    return name
                page.wait_for_timeout(800)
    except Exception as exc:  # noqa: BLE001
        log(f"  ⚠️  callee-name resolution failed ({type(exc).__name__}: {exc})")
        return None
