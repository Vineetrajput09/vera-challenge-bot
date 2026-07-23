"""
The composition pipeline: resolve_anchor -> build_draft -> llm_polish -> lint_message.

compose() is the public entrypoint per challenge-brief.md §7.1:

    def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None) -> dict

Design notes (see README.md for the full rationale):

- build_draft() is deterministic and template-driven. It is ALWAYS computed
  and is what ships if no LLM is configured -- it's written to stand on its
  own, not as an LLM placeholder.
- llm_polish() is optional (Anthropic/OpenAI, temperature 0) and may only
  improve phrasing/code-mixing. It is never allowed to add a fact that
  wasn't already in the draft -- lint_message() enforces that and falls
  back to the safe draft on any violation.
- Every trigger `kind` is grouped into one of six families so we write one
  drafting function per family instead of per kind. Two kinds
  (`customer_lapsed_soft`, `appointment_tomorrow`) never carry a real
  payload anywhere in the generated dataset -- every drafting path has a
  non-placeholder fallback that derives from merchant/customer state
  instead of trusting trigger.payload blindly.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Trigger kind -> family grouping
# ---------------------------------------------------------------------------

FAMILY_BY_KIND = {
    # knowledge / compliance -- external research, regulation, trend, alert
    "research_digest": "knowledge_compliance",
    "category_research_digest_release": "knowledge_compliance",
    "regulation_change": "knowledge_compliance",
    "cde_opportunity": "knowledge_compliance",
    "category_seasonal": "knowledge_compliance",
    "category_trend_movement": "knowledge_compliance",
    "supply_alert": "knowledge_compliance",
    # performance -- the merchant's own numbers moved
    "perf_dip": "performance",
    "perf_spike": "performance",
    "seasonal_perf_dip": "performance",
    "milestone_reached": "performance",
    "review_theme_emerged": "performance",
    "gbp_unverified": "performance",
    # local / competitive events
    "festival_upcoming": "local_event",
    "ipl_match_today": "local_event",
    "competitor_opened": "local_event",
    "weather_heatwave": "local_event",
    "local_news_event": "local_event",
    # merchant behavior -- merchant's relationship with Vera / subscription
    "dormant_with_vera": "merchant_behavior",
    "curious_ask_due": "merchant_behavior",
    "active_planning_intent": "merchant_behavior",
    "renewal_due": "merchant_behavior",
    "winback_eligible": "merchant_behavior",
    "scheduled_recurring": "merchant_behavior",
    # customer booking -- recall/appointment/trial/refill flows
    "recall_due": "customer_booking",
    "appointment_tomorrow": "customer_booking",
    "trial_followup": "customer_booking",
    "wedding_package_followup": "customer_booking",
    "chronic_refill_due": "customer_booking",
    # customer lapse -- winback framing
    "customer_lapsed_soft": "customer_lapse",
    "customer_lapsed_hard": "customer_lapse",
}

FALLBACK_FAMILY_BY_SCOPE = {"merchant": "merchant_behavior", "customer": "customer_booking"}


def family_for(trigger: dict) -> str:
    kind = trigger.get("kind", "")
    if kind in FAMILY_BY_KIND:
        return FAMILY_BY_KIND[kind]
    return FALLBACK_FAMILY_BY_SCOPE.get(trigger.get("scope", "merchant"), "merchant_behavior")


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _identity(merchant: dict) -> dict:
    return merchant.get("identity", {}) or {}


def owner_salutation(category: dict, merchant: dict) -> str:
    ident = _identity(merchant)
    first = ident.get("owner_first_name") or ident.get("name") or "there"
    if category.get("slug") == "dentists" and not str(first).startswith("Dr."):
        return f"Dr. {first}"
    return first


def merchant_uses_hindi(merchant: dict) -> bool:
    return "hi" in (_identity(merchant).get("languages") or [])


def customer_uses_hindi(customer: Optional[dict]) -> bool:
    if not customer:
        return False
    lp = ((customer.get("identity", {}) or {}).get("language_pref") or "").lower()
    return lp == "hi" or lp.startswith("hi-en") or "hi-en" in lp


def active_offers(merchant: dict) -> list[dict]:
    return [o for o in merchant.get("offers", []) or [] if o.get("status") == "active"]


def is_placeholder(payload: Optional[dict]) -> bool:
    return not payload or payload.get("placeholder") is True


def digest_item_by_id(category: dict, item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    for item in category.get("digest", []) or []:
        if item.get("id") == item_id:
            return item
    return None


def digest_items_by_kind(category: dict, kind: str) -> list[dict]:
    return [d for d in category.get("digest", []) or [] if d.get("kind") == kind]


def fmt_pct(x) -> Optional[str]:
    if x is None:
        return None
    try:
        return f"{abs(float(x)) * 100:.0f}%"
    except (TypeError, ValueError):
        return None


def direction_word(x, up_word="up", down_word="down") -> str:
    try:
        return up_word if float(x) >= 0 else down_word
    except (TypeError, ValueError):
        return up_word


_METRIC_ALIASES = {"review_count": "reviews", "views": "views", "calls": "calls", "directions": "directions"}


def humanize_metric(raw: Optional[str]) -> str:
    if not raw:
        return "numbers"
    return _METRIC_ALIASES.get(raw, str(raw).replace("_", " "))


def humanize_snake(raw) -> str:
    """'summer_2026' -> 'Summer 2026'; generic snake_case -> space-joined."""
    if raw is None:
        return ""
    return str(raw).replace("_", " ").strip()


def humanize_trend_item(raw: str) -> str:
    """'ORS_demand_+40' -> 'ORS demand +40%'; falls back to a plain space-join."""
    m = re.match(r"^(.*)_([+-]?\d+)$", str(raw))
    if m:
        return f"{m.group(1).replace('_', ' ')} {m.group(2)}%"
    return str(raw).replace("_", " ")


def fmt_datetime_human(iso_str: Optional[str]) -> Optional[str]:
    """'2026-05-02T19:00:00+05:30' -> '2 May 2026, 7:00 PM'. Falls back to
    the raw string if it doesn't parse."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_str))
        time_part = dt.strftime("%I:%M %p").lstrip("0")
        return f"{dt.day} {dt.strftime('%b %Y')}, {time_part}"
    except (ValueError, TypeError):
        return str(iso_str)


def fmt_date_human(iso_str: Optional[str]) -> Optional[str]:
    """'2026-04-28T00:00:00+05:30' / '2026-04-28' -> '28 Apr 2026'. Falls
    back to the raw string if it doesn't parse -- never fabricates, never
    crashes."""
    if not iso_str:
        return None
    try:
        date_part = str(iso_str).split("T")[0]
        d = datetime.strptime(date_part, "%Y-%m-%d")
        return f"{d.day} {d.strftime('%b %Y')}"
    except (ValueError, TypeError):
        return str(iso_str)


def fmt_service_name(raw: Optional[str]) -> str:
    """'6_month_cleaning' -> '6-month cleaning recall'; generic snake_case
    falls back to a plain space-joined phrase."""
    if not raw:
        return "recall"
    parts = str(raw).split("_")
    if len(parts) >= 3 and parts[1] == "month":
        return f"{parts[0]}-month {' '.join(parts[2:])} recall"
    return " ".join(parts)


def customer_display_name(customer: dict) -> str:
    name = (customer.get("identity", {}) or {}).get("name") or "there"
    # strip "(parent: X)" annotations for the greeting itself
    return name.split(" (")[0].strip() or name


# ---------------------------------------------------------------------------
# Stage 1 -- resolve_anchor: find the single most relevant, verifiable fact
# ---------------------------------------------------------------------------

def resolve_anchor(category: dict, merchant: dict, trigger: dict, customer: Optional[dict]) -> dict:
    """Extract a facts dict for this trigger. Checks trigger.payload first;
    falls back to category.digest, then merchant.performance/signals, then
    customer.relationship/state when the payload is thin or a placeholder.
    Every value here traces to a real field -- nothing is invented.
    """
    kind = trigger.get("kind", "")
    family = family_for(trigger)
    payload = trigger.get("payload") or {}
    facts: dict = {"kind": kind, "family": family}

    if family == "knowledge_compliance":
        facts.update(_anchor_knowledge_compliance(category, merchant, kind, payload))
    elif family == "performance":
        facts.update(_anchor_performance(category, merchant, kind, payload))
    elif family == "local_event":
        facts.update(_anchor_local_event(merchant, kind, payload))
    elif family == "merchant_behavior":
        facts.update(_anchor_merchant_behavior(merchant, kind, payload))
    elif family == "customer_booking":
        facts.update(_anchor_customer_booking(merchant, customer, kind, payload))
    elif family == "customer_lapse":
        facts.update(_anchor_customer_lapse(merchant, customer, kind, payload))

    return facts


def _anchor_knowledge_compliance(category: dict, merchant: dict, kind: str, payload: dict) -> dict:
    out: dict = {}
    if kind in ("research_digest", "category_research_digest_release"):
        item = digest_item_by_id(category, payload.get("top_item_id"))
        if not item:
            candidates = digest_items_by_kind(category, "research")
            item = candidates[0] if candidates else None
        if item:
            out.update(
                title=item.get("title"), source=item.get("source"),
                trial_n=item.get("trial_n"), segment=item.get("patient_segment"),
                summary=item.get("summary"), actionable=item.get("actionable"),
            )
    elif kind == "regulation_change":
        item = digest_item_by_id(category, payload.get("top_item_id"))
        if not item:
            candidates = digest_items_by_kind(category, "compliance")
            item = candidates[0] if candidates else None
        if item:
            out.update(title=item.get("title"), source=item.get("source"), summary=item.get("summary"),
                       actionable=item.get("actionable"))
        out["deadline"] = payload.get("deadline_iso")
    elif kind == "cde_opportunity":
        item = digest_item_by_id(category, payload.get("digest_item_id"))
        if not item:
            candidates = digest_items_by_kind(category, "cde")
            item = candidates[0] if candidates else None
        if item:
            out.update(title=item.get("title"), source=item.get("source"), date=item.get("date"),
                       summary=item.get("summary"))
        out.update(credits=payload.get("credits"), fee=payload.get("fee"))
    elif kind == "category_seasonal":
        out.update(season=payload.get("season"), trends=payload.get("trends"),
                   shelf_action=payload.get("shelf_action_recommended"))
    elif kind == "category_trend_movement":
        out.update(query=payload.get("query"), delta_yoy=payload.get("delta_yoy"))
        if not out.get("query") and category.get("trend_signals"):
            top = category["trend_signals"][0]
            out.update(query=top.get("query"), delta_yoy=top.get("delta_yoy"), segment_age=top.get("segment_age"))
    elif kind == "supply_alert":
        out.update(molecule=payload.get("molecule"), batches=payload.get("affected_batches"),
                   manufacturer=payload.get("manufacturer"))
        if not out.get("molecule") and payload.get("alert_id"):
            item = digest_item_by_id(category, payload.get("alert_id"))
            if item:
                out.update(title=item.get("title"), source=item.get("source"))
        chronic = (merchant.get("customer_aggregate", {}) or {}).get("chronic_rx_count")
        if chronic:
            out["chronic_rx_count"] = chronic
    if not out and category.get("digest"):
        item = category["digest"][0]
        out.update(title=item.get("title"), source=item.get("source"), summary=item.get("summary"))
    return out


def _anchor_performance(category: dict, merchant: dict, kind: str, payload: dict) -> dict:
    out: dict = {}
    perf = merchant.get("performance", {}) or {}
    peer = category.get("peer_stats", {}) or {}
    if kind in ("perf_dip", "perf_spike"):
        metric = payload.get("metric")
        delta = payload.get("delta_pct")
        baseline = payload.get("vs_baseline")
        if metric is None:
            # placeholder fallback: derive straight from performance.delta_7d
            delta7 = perf.get("delta_7d", {}) or {}
            if delta7.get("views_pct") is not None:
                metric, delta = "views", delta7.get("views_pct")
            elif delta7.get("calls_pct") is not None:
                metric, delta = "calls", delta7.get("calls_pct")
        out.update(metric=metric, delta_pct=delta, baseline=baseline)
    elif kind == "seasonal_perf_dip":
        out.update(metric=payload.get("metric"), delta_pct=payload.get("delta_pct"),
                   season_note=payload.get("season_note"), is_seasonal=payload.get("is_expected_seasonal"))
        if out.get("metric") is None:
            delta7 = perf.get("delta_7d", {}) or {}
            out.update(metric="views", delta_pct=delta7.get("views_pct"))
        agg = merchant.get("customer_aggregate", {}) or {}
        out["member_count"] = agg.get("total_active_members")
    elif kind == "milestone_reached":
        out.update(metric=payload.get("metric"), value_now=payload.get("value_now"),
                   milestone_value=payload.get("milestone_value"), imminent=payload.get("is_imminent"))
    elif kind == "review_theme_emerged":
        out.update(theme=payload.get("theme"), occurrences=payload.get("occurrences_30d"),
                   trend=payload.get("trend"), quote=payload.get("common_quote"))
        if out.get("theme") is None and merchant.get("review_themes"):
            rt = merchant["review_themes"][0]
            out.update(theme=rt.get("theme"), occurrences=rt.get("occurrences_30d"), quote=rt.get("common_quote"))
    elif kind == "gbp_unverified":
        out.update(verified=payload.get("verified", False), path=payload.get("verification_path"),
                   uplift_pct=payload.get("estimated_uplift_pct"))
    out.setdefault("ctr", perf.get("ctr"))
    out.setdefault("peer_ctr", peer.get("avg_ctr"))
    out.setdefault("views", perf.get("views"))
    return out


def _anchor_local_event(merchant: dict, kind: str, payload: dict) -> dict:
    out: dict = {}
    ident = _identity(merchant)
    if kind == "festival_upcoming":
        out.update(festival=payload.get("festival"), date=payload.get("date"),
                   days_until=payload.get("days_until"))
    elif kind == "ipl_match_today":
        out.update(match=payload.get("match"), venue=payload.get("venue"),
                   match_time=payload.get("match_time_iso"), is_weeknight=payload.get("is_weeknight"))
    elif kind == "competitor_opened":
        out.update(competitor_name=payload.get("competitor_name"), distance_km=payload.get("distance_km"),
                   their_offer=payload.get("their_offer"))
    elif kind == "weather_heatwave":
        out.update(temp_c=payload.get("temp_c"), city=payload.get("city", ident.get("city")))
    elif kind == "local_news_event":
        out.update(headline=payload.get("headline"), duration=payload.get("duration"))
    out.setdefault("locality", ident.get("locality"))
    out.setdefault("city", ident.get("city"))
    return out


def _anchor_merchant_behavior(merchant: dict, kind: str, payload: dict) -> dict:
    out: dict = {}
    if kind == "dormant_with_vera":
        out.update(days_since=payload.get("days_since_last_merchant_message"), last_topic=payload.get("last_topic"))
    elif kind == "curious_ask_due":
        out.update(ask_template=payload.get("ask_template"))
    elif kind == "active_planning_intent":
        out.update(topic=payload.get("intent_topic"), last_message=payload.get("merchant_last_message"))
    elif kind == "renewal_due":
        out.update(days_remaining=payload.get("days_remaining"), plan=payload.get("plan"),
                   amount=payload.get("renewal_amount"))
        sub = merchant.get("subscription", {}) or {}
        out.setdefault("days_remaining", sub.get("days_remaining"))
        out.setdefault("plan", sub.get("plan"))
    elif kind == "winback_eligible":
        out.update(days_since_expiry=payload.get("days_since_expiry"), perf_dip_pct=payload.get("perf_dip_pct"),
                   lapsed_customers=payload.get("lapsed_customers_added_since_expiry"))
    elif kind == "scheduled_recurring":
        out.update(cadence_topic=payload.get("topic"))
    out.setdefault("signals", merchant.get("signals", []))
    return out


def _anchor_customer_booking(merchant: dict, customer: Optional[dict], kind: str, payload: dict) -> dict:
    out: dict = {}
    cust_prefs = (customer or {}).get("preferences", {}) or {}
    cust_rel = (customer or {}).get("relationship", {}) or {}
    offers = active_offers(merchant)

    if kind == "recall_due":
        out.update(service_due=payload.get("service_due"), last_service_date=payload.get("last_service_date"),
                   due_date=payload.get("due_date"), slots=payload.get("available_slots"))
    elif kind == "appointment_tomorrow":
        # always a placeholder in this dataset -- fall back to relationship/prefs
        out.update(preferred_slot=cust_prefs.get("preferred_slots"))
    elif kind == "trial_followup":
        out.update(trial_date=payload.get("trial_date"), next_options=payload.get("next_session_options"))
        if not out.get("trial_date") and cust_rel.get("visits_total") == 1:
            out["trial_date"] = cust_rel.get("first_visit")
    elif kind == "wedding_package_followup":
        out.update(wedding_date=payload.get("wedding_date"), trial_completed=payload.get("trial_completed"),
                   days_to_wedding=payload.get("days_to_wedding"), next_window=payload.get("next_step_window_open"))
        if not out.get("wedding_date"):
            out["wedding_date"] = cust_prefs.get("wedding_date")
    elif kind == "chronic_refill_due":
        out.update(molecules=payload.get("molecule_list"), last_refill=payload.get("last_refill"),
                   stock_runs_out=payload.get("stock_runs_out_iso"),
                   delivery_saved=payload.get("delivery_address_saved"))
        if not out.get("molecules"):
            chronic_services = [s for s in cust_rel.get("services_received", []) if "chronic_rx" in str(s)]
            out["molecules"] = chronic_services or None

    out["active_offers"] = offers
    out["preferred_slot"] = out.get("preferred_slot") or cust_prefs.get("preferred_slots")
    return out


def _anchor_customer_lapse(merchant: dict, customer: Optional[dict], kind: str, payload: dict) -> dict:
    out: dict = {}
    cust_rel = (customer or {}).get("relationship", {}) or {}
    cust_state = (customer or {}).get("state")
    out.update(days_since=payload.get("days_since_last_visit"), previous_focus=payload.get("previous_focus"),
               previous_months=payload.get("previous_membership_months"))
    # both customer_lapsed_soft and appointment_tomorrow are placeholder-only
    # kinds in this dataset -- always have a relationship-derived fallback.
    out.setdefault("last_visit", cust_rel.get("last_visit"))
    out["state"] = cust_state
    out["active_offers"] = active_offers(merchant)
    return out


# ---------------------------------------------------------------------------
# Stage 2 -- build_draft: deterministic, template-driven, always computed
# ---------------------------------------------------------------------------

def build_draft(category: dict, merchant: dict, trigger: dict, customer: Optional[dict], anchor: dict) -> dict:
    family = anchor["family"]
    builder = {
        "knowledge_compliance": _draft_knowledge_compliance,
        "performance": _draft_performance,
        "local_event": _draft_local_event,
        "merchant_behavior": _draft_merchant_behavior,
        "customer_booking": _draft_customer_booking,
        "customer_lapse": _draft_customer_lapse,
    }[family]
    body, cta = builder(category, merchant, trigger, customer, anchor)

    send_as = "merchant_on_behalf" if customer else "vera"
    suppression_key = trigger.get("suppression_key") or (
        f"{trigger.get('kind','trigger')}:{trigger.get('merchant_id','')}:{trigger.get('customer_id','') or ''}"
    )
    rationale = _rationale_for(family, anchor, customer)

    return {
        "body": body.strip(),
        "cta": cta,
        "send_as": send_as,
        "suppression_key": suppression_key,
        "rationale": rationale,
    }


def _rationale_for(family: str, anchor: dict, customer: Optional[dict]) -> str:
    kind = anchor.get("kind", "trigger")
    scope_note = "customer-facing" if customer else "merchant-facing"
    return (
        f"{family.replace('_', ' ')} trigger ({kind}), {scope_note}; anchored on a verifiable fact "
        f"from the pushed contexts, single low-friction next step as the CTA."
    )


def _draft_knowledge_compliance(category, merchant, trigger, customer, anchor) -> tuple[str, str]:
    who = owner_salutation(category, merchant)
    kind = anchor["kind"]

    if kind == "supply_alert":
        molecule = anchor.get("molecule") or (anchor.get("title") or "a recalled product")
        batches = anchor.get("batches") or []
        batch_note = f" (batches {', '.join(batches)})" if batches else ""
        mfr = f" by {anchor['manufacturer']}" if anchor.get("manufacturer") else ""
        count = anchor.get("chronic_rx_count")
        count_note = f" Pulled your repeat-Rx list: {count} of your chronic-Rx customers may be affected." if count else \
            " Worth checking your repeat-Rx records for this molecule."
        body = (
            f"{who}, urgent: voluntary recall on {molecule}{batch_note}{mfr}.{count_note} "
            f"Want me to draft the customer notice + a replacement-pickup workflow?"
        )
        return body, "binary_yes_no"

    if kind == "regulation_change":
        title = anchor.get("title") or "a new regulation affecting your category"
        source = f" — {anchor['source']}" if anchor.get("source") else ""
        deadline_human = fmt_date_human(anchor.get("deadline"))
        deadline = f" Deadline: {deadline_human}." if deadline_human else ""
        actionable = f" {anchor['actionable']}" if anchor.get("actionable") else ""
        body = f"{who}, heads up: {title}{source}.{deadline}{actionable} Want me to draft a compliance checklist?"
        return body, "open_ended"

    if kind == "cde_opportunity":
        title = anchor.get("title") or "a relevant CDE session"
        date_human = fmt_datetime_human(anchor.get("date"))
        date = f" on {date_human}" if date_human else ""
        fee_note = f", {humanize_snake(anchor['fee'])}" if anchor.get("fee") else ""
        credits = f" ({anchor['credits']} credits{fee_note})" if anchor.get("credits") else ""
        body = f"{who}, {title}{date}{credits}. Want me to add it to your calendar and remind you a day before?"
        return body, "binary_yes_no"

    if kind == "category_seasonal":
        trends = anchor.get("trends") or []
        trend_note = "; ".join(humanize_trend_item(t) for t in trends[:3]) if trends else "a seasonal demand shift in your category"
        season = humanize_snake(anchor.get("season")).title() if anchor.get("season") else "this period"
        body = (
            f"{who}, seasonal shift flagged for {season}: {trend_note}. "
            f"Want me to suggest a shelf/stock adjustment based on this?"
        )
        return body, "open_ended"

    if kind == "category_trend_movement":
        query = anchor.get("query")
        delta = fmt_pct(anchor.get("delta_yoy"))
        if query and delta:
            body = (
                f"{who}, '{query}' searches are up {delta} YoY"
                + (f" in the {anchor['segment_age']} band" if anchor.get("segment_age") else "")
                + ". Want me to position your listing against this trend?"
            )
        else:
            body = f"{who}, a demand trend shift showed up for your category this week. Want me to pull the details?"
        return body, "open_ended"

    # research_digest / category_research_digest_release / generic fallback
    title = anchor.get("title")
    source = anchor.get("source")
    if title:
        trial_note = f" {anchor['trial_n']}-patient trial" if anchor.get("trial_n") else " Recent item"
        segment_note = f" relevant to your {str(anchor['segment']).replace('_', ' ')} cohort" if anchor.get("segment") else ""
        source_note = f" — {source}" if source else ""
        body = (
            f"{who}, latest research digest landed. One item{segment_note}:{trial_note} shows {title.lower() if title[0].isupper() else title}. "
            f"Worth a look. Want me to pull it + draft a patient-ed WhatsApp you can share?{source_note}"
        )
    else:
        body = f"{who}, this week's category research digest has a couple of items worth a look. Want me to send the summary?"
    return body, "open_ended"


def _draft_performance(category, merchant, trigger, customer, anchor) -> tuple[str, str]:
    who = owner_salutation(category, merchant)
    kind = anchor["kind"]

    if kind in ("perf_dip", "perf_spike"):
        metric = anchor.get("metric") or "views"
        pct = fmt_pct(anchor.get("delta_pct"))
        direction = direction_word(anchor.get("delta_pct"))
        baseline_note = f" (vs your usual ~{anchor['baseline']}/day)" if anchor.get("baseline") else ""
        if pct:
            body = (
                f"{who}, your {metric} are {direction} {pct} this week{baseline_note}. "
                f"Want me to pull the likely driver and suggest a next step?"
            )
        else:
            body = f"{who}, noticed a shift in your {metric} this week. Want me to break down what moved?"
        return body, "open_ended"

    if kind == "seasonal_perf_dip":
        metric = anchor.get("metric") or "views"
        pct = fmt_pct(anchor.get("delta_pct"))
        note = anchor.get("season_note", "").replace("_", " ")
        members = anchor.get("member_count")
        member_note = f" For now, focus retention on your {members} members." if members else ""
        if pct:
            body = (
                f"{who}, your {metric} are down {pct} this week — this lines up with the expected "
                f"{note or 'seasonal'} dip, not a real problem.{member_note} Want me to draft a retention push "
                f"to ride out the dip?"
            )
        else:
            body = f"{who}, this looks like the expected seasonal dip, not a real problem.{member_note} Want me to draft a retention push?"
        return body, "open_ended"

    if kind == "milestone_reached":
        metric = humanize_metric(anchor.get("metric") or "review_count")
        now, target = anchor.get("value_now"), anchor.get("milestone_value")
        if now is not None and target is not None:
            remaining = max(0, target - now)
            body = (
                f"{who}, you're at {now} {metric} — {remaining} away from {target}. "
                f"Want me to draft a 'help us hit {target}' review-ask post?"
            )
        else:
            body = f"{who}, you're closing in on a {metric} milestone. Want me to draft a review-ask post to help close the gap?"
        return body, "open_ended"

    if kind == "review_theme_emerged":
        theme = (anchor.get("theme") or "a recurring theme").replace("_", " ")
        occ = anchor.get("occurrences")
        quote = anchor.get("quote")
        occ_note = f"{occ} reviews this month mention it" if occ else "a few recent reviews mention it"
        quote_note = f" (one reads: \"{quote}\")" if quote else ""
        body = f"{who}, {occ_note} — {theme}{quote_note}. Want me to draft a response template + an internal fix checklist?"
        return body, "open_ended"

    if kind == "gbp_unverified":
        uplift = fmt_pct(anchor.get("uplift_pct"))
        uplift_note = f", typically worth ~{uplift} more views once verified" if uplift else ""
        body = (
            f"{who}, your Google profile isn't verified yet{uplift_note}. "
            f"Want me to walk you through the postcard/phone verification path?"
        )
        return body, "binary_yes_no"

    ctr = anchor.get("ctr")
    peer_ctr = anchor.get("peer_ctr")
    if ctr is not None and peer_ctr is not None:
        body = (
            f"{who}, your CTR is {ctr*100:.1f}% vs a peer average of {peer_ctr*100:.1f}%. "
            f"Want me to look at what's driving the gap?"
        )
    else:
        body = f"{who}, a performance shift showed up on your dashboard this week. Want me to break it down?"
    return body, "open_ended"


def _draft_local_event(category, merchant, trigger, customer, anchor) -> tuple[str, str]:
    who = owner_salutation(category, merchant)
    kind = anchor["kind"]

    if kind == "festival_upcoming":
        festival = anchor.get("festival") or "the upcoming festival"
        days = anchor.get("days_until")
        days_note = f" ({days} days away)" if days is not None else ""
        offers = active_offers(merchant)
        offer_note = f" Your '{offers[0]['title']}' is already active — want me to push it as a festival post?" if offers \
            else " Want me to draft a festival-specific post?"
        body = f"{who}, {festival}{days_note} — worth planning for.{offer_note}"
        return body, "binary_yes_no"

    if kind == "ipl_match_today":
        match = anchor.get("match") or "tonight's match"
        venue = anchor.get("venue")
        venue_note = f" at {venue}" if venue else ""
        is_weeknight = anchor.get("is_weeknight")
        offers = active_offers(merchant)
        offer = offers[0]["title"] if offers else None
        if is_weeknight is False:
            weekend_note = (
                "Weekend IPL nights usually shift covers down (people watch at home) — "
                "skip a dine-in push"
            )
            action_note = f" and lean on '{offer}' as a delivery-only special instead." if offer else \
                " and lean on delivery instead."
        else:
            weekend_note = "Weeknight IPL matches usually drive extra footfall — worth leaning into it"
            action_note = f" with '{offer}' front and center." if offer else "."
        body = f"Quick heads-up {who} — {match}{venue_note} today. {weekend_note}{action_note} Want me to draft the promo?"
        return body, "binary_yes_no"

    if kind == "competitor_opened":
        name = anchor.get("competitor_name") or "a new competitor"
        dist = anchor.get("distance_km")
        dist_note = f" {dist}km away" if dist is not None else " nearby"
        their_offer = anchor.get("their_offer")
        offer_note = f" — they're running '{their_offer}'." if their_offer else "."
        body = f"{who}, {name} opened{dist_note} on GBP{offer_note} Want me to compare their listing against yours and flag any gaps?"
        return body, "open_ended"

    if kind == "weather_heatwave":
        temp = anchor.get("temp_c")
        city = anchor.get("city") or "your city"
        temp_note = f"{temp}°C in {city} today" if temp else f"a heatwave in {city} today"
        body = f"{who}, {temp_note} — worth adjusting today's push (footfall patterns shift on extreme-weather days). Want a quick suggestion?"
        return body, "open_ended"

    if kind == "local_news_event":
        headline = anchor.get("headline") or "a local disruption"
        body = f"{who}, heads up: {headline} nearby — could affect footfall today. Want me to suggest a quick adjustment?"
        return body, "open_ended"

    body = f"{who}, something worth flagging came up in your area today. Want the details?"
    return body, "open_ended"


def _draft_merchant_behavior(category, merchant, trigger, customer, anchor) -> tuple[str, str]:
    who = owner_salutation(category, merchant)
    kind = anchor["kind"]

    if kind == "curious_ask_due":
        body = (
            f"Hi {who}! Quick check — what's been the most-asked-for service this week? "
            f"I'll turn the answer into a Google post + a short WhatsApp reply you can reuse. Takes 5 min."
        )
        return body, "open_ended"

    if kind == "active_planning_intent":
        topic = (anchor.get("topic") or "the idea you raised").replace("_", " ")
        offers = active_offers(merchant)
        offer_note = f" Your existing '{offers[0]['title']}' offer could anchor the pricing." if offers else ""
        body = (
            f"{who}, following up on {topic} — happy to draft a starter version.{offer_note} "
            f"Want me to put together a first draft you can edit?"
        )
        return body, "binary_confirm_cancel"

    if kind == "renewal_due":
        days = anchor.get("days_remaining")
        plan = anchor.get("plan")
        amount = anchor.get("amount")
        days_note = f"{days} days" if days is not None else "a few days"
        plan_note = f" your {plan} plan" if plan else " your plan"
        amount_note = f" (₹{amount})" if amount else ""
        body = f"{who}, {plan_note} renews in {days_note}{amount_note}. Want me to lock in the renewal now so there's no gap in your listing?"
        return body, "binary_yes_no"

    if kind == "winback_eligible":
        days = anchor.get("days_since_expiry")
        dip = fmt_pct(anchor.get("perf_dip_pct"))
        lapsed = anchor.get("lapsed_customers")
        days_note = f"{days} days" if days is not None else "a while"
        dip_note = f", views down {dip} since" if dip else ""
        lapsed_note = f" and {lapsed} more customers have lapsed in that window" if lapsed else ""
        body = (
            f"{who}, it's been {days_note} since your subscription lapsed{dip_note}{lapsed_note}. "
            f"Want me to show what reactivating would recover?"
        )
        return body, "binary_yes_no"

    if kind == "dormant_with_vera":
        days = anchor.get("days_since")
        topic = anchor.get("last_topic")
        days_note = f"it's been {days} days since we last spoke" if days is not None else "it's been a while since we last spoke"
        topic_note = f" (we were on {str(topic).replace('_', ' ')})" if topic else ""
        body = f"{who}, {days_note}{topic_note} — still worth picking back up? I've got a couple of quick wins ready whenever you are."
        return body, "open_ended"

    body = f"{who}, checking in on your account this week — anything I can help move forward?"
    return body, "open_ended"


def _draft_customer_booking(category, merchant, trigger, customer, anchor) -> tuple[str, str]:
    ident = _identity(merchant)
    merchant_name = ident.get("name", "our clinic")
    owner = ident.get("owner_first_name")
    cust_name = customer_display_name(customer) if customer else "there"
    use_hi = customer_uses_hindi(customer)
    kind = anchor["kind"]
    offers = anchor.get("active_offers") or []
    offer_note_en = f" {offers[0]['title']}." if offers else ""

    if kind == "recall_due":
        service = fmt_service_name(anchor.get("service_due"))
        slots = anchor.get("slots") or []
        if slots:
            labels = [s.get("label") for s in slots if s.get("label")]
            slot_text = " ya ".join(labels) if use_hi else " or ".join(labels)
        else:
            slot_text = None
        if use_hi:
            body = f"Hi {cust_name}, {merchant_name} yahan 🦷 Aapka {service} due hai."
            if slot_text:
                body += f" Apke liye slots ready hain: {slot_text}."
            body += f"{offer_note_en} Reply 1/2 to book, or tell us a time that works."
        else:
            body = f"Hi {cust_name}, {merchant_name} here. Your {service} is due."
            if slot_text:
                body += f" We have slots open: {slot_text}."
            body += f"{offer_note_en} Reply 1/2 to book, or tell us a time that works."
        return body, "multi_choice_slot" if slots else "open_ended"

    if kind == "appointment_tomorrow":
        pref = anchor.get("preferred_slot")
        pref_note = f" ({str(pref).replace('_', ' ')} works best for you, if that's still the plan)" if pref else ""
        if use_hi:
            body = f"Hi {cust_name}, {merchant_name} yahan. Kal aapki appointment hai{pref_note} — reminder bhej rahe hain. Confirm karenge?"
        else:
            body = f"Hi {cust_name}, {merchant_name} here. Reminder for your appointment tomorrow{pref_note}. Can you confirm you're still coming?"
        return body, "binary_yes_no"

    if kind == "trial_followup":
        options = anchor.get("next_options") or []
        opt_label = options[0].get("label") if options and options[0].get("label") else None
        opt_note = f" Next session option: {opt_label}." if opt_label else ""
        if use_hi:
            body = f"Hi {cust_name}, trial ke baad kaisa laga? {merchant_name} se hain.{opt_note} Continue karna chahenge?"
        else:
            body = f"Hi {cust_name}, how did the trial go? This is {merchant_name}.{opt_note} Want to continue?"
        return body, "binary_yes_no"

    if kind == "wedding_package_followup":
        days = anchor.get("days_to_wedding")
        days_note = f"{days} days to your wedding" if days is not None else "your wedding coming up"
        offer_note = f" {offers[0]['title']} could be a good fit." if offers else ""
        if use_hi:
            body = f"Hi {cust_name} 💍 {merchant_name} se {owner or ''} yahan. {days_note} — perfect window to start prep.{offer_note} Book karein?"
        else:
            body = f"Hi {cust_name} 💍 This is {owner or merchant_name}. {days_note} — good window to start prep.{offer_note} Want me to hold a slot?"
        return body, "binary_yes_no"

    if kind == "chronic_refill_due":
        molecules = [m.replace("chronic_rx_", "").replace("_", " ") for m in (anchor.get("molecules") or []) if m]
        mol_phrase = ", ".join(molecules) if molecules else "regular medicines"
        runs_out_human = fmt_date_human(anchor.get("stock_runs_out"))
        runs_out_note = f" by {runs_out_human}" if runs_out_human else " soon"
        delivery_note = " Free home delivery to your saved address." if anchor.get("delivery_saved") else ""
        if use_hi:
            body = f"Namaste — {merchant_name} yahan. Aapki {mol_phrase} khatam ho rahi hai{runs_out_note}.{delivery_note} Reply CONFIRM to dispatch."
        else:
            body = f"Hi — {merchant_name} here. Your {mol_phrase} run out{runs_out_note}.{delivery_note} Reply CONFIRM to dispatch."
        return body, "binary_yes_no"

    body = f"Hi {cust_name}, {merchant_name} here — following up on your upcoming visit. Reply to confirm."
    return body, "binary_yes_no"


def _draft_customer_lapse(category, merchant, trigger, customer, anchor) -> tuple[str, str]:
    ident = _identity(merchant)
    merchant_name = ident.get("name", "our business")
    owner = ident.get("owner_first_name")
    cust_name = customer_display_name(customer) if customer else "there"
    use_hi = customer_uses_hindi(customer)
    offers = anchor.get("active_offers") or []
    offer_note = f" We've got '{offers[0]['title']}' running right now." if offers else ""

    days_since = anchor.get("days_since")
    last_visit_human = fmt_date_human(anchor.get("last_visit"))
    if days_since is not None:
        weeks = round(days_since / 7)
        duration_phrase = f"about {weeks} week{'s' if weeks != 1 else ''}" if weeks >= 1 else f"{days_since} days"
        since_suffix = " since your last visit"
    elif last_visit_human:
        duration_phrase = "a while"
        since_suffix = f" — your last visit was {last_visit_human}"
    else:
        duration_phrase = "a while"
        since_suffix = " since your last visit"
    focus = anchor.get("previous_focus")
    focus_note = f" that still fits your {str(focus).replace('_', ' ')} goal" if focus else ""

    if use_hi:
        body = (
            f"Hi {cust_name} 👋 {owner or merchant_name} se yahan. Kaafi time ho gaya hai ({duration_phrase}{since_suffix}) — "
            f"koi baat nahi, aisa hota hai.{offer_note} Kuch naya try karna chahenge{focus_note}? Reply YES — no commitment."
        )
    else:
        body = (
            f"Hi {cust_name} 👋 {owner or merchant_name} here. It's been {duration_phrase}{since_suffix} — "
            f"no judgment, happens to everyone.{offer_note} Want to try something{focus_note}? Reply YES — no commitment."
        )
    return body, "binary_yes_no"


# ---------------------------------------------------------------------------
# Stage 3 -- llm_polish (optional): improve phrasing only, never add facts
# ---------------------------------------------------------------------------

_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
_OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
_LLM_MODEL = os.environ.get("VERA_LLM_MODEL", "")


def llm_polish(draft: dict, category: dict, merchant: dict, trigger: dict, customer: Optional[dict]) -> Optional[str]:
    """Return a polished body string, or None if no LLM is configured / call fails.
    Never allowed to introduce new facts -- lint_message() enforces that.
    """
    if not _ANTHROPIC_KEY and not _OPENAI_KEY:
        return None

    system = (
        "You polish WhatsApp business messages for natural phrasing and, where the "
        "customer's language preference calls for it, natural Hindi-English code-mixing. "
        "You must NOT add any fact, number, date, name, or claim that is not already present "
        "in the draft message. Do not add a URL. Return ONLY the revised message body, no "
        "preamble, no explanation, no quotes."
    )
    lang_pref = (customer.get("identity", {}).get("language_pref") if customer else None) or \
                ("hi-en mix" if merchant_uses_hindi(merchant) else "english")
    prompt = (
        f"Category: {category.get('slug')}\n"
        f"Voice tone: {category.get('voice', {}).get('tone')}\n"
        f"Language preference: {lang_pref}\n"
        f"Draft message:\n{draft['body']}\n\n"
        f"Rewrite it to sound more natural while keeping every fact identical."
    )

    try:
        if _ANTHROPIC_KEY:
            return _call_anthropic(system, prompt)
        return _call_openai(system, prompt)
    except Exception:
        return None


def _call_anthropic(system: str, prompt: str) -> str:
    import json
    from urllib import request as urlrequest

    model = _LLM_MODEL or "claude-3-5-sonnet-20241022"
    body = json.dumps({
        "model": model, "max_tokens": 400, "temperature": 0,
        "system": system, "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urlrequest.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": _ANTHROPIC_KEY, "Content-Type": "application/json", "anthropic-version": "2023-06-01"},
    )
    resp = urlrequest.urlopen(req, timeout=25)
    data = json.loads(resp.read().decode("utf-8"))
    return data["content"][0]["text"].strip()


def _call_openai(system: str, prompt: str) -> str:
    import json
    from urllib import request as urlrequest

    model = _LLM_MODEL or "gpt-4o-mini"
    body = json.dumps({
        "model": model, "temperature": 0,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urlrequest.Request(
        "https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {_OPENAI_KEY}", "Content-Type": "application/json"},
    )
    resp = urlrequest.urlopen(req, timeout=25)
    data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Stage 4 -- lint_message: reject polish on any violation, fall back to draft
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
_NUM_RE = re.compile(r"\d[\d,]*\.?\d*")


def _digit_groups(text: str) -> set[str]:
    return {re.sub(r"[,.]", "", m) for m in _NUM_RE.findall(text)}


def lint_message(draft_body: str, candidate_body: Optional[str], category: dict, merchant: dict) -> str:
    """Validate candidate_body (the LLM-polished text). Fall back to
    draft_body if it introduces a URL, a taboo word, a new number not in
    the draft, or repeats a prior message verbatim.
    """
    if not candidate_body:
        return draft_body
    if _URL_RE.search(candidate_body):
        return draft_body

    taboo = [t.lower() for t in (category.get("voice", {}) or {}).get("vocab_taboo", [])]
    lowered = candidate_body.lower()
    if any(t in lowered for t in taboo if t):
        return draft_body

    draft_nums = _digit_groups(draft_body)
    candidate_nums = _digit_groups(candidate_body)
    if candidate_nums - draft_nums:
        return draft_body

    prior_bodies = {h.get("body") for h in merchant.get("conversation_history", []) or [] if h.get("from") in ("vera", "merchant_on_behalf")}
    if candidate_body.strip() in prior_bodies:
        return draft_body

    return candidate_body


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """
    Returns a dict with keys: body, cta, send_as, suppression_key, rationale.
    Deterministic given the same inputs (LLM polish step, if configured,
    runs at temperature 0 and is linted against fact-fabrication).
    """
    anchor = resolve_anchor(category, merchant, trigger, customer)
    draft = build_draft(category, merchant, trigger, customer, anchor)

    polished = llm_polish(draft, category, merchant, trigger, customer)
    final_body = lint_message(draft["body"], polished, category, merchant)

    # defensive: never emit a URL or empty body, even from our own templates
    if not final_body or _URL_RE.search(final_body):
        final_body = draft["body"]

    return {
        "body": final_body,
        "cta": draft["cta"],
        "send_as": draft["send_as"],
        "suppression_key": draft["suppression_key"],
        "rationale": draft["rationale"],
    }
