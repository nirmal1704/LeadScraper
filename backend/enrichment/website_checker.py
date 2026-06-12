"""enrichment/website_checker.py — Async batch website checker."""
import asyncio
import httpx
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TIMEOUT = 10
CONCURRENCY = 30

# Status codes that signal an active site blocked by a WAF/Cloudflare
_ACTIVE_CODES = {401, 403, 405, 406, 429, 503}


@dataclass
class WebsiteResult:
    url: str
    status: str   # "Up" | "Down" | "Timeout" | "Error"
    has_https: bool
    has_mobile_meta: bool


def _status_from_code(code: int) -> str:
    return "Up" if code < 400 or code in _ACTIVE_CODES else "Down"


def _has_viewport(html: str) -> bool:
    return 'name="viewport"' in html or "name='viewport'" in html


async def _check_one(client: httpx.AsyncClient, url: str) -> WebsiteResult:
    if not url.startswith("http"):
        url = "https://" + url

    try:
        r = await client.get(url, timeout=TIMEOUT, follow_redirects=True)
        final_url = str(r.url)
        return WebsiteResult(
            url=final_url,
            status=_status_from_code(r.status_code),
            has_https=final_url.startswith("https://"),
            has_mobile_meta=_has_viewport(r.text[:5000]),
        )
    except httpx.TimeoutException:
        return WebsiteResult(url=url, status="Timeout", has_https=url.startswith("https://"), has_mobile_meta=False)
    except Exception:
        # HTTPS failed — try plain HTTP fallback
        if url.startswith("https://"):
            try:
                http_url = "http://" + url[8:]
                r = await client.get(http_url, timeout=TIMEOUT, follow_redirects=True)
                final_url = str(r.url)
                return WebsiteResult(
                    url=final_url,
                    status=_status_from_code(r.status_code),
                    has_https=final_url.startswith("https://"),
                    has_mobile_meta=_has_viewport(r.text[:5000]),
                )
            except Exception:
                pass
        return WebsiteResult(url=url, status="Error", has_https=False, has_mobile_meta=False)


async def check_websites_batch(leads: list[dict]) -> dict[str, WebsiteResult]:
    """Check websites for all leads that have one. Returns dict mapping lead_id → WebsiteResult."""
    to_check = [(l["id"], l["website"]) for l in leads if l.get("website")]
    if not to_check:
        return {}

    sem = asyncio.Semaphore(CONCURRENCY)
    results: dict[str, WebsiteResult] = {}

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
        verify=False,
    ) as client:
        async def _bounded(lead_id, url):
            async with sem:
                results[lead_id] = await _check_one(client, url)

        await asyncio.gather(*[_bounded(lid, url) for lid, url in to_check])

    return results
