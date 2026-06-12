"""
llm/query_generator.py
──────────────────────
Uses Groq llama-3.3-70b-versatile (temp=0.2) to turn a plain-English lead
description into a structured scraping plan.

Temperature 0.2 rationale:
  - 0.0 → identical output every run, zero query diversity
  - 0.2 → JSON structure integrity guaranteed, natural synonym/phrasing variation
  - 0.5+ → risks malformed JSON and hallucinated city names

Plan fields:
  - segment        : free-form description of target
  - lead_intent    : "physical" | "online" | "hybrid"
  - search_strategy: "maps_first" | "web_first" | "both"
  - web_queries    : separate web-search query list
  - filters        : open-ended predicate list (field + op + value)
  - exclude_terms  : bad-result signals
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

{{
  "segment": "plain-English description of the target business segment",
  "lead_intent": "physical" | "online" | "hybrid",
  "search_strategy": "maps_first" | "web_first" | "both",
  "cities": ["City1", "City2", ...],
  "search_queries": ["query1", "query2", ...],
  "web_queries": ["web query1", "web query2", ...],
  "sources": ["gmaps", "linkedin.com", ...],
  "max_leads": 30,
  "max_areas": 5,
  "filters": [],
  "exclude_terms": ["directory", "top 10", ...]
}}

=== INTENT & STRATEGY ===

lead_intent:
  - "physical" → businesses with a physical location (restaurants, studios, shops, clinics)
  - "online"   → remote/digital-only (SaaS, freelancers, creators, social media personalities)
  - "hybrid"   → both physical office AND online presence (agencies, consultants, brokers, firms)

search_strategy:
  - "maps_first" → physical leads (Google Maps primary)
  - "web_first"  → online leads (web search + social platforms primary)
  - "both"       → hybrid leads (run Maps AND web)

=== SOCIAL MEDIA / ONLINE DISCOVERY ===
If the user asks for people or businesses "on social media", "on Instagram", "on LinkedIn",
"online traders", "content creators", or similar digital-presence targets:
  - Set lead_intent = "online", search_strategy = "web_first"
  - Set sources to social platforms only (NO gmaps)
  - search_queries must be SHORT professional role/niche terms (2-4 words), NEVER the user's sentence
    Example for "traders and financial advisors on social media":
      ["stock trader", "financial advisor", "SEBI registered advisor",
       "mutual fund distributor", "wealth manager", "equity analyst",
       "forex trader", "commodity trader", "investment advisor"]
  - web_queries must add city + platform signal:
      ["financial advisor Instagram India", "stock trader LinkedIn profile India",
       "SEBI advisor contact India", "mutual fund advisor portfolio India"]

=== FIRMS & HYBRID TARGETS ===
If the user mentions firms, companies, agencies, brokerages, studios, or similar organizations:
  - Set lead_intent = "hybrid", search_strategy = "both"
  - Always include "gmaps" in sources (firms have offices)
  - Generate BOTH firm-level queries ("wealth management firm", "SEBI registered broker")
    AND individual-level queries ("financial advisor", "equity analyst")
  - Add relevant professional network sources (linkedin.com for finance/corporate firms)

=== SOURCES CATALOG ===
Pick the most relevant sources for the target segment. Never add more than 4 total.

Physical / local businesses:
  - "gmaps"           → Google Maps (always for physical/hybrid)

Professional / B2B:
  - "linkedin.com"    → corporates, consultants, agencies, finance professionals, B2B SaaS
  - "crunchbase.com"  → startups, funded companies, tech firms
  - "wellfound.com"   → tech startups (AngelList)
  - "clutch.co"       → agencies, IT firms, consultants

Social / creative:
  - "instagram.com"   → fashion, food, fitness, wellness, traders, lifestyle creators
  - "x.com"           → traders, investors, finance influencers, tech founders, journalists
  - "youtube.com"     → educators, coaches, creators, review channels
  - "behance.net"     → graphic designers, UI/UX, illustrators, photographers
  - "medium.com"      → bloggers, writers, thought leaders, indie analysts
  - "substack.com"    → newsletter writers, independent journalists, finance analysts

Freelance / marketplace:
  - "fiverr.com"      → freelance designers, writers, video editors, voice artists
  - "upwork.com"      → freelance developers, marketers, accountants

Discovery / community:
  - "producthunt.com" → SaaS products, indie makers, app founders
  - "github.com"      → developers, open-source maintainers

=== FILTERS — FULLY OPEN-ENDED PREDICATES ===
If the user states ANY qualification or selection criteria (e.g. "who need a website",
"with fewer than 100 reviews", "no HTTPS", "have email", "rating below 3",
"no portfolio", "hot leads only", "who are restaurants"), convert it into structured
filter predicates. The filter system is completely open — you are NOT limited to
predefined categories.

Each filter is a JSON object:
  - "field"  : the lead data field to check
  - "op"     : the comparison operator
  - "value"  : (optional) the comparison value
  - "label"  : short human-readable description

Available fields and operators:

  website          → ops: "is_null", "is_not_null"
  website_status   → ops: "in", "not_in"        values: list from ["Up","Down","Error","Timeout","Unknown"]
  has_https        → ops: "eq"                   values: true | false
  has_mobile_meta  → ops: "eq"                   values: true | false
  email            → ops: "is_null", "is_not_null"
  phone            → ops: "is_null", "is_not_null"
  has_instagram    → ops: "eq"                   values: true | false
  rating           → ops: "gt", "lt", "gte", "lte", "eq"    values: float 0–5
  review_count     → ops: "gt", "lt", "gte", "lte", "eq"    values: integer
  score            → ops: "gt", "lt", "gte", "lte"           values: integer 0–100
  priority         → ops: "eq", "in"             values: "Hot"|"Warm"|"Medium"|"Cold"
  category         → ops: "contains", "not_contains"         values: keyword string
  source           → ops: "eq", "neq"            values: "Google Maps"|"Instagram"|"LinkedIn" etc

Examples:
  "who need a website or portfolio"    → [{{"field":"website","op":"is_null","label":"No website"}}]
  "with broken website"                → [{{"field":"website_status","op":"in","value":["Down","Error","Timeout"],"label":"Broken website"}}]
  "no HTTPS"                           → [{{"field":"has_https","op":"eq","value":false,"label":"No HTTPS"}}]
  "have a contact email"               → [{{"field":"email","op":"is_not_null","label":"Has email"}}]
  "rating below 4"                     → [{{"field":"rating","op":"lt","value":4.0,"label":"Rating below 4"}}]
  "fewer than 50 reviews"              → [{{"field":"review_count","op":"lt","value":50,"label":"Fewer than 50 reviews"}}]
  "hot leads only"                     → [{{"field":"priority","op":"in","value":["Hot"],"label":"Hot leads only"}}]
  "no phone number"                    → [{{"field":"phone","op":"is_null","label":"No phone"}}]
  "score above 70"                     → [{{"field":"score","op":"gt","value":70,"label":"Score above 70"}}]
  "restaurant or cafe"                 → [{{"field":"category","op":"contains","value":"restaurant","label":"Is restaurant or cafe"}}]

Multiple criteria: combine as an array — ALL must match (AND logic).
If no criteria mentioned: set filters = [].

=== QUERY RULES ===

cities (max 6 Indian cities):
  - Pick the most business-dense relevant cities for this segment
  - If user mentions specific cities, use only those

search_queries (max 12, for Google Maps — skip entirely if lead_intent=online):
  - SHORT (2-4 words), specific professional/category terms
  - NEVER echo the user's sentence, NEVER include city names
  - Diverse: cover category, role, niche, service type

web_queries (max 8, for general web search):
  - Include city name and contact/social signal
  - e.g. "financial advisor Instagram Mumbai contact"
  - If a "has_email" filter is requested: generate queries that specifically
    surface profiles with visible emails:
    * Include "gmail.com" OR "contact@" OR "email" in the query
    * Example: "stock trader India gmail.com instagram"
    * Example: "financial advisor Mumbai contact email site:linktr.ee"
    * linktr.ee, linkinbio, bio.link pages often show emails publicly

exclude_terms (3-5 terms): aggregator/directory signals to discard
  e.g. ["justdial", "directory", "list of", "top 10", "sulekha"]

max_leads: integer
  - "few"/"quick" → 10–15  |  default → 30  |  "thorough" → 80–100  |  "massive" → 150–200

max_areas: neighbourhoods per city (default 5, max 8)

=== CRITICAL RULES ===
1. NEVER include city/country names in search_queries
2. NEVER use "near me", "best X", "top X", "how to" in search_queries
3. NEVER echo the user's raw sentence as a query — decompose into short terms
4. online/web_first plans: OMIT gmaps from sources
5. physical/hybrid plans: ALWAYS include gmaps
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
                    f"queries={len(result.get('search_queries', []))}, "
                    f"filters={len(result.get('filters', []))}")
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

    raw_queries = _clean_queries(plan.get("search_queries") or [], plan["cities"])
    # Sanity-check: drop any query that looks like the user's raw sentence
    # (longer than 60 chars = almost certainly echoed verbatim from the prompt)
    raw_queries = [q for q in raw_queries if len(q) <= 60]
    plan["search_queries"] = raw_queries[:12]
    if not plan["search_queries"]:
        plan["search_queries"] = defaults["search_queries"]

    plan["web_queries"] = _clean_web_queries(plan.get("web_queries") or [])[:8]
    plan["sources"] = _clean_sources(plan.get("sources") or [])
    plan["max_leads"] = _clamp_int(plan.get("max_leads"), 30, 5, 10000)
    plan["max_areas"] = _clamp_int(plan.get("max_areas"), 5, 1, FREE_TIER_MAX_AREAS)
    plan["exclude_terms"] = [str(x).lower().strip() for x in (plan.get("exclude_terms") or [])][:8]

    # Validate filters — each must be a dict with at least "field" and "op"
    plan["filters"] = [
        f for f in (plan.get("filters") or [])
        if isinstance(f, dict) and "field" in f and "op" in f
    ]
    plan["free_tier_cap_applied"] = False

    # Source rules:
    #   physical / hybrid  → always need gmaps
    #   online / web_first → gmaps is WRONG here; remove it
    intent = plan["lead_intent"]
    strategy = plan["search_strategy"]
    if intent in ("physical", "hybrid") and "gmaps" not in plan["sources"]:
        plan["sources"].insert(0, "gmaps")
    elif intent == "online" or strategy == "web_first":
        # Explicitly strip gmaps from online/web_first plans
        plan["sources"] = [s for s in plan["sources"] if s != "gmaps"]
        # Ensure at least one usable source remains
        if not plan["sources"]:
            plan["sources"] = ["linkedin.com", "instagram.com"]

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


# Allowed source domains (everything the GenericScraper can meaningfully dork)
_ALLOWED_SOURCES = {
    "gmaps",
    # Professional
    "linkedin.com", "crunchbase.com", "wellfound.com", "clutch.co",
    # Social / creative
    "instagram.com", "x.com", "twitter.com", "youtube.com",
    "behance.net", "medium.com", "substack.com",
    # Freelance / marketplace
    "fiverr.com", "upwork.com",
    # Discovery
    "producthunt.com", "github.com",
}


def _clean_sources(sources: list) -> list[str]:
    if not isinstance(sources, list):
        return ["gmaps"]
    allowed, seen = [], set()
    for source in sources:
        s = (str(source).lower()
             .replace("https://", "").replace("http://", "")
             .replace("www.", "").strip("/"))
        # Normalize aliases
        if s in {"google maps", "maps", "google"}:
            s = "gmaps"
        if s == "twitter.com":
            s = "x.com"  # normalize to current domain
        # Only allow sources we know how to scrape
        if s in _ALLOWED_SOURCES and s not in seen:
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
