"""
enrichment/scorer.py
────────────────────
Sector-aware lead scorer 0–100 with priority assignment.

Scoring adapts to the lead's intent (physical / online / hybrid):
  - physical: no website = Hot (classic "needs a website" signal)
  - online:   no website = Hot but from a different angle (online biz with no web presence)
  - hybrid:   blended scoring

Extra signals:
  - has_email (+8–15): outreach-ready lead
  - has_category (+confidence): verified Maps category
  - permanently_closed: hard skip (score=0)
  - open_now=False: soft warning (not filtered unless permanently closed)
"""

import re
from urllib.parse import urlparse


def _domain(url: str) -> str | None:
    host = urlparse(url or "").netloc.lower().replace("www.", "")
    return host or None


def normalize_socials(lead: dict):
    """
    If the website field is actually a social profile, move it out of website
    so the lead scores as website-less (correct for outreach targeting).
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


def score_lead(lead: dict) -> tuple[int, str]:
    """
    Score a lead 0–100 and assign a priority tier.

    Priority tiers:
      Hot    ≥ 85  — very high opportunity
      Warm   ≥ 65  — good opportunity
      Medium ≥ 45  — moderate
      Cold   ≥ 25  — low but non-zero
      Skip   <  25  — discard
    """
    lead_intent = lead.get("lead_intent", "physical")
    website = lead.get("website")
    website_status = lead.get("website_status")
    has_https = lead.get("has_https")
    has_mobile_meta = lead.get("has_mobile_meta")
    has_instagram = lead.get("has_instagram", False)
    has_email = bool(lead.get("email"))
    review_count = lead.get("review_count") or 0
    permanently_closed = lead.get("permanently_closed", False)

    # ── Hard filter: permanently closed ──────────────────────────────────────
    if permanently_closed:
        lead["lead_type"] = "Permanently closed — skip"
        lead["confidence"] = lead.get("confidence") or 0
        return 0, "Skip"

    # ── Base scoring by website state ─────────────────────────────────────────
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

    # ── Bonus signals ────────────────────────────────────────────────────────
    if has_email:
        # Email found = outreach-ready lead, significant bonus
        score += 12 if lead_intent == "online" else 8

    if not website:
        if review_count >= 10:
            score += 5  # Established presence but no web
        if has_instagram:
            score += 3  # Has social but no website

    if lead.get("has_zomato") or lead.get("has_swiggy"):
        if not website:
            score += 4  # Listed on food platforms, still needs a site

    if lead.get("category"):
        # Category available means we have more context — bump confidence
        lead["confidence"] = min((lead.get("confidence") or 55) + 5, 100)

    score = min(score, 100)

    # ── Priority tier assignment ──────────────────────────────────────────────
    if score >= 85:
        priority = "Hot"
    elif score >= 65:
        priority = "Warm"
    elif score >= 45:
        priority = "Medium"
    elif score >= 25:
        priority = "Cold"
    else:
        priority = "Skip"

    # ── Evidence trail ────────────────────────────────────────────────────────
    lead["confidence"] = lead.get("confidence") or 40
    if website_status:
        parts = [p for p in [lead.get("evidence"), f"website: {website_status}"] if p]
        lead["evidence"] = "; ".join(parts)

    return score, priority


def score_leads_batch(leads: list[dict]) -> list[dict]:
    """Score a list of lead dicts in-place. Returns the same list."""
    for lead in leads:
        normalize_socials(lead)
        score, priority = score_lead(lead)
        lead["score"] = score
        lead["priority"] = priority
    return leads
