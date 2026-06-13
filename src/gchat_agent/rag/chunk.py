"""Chunk KB docs and chat history into overlapping `Passage`s (§5.5).

Pure stdlib. Two entry points:

- `chunk_document` — split one knowledge-base document (Markdown or plain text)
  into section-aware, word-overlapping passages tagged `kind="kb"`. Markdown
  headings (`#`..`######`) become the passage `section`; long sections are
  windowed into overlapping chunks so a single oversized section never produces
  one giant passage.
- `chunk_history` — turn recent chat `Message`s into `kind="chat"` snippets that
  carry their `create_time` for the recency boost (§boost). Short consecutive
  messages are packed together so each snippet has enough lexical signal for
  BM25 without blurring provenance (the snippet `source` is the first message id
  it contains).
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from gchat_agent.rag.base import Passage

if TYPE_CHECKING:  # avoid importing models at runtime (chunking needs no model)
    from gchat_agent.models import Message

# Word-window sizing (in whitespace tokens). Overlap keeps a fact that straddles
# a chunk boundary retrievable from either side.
_CHUNK_WORDS = 180
_CHUNK_OVERLAP = 40
_CHAT_PACK_WORDS = 120  # cap on words packed into one chat snippet

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def _window(words: list[str], size: int, overlap: int) -> list[list[str]]:
    """Slide a `size`-word window with `overlap` over `words`.

    Returns at least one (possibly short) window for non-empty input; an empty
    input yields no windows. `overlap` is clamped below `size` so the window
    always advances."""
    if not words:
        return []
    if size <= 0:
        return [list(words)]
    step = max(1, size - max(0, min(overlap, size - 1)))
    chunks: list[list[str]] = []
    i = 0
    n = len(words)
    while i < n:
        chunks.append(words[i : i + size])
        if i + size >= n:
            break
        i += step
    return chunks


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split Markdown/plain text into `(section_heading, body)` pairs.

    Content before the first heading is attributed to section `""`. Headings
    themselves are not duplicated into the body."""
    sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_body: list[str] = []
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            if current_body or sections:
                sections.append((current_heading, current_body))
            current_heading = m.group(2).strip()
            current_body = []
        else:
            current_body.append(line)
    sections.append((current_heading, current_body))
    return [(h, "\n".join(b).strip()) for h, b in sections]


def chunk_document(
    text: str,
    source: str,
    *,
    chunk_words: int = _CHUNK_WORDS,
    overlap: int = _CHUNK_OVERLAP,
) -> list[Passage]:
    """Split a KB document into overlapping `kind="kb"` passages.

    Each Markdown section becomes one or more passages; long sections are
    windowed (`chunk_words` with `overlap`). `create_time` is `""` for KB docs.
    Empty/whitespace input yields no passages."""
    passages: list[Passage] = []
    for heading, body in _split_sections(text):
        body = body.strip()
        if not body:
            continue
        words = body.split()
        for window in _window(words, chunk_words, overlap):
            chunk_text = " ".join(window).strip()
            if not chunk_text:
                continue
            passages.append(
                Passage(
                    text=chunk_text,
                    source=source,
                    section=heading,
                    kind="kb",
                    create_time="",
                )
            )
    return passages


def chunk_history(
    messages: list["Message"],
    *,
    pack_words: int = _CHAT_PACK_WORDS,
) -> list[Passage]:
    """Turn chat `Message`s into `kind="chat"` passages for retrieval.

    Consecutive short messages are packed into one snippet (up to `pack_words`)
    so each snippet carries enough lexical signal; the snippet `source` is the
    first contained message id and `create_time` is the latest contained message
    time (so the recency boost ranks the freshest snippet highest). Empty text
    messages are skipped."""
    passages: list[Passage] = []
    buf_lines: list[str] = []
    buf_words = 0
    buf_source = ""
    buf_time = ""

    def flush() -> None:
        nonlocal buf_lines, buf_words, buf_source, buf_time
        if buf_lines:
            passages.append(
                Passage(
                    text="\n".join(buf_lines).strip(),
                    source=buf_source,
                    section="chat",
                    kind="chat",
                    create_time=buf_time,
                )
            )
        buf_lines = []
        buf_words = 0
        buf_source = ""
        buf_time = ""

    for m in messages:
        body = (m.text or "").strip()
        if not body:
            continue
        who = m.sender or "(unknown)"
        line = f"#{m.id} {who}: {body}"
        w = len(body.split())
        if buf_lines and buf_words + w > pack_words:
            flush()
        if not buf_lines:
            buf_source = m.id
        buf_lines.append(line)
        buf_words += w
        if m.create_time:  # keep the latest timestamp seen in the buffer
            buf_time = m.create_time if m.create_time > buf_time else buf_time
    flush()
    return passages
