"""workers/job_runner.py — Runs scraping jobs in background threads."""
import asyncio
import logging
import threading
import re
import os
import random
import gc
import uuid
from datetime import datetime, timezone

from firebase_config import get_db
from llm.query_generator import generate_search_plan
from scrapers.gmaps_scraper import GMapsScraperV2
from scrapers.generic_scraper import GenericScraper
from enrichment.website_checker import check_websites_batch
from enrichment.scorer import score_leads_batch, apply_filters_batch, normalize_socials, apply_filters
from enrichment.profile_enricher import enrich_social_profiles

logger = logging.getLogger(__name__)

_active_jobs: dict[str, bool] = {}


def request_stop(job_id: str):
    _active_jobs[job_id] = True


def _should_stop(job_id: str) -> bool:
    return _active_jobs.get(job_id, False)


RENDER_FREE_TIER: bool = os.getenv("RENDER_FREE_TIER", "false").lower() == "true"
MAX_CONCURRENT_JOBS = 1 if RENDER_FREE_TIER else 2
_job_semaphore = threading.Semaphore(MAX_CONCURRENT_JOBS)

GMAPS_PAGE_CONCURRENCY: int = 1 if RENDER_FREE_TIER else 2
GENERIC_CONCURRENCY: int = 1
GMAPS_BATCH_SIZE: int = 1 if RENDER_FREE_TIER else 6
GENERIC_USE_BROWSER: bool = not RENDER_FREE_TIER
LEAD_BUFFER_FLUSH_SIZE = 50
ZERO_YIELD_ABORT = 5      # consecutive zero-yield combos before aborting a source

SOCIAL_SOURCES = {
    "instagram.com", "linkedin.com", "x.com", "twitter.com",
    "youtube.com", "behance.net", "medium.com", "substack.com",
    "fiverr.com", "upwork.com", "github.com", "producthunt.com",
}
DIRECTORY_SOURCES = {"crunchbase.com", "wellfound.com", "clutch.co"}


# ── Firestore helpers ─────────────────────────────────────────────────────────

def _log(db, user_id: str, job_id: str, message: str):
    try:
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        ts = datetime.now(ist).strftime("%H:%M:%S")
        from google.cloud import firestore as _fs
        ref = db.collection("users").document(user_id).collection("jobs").document(job_id)
        ref.update({"logs": _fs.ArrayUnion([f"[{ts}] {message}"]), "updated_at": datetime.now(timezone.utc)})
    except Exception as e:
        logger.debug(f"Log write failed: {e}")


def _log_error(db, user_id: str, job_id: str, source: str, query: str, city: str, error: str):
    _log(db, user_id, job_id, f"[ERROR] {source} / {query} / {city}: {error}")
    try:
        from google.cloud import firestore as _fs
        ref = db.collection("users").document(user_id).collection("jobs").document(job_id)
        ref.update({
            "errors": _fs.ArrayUnion([{"source": source, "query": query, "city": city, "error": str(error)}]),
            "updated_at": datetime.now(timezone.utc),
        })
    except Exception:
        pass


def _set_status(db, user_id: str, job_id: str, status: str, extra: dict = None):
    ref = db.collection("users").document(user_id).collection("jobs").document(job_id)
    update = {"status": status, "updated_at": datetime.now(timezone.utc)}
    if extra:
        update.update(extra)
    ref.set(update, merge=True)


def _save_leads(db, user_id: str, job_id: str, leads: list[dict], overwrite: bool = False):
    if not leads:
        return
    batch = db.batch()
    col = db.collection("users").document(user_id).collection("jobs").document(job_id).collection("leads")
    for lead in leads:
        ref = col.document(lead["id"])
        batch.set(ref, lead) if overwrite else batch.set(ref, lead, merge=True)
    try:
        batch.commit()
    except Exception as e:
        logger.error(f"Firestore batch write failed: {e}")


# ── Dedup helpers ─────────────────────────────────────────────────────────────

def _make_dedup_key(lead: dict) -> str:
    return (
        re.sub(r"[^a-z0-9]", "", (lead.get("name") or "").lower())
        + re.sub(r"[^0-9]", "", lead.get("phone") or "")[-7:]
        + re.sub(r"[^a-z]", "", (lead.get("city") or "").lower())
    )


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _name_similarity(a: str, b: str) -> float:
    """Bigram Jaccard similarity — fast, no external libraries."""
    def bigrams(s):
        return {s[i:i+2] for i in range(len(s) - 1)} if len(s) >= 2 else set()
    ba, bb = bigrams(a), bigrams(b)
    if not ba and not bb:
        return 1.0
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


def _merge_lead_pair(winner: dict, other: dict) -> dict:
    """Merge social/contact data from `other` into `winner`, keeping the richer values."""
    for field in ("email", "phone", "instagram_handle", "follower_count", "bio", "external_link"):
        if not winner.get(field) and other.get(field):
            winner[field] = other[field]
    if other.get("has_instagram"):
        winner["has_instagram"] = True
    # Merge social_links
    existing = set((winner.get("social_links") or "").split(", "))
    incoming = set((other.get("social_links") or "").split(", "))
    merged = sorted(existing | incoming - {""})
    if merged:
        winner["social_links"] = ", ".join(merged)
    # Keep higher score
    if (other.get("score") or 0) > (winner.get("score") or 0):
        winner["score"] = other["score"]
        winner["priority"] = other["priority"]
    return winner


def merge_cross_platform_leads(leads: list[dict]) -> list[dict]:
    """
    Group leads by (normalized name, city) similarity and merge duplicates.
    A lead found on Instagram AND LinkedIn for the same person becomes one
    richly-populated record instead of two sparse ones.

    Similarity threshold: 80% bigram Jaccard on normalized name, same city.
    """
    if not leads:
        return leads

    clusters: list[list[dict]] = []
    assigned = [False] * len(leads)

    for i, lead in enumerate(leads):
        if assigned[i]:
            continue
        cluster = [lead]
        assigned[i] = True
        ni = _norm_name(lead.get("name", ""))
        ci = re.sub(r"[^a-z]", "", (lead.get("city") or "").lower())

        for j in range(i + 1, len(leads)):
            if assigned[j]:
                continue
            other = leads[j]
            nj = _norm_name(other.get("name", ""))
            cj = re.sub(r"[^a-z]", "", (other.get("city") or "").lower())

            if ci != cj:
                continue
            if len(ni) < 4 or len(nj) < 4:
                continue
            if _name_similarity(ni, nj) >= 0.80:
                cluster.append(other)
                assigned[j] = True

        clusters.append(cluster)

    merged_leads = []
    for cluster in clusters:
        if len(cluster) == 1:
            merged_leads.append(cluster[0])
            continue
        # Winner = lead with highest score, or first if tied
        winner = max(cluster, key=lambda l: l.get("score") or 0)
        for other in cluster:
            if other is not winner:
                winner = _merge_lead_pair(winner, other)
        merged_leads.append(winner)

    return merged_leads


# ── Cache helpers ─────────────────────────────────────────────────────────────

CACHE_TTL_HOURS = 24


def _cache_key(query: str, city: str, source: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", f"{source}_{query}_{city}".lower())


def _get_cached_leads(db, key: str) -> list[dict] | None:
    try:
        doc = db.collection("lead_cache").document(key).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        age_hours = (datetime.now(timezone.utc) - data["cached_at"]).total_seconds() / 3600
        return data.get("leads", []) if age_hours <= CACHE_TTL_HOURS else None
    except Exception:
        return None


def _set_cached_leads(db, key: str, leads: list[dict]):
    try:
        db.collection("lead_cache").document(key).set({"leads": leads, "cached_at": datetime.now(timezone.utc)})
    except Exception as e:
        logger.debug(f"Cache write failed: {e}")


def _domain_from_url(url: str) -> str | None:
    from urllib.parse import urlparse
    host = urlparse(url or "").netloc.lower().replace("www.", "")
    return host or None


# ── Main async job ────────────────────────────────────────────────────────────

async def _run_async(user_id: str, job_id: str, user_query: str, sources: list[str]):
    db = get_db()
    log = lambda msg: _log(db, user_id, job_id, msg)
    stop = lambda: _should_stop(job_id)

    all_leads: list[dict] = []
    seen_hashes: set[str] = set()
    dedup_lock = asyncio.Lock()
    lead_buffer: list[dict] = []
    buffer_lock = asyncio.Lock()
    combos_done = [0]
    progress_lock = asyncio.Lock()
    gmaps_sem = asyncio.Semaphore(GMAPS_PAGE_CONCURRENCY)
    generic_sem = asyncio.Semaphore(GENERIC_CONCURRENCY)

    # #3: zero-yield streak tracking per source key
    zero_streak: dict[str, int] = {}
    streak_lock = asyncio.Lock()

    # #9: background enrichment queue
    enrich_queue: asyncio.Queue = asyncio.Queue()
    enrich_done = asyncio.Event()

    try:
        log("Analysing your request...")
        plan = generate_search_plan(user_query)

        cities = plan["cities"]
        queries = plan["search_queries"]
        web_queries = plan.get("web_queries") or queries[:6]
        lead_intent = plan.get("lead_intent", "physical")
        search_strategy = plan.get("search_strategy", "maps_first")
        active_filters = plan.get("filters") or []

        active_sources = list(sources) if sources else list(plan["sources"])
        if lead_intent in ("physical", "hybrid") and "gmaps" not in active_sources:
            active_sources.insert(0, "gmaps")

        max_leads = plan.get("max_leads", 30)
        max_areas = plan.get("max_areas", 5)

        _social_q_limit = 3 if RENDER_FREE_TIER else 5
        gmaps_combos_count = len(cities) * len(queries) if "gmaps" in active_sources else 0
        generic_sources = [s for s in active_sources if s != "gmaps"]
        generic_combos_count = sum(
            len(cities) * (min(len(queries), _social_q_limit) if s in SOCIAL_SOURCES else len(queries))
            for s in generic_sources
        )
        should_run_web = lead_intent in ("online", "hybrid") or search_strategy in ("web_first", "both")
        wq_limit = 3 if RENDER_FREE_TIER else 6
        web_combos_count = len(cities) * min(len(web_queries), wq_limit) if should_run_web else 0
        total_combos = max(1, gmaps_combos_count + generic_combos_count + web_combos_count)

        log(f"Plan: {len(cities)} cities · {len(queries)} queries · intent={lead_intent} · strategy={search_strategy}")
        log(f"Target: {max_leads} leads · {max_areas} areas/city · {total_combos} total combos (parallel)")
        if RENDER_FREE_TIER:
            log("Render free-tier mode: memory-safe concurrency (1 page at a time)")
        log(f"Sources: {', '.join(active_sources)}")
        log(f"Cities: {', '.join(cities)}")
        log(f"Queries: {', '.join(queries[:5])}{'...' if len(queries) > 5 else ''}")
        if active_filters:
            log(f"Filters (hard): {' · '.join(f.get('label', f.get('field', '?')) for f in active_filters)}")
        if plan.get("free_tier_cap_applied"):
            log("Free-tier cap applied: focused batch mode.")

        def _can_prefilter(f: dict) -> bool:
            field = f.get("field", "")
            if field in ("website_status", "has_https", "has_mobile_meta", "score", "priority", "follower_count"):
                return False
            return True

        pre_filters = [f for f in active_filters if _can_prefilter(f)] if active_filters else []

        random.shuffle(cities)
        random.shuffle(queries)
        _set_status(db, user_id, job_id, "running", {"plan": plan})

        # ── #9: Background enrichment worker ──────────────────────────────────
        # Leads are written to Firestore immediately (unscored) so the UI counter
        # ticks up in real-time. Enrichment (website check + profile) runs async
        # in a background coroutine and updates each lead when done.

        async def _enrich_worker():
            """Consume leads from enrich_queue, enrich them, then overwrite in Firestore."""
            while True:
                try:
                    item = await asyncio.wait_for(enrich_queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    if enrich_done.is_set() and enrich_queue.empty():
                        break
                    continue

                if item is None:  # sentinel
                    break

                batch_leads, intent = item
                try:
                    # Social profile enrichment
                    batch_leads = await enrich_social_profiles(batch_leads)

                    # Website enrichment
                    web_results = await check_websites_batch(batch_leads)
                    for lead in batch_leads:
                        result = web_results.get(lead.get("id"))
                        if result:
                            lead["website"] = result.url
                            lead["website_domain"] = _domain_from_url(result.url)
                            lead["website_status"] = result.status
                            lead["has_https"] = result.has_https
                            lead["has_mobile_meta"] = result.has_mobile_meta

                    # Score with enriched data
                    score_leads_batch(batch_leads, filters=active_filters)

                    # Filter gate
                    if active_filters:
                        batch_leads = apply_filters_batch(batch_leads, active_filters)

                    # Overwrite the already-written raw leads with enriched versions
                    await asyncio.to_thread(_save_leads, db, user_id, job_id, batch_leads, True)
                except Exception as e:
                    logger.error(f"Enrich worker error: {e}")
                finally:
                    enrich_queue.task_done()

        # Start the background enrichment worker
        enrich_task = asyncio.ensure_future(_enrich_worker())

        # ── Immediate write + enqueue for enrichment ───────────────────────────

        async def _flush_buffer(force: bool = False):
            async with buffer_lock:
                if lead_buffer and (force or len(lead_buffer) >= LEAD_BUFFER_FLUSH_SIZE):
                    snapshot = list(lead_buffer)
                    lead_buffer.clear()
                    await asyncio.to_thread(_save_leads, db, user_id, job_id, snapshot)

        async def _process_leads(raw_leads: list[dict], intent: str = lead_intent):
            if not raw_leads:
                return 0

            for lead in raw_leads:
                lead.setdefault("lead_intent", intent)
                lead.setdefault("id", str(uuid.uuid4()))
                normalize_socials(lead)

            if pre_filters:
                raw_leads = [l for l in raw_leads if apply_filters(l, pre_filters)]

            if not raw_leads:
                return 0

            # Dedup
            new_leads: list[dict] = []
            async with dedup_lock:
                ceiling = max_leads * (1.5 if active_filters else 1)
                for lead in raw_leads:
                    if len(all_leads) + len(new_leads) >= ceiling:
                        break
                    h = _make_dedup_key(lead)
                    if h not in seen_hashes:
                        seen_hashes.add(h)
                        new_leads.append(lead)

            if not new_leads:
                return 0

            # #9: Write raw leads immediately so UI count updates instantly
            await asyncio.to_thread(_save_leads, db, user_id, job_id, new_leads)
            async with buffer_lock:
                all_leads.extend(new_leads)

            # Queue for background enrichment
            await enrich_queue.put((new_leads, intent))

            return len(new_leads)

        async def _tick(label: str = "", yielded: int = 0):
            async with progress_lock:
                combos_done[0] += 1
                done, n = combos_done[0], len(all_leads)
            pct = int(done / total_combos * 100)
            log(f"[{pct}%] {label} — {n} leads ({done}/{total_combos} combos done)")
            return yielded

        # ── #3: Zero-yield streak check ───────────────────────────────────────

        async def _check_streak(source_key: str) -> bool:
            """Return True if this source should be aborted due to too many zero-yield combos."""
            if active_filters:
                return False
            async with streak_lock:
                return zero_streak.get(source_key, 0) >= ZERO_YIELD_ABORT

        async def _update_streak(source_key: str, yielded: int):
            if active_filters:
                return
            async with streak_lock:
                if yielded == 0:
                    zero_streak[source_key] = zero_streak.get(source_key, 0) + 1
                else:
                    zero_streak[source_key] = 0

        # ── GMaps phase ───────────────────────────────────────────────────────
        if "gmaps" in active_sources:
            log(f"--- GMaps phase: batches of {GMAPS_BATCH_SIZE} combos ---")
            gmaps = GMapsScraperV2(db=db, progress_cb=log, stop_flag=stop)
            await gmaps.start()
            try:
                async def _gmaps_combo(query: str, city: str):
                    if stop() or len(all_leads) >= max_leads:
                        return
                    if "online" in query.lower() and lead_intent == "online":
                        return
                    if await _check_streak("gmaps"):
                        return

                    ckey = _cache_key(query, city, "gmaps")
                    cached = await asyncio.to_thread(_get_cached_leads, db, ckey)
                    if cached:
                        n = await _process_leads(cached, intent=lead_intent)
                        await _tick(f"GMaps '{query}' in {city} [cached]", n)
                        await _update_streak("gmaps", n)
                        return

                    async with gmaps_sem:
                        if stop() or len(all_leads) >= max_leads:
                            return
                        remaining = max_leads - len(all_leads)
                        try:
                            raw = await gmaps.scrape_city(query, city, max_per_city=remaining, max_areas=max_areas)
                            if raw:
                                await asyncio.to_thread(_set_cached_leads, db, ckey, raw)
                        except Exception as e:
                            _log_error(db, user_id, job_id, "gmaps", query, city, str(e))
                            raw = []

                    n = await _process_leads(raw, intent=lead_intent)
                    await _tick(f"GMaps '{query}' in {city}", n)
                    await _update_streak("gmaps", n)

                gmaps_tasks = [_gmaps_combo(q, c) for c in cities for q in queries]
                for i in range(0, len(gmaps_tasks), GMAPS_BATCH_SIZE):
                    if stop() or len(all_leads) >= max_leads:
                        break
                    await asyncio.gather(*gmaps_tasks[i:i + GMAPS_BATCH_SIZE], return_exceptions=True)
                    gc.collect()

                no_web = [l for l in all_leads if not l.get("website") and l.get("source") == "Google Maps"]
                ig_limit = 5 if RENDER_FREE_TIER else 10
                if no_web and not stop():
                    log(f"Instagram lookup for {min(len(no_web), ig_limit)} leads...")
                    ig_sem = asyncio.Semaphore(1 if RENDER_FREE_TIER else 3)

                    async def _fetch_ig(lead: dict):
                        if stop():
                            return
                        async with ig_sem:
                            has_ig, handle = await gmaps.find_instagram(lead["name"], lead.get("city", ""))
                        if has_ig:
                            lead["has_instagram"] = True
                            lead["instagram_handle"] = handle
                            lead["lead_type"] = "No website, Instagram found"
                            lead["confidence"] = max(lead.get("confidence") or 0, 75)

                    await asyncio.gather(*[_fetch_ig(l) for l in no_web[:ig_limit]], return_exceptions=True)
                    score_leads_batch(no_web[:ig_limit])
                    await asyncio.to_thread(_save_leads, db, user_id, job_id, no_web[:ig_limit], True)
            finally:
                await gmaps.stop()
                gc.collect()

        # ── Generic / social web phase ────────────────────────────────────────
        run_generic = bool(generic_sources) or should_run_web
        if run_generic and not stop() and len(all_leads) < max_leads:
            log("--- Generic web phase (httpx/DDG mode on free tier) ---" if RENDER_FREE_TIER else "--- Generic web phase: parallel ---")
            gen = GenericScraper(progress_cb=log, stop_flag=stop)
            if GENERIC_USE_BROWSER:
                await gen.start()
            try:
                async def _generic_combo(source: str, query: str, city: str):
                    if stop() or len(all_leads) >= max_leads:
                        return
                    if await _check_streak(source):
                        return
                    async with generic_sem:
                        if stop() or len(all_leads) >= max_leads:
                            return
                        remaining = max_leads - len(all_leads)
                        try:
                            raw = await asyncio.wait_for(
                                gen.scrape_domain(domain=source, query=query, city=city, max_leads=remaining),
                                timeout=120
                            )
                        except asyncio.TimeoutError:
                            _log_error(db, user_id, job_id, source, query, city, "Timeout reached (120s)")
                            raw = []
                        except Exception as e:
                            _log_error(db, user_id, job_id, source, query, city, str(e))
                            raw = []
                    n = await _process_leads(raw, intent=lead_intent)
                    await _tick(f"{source} '{query}' in {city}", n)
                    await _update_streak(source, n)

                async def _social_combo(platform: str, query: str, city: str):
                    if stop() or len(all_leads) >= max_leads:
                        return
                    if await _check_streak(platform):
                        return

                    ckey = _cache_key(query, city, platform)
                    cached = await asyncio.to_thread(_get_cached_leads, db, ckey)
                    if cached:
                        platform_keyword = platform.replace(".com", "").replace(".", "")
                        for lead in cached:
                            lead["source"] = platform_keyword.capitalize()
                        n = await _process_leads(cached, intent=lead_intent)
                        await _tick(f"{platform} '{query}' in {city} [cached]", n)
                        await _update_streak(platform, n)
                        return

                    async with generic_sem:
                        if stop() or len(all_leads) >= max_leads:
                            return
                        remaining = max_leads - len(all_leads)
                        platform_keyword = platform.replace(".com", "").replace(".", "")
                        try:
                            raw = await asyncio.wait_for(
                                gen.scrape_social(platform=platform, query=query, city=city, max_leads=remaining),
                                timeout=120
                            )
                            for lead in raw:
                                lead["source"] = platform_keyword.capitalize()
                            if raw:
                                await asyncio.to_thread(_set_cached_leads, db, ckey, raw)
                        except asyncio.TimeoutError:
                            _log_error(db, user_id, job_id, platform, query, city, "Timeout reached (120s)")
                            raw = []
                        except Exception as e:
                            _log_error(db, user_id, job_id, platform, query, city, str(e))
                            raw = []
                    n = await _process_leads(raw, intent=lead_intent)
                    await _tick(f"{platform} '{query}' in {city}", n)
                    await _update_streak(platform, n)

                async def _web_combo(query: str, city: str):
                    if stop() or len(all_leads) >= max_leads:
                        return
                    if await _check_streak("web"):
                        return
                    async with generic_sem:
                        if stop() or len(all_leads) >= max_leads:
                            return
                        remaining = max_leads - len(all_leads)
                        try:
                            raw = await asyncio.wait_for(
                                gen.scrape_web(query=query, city="", max_leads=remaining),
                                timeout=120
                            )
                        except asyncio.TimeoutError:
                            _log_error(db, user_id, job_id, "web", query, city, "Timeout reached (120s)")
                            raw = []
                        except Exception as e:
                            _log_error(db, user_id, job_id, "web", query, city, str(e))
                            raw = []
                    n = await _process_leads(raw, intent=lead_intent)
                    await _tick(f"Web '{query[:40]}' in {city}", n)
                    await _update_streak("web", n)

                social_q_limit = 3 if RENDER_FREE_TIER else 5
                all_tasks: list = []
                for src in generic_sources:
                    if src in SOCIAL_SOURCES:
                        for q in queries[:social_q_limit]:
                            for c in cities:
                                all_tasks.append(_social_combo(src, q, c))
                    else:
                        for q in queries:
                            for c in cities:
                                all_tasks.append(_generic_combo(src, q, c))

                if should_run_web:
                    for q in web_queries[:wq_limit]:
                        for c in cities:
                            all_tasks.append(_web_combo(q, c))

                generic_batch_size = 1 if RENDER_FREE_TIER else 3
                for i in range(0, len(all_tasks), generic_batch_size):
                    if stop() or len(all_leads) >= max_leads:
                        break
                    await asyncio.gather(*all_tasks[i:i + generic_batch_size], return_exceptions=True)
                    if i + generic_batch_size < len(all_tasks):
                        await asyncio.sleep(0.5)
            finally:
                if GENERIC_USE_BROWSER:
                    await gen.stop()
                gc.collect()

        # ── #9: Wait for background enrichment to drain ───────────────────────
        enrich_done.set()
        log("Enriching leads in background...")
        await enrich_queue.join()
        await enrich_queue.put(None)  # sentinel to stop worker
        await enrich_task

        # ── #2: Cross-platform dedup & merge ─────────────────────────────────
        before = len(all_leads)
        all_leads[:] = merge_cross_platform_leads(all_leads)
        merged_away = before - len(all_leads)
        if merged_away > 0:
            log(f"Cross-platform merge: {merged_away} duplicates merged into richer records")
            # Persist merged winners (overwrite) — orphaned leads will age out naturally
            await asyncio.to_thread(_save_leads, db, user_id, job_id, all_leads, True)

        if stop():
            log("Job was stopped by user.")
            _set_status(db, user_id, job_id, "stopped", {"leads_count": len(all_leads)})
            return

        hot = sum(1 for l in all_leads if l.get("priority") == "Hot")
        warm = sum(1 for l in all_leads if l.get("priority") == "Warm")
        log(f"✓ Complete — {len(all_leads)} leads found: {hot} Hot, {warm} Warm")
        _set_status(db, user_id, job_id, "done", {"leads_count": len(all_leads), "hot_count": hot, "warm_count": warm})

    except Exception as e:
        logger.exception(e)
        log(f"Job error: {e}")
        _set_status(db, user_id, job_id, "error", {"error": str(e)})
    finally:
        _active_jobs.pop(job_id, None)


def _run_job_thread(user_id: str, job_id: str, user_query: str, sources: list[str]):
    db = get_db()
    if _job_semaphore._value == 0:
        _log(db, user_id, job_id, f"Server at capacity ({MAX_CONCURRENT_JOBS}/{MAX_CONCURRENT_JOBS} jobs). You are in the queue...")
    with _job_semaphore:
        _set_status(db, user_id, job_id, "running")
        _log(db, user_id, job_id, "Server slot acquired — starting job...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run_async(user_id, job_id, user_query, sources))
        except Exception as e:
            logger.error(f"Worker thread error: {e}")
        finally:
            loop.close()


def start_job(user_id: str, job_id: str, user_query: str, sources: list[str]):
    _active_jobs[job_id] = False
    db = get_db()
    _set_status(db, user_id, job_id, "queued")
    _log(db, user_id, job_id, "Job queued. Allocating server resources...")
    threading.Thread(target=_run_job_thread, args=(user_id, job_id, user_query, sources), daemon=True).start()
