"""
workers/job_runner.py
─────────────────────
Runs a full scraping job in a background thread.

Architecture:
  ┌── Thread (isolated event loop per job) ─────────────────────────────┐
  │  1. LLM plan generation                                              │
  │  2. GMaps phase — asyncio.gather over ALL (query × city) combos     │
  │     └─ Semaphore controls concurrent page count (RAM guard)          │
  │  3. Generic phase — asyncio.gather over ALL (source × query × city) │
  │     └─ Separate semaphore; httpx fallback uses no browser RAM        │
  │  4. IG enrichment — parallel (Semaphore=3), post-GMaps phase        │
  │  5. Buffered Firestore writes (flush every 50 leads)                 │
  └──────────────────────────────────────────────────────────────────────┘

RENDER_FREE_TIER env var:
  true  → GMaps page concurrency = 1 (safe under 512MB RAM)
  false → GMaps page concurrency = 2 (faster, needs ~400MB free)
"""

import asyncio
import logging
import threading
import re
import os
import random
from datetime import datetime, timezone

from firebase_config import get_db
from llm.query_generator import generate_search_plan
from scrapers.gmaps_scraper import GMapsScraperV2
from scrapers.generic_scraper import GenericScraper
from enrichment.website_checker import check_websites_batch
from enrichment.scorer import score_leads_batch, apply_filters_batch

logger = logging.getLogger(__name__)

# ── Job Registry ──────────────────────────────────────────────────────────────

_active_jobs: dict[str, bool] = {}    # job_id → stop_requested


def request_stop(job_id: str):
    _active_jobs[job_id] = True


def _should_stop(job_id: str) -> bool:
    return _active_jobs.get(job_id, False)


# ── Configuration ─────────────────────────────────────────────────────────────

RENDER_FREE_TIER: bool = os.getenv("RENDER_FREE_TIER", "false").lower() == "true"

# Free tier: 1 job max (one Chromium browser fits in 512MB, two do not)
# Full tier: 2 concurrent jobs
MAX_CONCURRENT_JOBS = 1 if RENDER_FREE_TIER else 2
_job_semaphore = threading.Semaphore(MAX_CONCURRENT_JOBS)

# Concurrent Playwright pages for GMaps.
# Free tier: 1 page, processed in small sequential batches
# Full tier: 2 pages
GMAPS_PAGE_CONCURRENCY: int = 1 if RENDER_FREE_TIER else 2
GENERIC_CONCURRENCY: int = 1

# How many (query × city) combos to fan-out at once.
# Prevents hundreds of coroutines all trying to open a page simultaneously.
GMAPS_BATCH_SIZE: int = 3 if RENDER_FREE_TIER else 6

# On free tier: skip launching a second Chromium for GenericScraper.
# GenericScraper will fall back to httpx (DDG HTML / ddgs) automatically.
GENERIC_USE_BROWSER: bool = not RENDER_FREE_TIER

# Firestore write buffer — commit every N leads to cut round-trips
LEAD_BUFFER_FLUSH_SIZE = 50


# ── Firestore helpers ─────────────────────────────────────────────────────────

def firestore_array_union(value: str):
    from google.cloud import firestore
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    ts = datetime.now(ist).strftime("%H:%M:%S")
    return firestore.ArrayUnion([f"[{ts}] {value}"])


def _log(db, user_id: str, job_id: str, message: str):
    """Append a log line to Firestore (frontend onSnapshot fires immediately)."""
    try:
        ref = db.collection("users").document(user_id).collection("jobs").document(job_id)
        ref.update({
            "logs": firestore_array_union(message),
            "updated_at": datetime.now(timezone.utc),
        })
    except Exception as e:
        logger.debug(f"Log write failed: {e}")


def _set_status(db, user_id: str, job_id: str, status: str, extra: dict = None):
    ref = db.collection("users").document(user_id).collection("jobs").document(job_id)
    update = {"status": status, "updated_at": datetime.now(timezone.utc)}
    if extra:
        update.update(extra)
    ref.set(update, merge=True)


def _save_leads(db, user_id: str, job_id: str, leads: list[dict], overwrite: bool = False):
    """Batch-write leads to Firestore."""
    if not leads:
        return
    batch = db.batch()
    col = (db.collection("users").document(user_id)
             .collection("jobs").document(job_id)
             .collection("leads"))
    for lead in leads:
        ref = col.document(lead["id"])
        if overwrite:
            batch.set(ref, lead)
        else:
            batch.set(ref, lead, merge=True)
    try:
        batch.commit()
    except Exception as e:
        logger.error(f"Firestore batch write failed: {e}")


# ── Dedup ─────────────────────────────────────────────────────────────────────

def _make_dedup_key(lead: dict) -> str:
    """
    Phonetically normalized dedup key.
    Tolerates: accent variants, symbol differences, spacing, phone formatting.
    E.g. "Café Mocha" and "Cafe Mocha" get the same key.
    """
    norm_name = re.sub(r"[^a-z0-9]", "", (lead.get("name") or "").lower())
    norm_phone = re.sub(r"[^0-9]", "", lead.get("phone") or "")[-7:]
    norm_city = re.sub(r"[^a-z]", "", (lead.get("city") or "").lower())
    return f"{norm_name}{norm_phone}{norm_city}"


def _domain_from_url(url: str) -> str | None:
    from urllib.parse import urlparse
    host = urlparse(url or "").netloc.lower().replace("www.", "")
    return host or None


# ── Main async job ────────────────────────────────────────────────────────────

async def _run_async(user_id: str, job_id: str, user_query: str, sources: list[str]):
    db = get_db()
    log = lambda msg: _log(db, user_id, job_id, msg)
    stop = lambda: _should_stop(job_id)

    # ── Shared mutable state (asyncio-safe with locks) ────────────────────
    all_leads: list[dict] = []
    seen_hashes: set[str] = set()
    dedup_lock = asyncio.Lock()

    lead_buffer: list[dict] = []
    buffer_lock = asyncio.Lock()

    combos_done = [0]          # list for closure mutability
    progress_lock = asyncio.Lock()

    gmaps_sem = asyncio.Semaphore(GMAPS_PAGE_CONCURRENCY)
    generic_sem = asyncio.Semaphore(GENERIC_CONCURRENCY)

    try:
        # ── 1. LLM plan ───────────────────────────────────────────────────
        log("Analysing your request...")
        plan = generate_search_plan(user_query)

        cities = plan["cities"]
        queries = plan["search_queries"]
        web_queries = plan.get("web_queries") or queries[:6]
        lead_intent = plan.get("lead_intent", "physical")
        search_strategy = plan.get("search_strategy", "maps_first")
        active_filters = plan.get("filters") or []

        # Respect the LLM's source decision — don't blindly force gmaps
        if sources:  # caller-override (from API)
            active_sources = list(sources)
        else:
            active_sources = list(plan["sources"])
        # Only add gmaps if the plan calls for it (physical/hybrid)
        if lead_intent in ("physical", "hybrid") and "gmaps" not in active_sources:
            active_sources.insert(0, "gmaps")

        max_leads = plan.get("max_leads", 30)
        max_areas = plan.get("max_areas", 5)

        # Total work units (for progress reporting)
        gmaps_combos_count = len(cities) * len(queries) if "gmaps" in active_sources else 0
        generic_sources = [s for s in active_sources if s != "gmaps"]
        generic_combos_count = len(generic_sources) * len(cities) * len(queries)
        should_run_web_search = lead_intent in ("online", "hybrid") or search_strategy in ("web_first", "both")
        web_combos_count = len(cities) * min(len(web_queries), 6) if should_run_web_search else 0
        total_combos = max(1, gmaps_combos_count + generic_combos_count + web_combos_count)

        log(f"Plan: {len(cities)} cities · {len(queries)} queries · intent={lead_intent} · strategy={search_strategy}")
        log(f"Target: {max_leads} leads · {max_areas} areas/city · {total_combos} total combos (parallel)")
        if RENDER_FREE_TIER:
            log("Render free-tier mode: memory-safe concurrency (1 page at a time)")
        log(f"Sources: {', '.join(active_sources)}")
        log(f"Cities: {', '.join(cities)}")
        log(f"Queries: {', '.join(queries[:5])}{'...' if len(queries) > 5 else ''}")
        if active_filters:
            filter_labels = " · ".join(f.get("label", f.get("field", "?")) for f in active_filters)
            log(f"Filters (hard): {filter_labels}")

        if plan.get("free_tier_cap_applied"):
            log("Free-tier cap applied: focused batch mode.")

        random.shuffle(cities)
        random.shuffle(queries)
        _set_status(db, user_id, job_id, "running", {"plan": plan})


        # ── Helper: flush lead buffer to Firestore ────────────────────────
        async def _flush_buffer(force: bool = False):
            async with buffer_lock:
                if lead_buffer and (force or len(lead_buffer) >= LEAD_BUFFER_FLUSH_SIZE):
                    batch_snapshot = list(lead_buffer)
                    lead_buffer.clear()
                    # Run synchronous Firestore write in a thread to not block event loop
                    await asyncio.to_thread(_save_leads, db, user_id, job_id, batch_snapshot)

        # ── Helper: process a raw batch (dedup → website → score → filter → buffer) ─
        async def _process_leads(raw_leads: list[dict], intent: str = lead_intent):
            if not raw_leads:
                return

            # Tag each lead with the intent so scorer can adapt
            for lead in raw_leads:
                lead.setdefault("lead_intent", intent)

            # Dedup — atomic check-and-add under asyncio lock
            new_leads: list[dict] = []
            async with dedup_lock:
                # Use a higher ceiling for dedup so filters don't starve us
                dedup_ceiling = max_leads * (3 if active_filters else 1)
                for lead in raw_leads:
                    if len(all_leads) + len(new_leads) >= dedup_ceiling:
                        break
                    h = _make_dedup_key(lead)
                    if h not in seen_hashes:
                        seen_hashes.add(h)
                        new_leads.append(lead)

            if not new_leads:
                return

            # Website enrichment (already concurrent internally)
            website_results = await check_websites_batch(new_leads)
            for lead in new_leads:
                result = website_results.get(lead.get("id"))
                if result:
                    lead["website"] = result.url
                    lead["website_domain"] = _domain_from_url(result.url)
                    lead["website_status"] = result.status
                    lead["has_https"] = result.has_https
                    lead["has_mobile_meta"] = result.has_mobile_meta

            # Score (filter-aware: scoring adapts when filters present)
            score_leads_batch(new_leads, filters=active_filters)

            # Hard filter gate — drop non-matching leads before buffering
            if active_filters:
                before = len(new_leads)
                new_leads = apply_filters_batch(new_leads, active_filters)
                dropped = before - len(new_leads)
                if dropped:
                    logger.debug(f"Filter dropped {dropped}/{before} leads")

            # Only count/buffer leads that passed the filter
            matching = [l for l in new_leads if len(all_leads) < max_leads]
            if not matching:
                return

            # Add to shared state + buffer (under lock)
            async with buffer_lock:
                all_leads.extend(matching)
                lead_buffer.extend(matching)

            # Non-blocking flush
            await _flush_buffer()

        # ── Helper: log progress after each combo completes ───────────────
        async def _tick(label: str = ""):
            async with progress_lock:
                combos_done[0] += 1
                done = combos_done[0]
                n = len(all_leads)
            pct = int(done / total_combos * 100)
            log(f"[{pct}%] {label} — {n} leads ({done}/{total_combos} combos done)")

        # ── 2. GMaps Phase (batched fan-out to control RAM) ──────────────
        if "gmaps" in active_sources:
            log(f"--- GMaps phase: batches of {GMAPS_BATCH_SIZE} combos ---")
            gmaps_scraper = GMapsScraperV2(db=db, progress_cb=log, stop_flag=stop)
            await gmaps_scraper.start()
            try:
                async def _gmaps_combo(query: str, city: str):
                    """One atomic (query, city) scraping unit."""
                    if stop() or len(all_leads) >= max_leads:
                        return
                    if "online" in query.lower() and lead_intent == "online":
                        return

                    async with gmaps_sem:
                        if stop() or len(all_leads) >= max_leads:
                            return
                        remaining = max(5, max_leads - len(all_leads))
                        per_combo = max(5, max_leads // max(1, gmaps_combos_count))
                        target = min(remaining, per_combo + 3)

                        raw = await gmaps_scraper.scrape_city(
                            query, city,
                            max_per_city=target,
                            max_areas=max_areas,
                        )

                    await _process_leads(raw, intent=lead_intent)
                    await _tick(f"GMaps '{query}' in {city}")

                # Process in small batches instead of one massive gather
                # — prevents GMAPS_BATCH_SIZE × pages being open simultaneously
                gmaps_tasks = [
                    _gmaps_combo(q, c)
                    for c in cities
                    for q in queries
                ]
                for i in range(0, len(gmaps_tasks), GMAPS_BATCH_SIZE):
                    if stop() or len(all_leads) >= max_leads:
                        break
                    batch = gmaps_tasks[i:i + GMAPS_BATCH_SIZE]
                    await asyncio.gather(*batch, return_exceptions=True)
                    import gc; gc.collect()  # free page memory between batches

                # ── IG enrichment (post-GMaps, sequential on free tier) ────
                no_web = [
                    l for l in all_leads
                    if not l.get("website") and l.get("source") == "Google Maps"
                ]
                ig_limit = 5 if RENDER_FREE_TIER else 10
                if no_web and not stop():
                    log(f"Instagram lookup for {min(len(no_web), ig_limit)} leads...")
                    ig_sem = asyncio.Semaphore(1 if RENDER_FREE_TIER else 3)

                    async def _fetch_ig(lead: dict):
                        if stop():
                            return
                        async with ig_sem:
                            has_ig, handle = await gmaps_scraper.find_instagram(
                                lead["name"], lead.get("city", "")
                            )
                        if has_ig:
                            lead["has_instagram"] = True
                            lead["instagram_handle"] = handle
                            lead["lead_type"] = "No website, Instagram found"
                            lead["confidence"] = max(lead.get("confidence") or 0, 75)

                    await asyncio.gather(
                        *[_fetch_ig(l) for l in no_web[:ig_limit]],
                        return_exceptions=True,
                    )
                    score_leads_batch(no_web[:ig_limit])
                    await asyncio.to_thread(_save_leads, db, user_id, job_id, no_web[:ig_limit], True)

            finally:
                await gmaps_scraper.stop()
                import gc; gc.collect()  # release Chromium RAM before generic phase

        # ── 3. Generic Web Phase ──────────────────────────────────────────
        # On free tier: skip launching a second Chromium browser entirely.
        # GenericScraper.start() is NOT called — it will auto-use httpx fallback.
        run_generic = bool(generic_sources) or should_run_web_search
        if run_generic and not stop() and len(all_leads) < max_leads:
            log("--- Generic web phase (httpx/DDG mode on free tier) ---" if RENDER_FREE_TIER
                else "--- Generic web phase: parallel ---")
            gen_scraper = GenericScraper(progress_cb=log, stop_flag=stop)
            if GENERIC_USE_BROWSER:
                await gen_scraper.start()  # only start browser on full tier
            try:
                async def _generic_combo(source: str, query: str, city: str):
                    if stop() or len(all_leads) >= max_leads:
                        return
                    async with generic_sem:
                        if stop() or len(all_leads) >= max_leads:
                            return
                        remaining = max(3, max_leads - len(all_leads))
                        per_combo = max(3, max_leads // max(1, generic_combos_count))
                        target = min(remaining, per_combo + 2)

                        raw = await gen_scraper.scrape_domain(
                            domain=source, query=query, city=city, max_leads=target
                        )
                    await _process_leads(raw, intent=lead_intent)
                    await _tick(f"{source} '{query}' in {city}")

                async def _web_combo(query: str, city: str):
                    if stop() or len(all_leads) >= max_leads:
                        return
                    async with generic_sem:
                        if stop() or len(all_leads) >= max_leads:
                            return
                        remaining = max(3, max_leads - len(all_leads))
                        raw = await gen_scraper.scrape_web(
                            query=query, city=city, max_leads=min(remaining, 8)
                        )
                    await _process_leads(raw, intent=lead_intent)
                    await _tick(f"Web '{query}' in {city}")

                all_tasks: list = []
                for src in generic_sources:
                    all_tasks += [_generic_combo(src, q, c) for c in cities for q in queries]
                if should_run_web_search:
                    # Cap web combos on free tier to avoid too many concurrent httpx calls
                    wq_limit = 3 if RENDER_FREE_TIER else 6
                    all_tasks += [_web_combo(q, c) for c in cities for q in web_queries[:wq_limit]]

                # Batch the generic tasks too
                gen_batch = 3
                for i in range(0, len(all_tasks), gen_batch):
                    if stop() or len(all_leads) >= max_leads:
                        break
                    await asyncio.gather(*all_tasks[i:i + gen_batch], return_exceptions=True)

            finally:
                if GENERIC_USE_BROWSER:
                    await gen_scraper.stop()
                import gc; gc.collect()

        # ── 4. Final flush & summary ──────────────────────────────────────
        await _flush_buffer(force=True)

        if stop():
            log("Job was stopped by user.")
            _set_status(db, user_id, job_id, "stopped", {"leads_count": len(all_leads)})
            return

        hot = sum(1 for l in all_leads if l.get("priority") == "Hot")
        warm = sum(1 for l in all_leads if l.get("priority") == "Warm")
        log(f"✓ Complete — {len(all_leads)} leads found: {hot} Hot, {warm} Warm")
        _set_status(db, user_id, job_id, "done", {
            "leads_count": len(all_leads),
            "hot_count": hot,
            "warm_count": warm,
        })

    except Exception as e:
        logger.exception(e)
        log(f"Job error: {e}")
        _set_status(db, user_id, job_id, "error", {"error": str(e)})
    finally:
        _active_jobs.pop(job_id, None)


# ── Thread runner ─────────────────────────────────────────────────────────────

def _run_job_thread(user_id: str, job_id: str, user_query: str, sources: list[str]):
    """Run a single job in its own isolated thread + event loop."""
    db = get_db()
    if _job_semaphore._value == 0:
        _log(db, user_id, job_id,
             f"Server at capacity ({MAX_CONCURRENT_JOBS}/{MAX_CONCURRENT_JOBS} jobs). "
             "You are in the queue...")
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
    """Launch a new job in a dedicated background thread (non-blocking)."""
    _active_jobs[job_id] = False
    db = get_db()
    _set_status(db, user_id, job_id, "queued")
    _log(db, user_id, job_id, "Job queued. Allocating server resources...")
    t = threading.Thread(
        target=_run_job_thread,
        args=(user_id, job_id, user_query, sources),
        daemon=True,
    )
    t.start()
