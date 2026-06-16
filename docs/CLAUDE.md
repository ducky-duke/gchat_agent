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

Top-level design doc is [`PLAN.md`](../PLAN.md) (the `§N` citations throughout the code).
