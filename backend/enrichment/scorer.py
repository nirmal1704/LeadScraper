"""
enrichment/scorer.py

Scores leads for website-design outreach. No website remains the strongest signal,
but every lead now also carries a plain-English lead_type and evidence trail.
"""
import re
from urllib.parse import urlparse


def _domain(url: str) -> str | None:
    host = urlparse(url or "").netloc.lower().replace("www.", "")
    return host or None


def normalize_socials(lead: dict):
    """
    If the website field is actually a social profile, move it out of website so
    the lead is scored as website-less.
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


def score_lead(lead: dict) -> tuple[int, str]:
    """
    Score a lead 0-100 and assign priority.

    Hot: no website. Warm: broken/insecure website. Medium: weak website.
    Cold/Skip: lower likelihood of an easy website pitch.
    """
    website = lead.get("website")
    website_status = lead.get("website_status")
    has_https = lead.get("has_https")
    has_mobile_meta = lead.get("has_mobile_meta")
    has_instagram = lead.get("has_instagram", False)
    review_count = lead.get("review_count") or 0

    if not website:
        score = 90
        lead["lead_type"] = lead.get("lead_type") or "No website found"
    elif website_status in ("Down", "Error", "Timeout", "Unknown"):
        score = 75
        lead["lead_type"] = "Website broken or unreachable"
    elif has_https is False:
        score = 65
        lead["lead_type"] = "Website has no HTTPS"
    elif has_mobile_meta is False:
        score = 50
        lead["lead_type"] = "Website may not be mobile-friendly"
    else:
        score = 20
        lead["lead_type"] = "Working website"

    if not website:
        if review_count >= 10:
            score += 5
        if has_instagram:
            score += 3

    if lead.get("has_zomato") or lead.get("has_swiggy"):
        if not website:
            score += 4

    score = min(score, 100)

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

    lead["confidence"] = lead.get("confidence") or 40
    if website_status:
        evidence = [lead.get("evidence"), f"website status: {website_status}"]
        lead["evidence"] = "; ".join([item for item in evidence if item])

    return score, priority


def score_leads_batch(leads: list[dict]) -> list[dict]:
    """Score a list of lead dicts in place. Returns the same list."""
    for lead in leads:
        normalize_socials(lead)
        score, priority = score_lead(lead)
        lead["score"] = score
        lead["priority"] = priority
    return leads
