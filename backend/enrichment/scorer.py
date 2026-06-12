"""
enrichment/scorer.py
────────────────────
Filter evaluator + lead scorer.

Architecture
────────────
1. apply_filter(lead, f)       — evaluates ONE predicate against a lead dict
2. apply_filters(lead, filters) — returns True if lead matches ALL predicates (AND)
3. apply_filters_batch(leads, filters) — filters a list, hard-drops non-matches
4. score_lead(lead, filters)   — scores 0–100 and assigns priority tier

Scoring philosophy
──────────────────
When NO filters are specified:
  - Default behaviour: "no website = Hot" (classic website-sales use case)

When filters ARE specified:
  - Score is driven entirely by HOW WELL the lead matches the criteria,
    not by any assumptions about what a good lead is.
  - Leads that fail any filter are dropped BEFORE scoring (in apply_filters_batch).
  - Among passing leads, score reflects data richness (email, phone, reviews…)
    so the user can still sort/prioritise within the matching set.

This makes the scorer completely independent of the "website" assumption.
"""

import re
from urllib.parse import urlparse


# ─── Social normaliser ────────────────────────────────────────────────────────

def _domain(url: str) -> str | None:
    host = urlparse(url or "").netloc.lower().replace("www.", "")
    return host or None


def normalize_socials(lead: dict):
    """
    If the website field is actually a social profile URL, move it out so
    the lead is not incorrectly credited with a real website.
    """
    website = lead.get("website")
    if not website:
        return

    lead["website_domain"] = _domain(website)
    lower_site = website.lower()

    if "instagram.com" in lower_site:
        lead["has_instagram"] = True
        m = re.search(r"instagram\.com/([^/?#]+)", website)
        if m:
            lead["instagram_handle"] = m.group(1)
        lead["website"] = None
        lead["website_domain"] = None
        lead["lead_type"] = "Social profile only, no website"

    elif "facebook.com" in lower_site:
        lead["website"] = None
        lead["website_domain"] = None
        lead["lead_type"] = "Social profile only, no website"

    elif "youtube.com" in lower_site:
        lead["website"] = None
        lead["website_domain"] = None
        lead["lead_type"] = "Video channel only, no website"

    elif "linkedin.com" in lower_site:
        lead["website"] = None
        lead["website_domain"] = None
        lead["lead_type"] = "LinkedIn profile only, no website"

    elif "x.com" in lower_site or "twitter.com" in lower_site:
        lead["website"] = None
        lead["website_domain"] = None
        lead["lead_type"] = "Twitter/X profile only, no website"


# ─── Filter predicate evaluator ───────────────────────────────────────────────

def apply_filter(lead: dict, f: dict) -> bool:
    """
    Evaluate a single filter predicate against a lead.

    Predicate schema:
      {"field": str, "op": str, "value": any (optional), "label": str}

    Supported ops per field type:
      Nullable fields (website, email, phone):
        is_null, is_not_null
      Boolean fields (has_https, has_mobile_meta, has_instagram):
        eq
      String/enum fields (website_status, priority, source, category):
        eq, neq, in, not_in, contains, not_contains
      Numeric fields (rating, review_count, score):
        gt, lt, gte, lte, eq
    """
    field = f.get("field", "")
    op    = f.get("op", "")
    value = f.get("value")

    raw = lead.get(field)

    # ── Null/not-null ────────────────────────────────────────────────────────
    if op == "is_null":
        return raw is None or raw == "" or raw is False
    if op == "is_not_null":
        return raw is not None and raw != "" and raw is not False

    # ── Boolean equality ─────────────────────────────────────────────────────
    if op == "eq":
        if isinstance(value, bool):
            return bool(raw) == value
        if isinstance(raw, (int, float)) and isinstance(value, (int, float)):
            return raw == value
        return str(raw).lower() == str(value).lower()

    if op == "neq":
        if isinstance(value, bool):
            return bool(raw) != value
        return str(raw).lower() != str(value).lower()

    # ── Set membership ────────────────────────────────────────────────────────
    if op == "in":
        if not isinstance(value, list):
            value = [value]
        return str(raw).lower() in [str(v).lower() for v in value]

    if op == "not_in":
        if not isinstance(value, list):
            value = [value]
        return str(raw).lower() not in [str(v).lower() for v in value]

    # ── Substring ─────────────────────────────────────────────────────────────
    if op == "contains":
        return str(value).lower() in str(raw or "").lower()

    if op == "not_contains":
        return str(value).lower() not in str(raw or "").lower()

    # ── Numeric comparisons ───────────────────────────────────────────────────
    try:
        raw_n = float(raw) if raw is not None else None
        val_n = float(value) if value is not None else None
    except (TypeError, ValueError):
        return False

    if raw_n is None or val_n is None:
        return False

    if op == "gt":  return raw_n >  val_n
    if op == "lt":  return raw_n <  val_n
    if op == "gte": return raw_n >= val_n
    if op == "lte": return raw_n <= val_n

    # Unknown op — don't block the lead
    return True


def apply_filters(lead: dict, filters: list[dict]) -> bool:
    """Return True if the lead matches ALL filter predicates (AND logic)."""
    if not filters:
        return True
    return all(apply_filter(lead, f) for f in filters)


def apply_filters_batch(leads: list[dict], filters: list[dict]) -> list[dict]:
    """
    Hard-filter: keep only leads that match all predicates.
    Sets lead["filter_match"] = True on keepers.
    Non-matching leads are dropped entirely (never written to Firestore).
    """
    if not filters:
        return leads
    result = []
    for lead in leads:
        if apply_filters(lead, filters):
            lead["filter_match"] = True
            result.append(lead)
    return result


# ─── Scorer ───────────────────────────────────────────────────────────────────

def score_lead(lead: dict, filters: list[dict] | None = None) -> tuple[int, str]:
    """
    Score a lead 0–100 and assign a priority tier.

    When filters are provided and the lead has already passed them,
    scoring reflects DATA RICHNESS (how contactable/verifiable the lead is)
    rather than the "no website = good" assumption.

    When no filters are provided, the classic website-gap scoring applies
    (backwards compatible with the original use case).

    Priority tiers:
      Hot    ≥ 85  — very high opportunity
      Warm   ≥ 65  — good opportunity
      Medium ≥ 45  — moderate
      Cold   ≥ 25  — low but non-zero
      Skip   <  25  — discard
    """
    filters = filters or []
    lead_intent    = lead.get("lead_intent", "physical")
    website        = lead.get("website")
    website_status = lead.get("website_status")
    has_https      = lead.get("has_https")
    has_mobile_meta= lead.get("has_mobile_meta")
    has_instagram  = lead.get("has_instagram", False)
    has_email      = bool(lead.get("email"))
    has_phone      = bool(lead.get("phone"))
    review_count   = lead.get("review_count") or 0
    rating         = lead.get("rating") or 0
    permanently_closed = lead.get("permanently_closed", False)

    # ── Hard filter: permanently closed ──────────────────────────────────────
    if permanently_closed:
        lead["lead_type"] = "Permanently closed — skip"
        return 0, "Skip"

    if filters:
        # ── Filter-driven mode: score = data richness ─────────────────────
        # Base: 50 — the lead already passed the filter, so it's inherently valid
        score = 50

        # Contact signals (how reachable is this lead?)
        if has_email:   score += 20   # email = can cold-outreach directly
        if has_phone:   score += 12   # phone = can call
        if has_instagram: score += 6  # social presence

        # Verification signals (how real/established is this business?)
        if review_count >= 50:  score += 8
        elif review_count >= 10: score += 4
        if rating >= 4.0:       score += 4
        elif rating >= 3.0:     score += 2

        # Website state (useful context even in filter mode)
        if website and website_status == "Up":
            score += 3   # has a working site (relevant when filter is NOT about website)
        if has_https:        score += 2
        if has_mobile_meta:  score += 1

        if lead.get("category"):
            score += 2  # more verified data

        # Cap and set lead_type
        score = min(score, 100)
        if not lead.get("lead_type"):
            lead["lead_type"] = "Matched filter criteria"

    else:
        # ── Default mode: website-gap scoring (original behaviour) ────────
        if not website:
            score = 90 if lead_intent == "physical" else 85
            lead["lead_type"] = lead.get("lead_type") or (
                "No website found" if lead_intent == "physical"
                else "Online business, no website"
            )
        elif website_status in ("Down", "Error", "Timeout", "Unknown"):
            score = 75
            lead["lead_type"] = "Website broken or unreachable"
        elif has_https is False:
            score = 65 if lead_intent == "physical" else 60
            lead["lead_type"] = "Website has no HTTPS"
        elif has_mobile_meta is False:
            score = 50 if lead_intent == "physical" else 45
            lead["lead_type"] = "Website may not be mobile-friendly"
        else:
            score = 20
            lead["lead_type"] = "Working website"

        # Bonus signals
        if has_email:
            score += 12 if lead_intent == "online" else 8
        if not website:
            if review_count >= 10: score += 5
            if has_instagram:       score += 3
        if lead.get("has_zomato") or lead.get("has_swiggy"):
            if not website:         score += 4
        if lead.get("category"):
            lead["confidence"] = min((lead.get("confidence") or 55) + 5, 100)

        score = min(score, 100)

    # ── Priority tier ─────────────────────────────────────────────────────────
    if score >= 85:   priority = "Hot"
    elif score >= 65: priority = "Warm"
    elif score >= 45: priority = "Medium"
    elif score >= 25: priority = "Cold"
    else:             priority = "Skip"

    # ── Evidence trail ────────────────────────────────────────────────────────
    lead["confidence"] = lead.get("confidence") or 40
    if website_status:
        parts = [p for p in [lead.get("evidence"), f"website: {website_status}"] if p]
        lead["evidence"] = "; ".join(parts)

    return score, priority


def score_leads_batch(leads: list[dict], filters: list[dict] | None = None) -> list[dict]:
    """Score a list of lead dicts in-place. Returns the same list."""
    filters = filters or []
    for lead in leads:
        normalize_socials(lead)
        score, priority = score_lead(lead, filters)
        lead["score"] = score
        lead["priority"] = priority
    return leads
