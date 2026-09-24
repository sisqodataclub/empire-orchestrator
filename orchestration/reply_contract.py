# orchestration/reply_contract.py
"""
The reply contract.

Every CEO → user SEND_REPLY carries a machine-readable claim about
what the CEO did this turn:

    claimed_actions: ["read", "write", "delegate", ...]
    evidence_ids:    [1, 3, ...]   # call IDs from this turn's ledger

validate() checks the claim against the TurnLedger. If the claim
isn't backed by an actual tool call of the right kind, the reply is
rejected and the CEO sees a system note describing the mismatch.
The CEO retries with a truthful claim.

Rules
─────
1. Every claimed work class must be present in the ledger.
2. Work claims require evidence_ids; each ID must exist and match
   the claimed class.
3. Claiming "none" after doing direct work OR delegating → rejected.
4. Claiming direct work (read/write/execute) when only delegation
   occurred → rejected. (Pure ["delegate"] claims are exempt — that
   IS the truthful delegation claim.)
5. Claiming "incomplete" after delegating without yet reading
   anything → rejected. Forces the CEO to declare the delegation
   instead of hiding behind "I'm on it".
6. "none" and "incomplete" cannot be combined with work claims.

What is NOT validated here
──────────────────────────
Worker → CEO replies. Their SEND_REPLY goes to the CEO, who verifies
separately via read_agent_log. Only CEO → user replies are contracted.

The "is this chat or a claim?" distinction for ["none"] replies is
intentionally permissive for now — deferred to a later pass. A
["none"] reply is accepted if the ledger contains no work and no
delegation. Policing the *content* of a ["none"] reply (e.g. "I'm
on it, give me a bit") is a separate problem.
"""
from enum import Enum

from orchestration.turn_ledger import TurnLedger, ActionClass


class ClaimedAction(str, Enum):
    NONE       = "none"        # pure chat — no tools, no claim
    READ       = "read"
    WRITE      = "write"
    EXECUTE    = "execute"
    DELEGATE   = "delegate"
    INCOMPLETE = "incomplete"  # honest non-claim: failed / waiting / not started


# Which ActionClass must be present for each claim.
# NONE and INCOMPLETE require nothing.
_REQUIRED: dict[ClaimedAction, ActionClass | None] = {
    ClaimedAction.NONE:       None,
    ClaimedAction.READ:       ActionClass.READ,
    ClaimedAction.WRITE:      ActionClass.WRITE,
    ClaimedAction.EXECUTE:    ActionClass.EXECUTE,
    ClaimedAction.DELEGATE:   ActionClass.DELEGATE,
    ClaimedAction.INCOMPLETE: None,
}


# The set of claims that represent "direct work" — i.e. not delegation
# and not the two non-claim sentinels.
_DIRECT_WORK_CLAIMS = frozenset({
    ClaimedAction.READ,
    ClaimedAction.WRITE,
    ClaimedAction.EXECUTE,
})


def validate(
    claimed: list[ClaimedAction],
    evidence_ids: list[int],
    ledger: TurnLedger,
) -> tuple[bool, str]:
    """
    Return (ok, reason). reason == "ok" when ok is True.

    Called from the SEND_REPLY branch in agent_loop.run_agent_turn,
    only when cfg["is_ceo"] and target.startswith("user_").
    """
    # ── Structural checks on the claim itself ──────────────────────
    if not claimed:
        return False, (
            "claimed_actions is empty. "
            "Use ['none'] for chat or ['incomplete'] for an honest "
            "non-claim."
        )

    if not isinstance(evidence_ids, list):
        return False, "evidence_ids must be a list of integers."

    real = [
        c for c in claimed
        if c not in (ClaimedAction.NONE, ClaimedAction.INCOMPLETE)
    ]

    # Rule 6 — can't mix "nothing happened" with a work claim.
    if real and (
        ClaimedAction.NONE in claimed
        or ClaimedAction.INCOMPLETE in claimed
    ):
        return False, (
            "Cannot mix 'none'/'incomplete' with a work claim. "
            "Use ['incomplete'] alone to say no work happened."
        )

    present = ledger.classes_present()

    # ── Rule 1 — every claimed class must be present this turn ─────
    missing = {
        _REQUIRED[c] for c in real
        if _REQUIRED[c] is not None and _REQUIRED[c] not in present
    }
    if missing:
        missing_str = ", ".join(c.value for c in missing)
        return False, (
            f"Claimed {[c.value for c in real]}, but this turn's ledger "
            f"contains no {missing_str} action.\n"
            f"Actual calls this turn:\n{ledger.summary()}"
        )

    # ── Rule 2 — evidence required for work claims, and matching ───
    if real:
        if not evidence_ids:
            return False, (
                f"Claim {[c.value for c in real]} requires evidence_ids "
                f"referencing the call IDs that back it.\n"
                f"Actual calls this turn:\n{ledger.summary()}"
            )
        by_id = ledger.by_id()
        allowed = {_REQUIRED[c] for c in real if _REQUIRED[c] is not None}
        for eid in evidence_ids:
            if not isinstance(eid, int):
                return False, (
                    f"evidence_id {eid!r} is not an integer. "
                    f"Cite the numeric call IDs from TOOL RESULT blocks."
                )
            rec = by_id.get(eid)
            if rec is None:
                return False, (
                    f"evidence_id {eid} does not exist this turn. "
                    f"Cite only IDs from this turn's TOOL RESULT blocks.\n"
                    f"Actual calls this turn:\n{ledger.summary()}"
                )
            if rec.action_class not in allowed:
                return False, (
                    f"evidence #{eid} is {rec.action_class.value}, "
                    f"but the claim is {[c.value for c in real]}.\n"
                    f"Actual calls this turn:\n{ledger.summary()}"
                )

    # ── Rule 3 — claimed 'none' but did work OR delegated ──────────
    if ClaimedAction.NONE in claimed and (
        ledger.did_direct_work() or ledger.did_delegate()
    ):
        return False, (
            "You did work this turn but claimed 'none'. "
            "Use the appropriate claim (read / write / execute / "
            "delegate) and state what you did.\n"
            f"Actual calls this turn:\n{ledger.summary()}"
        )

    # ── Rule 4 — claimed direct work but only delegation occurred ──
    direct_claims = [c for c in real if c in _DIRECT_WORK_CLAIMS]
    if direct_claims and not ledger.did_direct_work():
        return False, (
            f"Claimed {[c.value for c in direct_claims]} but only "
            f"delegation occurred this turn. If you delegated, use "
            f"claimed_actions=['delegate'] and tell the user which "
            f"worker you asked.\n"
            f"Actual calls this turn:\n{ledger.summary()}"
        )

    # ── Rule 5 — 'incomplete' laundering after delegation ──────────
    # If the CEO delegated this turn and hasn't yet read anything,
    # the honest claim is ['delegate'], not ['incomplete']. This
    # closes the "I'm on it, give me a bit" hole: the CEO can't
    # narrate pending action under the incomplete umbrella when it
    # has already dispatched work to a worker.
    if (
        ClaimedAction.INCOMPLETE in claimed
        and ledger.did_delegate()
        and not ledger.did_direct_work()
    ):
        return False, (
            "You delegated to a worker this turn but claimed "
            "'incomplete'. State what you delegated: use "
            "claimed_actions=['delegate'] and name the worker in "
            "the body.\n"
            f"Actual calls this turn:\n{ledger.summary()}"
        )

    return True, "ok"
