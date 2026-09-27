# orchestration/loop_guard.py
"""
Deterministic circuit breaker for inter-agent message loops.

Integration point: `inbox.send` — the single funnel every inter-agent
message passes through. Delegations, worker reports, terminal acks,
SEND_REPLY, ASK_CEO, ASK_USER, grace replies, error messages — all
of them route through `inbox.send`. One hook here catches everything.

The guard sees every message between `ceo` and `worker_*`. It does
NOT see user messages (`user_*`) — the user is authoritative and must
never be blocked.

Three detection modes, in order of cost:

  1. Terminal acks ("Already reported — no new work this turn.")
     are dropped silently. They are stop signals, not messages.
     This alone kills half of the loop class you're hitting.

  2. Exact repeats — same direction, same agent pair, byte-identical
     after whitespace + lowercase normalization. Caught without
     ever touching the embedding model.

  3. Semantic near-repeats — reworded versions of the same message.
     Caught with sentence embeddings. This is the mode that fixes
     "core API is in the backend repo" repeated forty times in
     slightly different words.

No halt mode. When the guard blocks, `inbox.send` returns a
BlockedSend object instead of a row id. `messenger._send_message`
detects this and returns a clear tool result to the LLM. The turn
loop needs no changes — it already handles arbitrary tool results.

State is in-process and per-org. Lost on worker restart. That is
fine: the guard stops the loop before it triggers OOM, not after.
"""

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# Block reason constants
# ══════════════════════════════════════════════════════════════════════
# Named constants so downstream code (inbox._handle_block,
# messenger._render_blocked_result) can compare without string literals.
BLOCK_TERMINAL_ACK = "terminal_ack"
BLOCK_EXACT_REPEAT = "exact_repeat"
BLOCK_NEAR_REPEAT  = "near_repeat"


# ══════════════════════════════════════════════════════════════════════
# Terminal acks
# ══════════════════════════════════════════════════════════════════════
# The worker prompt instructs workers to send one of these when they
# have nothing left to do. They are terminal signals, not messages.
# Dropping them silently kills one whole class of loop before it
# reaches the embedding model.
_TERMINAL_ACK_PREFIXES = (
    "already reported",
    "no new work this turn",
    "already done",
    "nothing to do",
    "nothing new to add",
)


def _normalize(body: str) -> str:
    return " ".join((body or "").split()).lower()


def is_terminal_ack(body: str) -> bool:
    head = _normalize(body)
    return any(head.startswith(p) for p in _TERMINAL_ACK_PREFIXES)


# ══════════════════════════════════════════════════════════════════════
# Embedding model (lazy, cached, thread-safe)
# ══════════════════════════════════════════════════════════════════════
# all-MiniLM-L6-v2 is ~90 MB, ~15–30 ms per encode on CPU, and good
# enough for "is this sentence saying the same thing". Loaded on
# first use. If loading fails, semantic detection degrades gracefully
# to exact-repeat only — the guard still works, just with a narrower
# net.
_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from sentence_transformers import SentenceTransformer
            logger.info("loop_guard: loading embedding model (cold start)")
            _model = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("loop_guard: embedding model ready")
        except Exception:
            logger.exception(
                "loop_guard: embedding model load failed — "
                "semantic detection disabled, exact-match only"
            )
            _model = False  # sentinel: don't retry on every call
    return _model


def _embed(body: str):
    m = _get_model()
    if not m:
        return None
    try:
        vec = m.encode(body, normalize_embeddings=True)
        return vec.astype("float32")
    except Exception:
        logger.exception("loop_guard: embed failed")
        return None


def _cosine(a, b) -> float:
    # Both vectors are unit-length (normalize_embeddings=True), so
    # cosine similarity is just the dot product.
    return float((a * b).sum())


def warm_model() -> None:
    """
    Preload the embedding model. Call from boot (gm.py) to avoid a
    1–2s stall on the first send_message. Safe to call repeatedly.
    """
    _get_model()


# ══════════════════════════════════════════════════════════════════════
# Block descriptor
# ══════════════════════════════════════════════════════════════════════
@dataclass
class Block:
    """
    A message that was stopped by the guard.

    reason          — one of BLOCK_TERMINAL_ACK / BLOCK_EXACT_REPEAT /
                      BLOCK_NEAR_REPEAT
    matched_body    — the earlier message this one matched
    matched_age_sec — how long ago that earlier message was sent
    similarity      — cosine similarity, only set for near_repeat
    """
    reason: str
    matched_body: str
    matched_age_sec: float
    similarity: Optional[float] = None


# ══════════════════════════════════════════════════════════════════════
# The guard
# ══════════════════════════════════════════════════════════════════════
@dataclass
class LoopGuard:
    """
    Per-org guard. Tracks recent messages between agent pairs.

    Tuning:
      similarity_threshold — raise to block fewer rewordings; lower
                             to catch weaker repeats. 0.88 works for
                             the 'core API in backend repo' class of
                             loop.
      window_sec           — how far back to look for repeats.
      max_history          — max messages retained per guard.
      notify_cooldown_sec  — minimum gap between user notifications
                             about a tripped guard.
    """
    similarity_threshold: float = 0.88
    window_sec: float = 180.0
    max_history: int = 12
    notify_cooldown_sec: float = 600.0

    history: Deque[dict] = field(default_factory=lambda: deque(maxlen=12))
    last_notify_at: float = 0.0

    def __post_init__(self):
        # Re-wrap in case the caller passed a plain list/tuple.
        self.history = deque(self.history, maxlen=self.max_history)

    # ── Main entry point ────────────────────────────────────────────
    def observe(
        self,
        from_agent: str,
        to_agent: str,
        body: str,
        now: Optional[float] = None,
    ) -> Optional[Block]:
        """
        Return None if the message is fine to deliver.
        Return a Block if it should be stopped.
        """
        now = now if now is not None else time.time()

        # Prune entries older than the window.
        cutoff = now - self.window_sec
        while self.history and self.history[0]["ts"] < cutoff:
            self.history.popleft()

        # ── 1. Terminal ack: silent drop ─────────────────────────────
        if is_terminal_ack(body):
            return Block(
                reason=BLOCK_TERMINAL_ACK,
                matched_body=body,
                matched_age_sec=0.0,
            )

        norm = _normalize(body)

        # ── 2. Exact repeat: cheap, no embed ─────────────────────────
        for entry in self.history:
            if (entry["from"] == from_agent
                    and entry["to"] == to_agent
                    and entry["norm"] == norm):
                return Block(
                    reason=BLOCK_EXACT_REPEAT,
                    matched_body=entry["body"],
                    matched_age_sec=now - entry["ts"],
                    similarity=1.0,
                )

        # ── 3. Semantic near-repeat ──────────────────────────────────
        vec = _embed(body)
        if vec is not None:
            for entry in self.history:
                # Same direction only: a reworded CEO→worker message
                # is compared against prior CEO→worker messages.
                if entry["from"] != from_agent or entry["to"] != to_agent:
                    continue
                evec = entry.get("vec")
                if evec is None:
                    continue
                sim = _cosine(vec, evec)
                if sim >= self.similarity_threshold:
                    return Block(
                        reason=BLOCK_NEAR_REPEAT,
                        matched_body=entry["body"],
                        matched_age_sec=now - entry["ts"],
                        similarity=sim,
                    )

        # ── Not a repeat: record and pass ────────────────────────────
        self.history.append({
            "from": from_agent,
            "to": to_agent,
            "body": body,
            "norm": norm,
            "vec": vec,
            "ts": now,
        })
        return None

    # ── User notification gate ──────────────────────────────────────
    def should_notify_user(self, now: Optional[float] = None) -> bool:
        """
        Return True at most once per notify_cooldown_sec. Called by
        inbox._handle_block before writing to the user thread.
        """
        now = now if now is not None else time.time()
        if now - self.last_notify_at < self.notify_cooldown_sec:
            return False
        self.last_notify_at = now
        return True


# ══════════════════════════════════════════════════════════════════════
# Per-org singletons
# ══════════════════════════════════════════════════════════════════════
_guards: Dict[str, LoopGuard] = {}
_guards_lock = threading.Lock()


def get_guard(org_id: Optional[str] = None) -> LoopGuard:
    """
    Return the guard for this org. org_id defaults to $ORG_ID, then
    to the literal string "default" if the env var is unset.
    """
    org_id = org_id or os.environ.get("ORG_ID", "default")
    g = _guards.get(org_id)
    if g is None:
        with _guards_lock:
            g = _guards.get(org_id)
            if g is None:
                g = LoopGuard()
                _guards[org_id] = g
    return g
