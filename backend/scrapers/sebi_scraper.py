import asyncio
import logging
import uuid
import re
import random
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup
from camoufox.async_api import AsyncCamoufox

logger = logging.getLogger(__name__)

GOOGLE_SEARCH_URL = "https://www.google.com/search?q={query}&hl=en&gl=in&num=10"

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
    "fundoodata.com", "vymaps.com", "nicelocal.in", "sebi.gov.in"
}

class SebiScraper:
    def __init__(self, browser: AsyncCamoufox | None = None):
        self._browser = browser
        self._owns_browser = False

    async def start(self):
        if not self._browser:
            self._browser = await AsyncCamoufox(headless=True, block_images=True, i_know_what_im_doing=True).__aenter__()
            self._owns_browser = True

    async def stop(self):
        if self._owns_browser and self._browser:
            await self._browser.__aexit__(None, None, None)
            self._browser = None

    async def _new_page(self):
        ctx = await asyncio.wait_for(
            self._browser.new_context(locale="en-IN"),
            timeout=60.0,
        )
        return await asyncio.wait_for(ctx.new_page(), timeout=60.0)

    async def _close_page_ctx(self, page) -> None:
        if not page:
            return
        try:
            ctx = page.context
            await page.close()
            await ctx.close()
        except Exception:
            pass

    async def scrape(self, max_leads: int = 30) -> list[dict]:
        """Scrape financial advisors from SEBI registry."""
        if not self._browser:
            logger.error("Browser not provided to SebiScraper.")
            return []

        logger.info("Starting SEBI scraper for financial advisors...")
        page = await self._new_page()
        leads = []
        
        try:
            # Navigate to the SEBI Investment Adviser directory
            url = "https://www.sebi.gov.in/sebiweb/other/OtherAction.do?doRecognisedFpi=yes&intmId=13"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            
            while len(leads) < max_leads:
                # Parse current page HTML
                html = await page.content()
                soup = BeautifulSoup(html, "html.parser")
                
                cards = soup.select(".card-table")
                if not cards:
                    break
                
                for card in cards:
                    if len(leads) >= max_leads:
                        break
                    
                    data = {}
                    for row in card.select(".card-view"):
                        title_el = row.select_one(".title span")
                        value_el = row.select_one(".value span")
                        if title_el and value_el:
                            key = title_el.get_text(strip=True).replace(":", "").strip()
                            val = value_el.get_text(strip=True)
                            data[key] = val
                    
                    if not data.get("Name"):
                        continue
                    
                    # Extract fields
                    name = data.get("Name")
                    reg_no = data.get("Registration No.")
                    email = data.get("E-mail") or data.get("Email")
                    phone = data.get("Telephone")
                    address = data.get("Address")
                    contact_person = data.get("Contact Person")
                    
                    if email and " " not in email:
                        # Sometimes emails are obfuscated or missing, handle gracefully
                        email = email.replace("&#64;", "@").replace("&#46;", ".")
                    
                    leads.append({
                        "id": str(uuid.uuid4()),
                        "name": name,
                        "business_name": name,
                        "phone": phone,
                        "phone_number": phone,
                        "email": email,
                        "address": address,
                        "contact_person": contact_person,
                        "registration_no": reg_no,
                        "city": "Unknown", # We can try to extract from address later
                        "query": "financial advisor",
                        "source": "sebi.gov.in",
                        "lead_type": "SEBI Registered Advisor",
                        "confidence": 100,
                        "evidence": "SEBI official directory",
                        "priority": "Hot",
                        "score": 0,
                        "website": None,
                        "category": "Financial Advisor"
                    })
                
                if len(leads) >= max_leads:
                    break
                
                # Try to go to next page
                try:
                    next_btn = await page.query_selector("a[title='Next']")
                    if next_btn:
                        await next_btn.click(timeout=5000)
                        await asyncio.sleep(2)
                        await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    else:
                        break
                except Exception as e:
                    logger.debug(f"SEBI pagination error: {e}")
                    break

        except Exception as e:
            logger.error(f"SEBI scraping failed: {e}")
        finally:
            await self._close_page_ctx(page)
            
        return leads

    async def find_website(self, name: str, city: str) -> str | None:
        """Find official website via Google search."""
        if not self._browser: return None
        url = GOOGLE_SEARCH_URL.format(query=quote(f"{name} {city} official website"))
        page = None
        try:
            page = await self._new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.0, 2.0))
            
            links = await page.query_selector_all("a[data-ved]")
            for link in links[:5]:
                href = await link.get_attribute("href")
                if not href or href.startswith("/") or "google.com" in href: continue
                
                host = urlparse(href).netloc.lower().replace("www.", "")
                if not any(j in host for j in _JUNK_DOMAINS) and ("instagram.com" not in host) and ("linkedin.com" not in host):
                    return href
            return None
        except Exception as e:
            logger.debug(f"find_website failed for {name}: {e}")
            return None
        finally:
            await self._close_page_ctx(page)

    async def find_instagram(self, name: str, city: str) -> tuple[bool, str | None]:
        """Find Instagram handle via Google search."""
        if not self._browser: return False, None
        url = GOOGLE_SEARCH_URL.format(query=quote(f"{name} {city} instagram"))
        page = None
        try:
            page = await self._new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.0, 2.0))
            content = await page.content()
            bad = {"p", "explore", "reel", "stories", "tv", "accounts", "invites", "oauth", "about", "developer", "legal"}
            handles = [m for m in re.findall(r"instagram\.com/([A-Za-z0-9_.]+)", content) if m.lower() not in bad and len(m) > 2]
            return (True, handles[0]) if handles else (False, None)
        except Exception:
            return False, None
        finally:
            await self._close_page_ctx(page)
