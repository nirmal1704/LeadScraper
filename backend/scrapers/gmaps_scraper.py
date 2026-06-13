"""scrapers/gmaps_scraper.py — Google Maps scraper with area-level search and IG discovery."""
import asyncio
import re
import random
import logging
import uuid
from urllib.parse import quote, urlparse
from typing import Optional

import os
from camoufox.async_api import AsyncCamoufox

from llm.area_bootstrapper import bootstrap_city_areas, STATIC_CITY_AREAS

logger = logging.getLogger(__name__)

GMAPS_URL = "https://www.google.com/maps/search/{query}+in+{location}?hl=en&gl=in"
GOOGLE_SEARCH_URL = "https://www.google.com/search?q={query}&hl=en&gl=in"

_CLOSED_SIGNALS = {"permanently closed", "temporarily closed"}
_STATE_WORDS = {
    "india", "maharashtra", "karnataka", "delhi", "tamil nadu", "west bengal",
    "gujarat", "rajasthan", "uttar pradesh", "telangana", "andhra pradesh",
    "kerala", "madhya pradesh", "haryana", "punjab", "bihar", "jharkhand",
    "odisha",
}


def _get_seed_areas(city: str, db=None) -> list[str]:
    if city in STATIC_CITY_AREAS:
        return list(STATIC_CITY_AREAS[city])
    return bootstrap_city_areas(city, db=db)


def _extract_areas_from_address(address: str, city: str) -> list[str]:
    if not address or city.lower() not in address.lower():
        return []
    cleaned = re.sub(re.escape(city), "", address, flags=re.I).strip(", ")
    areas = []
    for part in [p.strip() for p in cleaned.split(",")]:
        words = part.split()
        if 1 <= len(words) <= 4 and not re.match(r"^\d+", part) and len(part) > 4 and part.lower() not in _STATE_WORDS:
            areas.append(part)
    return areas[:2]


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _display_domain(url: str) -> Optional[str]:
    if not url:
        return None
    host = urlparse(url).netloc.lower().replace("www.", "")
    return host or None


def _dedup_key(name: str, phone: str, city: str) -> str:
    return (
        re.sub(r"[^a-z0-9]", "", (name or "").lower())
        + re.sub(r"[^0-9]", "", phone or "")[-7:]
        + re.sub(r"[^a-z]", "", (city or "").lower())
    )


def _is_closed(text: str) -> bool:
    t = text.lower()
    return any(s in t for s in _CLOSED_SIGNALS)


class GMapsScraperV2:
    def __init__(self, db=None, progress_cb=None, stop_flag=None):
        self.db = db
        self.progress = progress_cb or (lambda msg: logger.info(msg))
        self.stop_flag = stop_flag or (lambda: False)
        self._pw = None
        self._browser = None

    async def start(self):
        proxy_url = os.getenv("PROXY_URL")
        proxy_config = {"server": proxy_url} if proxy_url else None
        
        self._camoufox_manager = AsyncCamoufox(
            headless=True,
            proxy=proxy_config,
            geoip=True if proxy_config else False,
            humanize=0.5,
            locale="en-IN",
        )
        self._browser = await self._camoufox_manager.__aenter__()

    async def stop(self):
        if hasattr(self, '_camoufox_manager') and self._camoufox_manager:
            await self._camoufox_manager.__aexit__(None, None, None)
        elif self._browser:
            await self._browser.close()

    async def _new_page(self):
        page = await self._browser.new_page()
        
        async def _abort(route):
            await route.abort()
            
        # Abort heavy maps data to save RAM
        await page.route("**/maps/vt/**", _abort)
        await page.route("**/maps/viewer/**", _abort)
        return page

    async def scrape_city(self, query: str, city: str, max_per_city: int = 50, max_areas: int = 6) -> list[dict]:
        """Scrape a city using area-level searches with dynamic area discovery."""
        all_leads: list[dict] = []
        seen_hashes: set[str] = set()
        discovered: set[str] = set()

        known_areas = set(_get_seed_areas(city, db=self.db))
        if self.db:
            try:
                doc = self.db.collection("geography").document("india").collection("cities").document(city).get()
                if doc.exists:
                    known_areas.update(doc.to_dict().get("areas", []))
            except Exception as e:
                logger.error(f"Failed to load Firestore areas for {city}: {e}")

        area_queue = list(known_areas)
        random.shuffle(area_queue)
        searched: set[str] = set()
        areas_done = 0

        while area_queue and areas_done < max_areas:
            if self.stop_flag() or len(all_leads) >= max_per_city:
                break

            area = area_queue.pop(0)
            if area in searched:
                continue
            searched.add(area)
            areas_done += 1

            try:
                leads, new_areas = await asyncio.wait_for(
                    self._scrape_area(query, city, area, seen_hashes, limit=max_per_city - len(all_leads)),
                    timeout=90,
                )
            except asyncio.TimeoutError:
                self.progress(f"  [TIMEOUT] Skipped slow area: {area} in {city} (>90s)")
                new_areas = []
                leads = []
            all_leads.extend(leads)

            new_discovered = [a for a in new_areas if a not in searched and a not in discovered and a not in known_areas]
            for a in new_discovered:
                discovered.add(a)
                area_queue.append(a)

            if new_discovered and self.db:
                def _save_areas(_new=new_discovered, _city=city):
                    try:
                        ref = self.db.collection("geography").document("india").collection("cities").document(_city)
                        doc = ref.get()
                        existing = doc.to_dict().get("areas", list(_get_seed_areas(_city))) if doc.exists else list(_get_seed_areas(_city))
                        for a in _new:
                            if a not in existing:
                                existing.append(a)
                        ref.set({"areas": existing[-15:]}, merge=True)
                    except Exception as exc:
                        logger.error(f"Failed to persist areas for {_city}: {exc}")
                import threading
                threading.Thread(target=_save_areas, daemon=True).start()

            import gc; gc.collect()

        return all_leads[:max_per_city]

    async def _scrape_area(self, query: str, city: str, area: str, seen: set, limit: int = 20) -> tuple[list[dict], list[str]]:
        location_str = f"{area}, {city}"
        url = GMAPS_URL.format(query=quote(query), location=quote(location_str))
        self.progress(f"Searching '{query}' in {location_str}")
        leads, new_areas = [], []
        page = await self._new_page()

        try:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                self.progress(f"  Retry navigation for {area}...")
                await asyncio.sleep(2)
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            await asyncio.sleep(random.uniform(1.5, 2.5))

            panel = await page.query_selector('[role="feed"]')
            if panel:
                for _ in range(4):
                    await panel.evaluate("el => el.scrollTop += 800")
                    await asyncio.sleep(random.uniform(0.3, 0.6))

            listings = (
                await page.query_selector_all('[data-result-index]') or
                await page.query_selector_all('.Nv2PK')
            )
            self.progress(f"  {len(listings)} results in {area}")

            for listing in listings[:20]:
                if self.stop_flag() or len(leads) >= limit:
                    break
                try:
                    lead, areas = await self._extract(page, listing, city, area, query)
                    if lead:
                        h = _dedup_key(lead["name"], lead.get("phone", ""), city)
                        if h not in seen:
                            seen.add(h)
                            lead["id"] = str(uuid.uuid4())
                            leads.append(lead)
                            new_areas.extend(areas)
                except Exception as e:
                    logger.debug(f"Extract error in {area}: {e}")
                await asyncio.sleep(random.uniform(0.2, 0.4))

        except Exception as e:
            self.progress(f"  GMaps error in {area}: {e}")
        finally:
            await page.context.close()

        return leads, list(set(new_areas))

    async def _extract(self, page, listing, city: str, area: str, query: str):
        try:
            card_text = (await listing.inner_text()).strip()
            card_name = card_text.splitlines()[0].strip() if card_text else ""

            if _is_closed(card_text):
                return None, []

            await listing.click(force=True, position={"x": 15, "y": 15})

            name = ""
            for _ in range(8):
                for sel in ["h1.DUwDvf", ".fontHeadlineLarge", "h1.qAWA2"]:
                    el = await page.query_selector(sel)
                    if el:
                        current = (await el.inner_text()).strip()
                        if current and current.lower() != "results":
                            cn, pn = _normalize(card_name), _normalize(current)
                            if cn in pn or pn in cn:
                                name = current
                                break
                if name:
                    break
                await asyncio.sleep(0.15)

            if not name:
                return None, []

            cn, pn = _normalize(card_name), _normalize(name)
            name_matches = (
                cn in pn or pn in cn
                or (len(cn) > 5 and cn[:int(len(cn) * 0.6)] in pn)
                or (len(pn) > 5 and pn[:int(len(pn) * 0.6)] in cn)
            )
            if not name_matches:
                return None, []

            panel_text = ""
            try:
                panel_text = (await page.inner_text("div.m6QErb", timeout=1500)).lower()
            except Exception:
                pass
            if _is_closed(panel_text):
                return None, []

            try:
                if await page.query_selector('[aria-label*="Permanently closed"]'):
                    return None, []
            except Exception:
                pass

            phone = ""
            phone_el = await page.query_selector('[data-item-id*="phone"] .Io6YTe')
            if phone_el:
                phone = re.sub(r"[^\d+\-\s]", "", (await phone_el.inner_text()).strip())

            address = ""
            addr_el = await page.query_selector('[data-item-id="address"] .Io6YTe')
            if addr_el:
                address = (await addr_el.inner_text()).strip()
            new_areas = _extract_areas_from_address(address, city)

            rating, review_count = None, None
            rating_el = await page.query_selector('.F7nice span[aria-label*="star"]')
            if rating_el:
                m = re.search(r"([\d.]+)\s+star", await rating_el.get_attribute("aria-label") or "")
                if m:
                    rating = float(m.group(1))
            review_el = await page.query_selector('.F7nice span[aria-label*="review"]')
            if review_el:
                m = re.search(r"([\d,]+)\s+review", await review_el.get_attribute("aria-label") or "")
                if m:
                    review_count = int(m.group(1).replace(",", ""))

            category = None
            for sel in [".DkEaL", ".y7PRA", "button.DkEaL"]:
                el = await page.query_selector(sel)
                if el:
                    t = (await el.inner_text()).strip()
                    if t and len(t) < 60:
                        category = t
                        break

            open_now = None
            try:
                hours_el = await page.query_selector(".o0Svhf")
                if hours_el:
                    t = (await hours_el.inner_text()).lower()
                    open_now = True if "open now" in t else (False if "closed" in t else None)
            except Exception:
                pass

            website, social_links = "", []
            for sel in ['a[data-item-id="authority"]', '[data-item-id="authority"] a']:
                web_el = await page.query_selector(sel)
                if web_el:
                    href = await web_el.get_attribute("href") or ""
                    if href and "google.com/maps" not in href and "google.com/search" not in href:
                        lower = href.lower()
                        if any(d in lower for d in ["justdial.com", "facebook.com", "instagram.com", "linkedin.com", "linktr.ee", "twitter.com", "x.com", "wa.me", "whatsapp.com", "youtube.com"]):
                            social_links.append(href)
                        else:
                            website = href
                    break

            try:
                profiles = await page.query_selector_all(
                    'a[href*="instagram.com"], a[href*="facebook.com"], a[href*="linkedin.com"], a[href*="youtube.com"], a[href*="justdial.com"]'
                )
                for p in profiles:
                    href = await p.get_attribute("href")
                    if href and "google.com" not in href and href not in social_links and href != website:
                        if any(d in href.lower() for d in ["justdial", "facebook", "instagram", "linkedin", "linktr", "twitter", "x.com", "wa.me", "whatsapp", "youtube"]):
                            social_links.append(href)
            except Exception:
                pass

            confidence = 55
            evidence = []
            if name_matches:
                confidence += 25; evidence.append("name matched card")
            if phone:
                confidence += 8; evidence.append("phone found")
            if address and city.lower() in address.lower():
                confidence += 8; evidence.append("city in address")
            if website:
                confidence += 4; evidence.append("website found")
            if category:
                confidence += 5; evidence.append(f"category: {category}")
            confidence = min(confidence, 100)

            return {
                "name": name,
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
                "category": category,
                "open_now": open_now,
                "permanently_closed": False,
                "source": "Google Maps",
                "lead_type": "Website found" if website else "No website on Google Maps",
                "confidence": confidence,
                "evidence": "; ".join(evidence),
                "website_status": None,
                "has_https": None,
                "has_mobile_meta": None,
                "social_links": ", ".join(social_links),
                "has_instagram": any("instagram.com" in s for s in social_links),
                "instagram_handle": next(
                    (s.split("instagram.com/")[1].strip("/") for s in social_links if "instagram.com/" in s),
                    None,
                ),
                "has_zomato": False,
                "has_swiggy": False,
                "score": 0,
                "priority": "Medium",
            }, new_areas

        except Exception as e:
            logger.debug(f"Extraction error: {e}")
            return None, []

    async def find_instagram(self, name: str, city: str) -> tuple[bool, Optional[str]]:
        """Find Instagram handle via Google search."""
        url = GOOGLE_SEARCH_URL.format(query=quote(f"{name} {city} instagram"))
        page = await self._new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.5, 2.5))
            content = await page.content()
            bad = {"p", "explore", "reel", "stories", "tv", "accounts", "invites", "oauth", "about", "developer", "legal"}
            handles = [m for m in re.findall(r"instagram\.com/([A-Za-z0-9_.]+)", content) if m.lower() not in bad and len(m) > 2]
            return (True, handles[0]) if handles else (False, None)
        except Exception:
            return False, None
        finally:
            await page.context.close()
