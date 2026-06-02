"""
llm/query_generator.py
──────────────────────
Uses Groq (free, fast) with LangChain to turn a plain-English lead description
into a structured scraping plan — no hardcoded cities, queries, or categories.
"""
import os
import json
import logging
import re
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser

logger = logging.getLogger(__name__)

FREE_TIER_MAX_AREAS = 8

KNOWN_CITY_NAMES = {
    "mumbai", "delhi", "new delhi", "bangalore", "bengaluru", "hyderabad",
    "chennai", "pune", "kolkata", "ahmedabad", "gurgaon", "gurugram",
    "noida", "thane", "navi mumbai",
}

SYSTEM_PROMPT = """You are a lead generation expert helping find local Indian businesses.

Given a user's description of the leads they want, return a JSON object with:
- "business_type": one of "classes", "cafes", "production", "services", "retail", "other"
- "cities": list of Indian cities to search (max 6, pick the most relevant)
- "search_queries": list of generic search terms (max 12, be specific and varied)
- "sources": list of domains to search (e.g., ["gmaps", "instagram.com", "youtube.com", "filmfreeway.com"])
- "max_leads": integer (determine from user's text, e.g. "quick check" = 10, "massive search" = 200, default 30)
- "max_areas": integer (determine from user's text, how many neighborhoods to explore, default 5, max 30)
- "filters": any specific filters like "small", "independent", "no chain" (list of strings)

Rules:
- Include "gmaps" for physical locations. Add generic websites (like "instagram.com", "zomato.com") if the user requests them or if they fit the business type well.
- Focus on SMALL LOCAL businesses, not chains or franchises.
- Make search queries specific: prefer "Kathak dance classes" over just "dance".
- CRITICAL: Return queries that a normal person would type into Google Maps to find a physical store (e.g. 'Coffee shops', 'Italian restaurants').
- CRITICAL: DO NOT return informational queries like 'cafe menu ideas' or 'best cafe in India'.
- CRITICAL: DO NOT include city or country names in the search_queries (e.g., output "yoga classes" NOT "yoga classes in Mumbai" or "yoga in India").
- CRITICAL: NEVER use the phrase "near me" in the search_queries. It breaks the geolocator.
- Respond ONLY with valid JSON. No explanation. No markdown.
"""

USER_PROMPT = "User wants: {user_query}"

_llm = None

def _get_llm():
    global _llm
    if _llm is None:
        _llm = ChatGroq(
            model="llama-3.1-8b-instant",
            temperature=0,
            api_key=os.getenv("GROQ_API_KEY"),
        )
    return _llm


def generate_search_plan(user_query: str) -> dict:
    try:
        prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("human", USER_PROMPT),
        ])
        chain = prompt | _get_llm() | JsonOutputParser()
        result = chain.invoke({"user_query": user_query})
        logger.info(f"LLM search plan: {result}")
        return _validate_plan(result)
    except Exception as e:
        logger.error(f"LLM query generation failed: {e}")
        return _fallback_plan(user_query)


def _validate_plan(plan: dict) -> dict:
    defaults = {
        "business_type": "other",
        "cities": ["Mumbai", "Delhi", "Bangalore"],
        "search_queries": ["local business"],
        "sources": ["gmaps"],
        "max_leads": 30,
        "max_areas": 5,
        "filters": [],
    }
    for k, v in defaults.items():
        if k not in plan or not plan[k]:
            plan[k] = v

    plan["cities"] = _clean_cities(plan["cities"])[:6] or defaults["cities"]
    plan["search_queries"] = _clean_queries(plan["search_queries"], plan["cities"])[:12]
    if not plan["search_queries"]:
        plan["search_queries"] = defaults["search_queries"]
    plan["sources"] = _clean_sources(plan["sources"])
    plan["max_leads"] = _clamp_int(plan.get("max_leads"), 30, 5, 10000)
    plan["max_areas"] = _clamp_int(plan.get("max_areas"), 5, 1, FREE_TIER_MAX_AREAS)
    plan["free_tier_cap_applied"] = False

    if "gmaps" not in plan["sources"]:
        plan["sources"].append("gmaps")

    return plan


def _clean_cities(cities) -> list[str]:
    if not isinstance(cities, list):
        return []
    cleaned = []
    seen = set()
    for city in cities:
        city = str(city).strip()
        if not city:
            continue
        if city.lower() == "bengaluru":
            city = "Bangalore"
        key = city.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(city)
    return cleaned


def _clean_queries(queries, cities) -> list[str]:
    if not isinstance(queries, list):
        return []
    city_words = set(KNOWN_CITY_NAMES)
    city_words.update(str(c).lower() for c in cities or [])
    cleaned = []
    seen = set()
    for query in queries:
        q = str(query).strip()
        if not q:
            continue
        q = re.sub(r"\bnear\s+me\b", "", q, flags=re.I)
        q = re.sub(r"\bin\s+india\b", "", q, flags=re.I)
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


def _clean_sources(sources) -> list[str]:
    if not isinstance(sources, list):
        return ["gmaps"]
    allowed = []
    seen = set()
    for source in sources:
        s = str(source).lower().replace("https://", "").replace("http://", "").replace("www.", "").strip("/")
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


def _fallback_plan(user_query: str) -> dict:
    return {
        "business_type": "other",
        "cities": ["Mumbai", "Bangalore", "Delhi"],
        "search_queries": [user_query.strip()[:60]],
        "sources": ["gmaps"],
        "max_leads": 50,
        "max_areas": 5,
        "filters": [],
    }
