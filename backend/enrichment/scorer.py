"""enrichment/scorer.py — Filter evaluator + lead scorer."""
import re
from urllib.parse import urlparse


def normalize_phone(phone: str | None) -> str | None:
    """Normalize an Indian phone number to E.164-ish format (+91XXXXXXXXXX)."""
    if not phone:
        return None
    digits = re.sub(r"[^\d]", "", phone)
    # Strip leading country codes: 0091, +91, 91 (if 12 digits)
    if digits.startswith("0091"):
        digits = digits[4:]
    elif digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    # Valid Indian mobile: 10 digits starting with 6-9
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    return phone  # return original if we can't confidently normalize


def _domain(url: str) -> str | None:
    host = urlparse(url or "").netloc.lower().replace("www.", "")
    return host or None


def normalize_socials(lead: dict):
    """Move social profile URLs out of website field."""
    website = lead.get("website")
    if not website:
        return

    lead["website_domain"] = _domain(website)
    lower = website.lower()

    if "instagram.com" in lower:
        lead["has_instagram"] = True
        m = re.search(r"instagram\.com/([^/?#]+)", website)
        if m:
            lead["instagram_handle"] = m.group(1)
        lead["website"] = None
        lead["website_domain"] = None
        lead["lead_type"] = "Social profile only, no website"
    elif "facebook.com" in lower:
        lead["website"] = None
        lead["website_domain"] = None
        lead["lead_type"] = "Social profile only, no website"
    elif "youtube.com" in lower:
        lead["website"] = None
        lead["website_domain"] = None
        lead["lead_type"] = "Video channel only, no website"
    elif "linkedin.com" in lower:
        lead["website"] = None
        lead["website_domain"] = None
        lead["lead_type"] = "LinkedIn profile only, no website"
    elif "x.com" in lower or "twitter.com" in lower:
        lead["website"] = None
        lead["website_domain"] = None
        lead["lead_type"] = "Twitter/X profile only, no website"


def apply_filter(lead: dict, f: dict) -> bool:
    """Evaluate a single filter predicate against a lead."""
    field = f.get("field", "")
    op    = f.get("op", "")
    value = f.get("value")
    raw   = lead.get(field)

    if op == "is_null":
        return raw is None or raw == "" or raw is False
    if op == "is_not_null":
        return raw is not None and raw != "" and raw is not False

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

    if op == "in":
        value = value if isinstance(value, list) else [value]
        return str(raw).lower() in [str(v).lower() for v in value]
    if op == "not_in":
        value = value if isinstance(value, list) else [value]
        return str(raw).lower() not in [str(v).lower() for v in value]

    if op == "contains":
        return str(value).lower() in str(raw or "").lower()
    if op == "not_contains":
        return str(value).lower() not in str(raw or "").lower()

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
    return True


# email/phone presence can't be reliably inferred from web snippets
_SNIPPET_UNRELIABLE = {"email", "phone"}


def _is_web_sourced(lead: dict) -> bool:
    return str(lead.get("source") or "").lower() != "google maps"


def apply_filters(lead: dict, filters: list[dict]) -> bool:
    """Return True if lead passes ALL filter predicates."""
    if not filters:
        return True
    web_sourced = _is_web_sourced(lead)
    for f in filters:
        field, op = f.get("field", ""), f.get("op", "")
        # Soft-skip: email/phone is_not_null on web-sourced leads — mark pending instead of dropping
        if web_sourced and field in _SNIPPET_UNRELIABLE and op == "is_not_null" and not lead.get(field):
            lead[f"{field}_pending"] = True
            continue
        if not apply_filter(lead, f):
            return False
    return True


def apply_filters_batch(leads: list[dict], filters: list[dict]) -> list[dict]:
    """Hard-filter: keep only leads matching all predicates."""
    if not filters:
        return leads
    result = []
    for lead in leads:
        if apply_filters(lead, filters):
            lead["filter_match"] = True
            result.append(lead)
    return result


def score_lead(lead: dict, filters: list[dict] | None = None) -> tuple[int, str]:
    """Score a lead 0–100 and assign a priority tier."""
    filters = filters or []
    website        = lead.get("website")
    website_status = lead.get("website_status")
    has_https      = lead.get("has_https")
    has_mobile_meta= lead.get("has_mobile_meta")
    has_instagram  = lead.get("has_instagram", False)
    has_email      = bool(lead.get("email"))
    has_phone      = bool(lead.get("phone"))
    review_count   = lead.get("review_count") or 0
    rating         = lead.get("rating") or 0
    lead_intent    = lead.get("lead_intent", "physical")
    follower_count = lead.get("follower_count") or 0

    if lead.get("permanently_closed"):
        lead["lead_type"] = "Permanently closed — skip"
        return 0, "Skip"

    if filters:
        score = 50
        if has_email:            score += 20
        if has_phone:            score += 12
        if has_instagram:        score += 6
        if review_count >= 50:   score += 8
        elif review_count >= 10: score += 4
        if rating >= 4.0:        score += 4
        elif rating >= 3.0:      score += 2
        if website and website_status == "Up": score += 3
        if has_https:            score += 2
        if has_mobile_meta:      score += 1
        if lead.get("category"): score += 2
        # Social presence signals (from profile_enricher)
        if follower_count >= 100_000:  score += 10
        elif follower_count >= 10_000: score += 6
        elif follower_count >= 1_000:  score += 3
        if lead.get("bio") and has_email: score += 4  # public email in bio = hot signal
        score = min(score, 100)
        if not lead.get("lead_type"):
            lead["lead_type"] = "Matched filter criteria"
    else:
        if not website:
            score = 90 if lead_intent == "physical" else 85
            lead["lead_type"] = lead.get("lead_type") or (
                "No website found" if lead_intent == "physical" else "Online business, no website"
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

        if has_email:                          score += 12 if lead_intent == "online" else 8
        if not website and review_count >= 10: score += 5
        if not website and has_instagram:      score += 3
        if (lead.get("has_zomato") or lead.get("has_swiggy")) and not website: score += 4
        if lead.get("category"):
            lead["confidence"] = min((lead.get("confidence") or 55) + 5, 100)
        score = min(score, 100)

    if score >= 85:   priority = "Hot"
    elif score >= 65: priority = "Warm"
    elif score >= 45: priority = "Medium"
    elif score >= 25: priority = "Cold"
    else:             priority = "Skip"

    lead["confidence"] = lead.get("confidence") or 40
    if website_status:
        parts = [p for p in [lead.get("evidence"), f"website: {website_status}"] if p]
        lead["evidence"] = "; ".join(parts)

    return score, priority


def score_leads_batch(leads: list[dict], filters: list[dict] | None = None) -> list[dict]:
    filters = filters or []
    for lead in leads:
        normalize_socials(lead)
        if lead.get("phone"):
            lead["phone"] = normalize_phone(lead["phone"]) or lead["phone"]
        score, priority = score_lead(lead, filters)
        lead["score"] = score
        lead["priority"] = priority
    return leads
