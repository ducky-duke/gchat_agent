# docs/ — design & reference

Hand-written design docs plus a bundled Google Chat REST mirror.

- **`ARCHITECTURE.md`** — system architecture, issue-processing flow, escalation logic.
- **`OVERVIEW.md`** — leadership-facing overview (non-technical).
- **`RAG_ANALYSIS.md`** — the `rag/` retrieval layer as actually built.
- **`SETUP_GOOGLE_CHAT.md`** — live-demo setup over user OAuth (3 personal accounts, 1
  space). Pairs with the auth gotchas in root [`MEMORY.md`](../MEMORY.md).
- **`google_chat/`** — bundled, read-only Google Chat **REST reference** (`*.md.txt`,
  mirrored under `reference/rest/v1/...`). Consult before changing
  `src/gchat_agent/chat/google_rest.py`; do not treat as project docs to edit.
  - **`google_chat/limits.md.txt`** — mirror of Google's [Usage limits](https://developers.google.com/workspace/chat/limits)
    page (the API quotas). The poll-interval-safety math: per-project **message reads
    3000/min**, per-space **reads 15/sec** vs **writes 1/sec**; message read/write are
    NOT per-user-throttled. So a 1s poll of one space (60 reads/min, ~2% of project read
    quota) is nowhere near the ceiling — the tight bucket is per-space **writes (1/sec)**
    when the bot posts bursts, not the poll rate. 429s use truncated exponential backoff.

Top-level design doc is [`PLAN.md`](../PLAN.md) (the `§N` citations throughout the code).
