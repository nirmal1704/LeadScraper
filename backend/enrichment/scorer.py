"""
enrichment/scorer.py — Fixed scoring.
No website = 90 = Hot. Always.
"""
import re

def normalize_socials(lead: dict):
    """
    If the website is actually a social media profile, move it to the correct column
    and clear the website field so it gets scored properly as a Hot lead.
    """
    website = lead.get("website")
    if not website:
        return
        
    lower_site = website.lower()
    if "instagram.com" in lower_site:
        lead["has_instagram"] = True
        m = re.search(r'instagram\.com/([^/]+)', website)
        if m:
            lead["instagram_handle"] = m.group(1)
        lead["website"] = None
    elif "facebook.com" in lower_site:
        lead["website"] = None
        # Could add facebook_handle here if needed
    elif "youtube.com" in lower_site:
        lead["website"] = None

def score_lead(lead: dict) -> tuple[int, str]:
    """
    Score a lead 0–100 and assign priority.

    Priority thresholds:
        Hot    ≥ 85   — No website, active business. Call today.
        Warm   65–84  — Broken or insecure website. Easy redesign pitch.
        Medium 45–64  — Has website, not mobile-friendly.
        Cold   25–44  — Decent website. Low priority.
        Skip    < 25  — Modern website. Don't bother.
    """
    website = lead.get("website")
    website_status = lead.get("website_status")
    has_https = lead.get("has_https")
    has_mobile_meta = lead.get("has_mobile_meta")
    has_instagram = lead.get("has_instagram", False)
    review_count = lead.get("review_count") or 0

    # ── Base score ─────────────────────────────────────────────────────────────
    if not website:
        score = 90                          # No website at all — best lead
    elif website_status in ("Down", "Error", "Timeout", "Unknown"):
        score = 75                          # Site exists but broken
    elif has_https is False:
        score = 65                          # HTTP only, no SSL
    elif has_mobile_meta is False:
        score = 50                          # Not mobile-friendly
    else:
        score = 20                          # Working modern website (or pending check)

    # ── Bonus signals ──────────────────────────────────────────────────────────
    if not website:
        if review_count >= 10:
            score += 5   # Active business with real customers — great pitch
        if has_instagram:
            score += 3   # Digitally aware but website-less — easiest sell

    if lead.get("has_zomato") or lead.get("has_swiggy"):
        if not website:
            score += 4   # On delivery apps but no website — obvious gap

    score = min(score, 100)

    # ── Priority label ─────────────────────────────────────────────────────────
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

    return score, priority


def score_leads_batch(leads: list[dict]) -> list[dict]:
    """Score a list of lead dicts in place. Returns the same list."""
    for lead in leads:
        normalize_socials(lead)
        score, priority = score_lead(lead)
        lead["score"] = score
        lead["priority"] = priority
    return leads
