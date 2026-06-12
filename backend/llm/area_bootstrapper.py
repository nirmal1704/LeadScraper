"""llm/area_bootstrapper.py — LLM-powered city neighbourhood discovery."""
import os
import logging
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser

logger = logging.getLogger(__name__)

STATIC_CITY_AREAS: dict[str, list[str]] = {
    "Mumbai":      ["Andheri", "Bandra", "Borivali", "Dadar", "Malad", "Goregaon", "Kandivali", "Thane", "Navi Mumbai", "Powai", "Chembur", "Mulund"],
    "Delhi":       ["Lajpat Nagar", "Dwarka", "Rohini", "Janakpuri", "Saket", "Vasant Kunj", "Rajouri Garden", "Karol Bagh", "Noida", "Gurgaon"],
    "Bangalore":   ["Indiranagar", "Koramangala", "Whitefield", "JP Nagar", "Jayanagar", "HSR Layout", "BTM Layout", "Malleshwaram", "Marathahalli", "Electronic City"],
    "Hyderabad":   ["Hitech City", "Kondapur", "Madhapur", "Kukatpally", "Secunderabad", "Ameerpet", "Gachibowli", "Banjara Hills", "Jubilee Hills", "Miyapur"],
    "Chennai":     ["Anna Nagar", "Velachery", "Adyar", "Porur", "Tambaram", "Nungambakkam", "T Nagar", "Mylapore", "Sholinganallur", "Perambur"],
    "Pune":        ["Kothrud", "Baner", "Wakad", "Hinjewadi", "Aundh", "Viman Nagar", "Hadapsar", "Kharadi", "Deccan", "Koregaon Park", "Shivajinagar", "Camp"],
    "Kolkata":     ["Salt Lake", "Behala", "Dum Dum", "New Town", "Gariahat", "Tollygunge", "Jadavpur", "Ballygunge", "Howrah", "Park Street"],
    "Ahmedabad":   ["Satellite", "Bopal", "Prahlad Nagar", "Navrangpura", "Vastrapur", "Maninagar", "Gota", "Chandkheda", "SG Highway", "Thaltej"],
    "Surat":       ["Adajan", "Vesu", "Piplod", "Katargam", "Althan", "Udhna", "Rander", "Pal", "Citylight", "Dumas Road"],
    "Jaipur":      ["Malviya Nagar", "Vaishali Nagar", "Mansarovar", "C Scheme", "Bani Park", "Raja Park", "Jagatpura", "Sanganer", "Sirsi Road", "Tonk Road"],
    "Lucknow":     ["Hazratganj", "Gomti Nagar", "Aliganj", "Indiranagar", "Mahanagar", "Rajajipuram", "Charbagh", "Alambagh", "Vikas Nagar", "Kanpur Road"],
    "Kochi":       ["Edapally", "Kakkanad", "Aluva", "Vyttila", "Fort Kochi", "MG Road", "Palarivattom", "Thripunithura", "Maradu", "Kaloor"],
    "Coimbatore":  ["RS Puram", "Peelamedu", "Gandhipuram", "Saibaba Colony", "Singanallur", "Ondipudur", "Vadavalli", "Kuniyamuthur"],
    "Indore":      ["Vijay Nagar", "Palasia", "MG Road", "Scheme 54", "AB Road", "Bhawarkua", "Rajwada", "Rau", "Bicholi Mardana"],
    "Nagpur":      ["Dharampeth", "Sitabuldi", "Sadar", "Hingna", "Manish Nagar", "Trimurti Nagar", "Bajaj Nagar", "Wardha Road", "Ambazari"],
}

_BOOTSTRAP_SYSTEM = """You are a geography expert with deep knowledge of Indian cities and business districts.

Given a city name, return a JSON object with ONE field:
- "areas": list of 12-15 well-known neighbourhoods, localities or business districts in this city

Selection criteria:
- Prioritize areas with high density of shops, offices, restaurants, and service businesses
- Include both upscale and middle-class commercial hubs
- Use widely recognized English spellings
- Do NOT include: city name, state names, country names, PIN codes, or highway names

Respond ONLY with valid JSON. No explanation, no markdown.
Example: {"areas": ["Kothrud", "Baner", "Wakad", "Hinjewadi", "Aundh"]}
"""

_llm = None


def _get_llm():
    global _llm
    if _llm is None:
        _llm = ChatGroq(model="llama-3.3-70b-versatile", temperature=0, api_key=os.getenv("GROQ_API_KEY"))
    return _llm


def bootstrap_city_areas(city: str, db=None) -> list[str]:
    """Return seed neighbourhood areas for a city. Uses static dict → Firestore cache → LLM."""
    if city in STATIC_CITY_AREAS:
        return STATIC_CITY_AREAS[city]

    if db:
        try:
            doc = db.collection("geography").document("india").collection("cities").document(city).get()
            if doc.exists:
                areas = doc.to_dict().get("areas", [])
                if areas:
                    return areas
        except Exception as e:
            logger.warning(f"Firestore area cache read failed for '{city}': {e}")

    try:
        chain = ChatPromptTemplate.from_messages([
            ("system", _BOOTSTRAP_SYSTEM),
            ("human", "City: {city}"),
        ]) | _get_llm() | JsonOutputParser()

        result = chain.invoke({"city": city})
        areas = [str(a).strip() for a in result.get("areas", []) if str(a).strip()]

        if areas and db:
            try:
                db.collection("geography").document("india").collection("cities").document(city).set(
                    {"areas": areas[:15], "source": "llm_bootstrap"}, merge=True
                )
            except Exception as e:
                logger.warning(f"Failed to cache bootstrap areas for '{city}': {e}")
        return areas[:15] if areas else [city]

    except Exception as e:
        logger.error(f"LLM area bootstrap failed for '{city}': {e}")
        return [city]
