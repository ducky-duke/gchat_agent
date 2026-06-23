# docs/ — design & reference

Hand-written design docs plus a bundled Google Chat REST mirror.

- **`ARCHITECTURE.md`** — system architecture, issue-processing flow, escalation logic.
- **`OVERVIEW.md`** — leadership-facing overview (non-technical).
- **`RAG_ANALYSIS.md`** — the `rag/` retrieval layer as actually built.
- **`SETUP_GOOGLE_CHAT.md`** — live-demo setup over user OAuth (3 personal accounts, 1
  space). Pairs with the auth gotchas in root [`MEMORY.md`](../MEMORY.md).
- **`CALL_AUTOMATION.md`** — the deep war-story / dead-end / proven-recipe record behind the
  `scripts/` native-ringing-call + AI-voice tools (browser session dead-ends, the
  flagged-account saga, `--watch-join`/`--watch-rest`/`huddle_watch` channels, the audio
  capture/inject investigation, Wayland-occlusion blocker, greeting-latency fixes). The lean
  per-script index in [`../scripts/CLAUDE.md`](../scripts/CLAUDE.md) points here.
- **`google_chat/`** — bundled, read-only Google Chat **REST reference** (`*.md.txt`,
  mirrored under `reference/rest/v1/...`). Consult before changing
  `src/gchat_agent/chat/google_rest.py`; do not treat as project docs to edit.
  - **`google_chat/limits.md.txt`** — mirror of Google's [Usage limits](https://developers.google.com/workspace/chat/limits)
    page (the API quotas). The poll-interval-safety math: per-project **message reads
    3000/min**, per-space **reads 15/sec** vs **writes 1/sec**; message read/write are
    NOT per-user-throttled. So a 1s poll of one space (60 reads/min, ~2% of project read
    quota) is nowhere near the ceiling — the tight bucket is per-space **writes (1/sec)**
    when the bot posts bursts, not the poll rate. 429s use truncated exponential backoff.
- **`gemini_live/`** — bundled, read-only **Gemini Live API** doc mirror (`*.md.txt`,
  crawled from `ai.google.dev/gemini-api/docs/`, path-mirrored: `live-api/...`,
  `models/...`). Real-time low-latency voice/vision over a stateful WSS connection.
  Reference for any future Live-API voice work (distinct from the current
  OpenRouter `audio.speech` TTS in `llm/tts.py`). Key pages: `live-api.md.txt`
  (overview), `live-guide.md.txt` (capabilities), `live-tools.md.txt` (function
  calling/Search), `live-session.md.txt` (session mgmt), `ephemeral-tokens.md.txt`
  (client-to-server auth), `live-api/get-started-{sdk,websocket}.md.txt` (tutorials),
  `live-api/live-translate.md.txt`. Re-crawl with `python docs/gemini_live/_crawl.py
  docs/gemini_live` (bounded BFS: follows only `gemini-api/docs` links matching
  `live`/`ephemeral-tokens`).

- **`google_meet/`** — bundled, read-only **Google Meet API** doc mirror (`*.md.txt`,
  crawled from `developers.google.com/workspace/meet/`, path-mirrored: `api/guides/...`,
  `api/reference/rest/v2/...`, `media-api/guides/...`). Two distinct APIs:
  - **Meet REST API** (`api/...`, GA): manage meetings. `spaces.create`
    (`POST https://meet.googleapis.com/v2/spaces`, scope
    `…/auth/meetings.space.created`) returns a `Space` with a `meetingUri`
    (`https://meet.google.com/abc-mnop-xyz`) — i.e. you CAN mint a join link
    programmatically and post it to Chat. Also conferences/participants/artifacts
    (recordings, transcripts) — all *after/around* a call, never live audio.
    Now IMPLEMENTED in `src/gchat_agent/meet/` (`spaces.create`) +
    `call/demo_meet_call.py`, gated by `MEET_LINKS`.
  - **Meet Media API** (`media-api/...`, Developer-Preview-gated): real-time media.
    ⚠️ **Receive-only** — every scope is `…media[.audio|.video].readonly` ("Capture
    real-time audio/video"); there is NO send/inject capability. So a bot can
    *consume* a Meet's audio (e.g. feed Gemini for transcription) but **cannot speak
    INTO the call**. Also requires the Cloud project, OAuth principal, AND all
    participants enrolled in the Developer Preview Program; restricted scopes; for
    @gmail.com-organized meetings the initiator must be present to consent.
  - **Bottom line for the "AI phone call" idea:** an AI cannot be a *speaking*
    participant on a Google Meet via these APIs. You can mint+share a join link
    (REST) and consume audio (Media API, preview), but the voice has to come from a
    real human or a non-Google bridge. Confirms the Chat-API voice ceiling in
    [`../MEMORY.md`](../MEMORY.md).

Top-level design doc is [`PLAN.md`](../PLAN.md) (the `§N` citations throughout the code).
