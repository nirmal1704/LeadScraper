"""
scrapers/gmaps_scraper.py
─────────────────────────
Google Maps scraper with intelligent area discovery.
Searches at neighbourhood level, discovers new areas from addresses.
Instagram found via Google search (not direct Instagram scraping).
"""

import asyncio
import re
import random
import logging
import uuid
from urllib.parse import quote
from typing import Optional

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)

GMAPS_URL = "https://www.google.com/maps/search/{query}+in+{location}?hl=en&gl=in"
GOOGLE_SEARCH_URL = "https://www.google.com/search?q={query}&hl=en&gl=in"

# Well-known areas for major cities — LLM may also suggest city names,
# and we discover more from addresses dynamically.
CITY_AREAS: dict[str, list[str]] = {
    "Mumbai": ["Andheri", "Bandra", "Borivali", "Dadar", "Malad", "Goregaon",
               "Kandivali", "Thane", "Navi Mumbai", "Powai", "Chembur", "Mulund"],
    "Delhi": ["Lajpat Nagar", "Dwarka", "Rohini", "Janakpuri", "Saket",
              "Vasant Kunj", "Rajouri Garden", "Karol Bagh", "Noida", "Gurgaon"],
    "Bangalore": ["Indiranagar", "Koramangala", "Whitefield", "JP Nagar", "Jayanagar",
                  "HSR Layout", "BTM Layout", "Malleshwaram", "Marathahalli"],
    "Hyderabad": ["Hitech City", "Kondapur", "Madhapur", "Kukatpally", "Secunderabad",
                  "Ameerpet", "Gachibowli", "Banjara Hills", "Jubilee Hills"],
    "Chennai": ["Anna Nagar", "Velachery", "Adyar", "Porur", "Tambaram",
                "Nungambakkam", "T Nagar", "Mylapore", "Sholinganallur"],
    "Pune": ["Kothrud", "Baner", "Wakad", "Hinjewadi", "Aundh",
             "Viman Nagar", "Hadapsar", "Kharadi", "Deccan", "Koregaon Park"],
    "Kolkata": ["Salt Lake", "Behala", "Dum Dum", "New Town", "Gariahat",
                "Tollygunge", "Jadavpur", "Ballygunge", "Howrah"],
    "Ahmedabad": ["Satellite", "Bopal", "Prahlad Nagar", "Navrangpura", "Vastrapur",
                  "Maninagar", "Gota", "Chandkheda"],
}


def _get_seed_areas(city: str) -> list[str]:
    return CITY_AREAS.get(city, [city])


def _extract_areas_from_address(address: str, city: str) -> list[str]:
    """Discover new neighbourhood names from a scraped address."""
    if not address:
        return []
    cleaned = address.replace(city, "").strip(", ")
    parts = [p.strip() for p in cleaned.split(",")]
    areas = []
    for part in parts:
        words = part.split()
        if 1 <= len(words) <= 4 and not re.match(r"^\d+$", part) and len(part) > 4:
            if part.lower() not in {city.lower(), "india", "maharashtra", "karnataka",
                                     "delhi", "tamil nadu", "west bengal", "gujarat"}:
                areas.append(part)
    return areas[:2]


class GMapsScraperV2:
    def __init__(self, db=None, progress_cb=None, stop_flag=None):
        self.db = db
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
            user_agent=random.choice([
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            ]),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        page = await ctx.new_page()
        try:
            stealth = Stealth()
            await stealth.apply_stealth_async(page)
        except Exception:
            pass
            
        # Block images and fonts to save RAM, but KEEP CSS (blocking CSS freezes GMaps)
        await page.route("**/*.{png,jpg,jpeg,woff,woff2,gif,webp}", lambda route: route.abort())
        # Block the heavy Google Maps 3D vector tiles to stop the map from rendering and save massive RAM
        await page.route("**/maps/vt/**", lambda route: route.abort())
        return page

    async def scrape_city(
        self,
        query: str,
        city: str,
        max_per_city: int = 50,
        max_areas: int = 6,
    ) -> list[dict]:
        """Scrape a city for a query using area-level searches with dynamic discovery."""
        all_leads: list[dict] = []
        seen_hashes: set[str] = set()
        discovered: set[str] = set()

        # Load known areas from persistent memory
        known_areas = set(_get_seed_areas(city))
        if self.db:
            try:
                doc = self.db.collection("geography").document("india").collection("cities").document(city).get()
                if doc.exists:
                    saved_areas = doc.to_dict().get("areas", [])
                    known_areas.update(saved_areas)
            except Exception as e:
                logger.error(f"Failed to fetch areas from DB: {e}")

        area_queue = list(known_areas)
        searched: set[str] = set()
        areas_done = 0

        while area_queue and areas_done < max_areas:
            if self.stop_flag():
                break
            if len(all_leads) >= max_per_city:
                break

            area = area_queue.pop(0)
            if area in searched:
                continue
            searched.add(area)
            areas_done += 1

            leads, new_areas = await self._scrape_area(query, city, area, seen_hashes)
            all_leads.extend(leads)

            new_discovered = []
            for a in new_areas:
                if a not in searched and a not in discovered and a not in known_areas:
                    discovered.add(a)
                    area_queue.append(a)
                    new_discovered.append(a)

            # Persist newly discovered areas to the database (in a background thread to prevent freezing)
            if new_discovered and self.db:
                def _save_db():
                    try:
                        ref = self.db.collection("geography").document("india").collection("cities").document(city)
                        from google.cloud import firestore
                        ref.set({"areas": firestore.ArrayUnion(new_discovered)}, merge=True)
                    except Exception as e:
                        logger.error(f"Failed to save discovered areas to DB: {e}")
                
                import threading
                threading.Thread(target=_save_db, daemon=True).start()

            # Force garbage collection between areas to free RAM
            import gc
            gc.collect()

            if len(all_leads) >= max_per_city:
                break

        return all_leads[:max_per_city]

    async def _scrape_area(
        self, query: str, city: str, area: str, seen: set
    ) -> tuple[list[dict], list[str]]:
        url = GMAPS_URL.format(query=quote(query), location=quote(area))
        self.progress(f"Searching '{query}' in {area}")
        leads = []
        new_areas = []
        page = await self._new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)

            # Scroll results panel
            panel = await page.query_selector('[role="feed"]')
            if panel:
                for _ in range(5):
                    await panel.evaluate("el => el.scrollTop += 800")
                    await asyncio.sleep(random.uniform(0.7, 1.2))

            listings = await page.query_selector_all('[data-result-index]') or \
                       await page.query_selector_all('.Nv2PK')

            self.progress(f"  {len(listings)} results in {area}")

            for listing in listings[:20]:
                if self.stop_flag():
                    break
                try:
                    lead, areas = await self._extract(page, listing, city, query)
                    if lead:
                        h = f"{lead['name'].lower()}{lead.get('phone','')}{city.lower()}"
                        if h not in seen:
                            seen.add(h)
                            lead["id"] = str(uuid.uuid4())
                            leads.append(lead)
                            new_areas.extend(areas)
                except Exception as e:
                    logger.debug(f"Extract error: {e}")
                await asyncio.sleep(random.uniform(0.4, 0.9))

        except Exception as e:
            self.progress(f"  GMaps error in {area}: {e}")
        finally:
            await page.context.close()

        return leads, list(set(new_areas))

    async def _extract(self, page, listing, city: str, query: str):
        try:
            await listing.click()
            await asyncio.sleep(random.uniform(1.0, 1.8))

            name = ""
            for sel in ['h1.DUwDvf', '.fontHeadlineLarge', 'h1']:
                el = await page.query_selector(sel)
                if el:
                    name = (await el.inner_text()).strip()
                    if name:
                        break
            if not name:
                return None, []

            phone = ""
            phone_el = await page.query_selector('[data-item-id*="phone"] .Io6YTe')
            if phone_el:
                phone = re.sub(r'[^\d+\-\s]', '', (await phone_el.inner_text()).strip())

            address = ""
            addr_el = await page.query_selector('[data-item-id="address"] .Io6YTe')
            if addr_el:
                address = (await addr_el.inner_text()).strip()

            new_areas = _extract_areas_from_address(address, city)

            rating, review_count = None, None
            rating_el = await page.query_selector('.F7nice span[aria-label*="star"]')
            if rating_el:
                aria = await rating_el.get_attribute("aria-label") or ""
                m = re.search(r'([\d.]+)\s+star', aria)
                if m:
                    rating = float(m.group(1))
            review_el = await page.query_selector('.F7nice span[aria-label*="review"]')
            if review_el:
                aria = await review_el.get_attribute("aria-label") or ""
                m = re.search(r'([\d,]+)\s+review', aria)
                if m:
                    review_count = int(m.group(1).replace(",", ""))

            website = ""
            # Try multiple selectors — GMaps renders this differently by listing type
            for web_sel in [
                'a[data-item-id="authority"]',
                'a[aria-label*="website" i]',
                'a[aria-label*="Website" i]',
                'a[data-tooltip*="website" i]',
                'a.CsEnBe[href^="http"]',
            ]:
                web_el = await page.query_selector(web_sel)
                if web_el:
                    href = await web_el.get_attribute("href") or ""
                    # Exclude Google Maps internal links
                    if href and "google.com/maps" not in href and "google.com/search" not in href:
                        website = href
                        break

            return {
                "name": name,
                "phone": phone or None,
                "address": address or None,
                "city": city,
                "query": query,
                "google_maps_url": page.url,
                "website": website or None,
                "rating": rating,
                "review_count": review_count,
                "source": "Google Maps",
                # enrichment fields filled later
                "website_status": None,
                "has_https": None,
                "has_mobile_meta": None,
                "has_instagram": False,
                "instagram_handle": None,
                "has_zomato": False,
                "has_swiggy": False,
                "score": 0,
                "priority": "Medium",
            }, new_areas

        except Exception as e:
            logger.debug(f"Extraction error: {e}")
            return None, []

    async def find_instagram(self, name: str, city: str) -> tuple[bool, Optional[str]]:
        """
        Find Instagram via Google search — much more reliable than scraping Instagram directly.
        Searches: "{name} {city} instagram"
        """
        query = quote(f"{name} {city} instagram")
        url = GOOGLE_SEARCH_URL.format(query=query)
        page = await self._new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            content = await page.content()
            # Look for instagram.com links in search results
            matches = re.findall(r'instagram\.com/([A-Za-z0-9_.]+)', content)
            handles = [m for m in matches if m not in {"p", "explore", "reel", "stories", "tv", "accounts"}]
            if handles:
                return True, handles[0]
            return False, None
        except Exception:
            return False, None
        finally:
            await page.context.close()
