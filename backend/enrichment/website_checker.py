"""
enrichment/website_checker.py
Async batch website checker using httpx.
"""
import asyncio
import httpx
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

TIMEOUT = 10
CONCURRENCY = 30


@dataclass
class WebsiteResult:
    url: str
    status: str          # "Live" | "Down" | "Timeout" | "Error"
    has_https: bool
    has_mobile_meta: bool


async def _check_one(client: httpx.AsyncClient, url: str) -> WebsiteResult:
    if not url.startswith("http"):
        url = "https://" + url
    has_https = url.startswith("https://")

    try:
        r = await client.get(url, timeout=TIMEOUT, follow_redirects=True)
        status = "Live" if r.status_code < 400 else "Down"
        
        # Check if the final destination actually uses HTTPS!
        # Google Maps often provides "http://" even if the site securely redirects to HTTPS.
        has_https = str(r.url).startswith("https://")
        
        # Check for mobile viewport meta tag
        html = r.text[:5000]
        has_mobile = 'name="viewport"' in html or "name='viewport'" in html
        return WebsiteResult(url=url, status=status, has_https=has_https, has_mobile_meta=has_mobile)
    except httpx.TimeoutException:
        return WebsiteResult(url=url, status="Timeout", has_https=has_https, has_mobile_meta=False)
    except Exception:
        # If HTTPS connection completely fails, they might ONLY have port 80 open!
        if url.startswith("https://"):
            try:
                http_url = url.replace("https://", "http://", 1)
                r = await client.get(http_url, timeout=TIMEOUT, follow_redirects=True)
                status = "Live" if r.status_code < 400 else "Down"
                has_https = str(r.url).startswith("https://")
                html = r.text[:5000]
                has_mobile = 'name="viewport"' in html or "name='viewport'" in html
                return WebsiteResult(url=http_url, status=status, has_https=has_https, has_mobile_meta=has_mobile)
            except Exception:
                pass
        return WebsiteResult(url=url, status="Error", has_https=has_https, has_mobile_meta=False)


async def check_websites_batch(leads: list[dict]) -> dict[str, WebsiteResult]:
    """
    Check websites for all leads that have one.
    Returns dict mapping lead_id → WebsiteResult.
    """
    to_check = [(l["id"], l["website"]) for l in leads if l.get("website")]
    if not to_check:
        return {}

    sem = asyncio.Semaphore(CONCURRENCY)
    results: dict[str, WebsiteResult] = {}

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
        verify=False
    ) as client:
        async def _bounded(lead_id, url):
            async with sem:
                results[lead_id] = await _check_one(client, url)

        await asyncio.gather(*[_bounded(lid, url) for lid, url in to_check])

    return results
