"""
Shared multi-turn conversation classifier.

Used by BOTH the live /v1/reply HTTP endpoint (bot.py) and the offline
conversation_handlers.respond() so the two entry points can't drift apart.

Priority order (per challenge-brief.md open challenges + testing-brief
replay scenarios):

    explicit opt-out
    -> repeated-verbatim-message auto-reply escalation
    -> hostile de-escalation
    -> intent-commitment
    -> off-topic redirect
    -> explicit wait request
    -> neutral acknowledgment
"""
from __future__ import annotations

import re

OPT_OUT_PATTERNS = [
    r"\bstop\b",
    r"\bunsubscribe\b",
    r"do ?n[o']?t (contact|message|text) me",
    r"not interested",
    r"stop messaging",
    r"stop sending",
    r"remove me",
    r"opt.?out",
]

AUTO_REPLY_PATTERNS = [
    r"thank you for (contacting|reaching out)",
    r"our team will (respond|get back|revert)",
    r"this is an automated (reply|response|message)",
    r"i am currently (unavailable|away|out of office)",
    r"we have received your (message|query|enquiry)",
    r"will revert (back )?(to you )?shortly",
    r"automated assistant",
]

HOSTILE_PATTERNS = [
    r"\b(useless|spam|scam|shut up|idiot|stupid|nonsense|bothering|harass(ing)?)\b",
    r"\bfuck\b",
    r"stop bothering",
    r"waste of (my )?time",
    r"why are you (bothering|harassing)",
]

INTENT_PATTERNS = [
    r"let'?s do it",
    r"\bgo ahead\b",
    r"\byes,? let'?s\b",
    r"\bok,? let'?s\b",
    r"what'?s next",
    r"\bconfirm\b",
    r"\bproceed\b",
    r"sign me up",
    r"i want to join",
    r"please (do|go ahead|proceed)",
    r"^\s*yes\b.*\b(next|go ahead|do it)\b",
]

OFF_TOPIC_PATTERNS = [
    r"\bgst\b",
    r"income tax",
    r"\bloan\b",
    r"\binsurance\b",
    r"\bswiggy\b",
    r"\bzomato\b",
    r"personal (number|whatsapp)",
    r"can you also help",
    r"unrelated question",
]

WAIT_PATTERNS = [
    r"call you back",
    r"\bnot now\b",
    r"\blater\b",
    r"busy right now",
    r"give me (some|a) time",
    r"remind me (later|tomorrow)",
    r"will get back to you",
]


def _match_any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def classify(message: str) -> str:
    """Single-label classification of one inbound message (no history)."""
    text = (message or "").strip()
    if not text:
        return "neutral"
    if _match_any(OPT_OUT_PATTERNS, text):
        return "opt_out"
    if _match_any(HOSTILE_PATTERNS, text):
        return "hostile"
    if _match_any(INTENT_PATTERNS, text):
        return "intent_commit"
    if _match_any(OFF_TOPIC_PATTERNS, text):
        return "off_topic"
    if _match_any(WAIT_PATTERNS, text):
        return "wait_request"
    if _match_any(AUTO_REPLY_PATTERNS, text):
        return "auto_reply_like"
    return "neutral"


def count_prior_occurrences(text: str, history: list[str]) -> int:
    """How many times this exact text has already appeared in history."""
    norm = (text or "").strip()
    return sum(1 for h in history if (h or "").strip() == norm)


def decide_action(
    message: str,
    repeat_count: int,
    pending_topic: str = "",
    merchant_name: str = "",
    cta_default: str = "open_ended",
) -> dict:
    """
    Decide the bot's next move for one inbound reply.

    `repeat_count` = how many times this EXACT message text has already
    been received from this merchant/customer, counted globally across
    conversations (not per conversation_id) -- WhatsApp Business auto-replies
    open fresh conversation_ids on some judge scenarios, so per-conversation
    tracking would never detect the pattern.

    Returns one of:
        {"action": "send", "body": ..., "cta": ..., "rationale": ...}
        {"action": "wait", "wait_seconds": ..., "rationale": ...}
        {"action": "end", "rationale": ...}
    """
    label = classify(message)

    if label == "opt_out":
        return {
            "action": "end",
            "rationale": "Merchant explicitly opted out; closing conversation and suppressing further sends.",
        }

    is_repeat_or_canned = label == "auto_reply_like" or repeat_count >= 1
    if is_repeat_or_canned:
        if repeat_count == 0:
            who = merchant_name or "the owner"
            topic_note = f" about {pending_topic}" if pending_topic else ""
            return {
                "action": "send",
                "body": (
                    f"Looks like an auto-reply 😊 When {who} gets a chance, "
                    f"just reply here{topic_note} and I'll pick it back up."
                ),
                "cta": "binary_yes_no",
                "rationale": "Detected merchant auto-reply text; sent one lightweight prompt for the human.",
            }
        elif repeat_count == 1:
            return {
                "action": "wait",
                "wait_seconds": 86400,
                "rationale": "Same auto-reply text twice in a row -- owner likely not at phone. Waiting 24h before retry.",
            }
        else:
            return {
                "action": "end",
                "rationale": "Auto-reply text repeated 3+ times with zero real engagement signal; closing conversation.",
            }

    if label == "hostile":
        return {
            "action": "end",
            "rationale": "Merchant expressed frustration/hostility; exiting gracefully without further engagement.",
        }

    if label == "intent_commit":
        topic_phrase = f" on {pending_topic}" if pending_topic else ""
        return {
            "action": "send",
            "body": f"Great — moving to action{topic_phrase} now. Reply CONFIRM and I'll send it through.",
            "cta": "binary_confirm_cancel",
            "rationale": "Merchant explicitly committed; switching from qualifying to action-execution immediately.",
        }

    if label == "off_topic":
        topic_phrase = f" the {pending_topic} thread" if pending_topic else " what we were discussing"
        return {
            "action": "send",
            "body": (
                "That one's outside what I can help with directly — worth checking with the right "
                f"specialist for that. Coming back to{topic_phrase} — want me to continue?"
            ),
            "cta": "open_ended",
            "rationale": "Out-of-scope ask declined politely; redirected back to the original thread without losing it.",
        }

    if label == "wait_request":
        return {
            "action": "wait",
            "wait_seconds": 1800,
            "rationale": "Merchant asked for time; backing off 30 minutes before re-engaging.",
        }

    topic_phrase = f" on {pending_topic}" if pending_topic else ""
    return {
        "action": "send",
        "body": f"Got it{topic_phrase} — here's the next step. Want me to go ahead?",
        "cta": cta_default,
        "rationale": "Acknowledged merchant's reply and advanced the conversation with a concrete next step.",
    }
