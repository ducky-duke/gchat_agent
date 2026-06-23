"""Env-driven settings (§5.1 + §10).

A tiny stdlib `.env` loader (no `python-dotenv`): parse a `.env` file if present,
overlay `os.environ`, then fall back to the §10 defaults. `load_config()` returns
a frozen `Config` with every key from §10, coerced to the right type.

Sensible defaults mean the mock-LLM / CI test path needs no configuration.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Final

# --- precedence: .env file < process env < defaults-as-fallback -------------
# We read .env first, overlay os.environ on top, then each Config field falls
# back to its hard-coded default when the key is absent from both layers.

_DEFAULT_ENV_FILE: Final[str] = ".env"

_TRUE_TOKENS: Final[frozenset[str]] = frozenset({"true", "1", "yes", "on", "y"})


def _clean_value(val: str) -> str:
    """Strip an inline `# comment` and surrounding quotes from a raw .env value.

    Quoting wins: a value wrapped in matching quotes is taken verbatim (any `#`
    inside is literal; trailing content after the closing quote is dropped). For
    an unquoted value a `#` that follows whitespace begins an inline comment, so
    `http://x#frag` or a leading `#fff` survive intact.

    A value that is only whitespace then a `#` comment (`KEY=   # note`) is an
    empty value — caught BEFORE the strip below, because stripping would make the
    `#` look like a leading literal (`KEY=#fff`) and slip past the `i > 0` guard."""
    if val[:1] in (" ", "\t") and val.lstrip().startswith("#"):
        return ""
    val = val.strip()
    if not val:
        return val
    if val[0] in ("'", '"'):
        quote = val[0]
        end = val.find(quote, 1)
        return val[1:end] if end != -1 else val[1:]
    for i, ch in enumerate(val):
        if ch == "#" and i > 0 and val[i - 1] in (" ", "\t"):
            return val[:i].rstrip()
    return val


def _parse_env_file(path: str) -> dict[str, str]:
    """Parse a `.env` file into a dict. KEY=VALUE per line; blanks and
    `#`-comment lines are ignored; inline `# comments` and surrounding quotes
    on values are stripped (see `_clean_value`). A missing file yields an empty
    dict (the test path needs no .env)."""
    values: dict[str, str] = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):  # tolerate `export KEY=VALUE`
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if not key:
                continue
            values[key] = _clean_value(val)
    return values


def _to_bool(value: str) -> bool:
    """Parse a bool from `true`/`1`/`yes` (case-insensitive)."""
    return value.strip().lower() in _TRUE_TOKENS


def _to_int(value: str, default: int) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return default


def _to_float(value: str, default: float) -> float:
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return default


@dataclass(frozen=True)
class Config:
    """Immutable, fully-resolved settings (every §10 key)."""

    # --- LLM provider / transport ---
    LLM_PROVIDER: str = "openrouter"  # or: mock
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "deepseek/deepseek-v4-flash"
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    # Send `extra_body={"reasoning": {"enabled": True}}` on every completion so
    # reasoning-capable models (e.g. deepseek-v4-flash) think before answering.
    # Set OPENROUTER_REASONING=false to disable (lower latency/cost).
    OPENROUTER_REASONING: bool = True
    # Comma-separated provider quantizations OpenRouter may route to (it skips
    # endpoints serving other quants), e.g. "fp8" or "fp8,bf16". Default empty =
    # no constraint (OpenRouter auto-routes) — a hard pin like "fp8" 404s on any
    # model with no endpoint at that quant, which breaks swapping models. Opt in
    # via .env. Sent as `extra_body={"provider": {"quantizations": [...]}}`.
    OPENROUTER_QUANTIZATIONS: str = ""

    # --- Observability (optional [observability] extra) ---
    OBSERVABILITY: str = "none"  # or: langfuse
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""

    # --- RAG ---
    RAG_DENSE: bool = False
    RAG_TOP_K: int = 5
    KB_DIR: str = "data/knowledge_base"
    # Episodic recall: when on, detection is shown a compact block of the few most
    # recently closed issues (title/category/outcome) so the model has memory of
    # what was already handled. Self-gating — empty on a fresh start (no closed
    # issues yet, e.g. after `./start_bot.sh` wipes `.state/`). Off ⇒ no block.
    EPISODIC_RECALL: bool = True

    # Cross-thread duplicate detection via the LLM: when on, a freshly-detected
    # candidate that the cheap lexical dedup did NOT match is checked against the
    # open issues in other threads (sharing a lexical hint) with one focused LLM
    # call — does it describe the SAME incident a second reporter already raised?
    # Catches paraphrases raw-token overlap can't merge safely. Off ⇒ lexical-only
    # cross-thread dedup. No effect on the `mock` path (it always answers "no").
    SEMANTIC_DEDUP: bool = True

    # --- Agent loop ---
    MAX_CLARIFY_ROUNDS: int = 3
    # Loop-breaker for the "duplicate question" failure: how many consecutive
    # clarify replies may leave the missing-facts set unchanged before the bot
    # stops re-asking and closes the issue with the remaining facts documented as
    # open questions. A reporter saying "I don't know" closes it immediately,
    # regardless of this count. Set to a very large value to effectively disable
    # the no-progress backstop (the explicit "I don't know" path still fires).
    MAX_NO_PROGRESS_ROUNDS: int = 2
    STALE_AFTER_IDLE_CYCLES: int = 3
    # Wall-clock grace before the bot reminds a reporter about a CLARIFYING issue
    # they haven't answered: once this many SECONDS have elapsed since the last
    # clarifying question with no reply, the bot posts ONE top-level @mention
    # nudge. Each issue is reminded exactly once (per-issue one-shot). Overdue
    # issues that cross the grace line in the SAME poll cycle are consolidated
    # into one @mention; issues that go overdue at different times each get their
    # own single reminder. Staleness is deferred until the nudge has had its
    # chance. 0 ⇒ remind on the first idle cycle; a negative value disables
    # escalation entirely.
    ESCALATE_AFTER_SECONDS: int = 300
    # When true, an issue is only advanced by replies posted IN ITS OWN THREAD
    # (plus the bot's own escalation/nudge thread, which is a 1:1 home for it):
    # the runner stops attributing a reporter's bare top-level / other-thread
    # messages to the issue (`_effective_conversation` source B). Safer in a busy
    # shared space — the bot can't "barge into" unrelated discussion by mistaking
    # an off-topic message for an answer — at the cost of not catching answers the
    # reporter types outside the thread. Default off (preserves out-of-thread
    # capture); recommended on for a live demo with non-staff participants.
    REQUIRE_IN_THREAD_REPLY: bool = False
    # Production "redirect-on-capture" (§ out-of-thread capture). When true, a
    # reporter's reply that lands OUTSIDE the issue thread is never trusted to
    # resolve the issue, feed the clarity/question LLM, enter the Q&A / report /
    # voice, or move `active_thread_id` — all the leak/mis-placement paths a busy
    # shared space exposes. Instead the runner records it as evidence (message
    # ids only) and posts ONE templated, LLM-free nudge into the issue's OWN
    # thread asking the reporter to confirm there; the issue then resolves only
    # from in-thread + home-thread (A) replies. This implies the in-thread-only
    # resolve gate regardless of REQUIRE_IN_THREAD_REPLY (it is that strict floor
    # PLUS a redirect). Default off; recommended for production in a space with
    # non-staff participants. (For a one-off live demo, the simpler
    # REQUIRE_IN_THREAD_REPLY is enough.)
    REDIRECT_OUT_OF_THREAD_REPLY: bool = False
    RESOLVE_CONFIDENCE_THRESHOLD: float = 0.8
    DETECT_WINDOW_MESSAGES: int = 50
    STATE_FILE: str = ".state/issues.json"
    REPORTS_DIR: str = "reports"

    # --- Resolution-report delivery ---
    # How a resolved issue's report is delivered:
    #   "disk"  — write Markdown to REPORTS_DIR (default; the offline/test path).
    #   "voice" — synthesize a spoken summary (TTS) and post it as an audio
    #             attachment to GOOGLE_VOICE_SPACE (falls back to disk if voice
    #             delivery is unavailable or fails, so a report is never lost).
    #   "both"  — write the Markdown AND post the voice attachment.
    REPORT_DELIVERY: str = "disk"  # disk | voice | both
    # Off-by-default, report-ONLY secret redaction. When on, the on-disk Markdown
    # report (and only it) has high-confidence secrets masked — bearer tokens,
    # OpenAI/Google-style API keys, JWTs. Deliberately conservative so it never
    # touches the LLM input path and won't mangle ticket ids / short tokens.
    # Recommended OFF: the bot never logs Authorization headers, so this is belt-
    # and-suspenders for the case a staff member pastes a live secret into chat.
    REDACT_REPORTS: bool = False
    # Text-to-speech (OpenRouter `audio.speech`, reuses the OpenRouter transport).
    # TTS_VOICE is model-specific — a wrong voice 404s ("Provider returned 404").
    # For x-ai/grok-voice-tts-1.0: default | ara | rex | sal | eve | leo (NOT the
    # OpenAI-style "alloy"). TTS_FORMAT is the audio container; grok accepts only
    # "mp3" (a real container) or "pcm" (raw, unplayable in Chat) — keep "mp3".
    TTS_MODEL: str = "x-ai/grok-voice-tts-1.0"
    TTS_VOICE: str = "default"
    TTS_FORMAT: str = "mp3"

    # --- Google Chat (real demo only — user OAuth, §7) ---
    GOOGLE_SPACE: str = ""
    # Where voice reports are delivered (a DM space with another account, or a
    # dedicated "reports" space). The bot must be a member. Empty ⇒ fall back to
    # the issue's own space, posting the voice into the issue thread.
    GOOGLE_VOICE_SPACE: str = ""
    GOOGLE_OAUTH_CLIENT: str = "secrets/oauth_client.json"
    GOOGLE_TOKEN_FILE: str = "secrets/token_bot.json"
    GOOGLE_QUOTA_PROJECT: str = ""
    # OPTIONAL override for the bot's own `users/<id>` (used to self-filter its
    # OWN messages out of detection, §5.7/§6). Normally the client auto-resolves
    # this from the OAuth tokeninfo endpoint on its first `me()` (`sub` == the
    # Chat user id) — needing only the `userinfo.email` scope the demo grants — so
    # self-filtering works from cycle 1 without pinning or posting. Set this only
    # to pin a known id (skips the one startup lookup) or for an offline path with
    # no tokeninfo reachability. Accepts a bare numeric id or the `users/<id>`
    # form. Precedence: this > persisted `.state/` > tokeninfo > learn-from-post.
    GOOGLE_BOT_USER_ID: str = ""
    POLL_INTERVAL_SECONDS: int = 15
    POLL_BACKFILL_SINCE: str = ""

    # --- GitHub issue export (optional) ---
    # When on, each resolved issue is also filed as a GitHub issue (report +
    # collected thread transcript) in GITHUB_REPO — a durable, searchable backlog
    # of technical issues. Off by default (the offline/test path needs no GitHub).
    GITHUB_ISSUES: bool = False
    # Target repo as "owner/name" (e.g. "dttran-glo/gchat-agent-issues"). Required
    # when GITHUB_ISSUES is on; ignored otherwise.
    GITHUB_REPO: str = ""
    # Personal-access / OAuth token with `repo` scope. Leave blank to let the
    # factory fall back to the host `gh auth token` (the demo machine is already
    # logged in), so no secret needs to live in .env.
    GITHUB_TOKEN: str = ""
    # Which host `gh` account to pull the token from when GITHUB_TOKEN is blank —
    # passed as `gh auth token --user <account>`. Pins the export to one account
    # (e.g. "ducky-duke") even when a different gh account is "active", so the bot
    # files under the intended owner without switching the machine's active login.
    # Blank ⇒ the active account's token (`gh auth token`).
    GITHUB_ACCOUNT: str = ""
    # REST API base (override only for GitHub Enterprise).
    GITHUB_API_URL: str = "https://api.github.com"

    # --- Outbound voice call on resolve (call/gemini_call.py) ---
    # When on, each resolved issue ALSO triggers an outbound voice call that RELAYS
    # the clarified incident to a human: the runner spawns call/gemini_call.py
    # (Gemini Live as the incident-duty assistant) as a DETACHED subprocess, off
    # the resolve critical path, reading the report's facts from a JSON file it
    # writes. Best-effort and self-serializing (one call at a time — the caller
    # browser + virtual audio devices can't host two).
    #
    # DEFAULT ON: once an issue is clarified the bot calls. It is still
    # SELF-GATING, though — the spawn is skipped (logged, never an error) unless a
    # GEMINI_API_KEY is configured, because gemini_call.py cannot work without one.
    # So the offline/test path (no key) and any deployment without a Gemini key
    # silently make no call; the call only fires on a machine actually set up for
    # it (key + the dedicated caller Brave profile + a VISIBLE desktop session —
    # Wayland suspends an occluded renderer, so never headless/CI). Set False to
    # disable entirely.
    CALL_ON_RESOLVE: bool = True
    # Gemini Live API key for the outbound call (DISTINCT from OPENROUTER_API_KEY).
    # The runner only reads it to decide whether the call can work (the spawn is
    # skipped when blank); gemini_call.py reads the real key from env/.env itself.
    GEMINI_API_KEY: str = ""
    # The call orchestrator to spawn (relative to the poller's cwd, i.e. repo root).
    CALL_SCRIPT: str = "call/gemini_call.py"
    # The callee's name the AI addresses on the call (who picks up the phone).
    CALL_CALLEE: str = "Duc"
    # Spoken + briefing language for the call: "en" (English) or "vi" (Vietnamese).
    CALL_LANGUAGE: str = "en"
    # Exact Chat DM URL to ring. Blank ⇒ gemini_call.py's built-in default (the
    # bot↔Duc DM). Set to call into a different DM/space.
    CALL_URL: str = ""
    # Name of the on-call engineer who RAISED/owns the incident, spoken on the call
    # ("an incident <owner> just raised"). The bot only knows the reporter by Chat
    # id, so this names them for the relay. Blank ⇒ a generic "the on-call engineer".
    CALL_OWNER: str = ""
    # Where the spawn's incident JSON + the call's stdout log are written (relative
    # to cwd). gitignored. The call orchestrator also writes its own detailed log
    # under logs/ on the live run.
    CALL_LOG_DIR: str = "logs"

    # --- Google Meet links (optional — Meet REST API, user OAuth) ---
    # When on, the demo can mint a real Google Meet meeting via the Meet REST API
    # (`spaces.create`) and post its join link into Chat so a human joins a live
    # incident call. Off by default (the offline/test path needs no Meet API).
    # Requires the OAuth token to carry the `meetings.space.created` scope — added
    # to scripts/authorize.py, so a token minted before that scope was added must
    # be re-authorized (an old token gets a 403 on create). An AI can't *speak*
    # on a Meet (the Media API is receive-only + preview-gated, see
    # docs/google_meet/); this only creates and shares the link.
    MEET_LINKS: bool = False
    # Meet REST API v2 resource base (override only for testing/proxies).
    MEET_API_URL: str = "https://meet.googleapis.com/v2"

    # --- Webhook (Phase 2 only) ---
    WEBHOOK_PORT: int = 8080
    WEBHOOK_AUTH_AUDIENCE: str = ""


# Which fields need non-string coercion (everything else stays a str).
_BOOL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "RAG_DENSE",
        "OPENROUTER_REASONING",
        "REQUIRE_IN_THREAD_REPLY",
        "REDIRECT_OUT_OF_THREAD_REPLY",
        "EPISODIC_RECALL",
        "SEMANTIC_DEDUP",
        "REDACT_REPORTS",
        "GITHUB_ISSUES",
        "MEET_LINKS",
        "CALL_ON_RESOLVE",
    }
)
_INT_KEYS: Final[frozenset[str]] = frozenset({
    "RAG_TOP_K",
    "MAX_CLARIFY_ROUNDS",
    "MAX_NO_PROGRESS_ROUNDS",
    "STALE_AFTER_IDLE_CYCLES",
    "ESCALATE_AFTER_SECONDS",
    "DETECT_WINDOW_MESSAGES",
    "POLL_INTERVAL_SECONDS",
    "WEBHOOK_PORT",
})
_FLOAT_KEYS: Final[frozenset[str]] = frozenset({"RESOLVE_CONFIDENCE_THRESHOLD"})


# Allowed values for the enum-like string settings (lower-cased on compare).
_ENUM_CHOICES: Final[dict[str, frozenset[str]]] = {
    "LLM_PROVIDER": frozenset({"mock", "openrouter"}),
    "OBSERVABILITY": frozenset({"none", "langfuse"}),
    "REPORT_DELIVERY": frozenset({"disk", "voice", "both"}),
}


def validate_config(config: Config) -> Config:
    """Fail fast on an out-of-range / mistyped setting (§10), returning `config`.

    Catches the misconfigurations the type coercion in `load_config` can't — an
    invalid enum (`REPORT_DELIVERY=voce`) or a nonsensical number (a negative
    threshold, a zero poll interval) — *at load*, with one message listing every
    problem, instead of a confusing failure deep in a poll cycle. Provider/key
    validation stays in `build_llm`/`build_tts` (they alone know the mock path is
    keyless), so this never rejects the offline `LLM_PROVIDER=mock` config. Raises
    `ValueError` if anything is wrong."""
    problems: list[str] = []

    for key, choices in _ENUM_CHOICES.items():
        val = str(getattr(config, key) or "").strip().lower()
        if val not in choices:
            allowed = ", ".join(sorted(choices))
            problems.append(f"{key}={getattr(config, key)!r} (allowed: {allowed})")

    if not (0.0 <= config.RESOLVE_CONFIDENCE_THRESHOLD <= 1.0):
        problems.append(
            f"RESOLVE_CONFIDENCE_THRESHOLD={config.RESOLVE_CONFIDENCE_THRESHOLD} "
            "(must be between 0.0 and 1.0)"
        )
    # (key, value, minimum) — each must be >= its floor.
    for key, value, minimum in (
        ("POLL_INTERVAL_SECONDS", config.POLL_INTERVAL_SECONDS, 1),
        ("MAX_CLARIFY_ROUNDS", config.MAX_CLARIFY_ROUNDS, 0),
        ("MAX_NO_PROGRESS_ROUNDS", config.MAX_NO_PROGRESS_ROUNDS, 1),
        ("STALE_AFTER_IDLE_CYCLES", config.STALE_AFTER_IDLE_CYCLES, 1),
        ("DETECT_WINDOW_MESSAGES", config.DETECT_WINDOW_MESSAGES, 1),
        ("RAG_TOP_K", config.RAG_TOP_K, 0),
    ):
        if value < minimum:
            problems.append(f"{key}={value} (must be >= {minimum})")
    if not (1 <= config.WEBHOOK_PORT <= 65535):
        problems.append(f"WEBHOOK_PORT={config.WEBHOOK_PORT} (must be 1..65535)")
    # GitHub export needs a target repo; require an "owner/name" shape so a typo
    # fails at load, not on the first file-issue attempt mid-cycle. The token may
    # be blank (the factory falls back to `gh auth token`), so it isn't checked.
    if config.GITHUB_ISSUES:
        repo = (config.GITHUB_REPO or "").strip()
        parts = repo.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            problems.append(
                f"GITHUB_REPO={config.GITHUB_REPO!r} "
                "(required when GITHUB_ISSUES is on; must be 'owner/name')"
            )

    if problems:
        raise ValueError(
            "invalid configuration:\n  - " + "\n  - ".join(problems)
        )
    return config


def load_config(env_file: str = _DEFAULT_ENV_FILE) -> Config:
    """Resolve a `Config` from (`.env` file < `os.environ`), defaulting any key
    absent from both to the field default. `int`/`float`/`bool` keys are coerced;
    everything else stays a string. The resolved config is `validate_config`-d
    before return, so an invalid enum/range fails at load, not mid-cycle."""
    layered: dict[str, str] = {}
    layered.update(_parse_env_file(env_file))
    layered.update(os.environ)  # process env overrides .env

    kwargs: dict[str, object] = {}
    for field in fields(Config):
        name = field.name
        if name not in layered:
            continue  # leave the dataclass default in place
        raw = layered[name]
        if name in _BOOL_KEYS:
            kwargs[name] = _to_bool(raw)
        elif name in _INT_KEYS:
            kwargs[name] = _to_int(raw, field.default)  # type: ignore[arg-type]
        elif name in _FLOAT_KEYS:
            kwargs[name] = _to_float(raw, field.default)  # type: ignore[arg-type]
        else:
            kwargs[name] = raw
    return validate_config(Config(**kwargs))  # type: ignore[arg-type]
