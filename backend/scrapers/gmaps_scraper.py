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
from urllib.parse import urlparse
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
    if not address or city.lower() not in address.lower():
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


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _display_domain(url: str) -> Optional[str]:
    if not url:
        return None
    host = urlparse(url).netloc.lower().replace("www.", "")
    return host or None


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
            permissions=["geolocation"],
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

            remaining_for_city = max_per_city - len(all_leads)
            leads, new_areas = await self._scrape_area(query, city, area, seen_hashes, limit=remaining_for_city)
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
                        doc = ref.get()
                        existing = doc.to_dict().get("areas", []) if doc.exists else _get_seed_areas(city)
                        for a in new_discovered:
                            if a not in existing:
                                existing.append(a)
                        # Keep only the newest 15 areas so we remove older ones and get fresher results
                        ref.set({"areas": existing[-15:]}, merge=True)
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
        self, query: str, city: str, area: str, seen: set, limit: int = 20
    ) -> tuple[list[dict], list[str]]:
        location_str = f"{area}, {city}"
        url = GMAPS_URL.format(query=quote(query), location=quote(location_str))
        self.progress(f"Searching '{query}' in {location_str}")
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
                if self.stop_flag() or len(leads) >= limit:
                    break
                try:
                    lead, areas = await self._extract(page, listing, city, area, query)
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

    async def _extract(self, page, listing, city: str, area: str, query: str):
        try:
            card_text = (await listing.inner_text()).strip()
            card_name = card_text.splitlines()[0].strip() if card_text else ""

            # Robustly click the card (force click on the top-left corner to avoid internal buttons)
            await listing.click(force=True, position={'x': 15, 'y': 15})
                
            # Actively wait for the details panel to update to this specific business
            name = ""
            for _ in range(15): # poll for up to 3 seconds
                for sel in ['h1.DUwDvf', '.fontHeadlineLarge', 'h1.qAWA2']:
                    el = await page.query_selector(sel)
                    if el:
                        current_name = (await el.inner_text()).strip()
                        if current_name and current_name.lower() != "results":
                            card_norm = _normalize_text(card_name)
                            panel_norm = _normalize_text(current_name)
                            if card_norm in panel_norm or panel_norm in card_norm:
                                name = current_name
                                break
                if name:
                    break
                await asyncio.sleep(0.2)
                
            if not name:
                # The details panel never opened, or the click failed.
                return None, []
                
            card_norm = _normalize_text(card_name)
            panel_norm = _normalize_text(name)
            name_matches_card = True

            # CRITICAL FIX: If the details panel name does not match the card name, it means the 
            # details panel failed to load (or we misclicked). We MUST abort extraction immediately 
            # so we don't accidentally extract the previous lead's website/phone number!
            if card_norm and not name_matches_card:
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
            # ONLY use selectors that are exclusive to the details panel!
            # If we use generic 'aria-label="Website"', it accidentally grabs the website button 
            # from the first card in the sidebar if the current business doesn't have one!
            for web_sel in [
                'a[data-item-id="authority"]',
                '[data-item-id="authority"] a'
            ]:
                web_el = await page.query_selector(web_sel)
                if web_el:
                    href = await web_el.get_attribute("href") or ""
                    # Exclude Google Maps internal links
                    if href and "google.com/maps" not in href and "google.com/search" not in href:
                        website = href
                        break

            confidence = 55
            evidence = []
            if name_matches_card:
                confidence += 25
                evidence.append("card name matched panel")
            else:
                evidence.append("card/panel name mismatch")
            if phone:
                confidence += 8
                evidence.append("phone found")
            if address and city.lower() in address.lower():
                confidence += 8
                evidence.append("address contains city")
            if website:
                confidence += 4
                evidence.append("website found")
            confidence = min(confidence, 100)

            return {
                "name": name,
                "place_name_from_card": card_name or None,
                "place_name_from_panel": name,
                "name_matches_card": name_matches_card,
                "phone": phone or None,
                "address": address or None,
                "city": city,
                "area": area,
                "query": query,
                "source_query": query,
                "source_city": city,
                "source_area": area,
                "google_maps_url": page.url,
                "website": website or None,
                "website_domain": _display_domain(website),
                "rating": rating,
                "review_count": review_count,
                "source": "Google Maps",
                "lead_type": "Website found" if website else "No website on Google Maps",
                "confidence": confidence,
                "evidence": "; ".join(evidence),
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
            bad_words = {"p", "explore", "reel", "stories", "tv", "accounts", "invites", "oauth", "about", "developer"}
            handles = [m for m in matches if m.lower() not in bad_words and len(m) > 2]
            if handles:
                return True, handles[0]
            return False, None
        except Exception:
            return False, None
        finally:
            await page.context.close()
