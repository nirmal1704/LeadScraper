"""
scrapers/gmaps_scraper.py
─────────────────────────
Google Maps scraper with:
  - Dynamic area discovery (Firestore sliding window + LLM bootstrap for unknown cities)
  - Category and open_now extraction from the details panel
  - Improved permanently-closed filtering (card + panel + aria)
  - Retry on navigation timeout before dropping a listing
  - Phonetically normalized deduplication
  - Instagram discovery via Google search
"""

import asyncio
import re
import random
import logging
import uuid
from urllib.parse import quote, urlparse
from typing import Optional

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from llm.area_bootstrapper import bootstrap_city_areas, STATIC_CITY_AREAS

logger = logging.getLogger(__name__)

GMAPS_URL = "https://www.google.com/maps/search/{query}+in+{location}?hl=en&gl=in"
GOOGLE_SEARCH_URL = "https://www.google.com/search?q={query}&hl=en&gl=in"


# ─── Area Helpers ──────────────────────────────────────────────────────────────

def _get_seed_areas(city: str, db=None) -> list[str]:
    """
    Return seed areas for a city.
    Priority: static dict → Firestore cache → LLM bootstrap.
    LLM is only called for cities not in STATIC_CITY_AREAS.
    """
    if city in STATIC_CITY_AREAS:
        return list(STATIC_CITY_AREAS[city])
    # bootstrap_city_areas checks Firestore first, then LLM
    return bootstrap_city_areas(city, db=db)


def _extract_areas_from_address(address: str, city: str) -> list[str]:
    """Discover new neighbourhood names from a scraped address string."""
    if not address or city.lower() not in address.lower():
        return []
    cleaned = re.sub(re.escape(city), "", address, flags=re.I).strip(", ")
    parts = [p.strip() for p in cleaned.split(",")]
    STATE_WORDS = {
        "india", "maharashtra", "karnataka", "delhi", "tamil nadu",
        "west bengal", "gujarat", "rajasthan", "uttar pradesh",
        "telangana", "andhra pradesh", "kerala", "madhya pradesh",
        "haryana", "punjab", "bihar", "jharkhand", "odisha",
    }
    areas = []
    for part in parts:
        words = part.split()
        if (1 <= len(words) <= 4
                and not re.match(r"^\d+", part)
                and len(part) > 4
                and part.lower() not in STATE_WORDS):
            areas.append(part)
    return areas[:2]


# ─── Utility ───────────────────────────────────────────────────────────────────

def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _display_domain(url: str) -> Optional[str]:
    if not url:
        return None
    host = urlparse(url).netloc.lower().replace("www.", "")
    return host or None


def _dedup_key(name: str, phone: str, city: str) -> str:
    """Phonetic dedup key — tolerates accents, symbols, spacing variants."""
    norm_name = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    norm_phone = re.sub(r"[^0-9]", "", phone or "")[-7:]  # last 7 digits
    norm_city = re.sub(r"[^a-z]", "", (city or "").lower())
    return f"{norm_name}{norm_phone}{norm_city}"


_CLOSED_SIGNALS = {"permanently closed", "temporarily closed"}


def _has_closed_signal(text: str) -> bool:
    t = text.lower()
    return any(s in t for s in _CLOSED_SIGNALS)


# ─── Scraper ───────────────────────────────────────────────────────────────────

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
                # Reduce JS heap to absolute minimum
                "--js-flags=--max-old-space-size=32",
                # Single renderer process — biggest RAM saver in containers
                "--renderer-process-limit=1",
                # Kill background networking and prefetch
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-plugins",
                "--disable-sync",
                "--no-first-run",
                "--mute-audio",
                # Disable features that leak RAM
                "--disable-features=TranslateUI,BlinkGenPropertyTrees,IsolateOrigins,site-per-process",
                "--disable-site-isolation-trials",
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
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
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

        # Block images/fonts to save RAM; keep CSS (blocking it freezes GMaps)
        await page.route(
            "**/*.{png,jpg,jpeg,woff,woff2,gif,webp,svg}",
            lambda route: route.abort()
        )
        # Block heavy 3D vector tiles (the map itself) — massive RAM saving
        await page.route("**/maps/vt/**", lambda route: route.abort())
        await page.route("**/maps/viewer/**", lambda route: route.abort())
        return page

    # ── Public: scrape a city for a query ───────────────────────────────────

    async def scrape_city(
        self,
        query: str,
        city: str,
        max_per_city: int = 50,
        max_areas: int = 6,
    ) -> list[dict]:
        """Scrape a city using area-level searches with dynamic discovery."""
        all_leads: list[dict] = []
        seen_hashes: set[str] = set()
        discovered: set[str] = set()

        # Load known areas — static dict first, then Firestore, then LLM bootstrap
        known_areas = set(_get_seed_areas(city, db=self.db))

        # Also pull any previously discovered areas from Firestore
        if self.db:
            try:
                doc = (
                    self.db.collection("geography")
                           .document("india")
                           .collection("cities")
                           .document(city)
                           .get()
                )
                if doc.exists:
                    saved = doc.to_dict().get("areas", [])
                    known_areas.update(saved)
            except Exception as e:
                logger.error(f"Failed to load Firestore areas for {city}: {e}")

        area_queue = list(known_areas)
        random.shuffle(area_queue)
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

            remaining = max_per_city - len(all_leads)
            leads, new_areas = await self._scrape_area(
                query, city, area, seen_hashes, limit=remaining
            )
            all_leads.extend(leads)

            # Queue newly discovered areas (from addresses)
            new_discovered = []
            for a in new_areas:
                if a not in searched and a not in discovered and a not in known_areas:
                    discovered.add(a)
                    area_queue.append(a)
                    new_discovered.append(a)

            # Persist discovered areas to Firestore (background thread, non-blocking)
            if new_discovered and self.db:
                def _save_areas(_new=new_discovered, _city=city):
                    try:
                        ref = (
                            self.db.collection("geography")
                                   .document("india")
                                   .collection("cities")
                                   .document(_city)
                        )
                        doc = ref.get()
                        existing = doc.to_dict().get("areas", list(_get_seed_areas(_city))) if doc.exists else list(_get_seed_areas(_city))
                        for a in _new:
                            if a not in existing:
                                existing.append(a)
                        # Keep newest 15 — sliding window for fresh results
                        ref.set({"areas": existing[-15:]}, merge=True)
                    except Exception as exc:
                        logger.error(f"Failed to persist areas for {_city}: {exc}")

                import threading
                threading.Thread(target=_save_areas, daemon=True).start()

            import gc
            gc.collect()

            if len(all_leads) >= max_per_city:
                break

        return all_leads[:max_per_city]

    # ── Area-level scrape ────────────────────────────────────────────────────

    async def _scrape_area(
        self, query: str, city: str, area: str, seen: set, limit: int = 20
    ) -> tuple[list[dict], list[str]]:
        location_str = f"{area}, {city}"
        url = GMAPS_URL.format(query=quote(query), location=quote(location_str))
        self.progress(f"Searching '{query}' in {location_str}")
        leads, new_areas = [], []
        page = await self._new_page()

        try:
            # Navigation with one retry on timeout
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                self.progress(f"  Retry navigation for {area}...")
                await asyncio.sleep(2)
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            await asyncio.sleep(random.uniform(2.5, 3.5))

            # Scroll the results panel to load more listings
            panel = await page.query_selector('[role="feed"]')
            if panel:
                for _ in range(6):
                    await panel.evaluate("el => el.scrollTop += 800")
                    await asyncio.sleep(random.uniform(0.6, 1.0))

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
                await asyncio.sleep(random.uniform(0.4, 0.8))

        except Exception as e:
            self.progress(f"  GMaps error in {area}: {e}")
        finally:
            await page.context.close()

        return leads, list(set(new_areas))

    # ── Detail panel extraction ──────────────────────────────────────────────

    async def _extract(self, page, listing, city: str, area: str, query: str):
        try:
            card_text = (await listing.inner_text()).strip()
            card_name = card_text.splitlines()[0].strip() if card_text else ""

            # ── Closed check on card (fast, before any click) ────────────────
            if _has_closed_signal(card_text):
                return None, []

            # ── Click the card to open the details panel ──────────────────────
            await listing.click(force=True, position={"x": 15, "y": 15})

            # ── Wait for the panel to show the correct business name ──────────
            name = ""
            for _ in range(15):
                for sel in ["h1.DUwDvf", ".fontHeadlineLarge", "h1.qAWA2"]:
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
                return None, []

            # ── Relaxed name match ────────────────────────────────────────────
            card_norm = _normalize_text(card_name)
            panel_norm = _normalize_text(name)
            name_matches_card = (
                card_norm in panel_norm or panel_norm in card_norm or
                (len(card_norm) > 5 and card_norm[:int(len(card_norm) * 0.6)] in panel_norm) or
                (len(panel_norm) > 5 and panel_norm[:int(len(panel_norm) * 0.6)] in card_norm)
            )
            if not name_matches_card:
                return None, []

            # ── Closed check on panel (full text) ────────────────────────────
            panel_text = ""
            try:
                panel_text = (await page.inner_text("div.m6QErb", timeout=1500)).lower()
            except Exception:
                pass
            if _has_closed_signal(panel_text):
                return None, []

            # ── Check permanently_closed flag via aria ────────────────────────
            permanently_closed = False
            try:
                perm_el = await page.query_selector('[aria-label*="Permanently closed"]')
                if perm_el:
                    permanently_closed = True
                    return None, []
            except Exception:
                pass

            # ── Phone ────────────────────────────────────────────────────────
            phone = ""
            phone_el = await page.query_selector('[data-item-id*="phone"] .Io6YTe')
            if phone_el:
                phone = re.sub(r"[^\d+\-\s]", "", (await phone_el.inner_text()).strip())

            # ── Address ──────────────────────────────────────────────────────
            address = ""
            addr_el = await page.query_selector('[data-item-id="address"] .Io6YTe')
            if addr_el:
                address = (await addr_el.inner_text()).strip()
            new_areas = _extract_areas_from_address(address, city)

            # ── Rating & reviews ─────────────────────────────────────────────
            rating, review_count = None, None
            rating_el = await page.query_selector('.F7nice span[aria-label*="star"]')
            if rating_el:
                aria = await rating_el.get_attribute("aria-label") or ""
                m = re.search(r"([\d.]+)\s+star", aria)
                if m:
                    rating = float(m.group(1))
            review_el = await page.query_selector('.F7nice span[aria-label*="review"]')
            if review_el:
                aria = await review_el.get_attribute("aria-label") or ""
                m = re.search(r"([\d,]+)\s+review", aria)
                if m:
                    review_count = int(m.group(1).replace(",", ""))

            # ── Category ─────────────────────────────────────────────────────
            category = None
            for cat_sel in [".DkEaL", ".y7PRA", "button.DkEaL"]:
                cat_el = await page.query_selector(cat_sel)
                if cat_el:
                    cat_text = (await cat_el.inner_text()).strip()
                    if cat_text and len(cat_text) < 60:
                        category = cat_text
                        break

            # ── Open/Closed status ───────────────────────────────────────────
            open_now = None
            try:
                hours_el = await page.query_selector(".o0Svhf")  # "Open now" / "Closed"
                if hours_el:
                    hours_text = (await hours_el.inner_text()).lower()
                    if "open now" in hours_text:
                        open_now = True
                    elif "closed" in hours_text:
                        open_now = False
            except Exception:
                pass

            # ── Website & social links ────────────────────────────────────────
            website = ""
            social_links = []

            for web_sel in ['a[data-item-id="authority"]', '[data-item-id="authority"] a']:
                web_el = await page.query_selector(web_sel)
                if web_el:
                    href = await web_el.get_attribute("href") or ""
                    if href and "google.com/maps" not in href and "google.com/search" not in href:
                        lower_href = href.lower()
                        if any(d in lower_href for d in [
                            "justdial.com", "facebook.com", "instagram.com",
                            "linkedin.com", "linktr.ee", "twitter.com", "x.com",
                            "wa.me", "whatsapp.com", "youtube.com",
                        ]):
                            social_links.append(href)
                        else:
                            website = href
                        break

            # Also scrape profile links section
            try:
                profiles = await page.query_selector_all(
                    'a[href*="instagram.com"], a[href*="facebook.com"], '
                    'a[href*="linkedin.com"], a[href*="youtube.com"], '
                    'a[href*="justdial.com"]'
                )
                for p in profiles:
                    href = await p.get_attribute("href")
                    if href and "google.com" not in href and href not in social_links and href != website:
                        if any(d in href.lower() for d in [
                            "justdial", "facebook", "instagram", "linkedin",
                            "linktr", "twitter", "x.com", "wa.me", "whatsapp", "youtube"
                        ]):
                            social_links.append(href)
            except Exception:
                pass

            # ── Confidence scoring ────────────────────────────────────────────
            confidence = 55
            evidence = []
            if name_matches_card:
                confidence += 25
                evidence.append("name matched card")
            if phone:
                confidence += 8
                evidence.append("phone found")
            if address and city.lower() in address.lower():
                confidence += 8
                evidence.append("city in address")
            if website:
                confidence += 4
                evidence.append("website found")
            if category:
                confidence += 5
                evidence.append(f"category: {category}")
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
                "category": category,
                "open_now": open_now,
                "permanently_closed": permanently_closed,
                "source": "Google Maps",
                "lead_type": "Website found" if website else "No website on Google Maps",
                "confidence": confidence,
                "evidence": "; ".join(evidence),
                # Enrichment fields (filled later by pipeline)
                "website_status": None,
                "has_https": None,
                "has_mobile_meta": None,
                "social_links": ", ".join(social_links),
                "has_instagram": any("instagram.com" in s for s in social_links),
                "instagram_handle": next(
                    (s.split("instagram.com/")[1].strip("/")
                     for s in social_links if "instagram.com/" in s),
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

    # ── Instagram finder ─────────────────────────────────────────────────────

    async def find_instagram(self, name: str, city: str) -> tuple[bool, Optional[str]]:
        """
        Find Instagram handle via Google search (much more reliable than direct scraping).
        Searches: "{name} {city} instagram"
        """
        query = quote(f"{name} {city} instagram")
        url = GOOGLE_SEARCH_URL.format(query=query)
        page = await self._new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.5, 2.5))
            content = await page.content()
            matches = re.findall(r"instagram\.com/([A-Za-z0-9_.]+)", content)
            bad_words = {
                "p", "explore", "reel", "stories", "tv", "accounts",
                "invites", "oauth", "about", "developer", "legal",
            }
            handles = [m for m in matches if m.lower() not in bad_words and len(m) > 2]
            if handles:
                return True, handles[0]
            return False, None
        except Exception:
            return False, None
        finally:
            await page.context.close()
