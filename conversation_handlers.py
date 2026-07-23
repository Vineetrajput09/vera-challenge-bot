"""
Optional §7.4 deliverable: pure multi-turn reply function usable outside
the HTTP harness. Delegates to dialogue.decide_action() -- the same
classifier bot.py's /v1/reply endpoint uses -- so live and offline behavior
can't drift apart.
"""
from __future__ import annotations

from typing import Optional, TypedDict

import dialogue


class ConversationState(TypedDict, total=False):
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str]
    merchant_name: str
    pending_topic: str
    cta_default: str
    inbound_history: list[str]  # every message text ever received from this
                                 # merchant/customer, across conversations
    sent_bodies: list[str]      # bodies already sent in THIS conversation
    ended: bool


def respond(state: ConversationState, merchant_message: str) -> dict:
    """Given the conversation so far + the merchant's latest message,
    produce the reply.

    Mutates `state` in place (appends to inbound_history/sent_bodies, sets
    `ended`) so the caller can persist it and pass it back in on the next
    turn.
    """
    if state.get("ended"):
        return {"action": "end", "rationale": "Conversation already closed."}

    history = state.setdefault("inbound_history", [])
    repeat_count = dialogue.count_prior_occurrences(merchant_message, history)
    history.append(merchant_message)

    result = dialogue.decide_action(
        message=merchant_message,
        repeat_count=repeat_count,
        pending_topic=state.get("pending_topic", ""),
        merchant_name=state.get("merchant_name", ""),
        cta_default=state.get("cta_default", "open_ended"),
    )

    if result["action"] == "end":
        state["ended"] = True
    elif result["action"] == "send":
        sent = state.setdefault("sent_bodies", [])
        body = result.get("body", "")
        if body in sent:
            result = {
                "action": "wait",
                "wait_seconds": 3600,
                "rationale": "Next reply would repeat a prior message verbatim; waiting instead of repeating.",
            }
        else:
            sent.append(body)

    return result
