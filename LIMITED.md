# Known Limitations

Current limitations of `gchat_agent` — behaviours the code does **not** handle (or
handles only partially) today. This is a *demo*; the list is deliberately honest so
the gaps are visible before anyone relies on the bot. Each entry names the scenario,
what the code actually does now, and the gap, with a `file:symbol` reference so the
claim is verifiable. Companion to [`CLAUDE.md`](CLAUDE.md) (behavioural specs) and
[`docs/OVERVIEW.md`](docs/OVERVIEW.md) (what *is* built).

Defaults referenced below (`config.py`): `MAX_CLARIFY_ROUNDS=3`,
`MAX_NO_PROGRESS_ROUNDS=2`, `STALE_AFTER_IDLE_CYCLES=3`, `ESCALATE_AFTER_SECONDS=300`,
`RESOLVE_CONFIDENCE_THRESHOLD=0.8`, `DETECT_WINDOW_MESSAGES=50`,
`POLL_INTERVAL_SECONDS=15`.

---

## 1. Clarification loop (detect → ask → resolve)

### 1.1 Reporter rambles / goes in circles → ends STALE with no report
**Scenario:** after the bot asks, the reporter keeps replying but never actually
narrows things down — each reply is a *different* non-answer, so the set of missing
facts keeps shifting instead of shrinking.

**Now:** the no-progress backstop only fires when the missing-facts set is *identical*
two rounds running. `runner._step_issue` computes `progressed = (not prev) or (curr !=
prev)` — **any** change to the set (even churn that doesn't converge) resets
`no_progress_rounds` to 0. So a fluctuating gap never trips `MAX_NO_PROGRESS_ROUNDS`;
only `MAX_CLARIFY_ROUNDS` (default 3) eventually stops the loop, and that path calls
`_mark_stale` — which writes **no report and no documented open questions**, unlike the
clean decline path which closes *with* gaps.

**Gap:** "progress" is measured as set inequality, not genuine convergence, and the
round-cap exit is a silent STALE rather than an honest "recorded with open questions".
A genuinely un-finalizable issue is dropped without a summary.

### 1.2 Reporter declines verbosely or in another language → not recognised as a decline
**Scenario:** the reporter effectively says "I can't answer that" but in a long
sentence, or not in English.

**Now:** the deterministic decline guard `runner._looks_like_decline` is keyword- and
length-based: the reply must be **≤ 8 words** (`_DECLINE_MAX_WORDS`) and match an
English phrase in `_DECLINE_PHRASES` ("i don't know", "not sure", …) with nothing
substantive left over. A verbose refusal ("Honestly there's no way for me to find that
out right now, sorry") exceeds the word cap; a non-English refusal ("không biết")
isn't in the phrase list.

**Gap:** an unrecognised decline doesn't take the immediate close-with-gaps path — it
falls through to the no-progress / round-cap machinery (see 1.1), so the bot may re-ask
a question the reporter already declined, or stale it. The prompt-level instruction
(layer 1, `prompts.clarity_prompt`) asks the *model* to treat declines as unobtainable,
but the deterministic safety net (layer 2) is English- and brevity-bound.

### 1.3 Decline that "makes progress" can still chase unanswerable facts
**Now:** by design, a decline closes the issue **only when it made no progress**
(`declined and not progressed` in `_step_issue`). When a reply both declines the asked
questions *and* surfaces a new core fact (owner, root cause), the bot keeps clarifying
the still-open facts — correct in the common case (see the screenshot-bug story in
`CLAUDE.md`).

**Gap:** the flip side is that if each round surfaces a *different* new fact while the
reporter declines the rest, the issue keeps the loop alive until the round cap, then
stales (1.1). There is no notion of "the reporter has fundamentally disengaged".

### 1.4 Reporter ignores entirely → one nudge, then STALE, then nothing
**Scenario:** the reporter never replies.

**Now:** anti-spam waiting (`_step_issue` idle branch), then exactly **one** top-level
@mention nudge after `ESCALATE_AFTER_SECONDS` (`_escalate_due`, one-shot per issue via
`Issue.escalated`), then STALE after `STALE_AFTER_IDLE_CYCLES`.

**Gap:** "stale" is only a status + a tombstone. There is **no human handoff** — no
assignee, no notification to a manager, no ticket created, no re-escalation. A dropped
issue just sits in state as `STALE`. The voice/Markdown report is produced only on
*resolution*, never on staleness, so an abandoned issue leaves no report at all.

### 1.5 Clarity threshold is a hard cut
**Now:** an issue resolves only when `is_clear` **and** `confidence >=
RESOLVE_CONFIDENCE_THRESHOLD` (0.8) **and** `missing_info` is empty (`_step_issue`).

**Gap:** a model that is correct but under-confident (e.g. returns 0.75) won't resolve a
genuinely clear issue — it re-asks or stales. The threshold is global, not per-category
or per-severity.

---

## 2. Detection & deduplication

### 2.1 Window-bound detection can miss a buried issue
**Now:** detection renders only the last `DETECT_WINDOW_MESSAGES` (50) messages
(`runner._detect` → `Conversation.tail`). Lever B also *defers* a new issue raised
inside an open issue's clarification thread until the next out-of-thread cycle.

**Gap:** if more than ~50 messages of other traffic arrive before detection next fires,
a deferred (or simply busy-channel) issue scrolls out of the tail and is **never
raised**. There is no backfill rescan of older history.

### 2.2 Same real-world problem in two threads → two issues
**Now:** dedup is fingerprint (thread + root message + category) plus a jaccard
title/summary similarity check in `IssueStore`.

**Gap:** two reporters describing the *same* incident in two different threads produce
two independent issues; there is no cross-thread semantic merge.

### 2.3 A recurrence in the same thread is suppressed by the tombstone
**Now:** resolved/stale issues are tombstoned by fingerprint; `_detect` skips any
candidate whose fingerprint `is_tombstoned`. Episodic recall *tells* the model it may
re-raise a recurring issue, but the fingerprint guard runs regardless.

**Gap:** if the same problem flares up again **in the same thread, same category** (same
fingerprint), the candidate is silently dropped. Only a recurrence from a *new* root
message (new fingerprint) gets re-raised. There is no "re-open a closed issue" path.

### 2.4 Detection quality is the model's judgement
**Now:** what counts as an "issue" is decided entirely by the LLM against the prompt.
The offline suite exercises this with `MockLLM`'s deterministic `#id` heuristic, **not**
real model behaviour.

**Gap:** false positives/negatives in real use depend on the chosen model; the test
suite validates orchestration, not detection accuracy.

---

## 3. Out-of-thread replies & multiple issues per reporter

### 3.1 Bare top-level reply is ambiguous with >1 open issue
**Now:** out-of-thread "source B" capture (`_out_of_thread_reporter_messages`) only
attributes a bare reply when the reporter has **exactly one** open awaiting issue; with
more than one it returns `[]` (can't tell which issue the reply answers).

**Gap:** a reporter juggling several of their own open issues *must* reply in-thread;
top-level answers to a specific issue are dropped. The two safety modes
(`REQUIRE_IN_THREAD_REPLY`, `REDIRECT_OUT_OF_THREAD_REPLY`) tighten this further by
design.

### 3.2 Consolidated multi-issue nudge has no reply home
**Now:** when one reporter has several overdue issues, `_post_escalation` sends **one**
@mention listing them but leaves `escalation_thread_id` unset (a reply in the shared
nudge thread can't be attributed to one of several issues).

**Gap:** the reporter is pointed back to the original threads; a reply typed under the
consolidated nudge isn't captured.

---

## 4. Restart & state

### 4.1 Clarity transcript is thin after a mid-issue restart
**Now:** the working conversation (`Runner._conversation`) lives in memory and, after a
restart, is rebuilt from only the messages fetched *since the cursor*
(`_fetch_new_messages`). `_new_replies` has a `last_bot_create_time` fallback so a fresh
reply is still detected, and `Issue.qa` / `questions_asked` are persisted.

**Gap:** the transcript *rendered to the clarity model* (`analyzer._issue_transcript` →
`Conversation.for_thread`) only contains what's currently in memory, so earlier
already-seen thread messages (including the reporter's prior answers) are **not
re-fetched** and may be absent from the prompt after a restart. The persisted `qa` feeds
the *report*, not the clarity prompt. Resolution still works in practice because issues
close fast (~3 min live), but a restart mid-clarification degrades context.

### 4.2 No history backfill on first run by default
**Now:** a true first run pins the cursor to *now* (`_since` → `_now()`), so the bot
ignores everything posted before it started unless `POLL_BACKFILL_SINCE` is set.

**Gap:** issues already sitting in the channel at startup are not detected.

---

## 5. Channel & content scope

### 5.1 One space, one runner, polling only
**Now:** a single-instance file lock (`_acquire_lock`) enforces one poller, bound to one
`GOOGLE_SPACE`. Ingestion is polling at `POLL_INTERVAL_SECONDS`; the webhook path is a
Phase-2 **stub**.

**Gap:** no multi-space fan-out from one process; reaction latency is bounded by the
poll interval and the per-cycle detection cost; no real-time push.

### 5.2 Text only — attachments, images, cards, reactions ignored
**Now:** the bot reasons over `Message.text` (`models.Message`). Images, uploaded files,
Chat cards, and emoji reactions carry no `text` it can read.

**Gap:** an issue reported as a screenshot, a pasted log file, or a reaction is invisible
to detection and clarity.

### 5.3 Message edits and deletions aren't tracked
**Now:** the cursor advances by `create_time` and dedups by message id (`_fetch_new_
messages` + the seen-id set). An edit doesn't change a message's id or `create_time`.

**Gap:** a reporter who *edits* an earlier message to add the missing detail (rather than
posting a new reply) won't be re-read; a deleted message stays in the bot's working view.

### 5.4 Per-space 1-write/sec Chat limit
**Now:** voice posts are serialised through a single-worker pool, and escalations are
consolidated. But ordinary clarify/confirm posts aren't rate-limited in code.

**Gap:** a burst (several issues resolving in the same cycle) can exceed Google's
per-space **1 write/sec** ceiling and rely on the client's retry/backoff rather than
proactive pacing. See [`docs/google_chat/limits.md.txt`](docs/google_chat/limits.md.txt).

---

## 6. Delivery & downstream

### 6.1 Voice is a file attachment, not a native voice message
**Now:** voice reports are posted as an MP3 **file card** (download-only), with the
transcript carried in the message body. This is a hard Google Chat API ceiling for bots
(see `CLAUDE.md` / `MEMORY.md` "Voice reports = audio FILE attachment only").

**Gap:** no waveform/native voice bubble is possible; the audio can't autoplay in-thread.

### 6.2 Voice is best-effort and optimistic
**Now:** voice runs off the critical path (`_deliver_voice_bg`); the in-thread
confirmation is posted **before** the voice outcome is known, so it always uses the
"recorded" wording even in the rare case voice fails and silently falls back to the
on-disk report.

**Gap:** the confirmation's wording can be optimistic relative to what was actually
delivered (the report still survives on disk; only the wording is ahead of the outcome).

### 6.3 Reports are local disk only — no ticketing integration
**Now:** resolution reports are Markdown files in `REPORTS_DIR` (or a voice note). There
is no Jira/Linear/ServiceNow/webhook export, and STALE issues produce no report at all
(see 1.4).

**Gap:** the documented trail lives on the bot's host machine; nothing is pushed to a
tracker a team would actually watch.

---

## 7. Security & cost

### 7.1 Prompt-injection defence is prompt-level only
**Now:** `prompts._ROLE` / `_render_user` mark the transcript and retrieved context as
UNTRUSTED data and instruct the model to never follow instructions inside them.

**Gap:** this is mitigation by instruction, not hard isolation. A sufficiently crafted
injection could still influence a weaker model; there is no structural guarantee.

### 7.2 Secret redaction is off by default, regex-based, report-only
**Now:** `REDACT_REPORTS` (default **off**) masks high-confidence secrets
(bearer/`sk-`/`AIza`/JWT) in the on-disk report via `report.redact_secrets`.

**Gap:** it never touches the **LLM input path**, so a secret pasted into chat is still
sent to the model/provider; it's regex-based (conservative — misses unusual formats);
and it's off unless explicitly enabled.

### 7.3 No cost / token budget enforcement
**Now:** token usage is accumulated and logged per cycle (`usage_snapshot`, the cycle
summary's `tokens`).

**Gap:** it is *observed*, not *capped* — there is no budget ceiling, so a busy space (or
a detection loop over a large window) can run up provider cost unbounded.

---

## 8. Testing & validation caveats

### 8.1 Offline suite proves orchestration, not model quality
The offline test suite runs entirely on `MockLLM`'s deterministic heuristics. It
validates the loop (detect → ask → resolve, escalation, loop-breaker, self-filter,
voice fallback) but says nothing about real-model detection accuracy or question quality.

### 8.2 Single live validation
The end-to-end live run was validated **once** (3 personal Gmail accounts, one space,
both issues resolved in ~3 min — see `MEMORY.md`). There is no continuous live
integration test, and model-portability is verified by manual runs across a handful of
vendors, not automatically.

---

## 9. Voice call on resolve (caller subsystem)

The `call/` subsystem (Gemini Live as the outbound AI caller — see
[`call/CLAUDE.md`](call/CLAUDE.md)) is demo-machine-only and is **not exercised by the
offline suite**. The entries below were observed on live calls.

### 9.1 Caller is half-duplex — callee speech over the AI's turn can be dropped
**Scenario:** the AI caller is mid-utterance (opening briefing, an answer, or a silence
check-in) and the callee speaks at the same time.

**Now:** Gemini Live is turn-based. Our side never gates the callee — `gemini_voice.
GeminiVoiceBridge._ear_to_gemini` forwards **every** ear frame to Gemini even while the model
is speaking — and a barge-in is honoured when it fires (`_gemini_to_queue` drops the queued AI
audio on `server_content.interrupted`). VAD sensitivity is already maxed (`build_live_config`'s
`automatic_activity_detection` = `START_SENSITIVITY_HIGH` / `END_SENSITIVITY_HIGH`,
`silence_duration_ms=800`).

**Gap:** whether the model actually *abandons its turn to listen* depends on Gemini's own VAD
tripping on speech that overlaps the model's own output — the hardest case for it. When it
doesn't trip, the callee's simultaneous sentence is swallowed: the audio reaches Gemini's ear
but never becomes a turn. There is no half/full-duplex control from our side beyond
sensitivity, which is already at the ceiling.

### 9.2 Silence nudge can fire on top of an already-speaking callee
**Scenario:** the callee is talking, but Gemini hasn't (yet) transcribed it, and the caller's
silence watchdog reaches its threshold and injects a check-in over them.

**Now:** the watchdog (`_ear_to_gemini`, `NUDGE_AFTER_SILENCE_S=12`, `MAX_NUDGES=3`) measures
"silence" by the last time Gemini *transcribed* the callee — `_gemini_to_queue` resets
`_last_voice_activity` and `_nudges_sent` only on `input_transcription`. It does **not** look at
raw ear-audio energy. The nudge itself is a `send_realtime_input(text=NUDGE_TRIGGER)` that
forces an immediate model turn.

**Gap:** if Gemini's input VAD under-triggers (low ear level, or halting speech under its
threshold), the callee can be speaking while the watchdog still counts the line as silent — so
the nudge lands mid-sentence and the model answers the nudge instead of the callee. Compounding
it, because the dropped speech is never transcribed, the watchdog isn't reset, so a second nudge
can follow. A raw-energy gate (suppress the nudge whenever the ear has had recent sound, not
just recent transcription) would close this but is not implemented.

---

## Priority gaps to close first

If this graduated from demo to product, the highest-value fixes are roughly:

1. **1.1 / 1.4** — converge-or-hand-off: detect genuine non-convergence (not just an
   identical gap) and route STALE issues to a human/tracker with a documented summary,
   instead of silently dropping them.
2. **2.1 / 2.3** — windowing and re-open: backfill/rescan so buried issues aren't lost,
   and allow a tombstoned issue to re-open on a genuine recurrence.
3. **5.2 / 5.3** — ingest attachments and react to edits, the two biggest blind spots
   in real chat usage.
4. **6.3** — push resolutions (and stale hand-offs) to a real tracker.
