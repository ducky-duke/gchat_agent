#!/usr/bin/env python3
"""Bounded crawler for the Gemini Live API doc family.

Starts from the seed page, follows only links under
ai.google.dev/gemini-api/docs/ that belong to the Live API set
(path contains 'live' or is 'ephemeral-tokens'), fetches the
.md.txt variant of each, and mirrors the URL path into OUT_DIR.
"""
import os
import re
import sys
import time
import urllib.request
import urllib.error

BASE = "https://ai.google.dev/gemini-api/docs/"
SEED = "https://ai.google.dev/gemini-api/docs/live-api"
OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else "docs/gemini_live"
MAX_PAGES = 40

LINK_RE = re.compile(r"\((https://ai\.google\.dev/gemini-api/docs/[^)\s]+)\)")


def canon(url):
    """Strip .md.txt, anchors and query; return the canonical page path key."""
    url = url.split("#", 1)[0].split("?", 1)[0]
    if url.endswith(".md.txt"):
        url = url[: -len(".md.txt")]
    return url.rstrip("/")


def is_live(page_url):
    if not page_url.startswith(BASE):
        return False
    rel = page_url[len(BASE):]
    return ("live" in rel) or (rel == "ephemeral-tokens")


def fetch(page_url):
    src = page_url + ".md.txt"
    req = urllib.request.Request(src, headers={"User-Agent": "doc-mirror/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def out_path(page_url):
    rel = page_url[len(BASE):]
    return os.path.join(OUT_DIR, rel + ".md.txt")


def main():
    seed = canon(SEED)
    queue = [seed]
    seen = {seed}
    saved = []
    while queue and len(saved) < MAX_PAGES:
        page = queue.pop(0)
        try:
            body = fetch(page)
        except urllib.error.HTTPError as e:
            print(f"SKIP {page} -> HTTP {e.code}")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"SKIP {page} -> {e}")
            continue
        dst = out_path(page)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(body)
        saved.append(dst)
        print(f"SAVED {dst} ({len(body)} bytes)")
        for m in LINK_RE.finditer(body):
            nxt = canon(m.group(1))
            if nxt not in seen and is_live(nxt):
                seen.add(nxt)
                queue.append(nxt)
        time.sleep(0.3)
    print(f"\nDONE: {len(saved)} pages saved to {OUT_DIR}/")
    for p in saved:
        print("  " + p)


if __name__ == "__main__":
    main()
