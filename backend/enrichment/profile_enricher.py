"""enrichment/profile_enricher.py — OG/JSON-LD/platform-specific profile metadata extraction."""
import asyncio
import json
import logging
import re
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

TIMEOUT = 8
CONCURRENCY = 10

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

_ENRICHABLE_DOMAINS = {
    "instagram.com", "linkedin.com", "x.com", "twitter.com",
    "behance.net", "github.com", "youtube.com",
}

_GENERIC_PLATFORM_NAMES = {
    "instagram", "linkedin", "youtube", "twitter", "x", "behance", "github", "facebook"
}


def _is_enrichable(url: str) -> bool:
    return bool(url) and any(d in url.lower() for d in _ENRICHABLE_DOMAINS)


def _is_generic_name(name: str) -> bool:
    return name.lower().strip() in _GENERIC_PLATFORM_NAMES


def _parse_follower_count(text: str) -> int | None:
    """Parse follower/subscriber count from text like '1.2M Followers' or '2,300 subscribers'."""
    m = re.search(r"([\d,.]+[KMkm]?)\s*(?:followers|subscribers|following)", text, re.I)
    if not m:
        return None
    raw = m.group(1).upper().replace(",", "")
    try:
        if "M" in raw:
            return int(float(raw.replace("M", "")) * 1_000_000)
        if "K" in raw:
            return int(float(raw.replace("K", "")) * 1_000)
        return int(raw)
    except ValueError:
        return None


def _extract_email(text: str) -> str | None:
    m = re.search(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b", text)
    return m.group(0) if m else None


def _clean_og_title(raw: str) -> str | None:
    """Clean an OG/page title into a person/business name."""
    # Remove platform suffixes: " • Instagram photos", "- YouTube", "/ X", "| LinkedIn"
    clean = re.split(r"\s*[•|\-–|/|]\s*", raw)[0].strip()
    # Remove " (@handle)" suffix
    clean = re.sub(r"\s*\(@[A-Za-z0-9_.]+\)\s*$", "", clean).strip()
    return clean if 3 <= len(clean) <= 80 else None


# ── Platform-specific extractors ──────────────────────────────────────────────

def _extract_instagram(html: str) -> dict:
    """Instagram: OG tags + embedded JSON in script tags."""
    result = {}
    soup = BeautifulSoup(html[:80000], "lxml")

    og_title = soup.find("meta", property="og:title")
    og_desc  = soup.find("meta", property="og:description")
    raw_title = og_title.get("content", "") if og_title else ""
    raw_desc  = og_desc.get("content", "") if og_desc else ""

    if raw_title:
        name = _clean_og_title(raw_title)
        if name and not _is_generic_name(name):
            result["name"] = name

    if raw_desc:
        result["bio"] = raw_desc[:300]
        fc = _parse_follower_count(raw_desc)
        if fc:
            result["follower_count"] = fc
        email = _extract_email(raw_desc)
        if email:
            result["email"] = email

    # Instagram embeds profile data in a script: "window.__additionalDataLoaded"
    # or in __initialData / shared data JSON
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text:
            continue
        # Try to find follower count directly in JSON blobs
        fc_m = re.search(r'"follower_count"\s*:\s*(\d+)', text)
        if fc_m and "follower_count" not in result:
            result["follower_count"] = int(fc_m.group(1))
        # External link in bio
        link_m = re.search(r'"external_url"\s*:\s*"([^"]+)"', text)
        if link_m:
            result["external_link"] = link_m.group(1)
        # Email from biography
        bio_m = re.search(r'"biography"\s*:\s*"([^"]*)"', text)
        if bio_m:
            bio_text = bio_m.group(1).replace("\\n", " ")
            if not result.get("bio"):
                result["bio"] = bio_text[:300]
            email = _extract_email(bio_text)
            if email and not result.get("email"):
                result["email"] = email

    return result


def _extract_youtube(html: str) -> dict:
    """YouTube: OG + JSON-LD + channel description."""
    result = {}
    soup = BeautifulSoup(html[:80000], "lxml")

    og_title = soup.find("meta", property="og:title")
    og_desc  = soup.find("meta", property="og:description")
    raw_title = og_title.get("content", "") if og_title else ""
    raw_desc  = og_desc.get("content", "") if og_desc else ""

    if raw_title:
        name = _clean_og_title(raw_title)
        if name and not _is_generic_name(name):
            result["name"] = name
    if raw_desc:
        result["bio"] = raw_desc[:300]
        fc = _parse_follower_count(raw_desc)
        if fc:
            result["follower_count"] = fc
        email = _extract_email(raw_desc)
        if email:
            result["email"] = email

    # YouTube embeds subscriber count in page JSON
    for script in soup.find_all("script"):
        text = script.string or ""
        if "subscriberCountText" in text:
            sc_m = re.search(r'"subscriberCountText"[^}]*?"simpleText"\s*:\s*"([^"]+)"', text)
            if sc_m and "follower_count" not in result:
                fc = _parse_follower_count(sc_m.group(1) + " subscribers")
                if fc:
                    result["follower_count"] = fc
            break

    return result


def _extract_linkedin(html: str) -> dict:
    """LinkedIn: OG + JSON-LD (they embed Person schema on public profiles)."""
    result = {}
    soup = BeautifulSoup(html[:80000], "lxml")

    og_title = soup.find("meta", property="og:title")
    og_desc  = soup.find("meta", property="og:description")
    raw_title = og_title.get("content", "") if og_title else ""
    raw_desc  = og_desc.get("content", "") if og_desc else ""

    if raw_title:
        name = _clean_og_title(raw_title)
        if name and not _is_generic_name(name):
            result["name"] = name
    if raw_desc:
        result["bio"] = raw_desc[:300]

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict):
                if data.get("name") and not result.get("name"):
                    result["name"] = data["name"]
                if data.get("description") and not result.get("bio"):
                    result["bio"] = data["description"][:300]
                if data.get("email"):
                    result["email"] = data["email"]
        except Exception:
            pass

    return result


def _extract_generic(html: str) -> dict:
    """Generic OG/JSON-LD extraction for Behance, GitHub, X, etc."""
    result = {}
    soup = BeautifulSoup(html[:50000], "lxml")

    og_title = soup.find("meta", property="og:title")
    og_desc  = soup.find("meta", property="og:description")
    raw_title = og_title.get("content", "") if og_title else (soup.title.get_text() if soup.title else "")
    raw_desc  = og_desc.get("content", "") if og_desc else ""

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict):
                if data.get("name"): result["name"] = data["name"]
                if data.get("description"): result["bio"] = data["description"][:300]
                if data.get("email"): result["email"] = data["email"]
        except Exception:
            pass

    if raw_title and "name" not in result:
        name = _clean_og_title(raw_title)
        if name and not _is_generic_name(name):
            result["name"] = name
    if raw_desc and "bio" not in result:
        result["bio"] = raw_desc[:300]
        fc = _parse_follower_count(raw_desc)
        if fc:
            result["follower_count"] = fc
        email = _extract_email(raw_desc)
        if email:
            result["email"] = email

    return result


def _extract_from_html(html: str, url: str) -> dict:
    url_lower = url.lower()
    if "instagram.com" in url_lower:
        return _extract_instagram(html)
    if "youtube.com" in url_lower:
        return _extract_youtube(html)
    if "linkedin.com" in url_lower:
        return _extract_linkedin(html)
    return _extract_generic(html)


# ── Main enrichment functions ─────────────────────────────────────────────────

def _get_enrichable_url(lead: dict) -> str | None:
    if lead.get("instagram_handle"):
        return f"https://instagram.com/{lead['instagram_handle']}"
    url = lead.get("website")
    if url and _is_enrichable(url):
        return url
    for link in (lead.get("social_links") or "").split(","):
        link = link.strip()
        if _is_enrichable(link):
            return link
    return None

async def _enrich_one(client: httpx.AsyncClient, lead: dict, url: str) -> dict:
    try:
        r = await client.get(url, timeout=TIMEOUT, follow_redirects=True)
        if r.status_code not in (200, 403):
            return lead

        extracted = _extract_from_html(r.text, url)

        if extracted.get("name") and not _is_generic_name(extracted["name"]):
            lead["name"] = extracted["name"]
        if extracted.get("bio") and not lead.get("evidence"):
            lead["evidence"] = extracted["bio"][:200]
        if extracted.get("email") and not lead.get("email"):
            lead["email"] = extracted["email"]
        if extracted.get("follower_count"):
            lead["follower_count"] = extracted["follower_count"]
        if extracted.get("external_link") and not lead.get("website"):
            lead["website"] = extracted["external_link"]
            # Clear status so website_checker will process it
            lead["website_status"] = None

    except Exception as e:
        logger.debug(f"Profile enrich failed for {url}: {e}")

    return lead


async def enrich_social_profiles(leads: list[dict]) -> list[dict]:
    """Batch-enrich social profile leads from OG tags / embedded JSON."""
    to_enrich = []
    for l in leads:
        url = _get_enrichable_url(l)
        if url:
            to_enrich.append((l, url))
            
    if not to_enrich:
        return leads

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(headers={"User-Agent": _UA}, verify=False) as client:
        async def _bounded(lead, url):
            async with sem:
                return await _enrich_one(client, lead, url)

        results = await asyncio.gather(*[_bounded(l, u) for l, u in to_enrich], return_exceptions=True)

    for (original, _), result in zip(to_enrich, results):
        if isinstance(result, dict):
            original.update(result)

    return leads
