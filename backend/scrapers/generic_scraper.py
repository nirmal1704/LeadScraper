"""
scrapers/generic_scraper.py
───────────────────────────
Generic AI scraper that uses Google Dorking to scrape leads from any domain 
(Instagram, YouTube, FilmFreeway, etc.) without getting blocked by their login walls.
"""

import asyncio
import re
import random
import logging
import uuid
from urllib.parse import quote
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)

GOOGLE_SEARCH_URL = "https://www.google.com/search?q={query}&hl=en&gl=in"


def _display_domain(url: str) -> str | None:
    host = urlparse(url or "").netloc.lower().replace("www.", "")
    return host or None

class GenericScraper:
    def __init__(self, progress_cb=None, stop_flag=None):
        self.progress = progress_cb or (lambda msg: logger.info(msg))
        self.stop_flag = stop_flag or (lambda: False)
        self._pw = None
        self._browser = None

    async def start(self):
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
            
        await page.route("**/*.{png,jpg,jpeg,woff,woff2,gif,webp}", lambda route: route.abort())
        return page

    async def scrape_domain(self, domain: str, query: str, city: str, max_leads: int = 10) -> list[dict]:
        """
        Scrape a specific domain using Google Dorks.
        Example: domain="instagram.com", query="yoga classes", city="Mumbai"
        """
        all_leads = []
        seen_urls = set()
        
        # Clean domain (remove https://, www)
        domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").strip("/")
        
        # Dork query: site:instagram.com "yoga classes" Mumbai
        dork = f'site:{domain} "{query}" {city}'
        self.progress(f"Searching {domain} for '{query}' in {city}")
        
        page = await self._new_page()
        try:
            url = GOOGLE_SEARCH_URL.format(query=quote(dork))
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            
            # Extract search results
            results = await page.query_selector_all('div.g')
            
            for res in results:
                if self.stop_flag() or len(all_leads) >= max_leads:
                    break
                    
                title_el = await res.query_selector('h3')
                link_el = await res.query_selector('a')
                snippet_el = await res.query_selector('div.VwiC3b')
                
                if title_el and link_el:
                    title = await title_el.inner_text()
                    link = await link_el.get_attribute('href')
                    snippet = await snippet_el.inner_text() if snippet_el else ""
                    
                    if link and link not in seen_urls and domain in link:
                        seen_urls.add(link)
                        
                        # Extract potential phone/email from snippet
                        phone_match = re.search(r'[\+\(]?[1-9][0-9 .\-\(\)]{8,}[0-9]', snippet)
                        email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', snippet)
                        
                        lead = {
                            "id": str(uuid.uuid4()),
                            "name": title.split('-')[0].split('|')[0].strip(),
                            "phone": phone_match.group(0).strip() if phone_match else None,
                            "email": email_match.group(0).strip() if email_match else None,
                            "address": None,
                            "city": city,
                            "area": None,
                            "query": query,
                            "source_query": query,
                            "source_city": city,
                            "source_area": None,
                            "website": link,
                            "website_domain": _display_domain(link),
                            "source": domain,
                            "lead_type": f"{domain} profile/search result",
                            "confidence": 45,
                            "evidence": "found from Google search result snippet",
                            "priority": "Medium",
                            "score": 0,
                        }
                        
                        # Only add if it seems like a real profile/page
                        if "login" not in link.lower() and "signup" not in link.lower():
                            all_leads.append(lead)
                            
        except Exception as e:
            logger.error(f"Error scraping {domain}: {e}")
            self.progress(f"  Failed to scrape {domain}")
        finally:
            await page.context.close()
            
        import gc
        gc.collect()
        
        self.progress(f"  Found {len(all_leads)} leads from {domain}")
        return all_leads
