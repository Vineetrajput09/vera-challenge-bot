# Vera Challenge Submission

## Approach

The composer is a 4-stage pipeline (`composer.py`), separate from the HTTP
layer (`bot.py`):

1. **`resolve_anchor`** — finds the single most relevant, verifiable fact for
   a trigger. It checks `trigger.payload` first; if that's thin or a
   `{"placeholder": true}` stub, it falls back to `category.digest` (matched
   by the id the trigger references), then `merchant.performance`/`signals`,
   then `customer.relationship`/`state`. Every trigger `kind` is grouped into
   one of six families (knowledge/compliance, performance, local-event,
   merchant-behavior, customer-booking, customer-lapse) so there's one
   drafting function per family instead of per kind (~26 kinds observed
   across the dataset + brief).
2. **`build_draft`** — deterministic, template-driven, grounded by
   construction (every number in the output traces back through `anchor` to
   a real field). This is always computed and is what ships if no LLM key is
   set — it's written to stand on its own, not as an LLM placeholder.
3. **`llm_polish`** (optional) — only runs if `ANTHROPIC_API_KEY` or
   `OPENAI_API_KEY` is set (temperature 0). Told to improve phrasing / do
   Hindi-English code-mixing, explicitly forbidden from adding any fact not
   already in the draft.
4. **`lint_message`** — rejects the polished version (falls back to the safe
   draft) if it introduces a URL, a taboo word from
   `category.voice.vocab_taboo`, a number not present in the draft, or
   repeats a prior `vera`/`merchant_on_behalf` message verbatim.

Multi-turn logic lives in `dialogue.py` as a single stateless classifier
(`decide_action`), imported by both `bot.py`'s `/v1/reply` handler and the
standalone `conversation_handlers.respond()` — so the two can't drift apart.
Priority order: explicit opt-out → repeated-verbatim auto-reply escalation →
hostile de-escalation → intent-commitment → off-topic redirect → explicit
wait request → neutral acknowledgment. Repeat-detection is tracked globally
per `(merchant_id, customer_id)`, not per `conversation_id` — the judge
simulator opens a fresh conversation on every auto-reply call, so
per-conversation tracking would never catch the pattern.

`bot.py` is a thin FastAPI wrapper: in-memory context store keyed by
`(scope, context_id)` with version-based idempotency, a suppression-key set
so `/v1/tick` never resends the same trigger, a per-tick cap of one new
conversation per merchant (restraint beyond the 20-action/tick spec cap),
and conversation state that feeds `dialogue.decide_action`.

## Tradeoffs

- **Templates over prompting.** With ~26 trigger kinds and 5 categories,
  writing one deterministic template per family (not per kind) was the only
  way to keep the logic auditable and guarantee zero fabrication without an
  LLM in the loop. The cost is that some drafts (e.g. a `trial_followup` with
  no real payload) read a little generic — but they never invent a fact.
- **Code-mixing is gated on real signals only.** Hindi-English mixing is used
  when `customer.identity.language_pref` contains "hi" (customer-facing) or
  `merchant.identity.languages` contains "hi" (merchant-facing). I did not
  attempt Telugu/Kannada/Tamil code-mixing (`te-en mix`, `kn-en mix`,
  `ta-en mix` appear in the dataset) because I can't verify translation
  correctness for those — safer to stay in clean English than ship
  plausible-sounding but possibly wrong regional phrasing. Flagged in
  "what would help" below.
- **`llm_polish` is opt-in, not required.** The brief allows "any LLM, any
  strategy," but a submission that only works with a key configured is
  fragile. The deterministic draft is designed to be the real answer, not a
  fallback — I optimized rationale/specificity/anti-fabrication there first.
- **One new conversation per merchant per tick.** Not required by the
  testing brief, but the case studies and brief both reward restraint over
  spam, so `/v1/tick` self-imposes this even though the spec cap is 20
  actions/tick.
- **`appointment_tomorrow` degrades to a generic reminder.** Since this kind
  never carries a real payload in the generated dataset (see Gotchas below)
  and `CustomerContext` has no appointment-date field at all, the draft can
  only reference `customer.preferences.preferred_slots` — it can't cite an
  actual appointment time without inventing one.

## What additional context would help most

1. **An explicit "now" / current-date field in `compose()`'s inputs.** Several
   customer-lapse and booking kinds are naturally time-relative ("it's been
   8 weeks"), but `compose()` must stay deterministic given the same inputs
   — so it can't call `datetime.now()`. Where the trigger payload doesn't
   supply `days_since_last_visit` directly, the draft can only cite the raw
   `last_visit` date, not a computed duration. A `context.as_of` timestamp
   passed alongside the four contexts would let every family compute clean
   relative-time phrasing safely.
2. **Real payloads for `customer_lapsed_soft` and `appointment_tomorrow`.**
   These two kinds are always `{"placeholder": true}` in
   `generate_dataset.py`'s output — every other kind has at least one richly
   populated instance to imitate. An actual appointment-date field on
   `CustomerContext` (or a populated trigger payload) would remove the need
   for a generic fallback on these two.
3. **A verified phrase bank for non-Hindi regional code-mixing.** The dataset
   has `te-en mix`, `kn-en mix`, `ta-en mix`, `mr` as real language
   preferences, but the brief's own guidance and examples only cover
   Hindi-English. A small set of vetted common phrases per language (the way
   `category.voice.tone_examples` works) would let the composer extend
   real code-mixing beyond Hindi without guessing.
4. **A merchant-catalog field for "eligible nearby corporate accounts"-type
   facts.** Case Study 6's building-name list ("Embassy Tech, RMZ Eco, Sigma
   Soft") isn't derivable from any field in `MerchantContext` as specified —
   the brief itself flags this as something the judge will check for
   fabrication. Either that data should live in the context, or the case
   study's bar should be read as "aspirational, not achievable from the
   given schema."

## Gotchas from the original brief — verified against this dataset

All of the listed gotchas held true after running `generate_dataset.py` and
inspecting the output:

- `customer_lapsed_soft` and `appointment_tomorrow` are placeholder-only in
  all 100 generated triggers (5 instances each, all `{"placeholder": true}`).
  Confirmed by grepping the generated trigger files.
- The two briefs do contradict on URLs; this submission follows the
  stricter testing-brief rule (hard-reject any URL, `lint_message` +
  a defensive final check in `compose()`).
- Auto-reply repeat tracking is keyed by `(merchant_id, customer_id)`
  globally, not by `conversation_id` — verified this matters by writing
  `test_bot.py`'s auto-reply scenario to deliberately use a fresh
  `conversation_id` on every turn (matching how `judge_simulator.py`'s
  `_auto_reply` test calls `/v1/reply`).
- `supply_alert`'s "N customers affected" is only emitted when
  `merchant.customer_aggregate.chronic_rx_count` is actually present (e.g.
  Apollo Pharmacy); otherwise the draft asks the merchant to check records
  instead of inventing a count.
- Nothing found to contradict the gotchas list — no changes needed there.

## Files

| File | Purpose |
|---|---|
| `bot.py` | FastAPI server, all 5 endpoints |
| `composer.py` | `compose()` + the 4-stage pipeline |
| `dialogue.py` | Shared multi-turn classifier |
| `conversation_handlers.py` | §7.4 optional `respond(state, merchant_message)` |
| `generate_submission.py` | Builds `submission.jsonl` from `test_pairs.json` |
| `submission.jsonl` | 30 composed messages, one per canonical test pair |
| `test_bot.py` | Local, no-LLM-key test script for all 5 endpoints + multi-turn behaviors |

## Running it

```bash
pip install fastapi uvicorn pydantic requests
python dataset/generate_dataset.py --seed-dir dataset --out dataset_expanded
uvicorn bot:app --host 0.0.0.0 --port 8080   # use another port if 8080 is taken locally
python generate_submission.py
BOT_URL=http://127.0.0.1:8080 python test_bot.py
```

Optional LLM polish: set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` (and
optionally `VERA_LLM_MODEL`) before starting `bot.py` / running
`generate_submission.py`.
