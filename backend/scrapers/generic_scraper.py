"""
scrapers/generic_scraper.py
───────────────────────────
Multi-strategy web scraper for lead discovery beyond Google Maps.

Search engine waterfall (automatic fallback):
  Strategy A — Google dork:    site:{domain} "{query}" {city}       [Playwright]
  Strategy B — Google web:     "{query}" "{city}" contact email      [Playwright]
  Strategy C — DuckDuckGo HTML: html.duckduckgo.com static page      [httpx, no browser]
  Strategy D — ddgs library:   DuckDuckGo search via Python lib      [sync, thread pool]

RAM profile:
  - Strategies A/B use the shared Playwright browser (started in start())
  - Strategies C/D use httpx and ddgs — zero browser memory, always available as fallback
  - If the browser fails to launch, A/B are skipped; C/D always work
"""

import asyncio
import re
import random
import logging
import uuid
from urllib.parse import quote, urlparse

import httpx
from bs4 import BeautifulSoup

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)

GOOGLE_SEARCH_URL = "https://www.google.com/search?q={query}&hl=en&gl=in&num=20"
DDG_HTML_URL = "https://html.duckduckgo.com/html/?q={query}&kl=in-en"

# Aggregator/directory domains to discard
_JUNK_DOMAINS = {
    "justdial.com", "sulekha.com", "yelp.com", "zomato.com", "swiggy.com",
    "quora.com", "reddit.com", "wikipedia.org", "wikihow.com",
    "indiamart.com", "tradeindia.com", "exportersindia.com",
    "yellowpages.in", "asklaila.com", "google.com", "bing.com",
    "clutch.co", "goodfirms.co", "upcity.com", "crunchbase.com", 
    "fiverr.com", "upwork.com", "freelancer.com", "glassdoor.com", 
    "trustpilot.com", "g2.com", "capterra.com", "expertise.com", 
    "threebestrated.in", "medium.com", "pinterest.com",
    "zoominfo.com", "rocketreach.co", "apollo.io", "lusha.com",
    "zaubacorp.com", "tofler.in", "instancial.com", "ambitionbox.com",
    "fundoodata.com", "vymaps.com", "nicelocal.in"
}

# Patterns for extracting contact info from text snippets
_EMAIL_RE = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
)
_PHONE_RE = re.compile(
    r"(?:(?:\+91|0091|91)?[\s\-.]?)?(?:[6-9]\d{9}|\d{3}[\s\-]\d{3}[\s\-]\d{4})"
)


def _display_domain(url: str) -> str | None:
    host = urlparse(url or "").netloc.lower().replace("www.", "")
    return host or None


# ── Social profile URL parser ──────────────────────────────────────────────────

# Regex patterns to extract a meaningful identity from a social profile URL
_SOCIAL_PROFILE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("instagram.com",  re.compile(r"instagram\.com/([A-Za-z0-9_.]+)/?(?:\?|$)", re.I)),
    ("x.com",          re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]+)/?(?:\?|$)", re.I)),
    ("twitter.com",    re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]+)/?(?:\?|$)", re.I)),
    ("linkedin.com",   re.compile(r"linkedin\.com/in/([A-Za-z0-9_\-]+)/?(?:\?|$)", re.I)),
    ("youtube.com",    re.compile(r"youtube\.com/(?:c/|user/|@)([A-Za-z0-9_\-]+)/?(?:\?|$)", re.I)),
    ("behance.net",    re.compile(r"behance\.net/([A-Za-z0-9_]+)/?(?:\?|$)", re.I)),
    ("github.com",     re.compile(r"github\.com/([A-Za-z0-9_\-]+)/?(?:\?|$)", re.I)),
]

# Pages on social platforms that are NOT individual profiles
_SOCIAL_NON_PROFILE_PATHS = {
    "explore", "search", "reels", "stories", "tags", "directory",
    "hashtag", "p", "reel", "tv", "accounts", "about", "login",
    "signup", "help", "terms", "privacy", "press", "blog",
}


def _extract_social_handle(url: str) -> str | None:
    """
    If `url` points to an individual social profile, return a human-readable
    version of the handle/username. Returns None for non-profile pages.
    """
    for _domain, pattern in _SOCIAL_PROFILE_PATTERNS:
        m = pattern.search(url)
        if m:
            handle = m.group(1)
            # Drop obviously non-profile path segments
            if handle.lower() in _SOCIAL_NON_PROFILE_PATHS:
                return None
            # Convert handle to readable name: underscores/dots → spaces, title case
            readable = re.sub(r"[_.\ ]+", " ", handle).strip().title()
            return readable if len(readable) >= 3 else None
    return None


def _is_junk_url(url: str, target_domain: str | None = None) -> bool:
    domain = _display_domain(url) or ""
    if target_domain and target_domain in domain:
        return False
    return (
        any(j in domain for j in _JUNK_DOMAINS)
        or "login" in url.lower()
        or "signup" in url.lower()
        or "register" in url.lower()
    )


_LISTICLE_RE = re.compile(r"(?i)\b(top\s+\d+|best\s+\d+|\d+\s+best|list\s+of|directory\s+of|firms\s+in|companies\s+in|agencies\s+in|traders\s+in|manufacturers\s+in|suppliers\s+in)\b")

def _is_listicle_title(title: str) -> bool:
    return bool(_LISTICLE_RE.search(title))


def _extract_contact(text: str) -> tuple[str | None, str | None]:
    """Extract first email and phone from a text blob."""
    email_m = _EMAIL_RE.search(text)
    phone_m = _PHONE_RE.search(text)
    email = email_m.group(0) if email_m else None
    phone = phone_m.group(0).strip() if phone_m else None
    return email, phone


def _build_lead(title: str, url: str, snippet: str, city: str, query: str, source_label: str, target_domain: str | None = None) -> dict | None:
    """Build a lead dict from a search result. Returns None if junk."""
    if not url or _is_junk_url(url, target_domain) or _is_listicle_title(title):
        return None

    # ── Social profile: extract name from the URL handle ──────────────────
    social_handle = _extract_social_handle(url)
    if social_handle:
        name = social_handle
    else:
        # ── Non-social page: clean the page title ─────────────────────────
        # If the result URL is on a known social domain but NOT a profile page
        # (e.g. instagram.com/explore/tags/trader) — skip it entirely.
        domain = _display_domain(url) or ""
        is_social_domain = any(pat[0] in domain for pat in _SOCIAL_PROFILE_PATTERNS)
        if is_social_domain:
            return None  # Non-profile social URL — not a lead

        # Clean generic page titles
        name = re.split(r"[-|·•–]", title)[0].strip()

    if not name or len(name) < 3:
        return None

    email, phone = _extract_contact(snippet)
    confidence = 40
    evidence_parts = ["web search result"]
    if email:
        confidence += 15
        evidence_parts.append("email in snippet")
    if phone:
        confidence += 10
        evidence_parts.append("phone in snippet")

    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "phone": phone,
        "email": email,
        "address": None,
        "city": city,
        "area": None,
        "query": query,
        "source_query": query,
        "source_city": city,
        "source_area": None,
        "website": url,
        "website_domain": _display_domain(url),
        "source": source_label,
        "lead_type": "Web presence found",
        "confidence": confidence,
        "evidence": "; ".join(evidence_parts),
        "priority": "Medium",
        "score": 0,
        # GMaps-only fields left empty
        "rating": None,
        "review_count": None,
        "category": None,
        "open_now": None,
        "permanently_closed": False,
        "social_links": "",
        "has_instagram": False,
        "instagram_handle": None,
        "has_zomato": False,
        "has_swiggy": False,
        "website_status": None,
        "has_https": None,
        "has_mobile_meta": None,
    }


class GenericScraper:
    def __init__(self, progress_cb=None, stop_flag=None):
        self.progress = progress_cb or (lambda msg: logger.info(msg))
        self.stop_flag = stop_flag or (lambda: False)
        self._pw = None
        self._browser = None  # May be None if launch fails — httpx fallback still works

    async def start(self):
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-webgl",
                    "--disable-3d-apis",
                    "--disable-software-rasterizer",
                    "--js-flags=--max-old-space-size=48",
                ],
            )
        except Exception as e:
            logger.warning(f"GenericScraper browser launch failed (will use httpx only): {e}")

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def _new_page(self):
        ctx = await self._browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        page = await ctx.new_page()
        try:
            stealth = Stealth()
            await stealth.apply_stealth_async(page)
        except Exception:
            pass
        await page.route(
            "**/*.{png,jpg,jpeg,woff,woff2,gif,webp,svg}",
            lambda route: route.abort()
        )
        return page

    # ── Strategy A: Google Dork (site:{domain}) ────────────────────────────

    async def _try_google_dork(
        self, domain: str, query: str, city: str, max_leads: int
    ) -> list[dict]:
        """Google dork: site:{domain} "{query}" {city}"""
        if not self._browser:
            return []
        domain_clean = domain.replace("https://", "").replace("http://", "").replace("www.", "").strip("/")
        dork = f'site:{domain_clean} "{query}" {city}'
        self.progress(f"Google dork: {dork}")
        page = await self._new_page()
        leads = []
        try:
            url = GOOGLE_SEARCH_URL.format(query=quote(dork))
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.5, 2.5))

            # Check for CAPTCHA / rate limiting
            content = await page.content()
            if "unusual traffic" in content.lower() or "captcha" in content.lower():
                self.progress("  Google rate-limited — switching to DuckDuckGo")
                return []

            results = await page.query_selector_all("div.g")
            for res in results:
                if self.stop_flag() or len(leads) >= max_leads:
                    break
                title_el = await res.query_selector("h3")
                link_el = await res.query_selector("a")
                snippet_el = await res.query_selector("div.VwiC3b, div.lyLwlc")
                if title_el and link_el:
                    title = await title_el.inner_text()
                    link = await link_el.get_attribute("href") or ""
                    snippet = await snippet_el.inner_text() if snippet_el else ""
                    if link and domain_clean in link:
                        lead = _build_lead(title, link, snippet, city, query, f"Google/{domain_clean}", target_domain=domain_clean)
                        if lead:
                            leads.append(lead)
        except Exception as e:
            logger.debug(f"Google dork failed: {e}")
        finally:
            await page.context.close()
        return leads

    # ── Strategy B: Direct Google Web Search ──────────────────────────────

    async def _try_google_web(self, query: str, city: str, max_leads: int) -> list[dict]:
        """Google web search for direct business contact pages."""
        if not self._browser:
            return []
        search_q = f'"{query}" "{city}" contact -site:justdial.com -site:sulekha.com -site:quora.com'
        self.progress(f"Google web search: {query} in {city}")
        page = await self._new_page()
        leads = []
        try:
            url = GOOGLE_SEARCH_URL.format(query=quote(search_q))
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.5, 2.5))

            content = await page.content()
            if "unusual traffic" in content.lower() or "captcha" in content.lower():
                self.progress("  Google rate-limited on web search")
                return []

            results = await page.query_selector_all("div.g")
            seen_domains = set()
            for res in results:
                if self.stop_flag() or len(leads) >= max_leads:
                    break
                title_el = await res.query_selector("h3")
                link_el = await res.query_selector("a")
                snippet_el = await res.query_selector("div.VwiC3b, div.lyLwlc")
                if title_el and link_el:
                    title = await title_el.inner_text()
                    link = await link_el.get_attribute("href") or ""
                    snippet = await snippet_el.inner_text() if snippet_el else ""
                    dom = _display_domain(link)
                    if link and dom and dom not in seen_domains:
                        seen_domains.add(dom)
                        lead = _build_lead(title, link, snippet, city, query, "Google Web")
                        if lead:
                            leads.append(lead)
        except Exception as e:
            logger.debug(f"Google web search failed: {e}")
        finally:
            await page.context.close()
        return leads

    # ── Strategy C: DuckDuckGo HTML (httpx, no browser needed) ────────────

    async def _try_ddg_html(self, query_str: str, city: str, query: str, max_leads: int, target_domain: str | None = None, platform_filter: str | None = None) -> list[dict]:
        """
        DuckDuckGo static HTML search — uses httpx, no Playwright needed.
        URL: html.duckduckgo.com/html/?q={query}
        platform_filter: if set, only keep results whose URL is on this domain.
        """
        self.progress(f"DuckDuckGo fallback: {query_str}")
        leads = []
        try:
            async with httpx.AsyncClient(
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                follow_redirects=True,
                timeout=20,
            ) as client:
                url = DDG_HTML_URL.format(query=quote(query_str))
                resp = await client.get(url)
                if resp.status_code != 200:
                    return []

                soup = BeautifulSoup(resp.text, "lxml")
                results = soup.select(".result")
                seen_domains = set()

                for res in results[:20]:
                    if self.stop_flag() or len(leads) >= max_leads:
                        break
                    title_el = res.select_one(".result__a")
                    snippet_el = res.select_one(".result__snippet")
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    href = title_el.get("href", "")
                    # DDG redirects through their own URL — extract the real URL
                    real_url_m = re.search(r"uddg=([^&]+)", href)
                    if real_url_m:
                        from urllib.parse import unquote
                        href = unquote(real_url_m.group(1))
                    snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                    dom = _display_domain(href)
                    if href and dom and dom not in seen_domains:
                        # If a platform filter is set, only keep on-platform URLs
                        if platform_filter and platform_filter not in dom:
                            continue
                        seen_domains.add(dom)
                        lead = _build_lead(title, href, snippet, city, query, "DuckDuckGo", target_domain=target_domain)
                        if lead:
                            leads.append(lead)
        except Exception as e:
            logger.debug(f"DDG HTML search failed: {e}")
        return leads

    # ── Strategy D: ddgs library (final fallback) ──────────────────────────

    async def _try_ddgs(self, query_str: str, city: str, query: str, max_leads: int, target_domain: str | None = None, platform_filter: str | None = None) -> list[dict]:
        """duckduckgo-search library — handles throttling, works as last resort."""
        self.progress(f"ddgs library fallback: {query_str}")
        leads = []
        try:
            def _sync_search():
                from duckduckgo_search import DDGS
                with DDGS() as ddgs:
                    return list(ddgs.text(query_str, max_results=max_leads * 2))

            results = await asyncio.to_thread(_sync_search)
            seen_domains = set()
            for r in results:
                if self.stop_flag() or len(leads) >= max_leads:
                    break
                href = r.get("href", "")
                title = r.get("title", "")
                snippet = r.get("body", "")
                dom = _display_domain(href)
                if href and dom and dom not in seen_domains:
                    # If a platform filter is set, only keep on-platform URLs
                    if platform_filter and platform_filter not in dom:
                        continue
                    seen_domains.add(dom)
                    lead = _build_lead(title, href, snippet, city, query, "DuckDuckGo", target_domain=target_domain)
                    if lead:
                        leads.append(lead)
        except Exception as e:
            logger.debug(f"ddgs search failed: {e}")
        return leads

    # ── Public: scrape a domain (dork) ────────────────────────────────────

    async def scrape_domain(
        self, domain: str, query: str, city: str, max_leads: int = 10
    ) -> list[dict]:
        """
        Scrape leads from a specific domain using the search engine waterfall.
        Falls back: Google dork → DDG HTML → ddgs library
        """
        domain_clean = (
            domain.replace("https://", "").replace("http://", "")
                  .replace("www.", "").strip("/")
        )
        # Strategy A — Google dork
        leads = await self._try_google_dork(domain_clean, query, city, max_leads)
        if leads:
            self.progress(f"  Found {len(leads)} leads from Google dork ({domain_clean})")
            return leads

        # Strategy C — DuckDuckGo HTML
        dork_q = f"site:{domain_clean} {query} {city}"
        leads = await self._try_ddg_html(dork_q, city, query, max_leads, target_domain=domain_clean, platform_filter=domain_clean)
        if leads:
            self.progress(f"  Found {len(leads)} leads from DDG HTML ({domain_clean})")
            return leads

        # Strategy D — ddgs library
        leads = await self._try_ddgs(dork_q, city, query, max_leads, target_domain=domain_clean, platform_filter=domain_clean)
        self.progress(f"  Found {len(leads)} leads from ddgs ({domain_clean})")
        return leads

    # ── Public: social platform search (profile-only results) ──────────────

    async def scrape_social(
        self, platform: str, query: str, city: str, max_leads: int = 10
    ) -> list[dict]:
        """
        Search for individual profiles on a specific social platform.
        Uses site:{platform} dork to restrict results, then filters to
        only profile URLs (using _extract_social_handle).
        
        Falls back: Google dork → DDG HTML+filter → ddgs+filter
        """
        platform_clean = (
            platform.replace("https://", "").replace("http://", "")
                    .replace("www.", "").strip("/")
        )
        self.progress(f"Social search: {query} on {platform_clean} in {city}")

        # Try Google dork first (site:{platform} query city)
        leads = await self._try_google_dork(platform_clean, query, city, max_leads)
        if leads:
            self.progress(f"  Found {len(leads)} social leads from Google dork ({platform_clean})")
            return leads

        # DDG with platform filter — only keep results on the platform domain
        dork_q = f"site:{platform_clean} {query} {city}"
        leads = await self._try_ddg_html(
            dork_q, city, query, max_leads,
            target_domain=platform_clean, platform_filter=platform_clean
        )
        if leads:
            self.progress(f"  Found {len(leads)} social leads from DDG ({platform_clean})")
            return leads

        # ddgs fallback with platform filter
        leads = await self._try_ddgs(
            dork_q, city, query, max_leads,
            target_domain=platform_clean, platform_filter=platform_clean
        )
        self.progress(f"  Found {len(leads)} social leads from ddgs ({platform_clean})")
        return leads

    # ── Public: general web search (for online/hybrid leads) ──────────────

    async def scrape_web(
        self, query: str, city: str, max_leads: int = 10
    ) -> list[dict]:
        """
        Direct web search for businesses that may not be on Google Maps.
        Best for online/hybrid leads: agencies, SaaS, freelancers, etc.
        Falls back: Google web → DDG HTML → ddgs library
        """
        # Strategy B — Google web
        leads = await self._try_google_web(query, city, max_leads)
        if leads:
            self.progress(f"  Found {len(leads)} leads from Google web ({query})")
            return leads

        # Strategy C — DuckDuckGo HTML
        ddg_q = f"{query} {city} contact"
        leads = await self._try_ddg_html(ddg_q, city, query, max_leads)
        if leads:
            self.progress(f"  Found {len(leads)} leads from DDG web ({query})")
            return leads

        # Strategy D — ddgs library
        leads = await self._try_ddgs(ddg_q, city, query, max_leads)
        self.progress(f"  Found {len(leads)} leads from ddgs web ({query})")
        return leads
