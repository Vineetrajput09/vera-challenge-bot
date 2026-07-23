"""
Vera-challenge submission bot: FastAPI server implementing the 5 endpoints
from challenge-testing-brief.md, on top of composer.compose() (the 4-stage
composition pipeline) and dialogue.decide_action() (the shared multi-turn
classifier also used by conversation_handlers.py).

Run: uvicorn bot:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

import composer
import dialogue

app = FastAPI(title="Vera Challenge Bot")
START = time.time()

MAX_ACTIONS_PER_TICK = 20

# ---------------------------------------------------------------------------
# In-memory state (per §11: nothing persisted after teardown; in-memory is
# fine per the testing brief as long as the bot doesn't restart mid-test)
# ---------------------------------------------------------------------------

# (scope, context_id) -> {"version": int, "payload": dict}
contexts: dict[tuple[str, str], dict] = {}

# conversation_id -> conversation state
conversations: dict[str, dict] = {}

# (merchant_id, customer_id_or_None) -> list of every inbound message text
# ever received from this party, across ALL conversation_ids. Needed because
# some judge scenarios open a fresh conversation_id per auto-reply turn, so
# per-conversation history alone would never catch the repeat.
party_inbound_history: dict[tuple[str, Optional[str]], list[str]] = {}

# suppression_keys already sent at least once -- don't resend the same
# trigger every tick.
sent_suppression_keys: set[str] = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_context(scope: str, context_id: str) -> Optional[dict]:
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None


def get_category_for_merchant(merchant: dict) -> Optional[dict]:
    return get_context("category", merchant.get("category_slug", ""))


# ---------------------------------------------------------------------------
# GET /v1/healthz, GET /v1/metadata
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in contexts.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Solo Builder",
        "team_members": ["Vineet"],
        "model": "template-first composer (deterministic), optional Anthropic/OpenAI polish at temperature=0",
        "approach": (
            "4-stage pipeline (resolve_anchor -> build_draft -> llm_polish -> lint_message) dispatched by "
            "trigger-kind family; shared classifier for multi-turn (auto-reply/hostile/intent/off-topic/wait)."
        ),
        "contact_email": "vineetrajput7902@gmail.com",
        "version": "1.0.0",
        "submitted_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# POST /v1/context
# ---------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


VALID_SCOPES = {"category", "merchant", "customer", "trigger"}


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in VALID_SCOPES:
        return {"accepted": False, "reason": "invalid_scope", "details": f"unknown scope '{body.scope}'"}

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}

    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# POST /v1/tick
# ---------------------------------------------------------------------------

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


def _make_conversation_id(merchant_id: str, customer_id: Optional[str], trigger: dict) -> str:
    party = customer_id or merchant_id
    kind = trigger.get("kind", "trigger")
    short_id = (trigger.get("id") or "")[-8:]
    return f"conv_{party}_{kind}_{short_id}"


def _template_name(send_as: str, family: str) -> str:
    return f"{send_as}_{family}_v1"


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions: list[dict] = []
    merchants_started_this_tick: set[str] = set()

    for trigger_id in body.available_triggers:
        if len(actions) >= MAX_ACTIONS_PER_TICK:
            break

        trigger = get_context("trigger", trigger_id)
        if not trigger:
            continue

        merchant_id = trigger.get("merchant_id")
        merchant = get_context("merchant", merchant_id) if merchant_id else None
        if not merchant:
            continue
        category = get_category_for_merchant(merchant)
        if not category:
            continue

        suppression_key = trigger.get("suppression_key") or f"{trigger.get('kind')}:{merchant_id}"
        if suppression_key in sent_suppression_keys:
            continue  # already sent this exact trigger -- don't resend every tick

        # restraint: at most one NEW conversation per merchant per tick
        if merchant_id in merchants_started_this_tick:
            continue

        customer_id = trigger.get("customer_id")
        customer = get_context("customer", customer_id) if customer_id else None

        composed = composer.compose(category, merchant, trigger, customer)

        conversation_id = _make_conversation_id(merchant_id, customer_id, trigger)
        family = composer.family_for(trigger)

        ident = merchant.get("identity", {}) or {}
        recipient_name = (customer.get("identity", {}).get("name") if customer else None) or \
            ident.get("owner_first_name") or ident.get("name") or "there"

        action = {
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trigger_id,
            "template_name": _template_name(composed["send_as"], family),
            "template_params": [recipient_name, composed["body"]],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": suppression_key,
            "rationale": composed["rationale"],
        }
        actions.append(action)

        sent_suppression_keys.add(suppression_key)
        merchants_started_this_tick.add(merchant_id)
        conversations[conversation_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger_id": trigger_id,
            "pending_topic": trigger.get("kind", "").replace("_", " "),
            "merchant_name": recipient_name,
            "cta_default": composed["cta"] if composed["cta"] in ("open_ended", "binary_yes_no") else "open_ended",
            "sent_bodies": [composed["body"]],
            "ended": False,
            "turn_number": 1,
        }
        party_key = (merchant_id, customer_id)
        party_inbound_history.setdefault(party_key, [])

    return {"actions": actions}


# ---------------------------------------------------------------------------
# POST /v1/reply
# ---------------------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: Optional[int] = None


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    convo = conversations.setdefault(body.conversation_id, {
        "merchant_id": body.merchant_id,
        "customer_id": body.customer_id,
        "pending_topic": "",
        "merchant_name": "",
        "cta_default": "open_ended",
        "sent_bodies": [],
        "ended": False,
        "turn_number": 0,
    })

    if convo.get("ended"):
        return {"action": "end", "rationale": "Conversation already closed."}

    merchant_id = body.merchant_id or convo.get("merchant_id")
    customer_id = body.customer_id or convo.get("customer_id")
    party_key = (merchant_id, customer_id)
    history = party_inbound_history.setdefault(party_key, [])
    repeat_count = dialogue.count_prior_occurrences(body.message, history)
    history.append(body.message)

    result = dialogue.decide_action(
        message=body.message,
        repeat_count=repeat_count,
        pending_topic=convo.get("pending_topic", ""),
        merchant_name=convo.get("merchant_name", ""),
        cta_default=convo.get("cta_default", "open_ended"),
    )

    convo["turn_number"] = (body.turn_number or convo.get("turn_number", 0)) + 1

    if result["action"] == "end":
        convo["ended"] = True
    elif result["action"] == "send":
        sent = convo.setdefault("sent_bodies", [])
        candidate_body = result.get("body", "")
        if candidate_body in sent:
            # anti-repetition guard: never resend the exact same body twice
            result = {
                "action": "wait",
                "wait_seconds": 3600,
                "rationale": "Next reply would repeat a prior message verbatim; waiting instead of repeating.",
            }
        else:
            sent.append(candidate_body)

    return result


# ---------------------------------------------------------------------------
# POST /v1/teardown (optional, per testing-brief §11)
# ---------------------------------------------------------------------------

@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    party_inbound_history.clear()
    sent_suppression_keys.clear()
    return {"accepted": True}


if __name__ == "__main__":
    import os as _os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(_os.environ.get("PORT", 8080)))
