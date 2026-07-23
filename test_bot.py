"""
Dependency-light local test script for bot.py. Exercises all 5 endpoints
plus the key multi-turn behaviors, WITHOUT needing an LLM API key (unlike
judge_simulator.py, which self-tests an LLM connection before running).

Usage:
    python bot.py &                      # or: uvicorn bot:app --port 8091
    BOT_URL=http://127.0.0.1:8091 python test_bot.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib import request as urlrequest, error as urlerror

BOT_URL = os.environ.get("BOT_URL", "http://127.0.0.1:8091").rstrip("/")
DATASET_DIR = Path(__file__).parent / "dataset_expanded"

PASS = []
FAIL = []


def load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def call(method: str, path: str, body: dict | None = None, timeout: int = 15):
    url = f"{BOT_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urlrequest.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        resp = urlrequest.urlopen(req, timeout=timeout)
        return resp.status, json.loads(resp.read().decode("utf-8"))
    except urlerror.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return None, {"_error": str(e)}


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}  {detail}")


URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)


def section(title: str):
    print(f"\n--- {title} ---")


# ---------------------------------------------------------------------------
# 0. Load a slice of real dataset for realistic payloads
# ---------------------------------------------------------------------------

categories = {c["slug"]: c for c in (load(p) for p in (DATASET_DIR / "categories").glob("*.json"))}
dentists = categories["dentists"]
gyms = categories["gyms"]

m001 = load(DATASET_DIR / "merchants" / "m_001_drmeera_dentist_delhi.json")
m008 = load(DATASET_DIR / "merchants" / "m_008_zenyoga_gym_chennai.json")
trg001 = load(DATASET_DIR / "triggers" / "trg_001_research_digest_dentists.json")  # research_digest
trg024 = load(DATASET_DIR / "triggers" / "trg_024_perf_spike_zen.json")  # unrelated trigger, merchant m_008


def main():
    print(f"Testing bot at {BOT_URL}\n")

    # -----------------------------------------------------------------
    section("healthz / metadata shape (pre-warmup)")
    status, hz = call("GET", "/v1/healthz")
    check("healthz returns 200", status == 200, f"got {status}: {hz}")
    check("healthz has status=ok", hz.get("status") == "ok")
    check("healthz has contexts_loaded dict", isinstance(hz.get("contexts_loaded"), dict))

    status, md = call("GET", "/v1/metadata")
    check("metadata returns 200", status == 200, f"got {status}")
    for field in ("team_name", "model", "approach", "contact_email", "version"):
        check(f"metadata has '{field}'", field in md, f"metadata={md}")

    # -----------------------------------------------------------------
    section("context push: idempotency + version replace + invalid scope")
    status, r = call("POST", "/v1/context", {
        "scope": "category", "context_id": "dentists", "version": 1, "payload": dentists,
        "delivered_at": "2026-04-26T09:45:00Z",
    })
    check("first push v1 accepted (200)", status == 200 and r.get("accepted") is True, f"{status} {r}")

    status, r = call("POST", "/v1/context", {
        "scope": "category", "context_id": "dentists", "version": 1, "payload": dentists,
        "delivered_at": "2026-04-26T09:45:00Z",
    })
    check("re-push same version -> stale_version", r.get("accepted") is False and r.get("reason") == "stale_version", f"{status} {r}")

    dentists_v2 = dict(dentists)
    status, r = call("POST", "/v1/context", {
        "scope": "category", "context_id": "dentists", "version": 2, "payload": dentists_v2,
        "delivered_at": "2026-04-26T10:00:00Z",
    })
    check("higher version replaces (200 accepted)", status == 200 and r.get("accepted") is True, f"{status} {r}")

    status, r = call("POST", "/v1/context", {
        "scope": "not_a_real_scope", "context_id": "x", "version": 1, "payload": {},
        "delivered_at": "2026-04-26T10:00:00Z",
    })
    check("invalid scope rejected", r.get("accepted") is False and r.get("reason") == "invalid_scope", f"{status} {r}")

    # push everything tick/reply tests need
    call("POST", "/v1/context", {"scope": "category", "context_id": "gyms", "version": 1, "payload": gyms, "delivered_at": "2026-04-26T10:00:00Z"})
    call("POST", "/v1/context", {"scope": "merchant", "context_id": "m_001_drmeera_dentist_delhi", "version": 1, "payload": m001, "delivered_at": "2026-04-26T10:00:00Z"})
    call("POST", "/v1/context", {"scope": "merchant", "context_id": "m_008_zenyoga_gym_chennai", "version": 1, "payload": m008, "delivered_at": "2026-04-26T10:00:00Z"})
    call("POST", "/v1/context", {"scope": "trigger", "context_id": trg001["id"], "version": 1, "payload": trg001, "delivered_at": "2026-04-26T10:00:00Z"})
    call("POST", "/v1/context", {"scope": "trigger", "context_id": trg024["id"], "version": 1, "payload": trg024, "delivered_at": "2026-04-26T10:00:00Z"})

    status, hz = call("GET", "/v1/healthz")
    loaded = hz.get("contexts_loaded", {})
    check("healthz reflects pushed contexts", loaded.get("category", 0) >= 2 and loaded.get("merchant", 0) >= 2 and loaded.get("trigger", 0) >= 2, f"{loaded}")

    # -----------------------------------------------------------------
    section("tick: well-formed action + suppression on repeat")
    status, r = call("POST", "/v1/tick", {"now": "2026-04-26T10:35:00Z", "available_triggers": [trg001["id"]]})
    actions = r.get("actions", [])
    check("tick returns 200", status == 200, f"{status} {r}")
    check("tick produced exactly one action for one trigger", len(actions) == 1, f"actions={actions}")

    action = actions[0] if actions else {}
    required_fields = ["conversation_id", "merchant_id", "send_as", "trigger_id", "cta", "suppression_key", "rationale", "body"]
    for field in required_fields:
        check(f"action has '{field}'", field in action and action[field] not in (None, ""), f"action={action}")
    check("body is non-empty", bool(action.get("body", "").strip()))
    check("body contains no URL", not URL_RE.search(action.get("body", "")), f"body={action.get('body')}")
    check("send_as is 'vera' (merchant-facing, no customer)", action.get("send_as") == "vera")

    conv_id = action.get("conversation_id")

    status, r2 = call("POST", "/v1/tick", {"now": "2026-04-26T10:40:00Z", "available_triggers": [trg001["id"]]})
    actions2 = r2.get("actions", [])
    check("repeat tick on same trigger is suppressed", len(actions2) == 0, f"actions2={actions2}")

    # a different trigger for a different merchant should still fire
    status, r3 = call("POST", "/v1/tick", {"now": "2026-04-26T10:45:00Z", "available_triggers": [trg024["id"]]})
    actions3 = r3.get("actions", [])
    check("tick for a fresh trigger/merchant still produces an action", len(actions3) == 1, f"actions3={actions3}")

    # -----------------------------------------------------------------
    section("reply: engaged follow-up")
    status, r = call("POST", "/v1/reply", {
        "conversation_id": conv_id, "merchant_id": m001["merchant_id"], "customer_id": None,
        "from_role": "merchant", "message": "Yes please send the abstract, also draft the patient WhatsApp.",
        "received_at": "2026-04-26T10:42:00Z", "turn_number": 2,
    })
    check("engaged reply returns 200", status == 200, f"{status} {r}")
    check("engaged reply action is 'send'", r.get("action") == "send", f"{r}")
    check("engaged reply body non-empty", bool(r.get("body", "").strip()), f"{r}")

    # -----------------------------------------------------------------
    section("reply: 4x identical canned auto-reply in a row (fresh conv_id each time, per-party tracking)")
    auto_text = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
    seen_actions = []
    for i in range(4):
        this_conv = f"conv_auto_test_{i}"  # deliberately a NEW conversation_id each turn
        status, r = call("POST", "/v1/reply", {
            "conversation_id": this_conv, "merchant_id": m001["merchant_id"], "customer_id": None,
            "from_role": "merchant", "message": auto_text,
            "received_at": "2026-04-26T10:50:00Z", "turn_number": i + 1,
        })
        seen_actions.append(r.get("action"))
    check("bot does not just keep sending on all 4 auto-reply turns", any(a in ("wait", "end") for a in seen_actions),
          f"actions were {seen_actions}")
    check("bot ends or waits by (at latest) the 4th identical auto-reply", seen_actions[3] in ("wait", "end"),
          f"actions were {seen_actions}")

    # -----------------------------------------------------------------
    section("reply: explicit opt-out")
    status, r = call("POST", "/v1/reply", {
        "conversation_id": "conv_optout_test", "merchant_id": m001["merchant_id"], "customer_id": None,
        "from_role": "merchant", "message": "Not interested. Stop messaging me.",
        "received_at": "2026-04-26T10:55:00Z", "turn_number": 2,
    })
    check("opt-out returns action 'end'", r.get("action") == "end", f"{r}")

    # -----------------------------------------------------------------
    section("reply: explicit intent transition (should act, not re-qualify)")
    status, r = call("POST", "/v1/reply", {
        "conversation_id": "conv_intent_test", "merchant_id": m001["merchant_id"], "customer_id": None,
        "from_role": "merchant", "message": "Ok let's do it, what's next?",
        "received_at": "2026-04-26T11:00:00Z", "turn_number": 2,
    })
    body_lower = r.get("body", "").lower()
    qualifying_phrases = ["would you", "do you", "can you tell", "what if", "how about"]
    check("intent-transition reply is action-oriented, not another qualifying question",
          r.get("action") == "send" and not any(p in body_lower for p in qualifying_phrases), f"{r}")

    # -----------------------------------------------------------------
    section("reply: off-topic curveball (should decline + redirect, not end/go silent)")
    status, r = call("POST", "/v1/reply", {
        "conversation_id": "conv_offtopic_test", "merchant_id": m001["merchant_id"], "customer_id": None,
        "from_role": "merchant", "message": "Btw can you also help me with my GST filing this month?",
        "received_at": "2026-04-26T11:05:00Z", "turn_number": 2,
    })
    check("off-topic curveball gets a 'send' (not end/silent)", r.get("action") == "send", f"{r}")
    check("off-topic reply body non-empty", bool(r.get("body", "").strip()), f"{r}")

    # -----------------------------------------------------------------
    section("teardown wipes state")
    status, r = call("POST", "/v1/teardown")
    check("teardown returns 200 accepted", status == 200 and r.get("accepted") is True, f"{status} {r}")
    status, hz = call("GET", "/v1/healthz")
    loaded = hz.get("contexts_loaded", {})
    check("healthz shows 0 contexts after teardown", all(v == 0 for v in loaded.values()), f"{loaded}")

    # -----------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"RESULTS: {len(PASS)} passed, {len(FAIL)} failed (of {len(PASS)+len(FAIL)})")
    if FAIL:
        print("\nFailed checks:")
        for name in FAIL:
            print(f"  - {name}")
    print("=" * 60)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
