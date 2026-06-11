"""
llm/query_generator.py
──────────────────────
Uses Groq llama-3.3-70b-versatile (temp=0.2) to turn a plain-English lead
description into a structured scraping plan.

Temperature 0.2 rationale:
  - 0.0 → identical output every run, zero query diversity
  - 0.2 → JSON structure integrity guaranteed, natural synonym/phrasing variation
  - 0.5+ → risks malformed JSON and hallucinated city names

New plan fields vs original:
  - segment       : free-form description of target (replaces rigid business_type enum)
  - lead_intent   : "physical" | "online" | "hybrid" (drives which scrapers activate)
  - search_strategy: "maps_first" | "web_first" | "both" (drives orchestration)
  - web_queries   : separate web-search query list (different angle from Maps queries)
  - exclude_terms : bad-result signals (directories, listicles, review sites)
"""

import os
import logging
import re
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser

logger = logging.getLogger(__name__)

FREE_TIER_MAX_AREAS = 8

# ─── System Prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a world-class lead generation strategist and expert in finding business prospects.

Given a user's description of the leads they want, return a valid JSON object with EXACTLY these fields:

{
  "segment": "plain-English description of the target business segment",
  "lead_intent": "physical" | "online" | "hybrid",
  "search_strategy": "maps_first" | "web_first" | "both",
  "cities": ["City1", "City2", ...],
  "search_queries": ["query1", "query2", ...],
  "web_queries": ["web query1", "web query2", ...],
  "sources": ["gmaps", "instagram.com", ...],
  "max_leads": 30,
  "max_areas": 5,
  "filters": [],
  "exclude_terms": ["directory", "top 10", ...]
}

=== FIELD RULES ===

lead_intent:
  - "physical" → businesses with a physical location (restaurants, studios, shops, clinics)
  - "online"   → remote/digital-only (SaaS, freelancers, content creators, e-commerce)
  - "hybrid"   → both physical and online presence (agencies, consultants, service businesses)

search_strategy:
  - "maps_first" → for physical leads (use Google Maps as primary)
  - "web_first"  → for online leads (use web search and LinkedIn as primary)
  - "both"       → for hybrid leads (run both Maps and web search)

cities (max 6 Indian cities):
  - Pick the most business-dense relevant cities for this segment
  - If user mentions specific cities, use only those

search_queries (max 12, for Google Maps):
  - Generate DIVERSE query families across multiple angles:
    * Category: "yoga studio", "wellness center"
    * Role/profession: "yoga instructor", "certified yoga teacher"
    * Niche/specialty: "hot yoga", "aerial yoga", "prenatal yoga"
    * Offering: "yoga classes", "yoga teacher training"
  - Make them sound like what a CUSTOMER types into Google Maps to find this business
  - SHORT (2-4 words each), specific, action-oriented

web_queries (max 8, for general web search):
  - Different angle from Maps queries — include contact/email signals
  - Example: "yoga studio Pune contact email", "yoga instructor India portfolio website"
  - Include city name here (unlike search_queries)

sources:
  - Always include "gmaps" for physical/hybrid leads
  - Add "linkedin.com" for B2B, tech, SaaS, corporate
  - Add "instagram.com" for creative, food, fashion, wellness
  - Add "youtube.com" for educators, creators, influencers
  - Never add more than 3 sources total

exclude_terms (3-5 terms):
  - Words that indicate a bad result (aggregator, directory, review site)
  - Example: ["directory", "list of", "top 10", "best yoga", "justdial"]

max_leads: integer derived from user intent
  - "quick" / "few" → 10-15
  - default → 30
  - "thorough" / "comprehensive" → 80-100
  - "massive" / "all" → 150-200

max_areas: how many neighbourhoods to search per city (default 5, never exceed 8)

=== CRITICAL DONT'S ===
1. NEVER include city/country names in search_queries (put them in cities)
2. NEVER use "near me", "best X in", "top X", "how to", or informational phrasing in search_queries
3. NEVER generate duplicate or near-duplicate queries
4. For online-only (SaaS, freelancers): set lead_intent="online", search_strategy="web_first"
5. For physical shops/restaurants: set lead_intent="physical", search_strategy="maps_first"
6. Respond ONLY with valid JSON. No explanation. No markdown fences.
"""

USER_PROMPT = "User wants: {user_query}"

_llm = None


def _get_llm():
    global _llm
    if _llm is None:
        _llm = ChatGroq(
            model="llama-3.3-70b-versatile",
            temperature=0.2,
            api_key=os.getenv("GROQ_API_KEY"),
        )
    return _llm


# ─── Public API ───────────────────────────────────────────────────────────────

def generate_search_plan(user_query: str) -> dict:
    """Generate a structured scraping plan from a user's plain-English query."""
    try:
        prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("human", USER_PROMPT),
        ])
        chain = prompt | _get_llm() | JsonOutputParser()
        result = chain.invoke({"user_query": user_query})
        logger.info(f"LLM plan generated: intent={result.get('lead_intent')}, "
                    f"strategy={result.get('search_strategy')}, "
                    f"cities={result.get('cities')}, "
                    f"queries={len(result.get('search_queries', []))}")
        return _validate_plan(result)
    except Exception as e:
        logger.error(f"LLM query generation failed: {e}")
        return _fallback_plan(user_query)


# ─── Validation ───────────────────────────────────────────────────────────────

def _validate_plan(plan: dict) -> dict:
    defaults = {
        "segment": "local businesses",
        "lead_intent": "physical",
        "search_strategy": "maps_first",
        "cities": ["Mumbai", "Delhi", "Bangalore"],
        "search_queries": ["local business"],
        "web_queries": [],
        "sources": ["gmaps"],
        "max_leads": 30,
        "max_areas": 5,
        "filters": [],
        "exclude_terms": ["directory", "list of", "top 10"],
    }

    # Fill missing fields
    for k, v in defaults.items():
        if k not in plan or plan[k] is None or plan[k] == "":
            plan[k] = v

    # Validate enum fields
    if plan["lead_intent"] not in ("physical", "online", "hybrid"):
        plan["lead_intent"] = "physical"
    if plan["search_strategy"] not in ("maps_first", "web_first", "both"):
        plan["search_strategy"] = "both" if plan["lead_intent"] == "hybrid" else (
            "web_first" if plan["lead_intent"] == "online" else "maps_first"
        )

    # Clean and cap lists
    plan["cities"] = _clean_cities(plan.get("cities") or [])[:6] or defaults["cities"]
    plan["search_queries"] = _clean_queries(plan.get("search_queries") or [], plan["cities"])[:12]
    if not plan["search_queries"]:
        plan["search_queries"] = defaults["search_queries"]

    plan["web_queries"] = _clean_web_queries(plan.get("web_queries") or [])[:8]
    plan["sources"] = _clean_sources(plan.get("sources") or [])
    plan["max_leads"] = _clamp_int(plan.get("max_leads"), 30, 5, 10000)
    plan["max_areas"] = _clamp_int(plan.get("max_areas"), 5, 1, FREE_TIER_MAX_AREAS)
    plan["exclude_terms"] = [str(x).lower().strip() for x in (plan.get("exclude_terms") or [])][:8]
    plan["free_tier_cap_applied"] = False

    # GMaps should be present for physical/hybrid, can be absent for pure online
    if plan["lead_intent"] != "online" and "gmaps" not in plan["sources"]:
        plan["sources"].insert(0, "gmaps")
    # For online-only with web_first, GMaps is optional but default to include it
    elif "gmaps" not in plan["sources"]:
        plan["sources"].append("gmaps")

    return plan


# ─── Cleaners ─────────────────────────────────────────────────────────────────

# Extended city name set for stripping city names out of queries
KNOWN_CITY_NAMES: set[str] = {
    "mumbai", "delhi", "new delhi", "bangalore", "bengaluru", "hyderabad",
    "chennai", "pune", "kolkata", "ahmedabad", "gurgaon", "gurugram",
    "noida", "thane", "navi mumbai", "surat", "jaipur", "lucknow",
    "kochi", "coimbatore", "bhopal", "indore", "nagpur", "visakhapatnam",
    "chandigarh", "vadodara", "agra", "nashik", "rajkot", "meerut",
    "faridabad", "patna", "ranchi", "raipur", "bhubaneswar", "amritsar",
    "ludhiana", "jalandhar", "mysore", "hubli", "mangalore", "tiruppur",
    "salem", "madurai", "tiruchirappalli", "jabalpur", "gwalior", "allahabad",
    "prayagraj", "varanasi", "udaipur", "jodhpur", "kota", "bikaner",
    "aurangabad", "solapur", "sangli", "kolhapur", "nanded",
}


def _clean_cities(cities: list) -> list[str]:
    if not isinstance(cities, list):
        return []
    cleaned, seen = [], set()
    canonical = {
        "bengaluru": "Bangalore",
        "bangalore": "Bangalore",
        "gurgaon": "Gurgaon",
        "gurugram": "Gurgaon",
        "new delhi": "Delhi",
    }
    for city in cities:
        city = str(city).strip()
        if not city:
            continue
        city = canonical.get(city.lower(), city)
        key = city.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(city)
    return cleaned


def _clean_queries(queries: list, cities: list) -> list[str]:
    """Strip city names, 'near me', informational phrasing from Maps queries."""
    if not isinstance(queries, list):
        return []
    city_words = set(KNOWN_CITY_NAMES)
    city_words.update(str(c).lower() for c in (cities or []))

    cleaned, seen = [], set()
    for query in queries:
        q = str(query).strip()
        if not q:
            continue
        # Strip bad phrases
        q = re.sub(r"\bnear\s+me\b", "", q, flags=re.I)
        q = re.sub(r"\bin\s+india\b", "", q, flags=re.I)
        q = re.sub(r"\bbest\s+", "", q, flags=re.I)
        q = re.sub(r"\btop\s+\d+\s*", "", q, flags=re.I)
        q = re.sub(r"\btop\s+", "", q, flags=re.I)
        q = re.sub(r"\bhow\s+to\b.*", "", q, flags=re.I)
        # Strip city names embedded in the query
        for city in sorted(city_words, key=len, reverse=True):
            q = re.sub(rf"\s+\bin\s+{re.escape(city)}\b", "", q, flags=re.I)
            q = re.sub(rf"\b{re.escape(city)}\b", "", q, flags=re.I)
        q = re.sub(r"\s+", " ", q).strip(" ,-")
        if len(q) < 3:
            continue
        key = q.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(q)
    return cleaned


def _clean_web_queries(queries: list) -> list[str]:
    """Web queries CAN include city names — light cleaning only."""
    if not isinstance(queries, list):
        return []
    cleaned, seen = [], set()
    for query in queries:
        q = str(query).strip()
        if not q or len(q) < 5:
            continue
        q = re.sub(r"\bnear\s+me\b", "", q, flags=re.I)
        q = re.sub(r"\s+", " ", q).strip()
        key = q.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(q)
    return cleaned


def _clean_sources(sources: list) -> list[str]:
    if not isinstance(sources, list):
        return ["gmaps"]
    allowed, seen = [], set()
    for source in sources:
        s = (str(source).lower()
             .replace("https://", "").replace("http://", "")
             .replace("www.", "").strip("/"))
        if s in {"google maps", "maps", "google"}:
            s = "gmaps"
        if s and s not in seen:
            seen.add(s)
            allowed.append(s)
    return allowed or ["gmaps"]


def _clamp_int(value, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(value)
    except Exception:
        value = default
    return max(min_value, min(value, max_value))


# ─── Fallback ─────────────────────────────────────────────────────────────────

def _fallback_plan(user_query: str) -> dict:
    """Minimal safe plan when LLM completely fails."""
    return {
        "segment": user_query.strip()[:120],
        "lead_intent": "physical",
        "search_strategy": "both",
        "cities": ["Mumbai", "Bangalore", "Delhi"],
        "search_queries": [user_query.strip()[:60]],
        "web_queries": [f"{user_query.strip()[:50]} contact email India"],
        "sources": ["gmaps"],
        "max_leads": 50,
        "max_areas": 5,
        "filters": [],
        "exclude_terms": ["directory", "list of", "top 10"],
        "free_tier_cap_applied": False,
    }
