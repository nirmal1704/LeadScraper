"""
llm/area_bootstrapper.py
────────────────────────
LLM-powered city neighbourhood discovery for any city not already known.
Called exactly ONCE per new city, result persisted to Firestore permanently.

Priority:
  1. Return immediately if city is in the static CITY_AREAS dict (zero cost)
  2. Return cached Firestore data if available (zero cost)
  3. Call Groq LLM to generate seed areas, save to Firestore, return (1 Groq call)
"""

import os
import logging
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser

logger = logging.getLogger(__name__)

# Static seed areas for the most common Indian cities — avoids an LLM call for these
STATIC_CITY_AREAS: dict[str, list[str]] = {
    "Mumbai": ["Andheri", "Bandra", "Borivali", "Dadar", "Malad", "Goregaon",
               "Kandivali", "Thane", "Navi Mumbai", "Powai", "Chembur", "Mulund"],
    "Delhi": ["Lajpat Nagar", "Dwarka", "Rohini", "Janakpuri", "Saket",
              "Vasant Kunj", "Rajouri Garden", "Karol Bagh", "Noida", "Gurgaon"],
    "Bangalore": ["Indiranagar", "Koramangala", "Whitefield", "JP Nagar", "Jayanagar",
                  "HSR Layout", "BTM Layout", "Malleshwaram", "Marathahalli", "Electronic City"],
    "Hyderabad": ["Hitech City", "Kondapur", "Madhapur", "Kukatpally", "Secunderabad",
                  "Ameerpet", "Gachibowli", "Banjara Hills", "Jubilee Hills", "Miyapur"],
    "Chennai": ["Anna Nagar", "Velachery", "Adyar", "Porur", "Tambaram",
                "Nungambakkam", "T Nagar", "Mylapore", "Sholinganallur", "Perambur"],
    "Pune": ["Kothrud", "Baner", "Wakad", "Hinjewadi", "Aundh",
             "Viman Nagar", "Hadapsar", "Kharadi", "Deccan", "Koregaon Park",
             "Shivajinagar", "Camp"],
    "Kolkata": ["Salt Lake", "Behala", "Dum Dum", "New Town", "Gariahat",
                "Tollygunge", "Jadavpur", "Ballygunge", "Howrah", "Park Street"],
    "Ahmedabad": ["Satellite", "Bopal", "Prahlad Nagar", "Navrangpura", "Vastrapur",
                  "Maninagar", "Gota", "Chandkheda", "SG Highway", "Thaltej"],
    "Surat": ["Adajan", "Vesu", "Piplod", "Katargam", "Althan",
              "Udhna", "Rander", "Pal", "Citylight", "Dumas Road"],
    "Jaipur": ["Malviya Nagar", "Vaishali Nagar", "Mansarovar", "C Scheme", "Bani Park",
               "Raja Park", "Jagatpura", "Sanganer", "Sirsi Road", "Tonk Road"],
    "Lucknow": ["Hazratganj", "Gomti Nagar", "Aliganj", "Indiranagar", "Mahanagar",
                "Rajajipuram", "Charbagh", "Alambagh", "Vikas Nagar", "Kanpur Road"],
    "Kochi": ["Edapally", "Kakkanad", "Aluva", "Vyttila", "Fort Kochi",
              "MG Road", "Palarivattom", "Thripunithura", "Maradu", "Kaloor"],
    "Coimbatore": ["RS Puram", "Peelamedu", "Gandhipuram", "Saibaba Colony",
                   "Singanallur", "Ondipudur", "Vadavalli", "Kuniyamuthur"],
    "Indore": ["Vijay Nagar", "Palasia", "MG Road", "Scheme 54", "AB Road",
               "Bhawarkua", "Rajwada", "Rau", "Bicholi Mardana"],
    "Nagpur": ["Dharampeth", "Sitabuldi", "Sadar", "Hingna", "Manish Nagar",
               "Trimurti Nagar", "Bajaj Nagar", "Wardha Road", "Ambazari"],
}


_BOOTSTRAP_SYSTEM = """You are a geography expert with deep knowledge of Indian cities and business districts.

Given a city name, return a JSON object with ONE field:
- "areas": list of 12-15 well-known neighbourhoods, localities or business districts in this city

Selection criteria:
- Prioritize areas with a high density of shops, offices, restaurants, and service businesses
- Include both upscale commercial areas and middle-class commercial hubs
- Use the most widely recognized English spelling
- Do NOT include: the city name itself, state names, country names, PIN codes, or highway names

Respond ONLY with valid JSON. No explanation, no markdown.

Example output format:
{"areas": ["Kothrud", "Baner", "Wakad", "Hinjewadi", "Aundh", "Viman Nagar", "Hadapsar", "Kharadi", "Deccan", "Koregaon Park", "Shivajinagar", "Camp", "Kalyani Nagar"]}
"""

_llm = None


def _get_llm():
    global _llm
    if _llm is None:
        _llm = ChatGroq(
            model="llama-3.3-70b-versatile",
            temperature=0,  # We want deterministic area lists
            api_key=os.getenv("GROQ_API_KEY"),
        )
    return _llm


def bootstrap_city_areas(city: str, db=None) -> list[str]:
    """
    Get seed neighbourhood areas for any city.
    Returns instantly for known cities; makes one LLM call for unknown cities.

    Args:
        city: City name (e.g. "Mumbai", "Bhopal", "Kota")
        db: Firestore client (optional, used for caching)

    Returns:
        List of area/neighbourhood strings. Falls back to [city] on all failures.
    """
    # 1. Static dict — zero cost, instant
    if city in STATIC_CITY_AREAS:
        return STATIC_CITY_AREAS[city]

    # 2. Firestore cache — fast, one DB read
    if db:
        try:
            doc = (
                db.collection("geography")
                  .document("india")
                  .collection("cities")
                  .document(city)
                  .get()
            )
            if doc.exists:
                areas = doc.to_dict().get("areas", [])
                if areas:
                    logger.info(f"Area cache hit for '{city}': {len(areas)} areas")
                    return areas
        except Exception as e:
            logger.warning(f"Firestore area cache read failed for '{city}': {e}")

    # 3. LLM bootstrap — one Groq call, then cached forever
    try:
        logger.info(f"Bootstrapping areas for unknown city via LLM: '{city}'")
        prompt = ChatPromptTemplate.from_messages([
            ("system", _BOOTSTRAP_SYSTEM),
            ("human", "City: {city}"),
        ])
        chain = prompt | _get_llm() | JsonOutputParser()
        result = chain.invoke({"city": city})
        areas = [str(a).strip() for a in result.get("areas", []) if str(a).strip()]

        if areas:
            logger.info(f"LLM generated {len(areas)} areas for '{city}'")
            # Persist to Firestore so we never call LLM for this city again
            if db:
                try:
                    ref = (
                        db.collection("geography")
                          .document("india")
                          .collection("cities")
                          .document(city)
                    )
                    ref.set({"areas": areas[:15], "source": "llm_bootstrap"}, merge=True)
                    logger.info(f"Cached bootstrap areas for '{city}'")
                except Exception as e:
                    logger.warning(f"Failed to cache bootstrap areas for '{city}': {e}")
            return areas[:15]

    except Exception as e:
        logger.error(f"LLM area bootstrap failed for '{city}': {e}")

    # Final fallback — search the city itself as a single "area"
    logger.warning(f"Using city name as sole area for '{city}' — no areas available")
    return [city]
