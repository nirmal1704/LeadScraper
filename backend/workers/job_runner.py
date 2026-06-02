"""
workers/job_runner.py
─────────────────────
Runs a full scraping job in a background thread.
Writes progress to Firestore → frontend reads in real time via onSnapshot.
"""
import asyncio
import logging
import threading
from datetime import datetime, timezone

from firebase_config import get_db
from llm.query_generator import generate_search_plan
from scrapers.gmaps_scraper import GMapsScraperV2
from enrichment.website_checker import check_websites_batch
from enrichment.scorer import score_leads_batch

logger = logging.getLogger(__name__)

_active_jobs: dict[str, bool] = {}   # job_id → stop requested


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
    return firestore.ArrayUnion([f"[{datetime.now().strftime('%H:%M:%S')}] {value}"])


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
        active_sources = sources if sources else plan["sources"]

        log(f"Plan ready — {len(cities)} cities, {len(queries)} search terms")
        log(f"Cities: {', '.join(cities)}")
        log(f"Queries: {', '.join(queries[:5])}{'...' if len(queries) > 5 else ''}")

        _set_status(db, user_id, job_id, "running", {"plan": plan})

        # ── 2. Scrape GMaps (primary source) ──────────────────────────────────
        if "gmaps" in active_sources:
            scraper = GMapsScraperV2(progress_cb=log, stop_flag=stop)
            await scraper.start()
            try:
                for city in cities:
                    if stop():
                        break
                    log(f"--- {city} ---")
                    for query in queries:
                        if stop():
                            break
                        leads = await scraper.scrape_city(
                            query=query, city=city, max_per_city=40, max_areas=5
                        )
                        all_leads.extend(leads)
                        log(f"  Found {len(leads)} leads for '{query}' in {city}")

                        # Save batch to Firestore as they come in
                        _save_leads(db, user_id, job_id, leads)

                    # Instagram enrichment per city (via Google search — reliable)
                    log(f"Checking Instagram for {city} leads...")
                    city_leads = [l for l in all_leads if l["city"] == city and not l.get("website")]
                    for lead in city_leads[:10]:
                        if stop():
                            break
                        has_ig, handle = await scraper.find_instagram(lead["name"], city)
                        if has_ig:
                            lead["has_instagram"] = True
                            lead["instagram_handle"] = handle
                            log(f"  Instagram found: {lead['name']} → @{handle}")
            finally:
                await scraper.stop()

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


def start_job(user_id: str, job_id: str, user_query: str, sources: list[str]):
    """Launch the scraping job in a background thread."""
    _active_jobs[job_id] = False

    def _thread_target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run_async(user_id, job_id, user_query, sources))
        finally:
            loop.close()

    t = threading.Thread(target=_thread_target, daemon=True)
    t.start()
    return t
