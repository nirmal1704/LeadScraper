"""
workers/job_runner.py
─────────────────────
Runs a full scraping job in a background thread.
Writes progress to Firestore → frontend reads in real time via onSnapshot.
"""
import asyncio
import logging
import threading
import queue
from datetime import datetime, timezone

from firebase_config import get_db
from llm.query_generator import generate_search_plan
from scrapers.gmaps_scraper import GMapsScraperV2
from scrapers.generic_scraper import GenericScraper
from enrichment.website_checker import check_websites_batch
from enrichment.scorer import score_leads_batch

logger = logging.getLogger(__name__)

_active_jobs: dict[str, bool] = {}   # job_id → stop requested
_job_queue = queue.Queue()
_worker_thread = None


def request_stop(job_id: str):
    _active_jobs[job_id] = True


def _should_stop(job_id: str) -> bool:
    return _active_jobs.get(job_id, False)


def _log(db, user_id: str, job_id: str, message: str):
    """Append a log line to Firestore. Frontend onSnapshot fires immediately."""
    try:
        ref = db.collection("users").document(user_id).collection("jobs").document(job_id)
        ref.update({
            "logs": firestore_array_union(message),
            "updated_at": datetime.now(timezone.utc),
        })
    except Exception as e:
        logger.debug(f"Log write failed: {e}")


def firestore_array_union(value):
    from google.cloud import firestore
    from datetime import datetime
    import pytz
    # Use Indian Standard Time (IST) for the logs shown on the frontend
    ist = pytz.timezone('Asia/Kolkata')
    current_time = datetime.now(ist).strftime('%H:%M:%S')
    return firestore.ArrayUnion([f"[{current_time}] {value}"])


def _set_status(db, user_id: str, job_id: str, status: str, extra: dict = None):
    ref = db.collection("users").document(user_id).collection("jobs").document(job_id)
    update = {"status": status, "updated_at": datetime.now(timezone.utc)}
    if extra:
        update.update(extra)
    ref.set(update, merge=True)


async def _run_async(user_id: str, job_id: str, user_query: str, sources: list[str]):
    db = get_db()
    log = lambda msg: _log(db, user_id, job_id, msg)
    stop = lambda: _should_stop(job_id)
    all_leads: list[dict] = []

    try:
        # ── 1. LLM generates the search plan ─────────────────────────────────
        log("Analysing your request...")
        plan = generate_search_plan(user_query)
        cities = plan["cities"]
        queries = plan["search_queries"]
        active_sources = plan["sources"]
        max_leads = plan.get("max_leads", 30)
        max_areas = plan.get("max_areas", 5)

        log(f"Plan ready — {len(cities)} cities, {len(queries)} terms, {len(active_sources)} sources")
        log(f"Limits: {max_leads} leads/query, {max_areas} areas/query")
        log(f"Sources: {', '.join(active_sources)}")
        log(f"Cities: {', '.join(cities)}")
        log(f"Queries: {', '.join(queries[:5])}{'...' if len(queries) > 5 else ''}")

        _set_status(db, user_id, job_id, "running", {"plan": plan})

        # ── 2. Execute Scraping ───────────────────────────────────────────────
        total_combos = len(active_sources) * len(cities) * len(queries)
        combos_done = 0

        for source in active_sources:
            if stop(): break
            
            if source == "gmaps":
                log("--- Starting GMaps Scraper ---")
                scraper = GMapsScraperV2(db=db, progress_cb=log, stop_flag=stop)
                await scraper.start()
                try:
                    for city in cities:
                        if stop(): break
                        for query in queries:
                            if stop() or len(all_leads) >= max_leads: break
                            
                            remaining_combos = total_combos - combos_done
                            target = max(1, (max_leads - len(all_leads)) // remaining_combos) if remaining_combos > 0 else 1
                            
                            leads = await scraper.scrape_city(
                                query=query, city=city, max_per_city=target, max_areas=max_areas
                            )
                            score_leads_batch(leads)
                            all_leads.extend(leads)
                            _save_leads(db, user_id, job_id, leads)
                            combos_done += 1
                            
                        # Quick IG enrichment
                        city_leads = [l for l in all_leads if l["city"] == city and not l.get("website")]
                        for lead in city_leads[:10]:
                            if stop(): break
                            has_ig, handle = await scraper.find_instagram(lead["name"], city)
                            if has_ig:
                                lead["has_instagram"] = True
                                lead["instagram_handle"] = handle
                finally:
                    await scraper.stop()
            
            else:
                log(f"--- Starting Generic Scraper for {source} ---")
                scraper = GenericScraper(progress_cb=log, stop_flag=stop)
                await scraper.start()
                try:
                    for city in cities:
                        if stop(): break
                        for query in queries:
                            if stop() or len(all_leads) >= max_leads: break
                            
                            remaining_combos = total_combos - combos_done
                            target = max(1, (max_leads - len(all_leads)) // remaining_combos) if remaining_combos > 0 else 1
                            
                            leads = await scraper.scrape_domain(
                                domain=source, query=query, city=city, max_leads=target
                            )
                            score_leads_batch(leads)
                            all_leads.extend(leads)
                            _save_leads(db, user_id, job_id, leads)
                            combos_done += 1
                finally:
                    await scraper.stop()

        if stop():
            log("Job was stopped by user.")
            _set_status(db, user_id, job_id, "stopped", {"leads_count": len(all_leads)})
            return

        # ── 3. Website enrichment ─────────────────────────────────────────────
        log(f"Checking {len(all_leads)} websites...")
        website_results = await check_websites_batch(all_leads)
        for lead in all_leads:
            result = website_results.get(lead.get("id"))
            if result:
                lead["website_status"] = result.status
                lead["has_https"] = result.has_https
                lead["has_mobile_meta"] = result.has_mobile_meta

        # ── 4. Score ──────────────────────────────────────────────────────────
        log("Scoring leads...")
        score_leads_batch(all_leads)

        # Update all leads in Firestore with enrichment data
        _save_leads(db, user_id, job_id, all_leads, overwrite=True)

        # ── 5. Mark done ────────────────────────────────────────────────────
        hot_count = sum(1 for l in all_leads if l.get("priority") == "Hot")
        warm_count = sum(1 for l in all_leads if l.get("priority") == "Warm")
        
        if stop():
            log(f"Stopped early. {len(all_leads)} leads found.")
            _set_status(db, user_id, job_id, "stopped", {
                "leads_count": len(all_leads),
                "hot_count": hot_count,
                "warm_count": warm_count,
            })
        else:
            log(f"Done. {len(all_leads)} leads found — {hot_count} Hot, {warm_count} Warm")
            _set_status(db, user_id, job_id, "done", {
                "leads_count": len(all_leads),
                "hot_count": hot_count,
                "warm_count": warm_count,
            })

    except Exception as e:
        logger.exception(e)
        log(f"Error: {e}")
        _set_status(db, user_id, job_id, "error", {"error": str(e)})
    finally:
        _active_jobs.pop(job_id, None)


def _save_leads(db, user_id: str, job_id: str, leads: list[dict], overwrite: bool = False):
    """Batch-write leads to Firestore under users/{uid}/jobs/{jid}/leads/"""
    if not leads:
        return
    batch = db.batch()
    col = db.collection("users").document(user_id).collection("jobs").document(job_id).collection("leads")
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


def _global_worker():
    """Runs continuously in the background, pulling jobs from the queue."""
    while True:
        job = _job_queue.get()
        if job is None:
            break
        user_id, job_id, user_query, sources = job
        
        # Mark as running right before we actually start
        db = get_db()
        _set_status(db, user_id, job_id, "running")
        _log(db, user_id, job_id, "Job pulled from queue. Starting execution...")
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run_async(user_id, job_id, user_query, sources))
        except Exception as e:
            logger.error(f"Global worker error: {e}")
        finally:
            loop.close()
            _job_queue.task_done()

def start_job(user_id: str, job_id: str, user_query: str, sources: list[str]):
    """Add job to the queue, start worker if not running."""
    global _worker_thread
    _active_jobs[job_id] = False
    
    # Let frontend know it's queued
    db = get_db()
    _set_status(db, user_id, job_id, "queued")
    _log(db, user_id, job_id, "Job queued. Waiting for server resources...")
    
    _job_queue.put((user_id, job_id, user_query, sources))
    
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_global_worker, daemon=True)
        _worker_thread.start()
        
    return _worker_thread
