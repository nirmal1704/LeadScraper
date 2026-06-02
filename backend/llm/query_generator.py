"""
llm/query_generator.py
──────────────────────
Uses Groq (free, fast) with LangChain to turn a plain-English lead description
into a structured scraping plan — no hardcoded cities, queries, or categories.
"""
import os
import json
import logging
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a lead generation expert helping find small local Indian businesses without websites.

Given a user's description of the leads they want, return a JSON object with:
- "business_type": one of "classes", "cafes", "production", "services", "retail", "other"
- "cities": list of Indian cities to search (max 6, pick the most relevant)
- "search_queries": list of Google Maps search terms (max 12, be specific and varied)
- "sources": list from ["gmaps", "justdial", "urbanpro", "zomato"] (pick what makes sense)
- "filters": any specific filters like "small", "independent", "no chain" (list of strings)

Rules:
- Always include "gmaps" in sources — it is the most reliable
- For food businesses add "zomato"  
- For tutors/classes add "urbanpro" and "justdial"
- Focus on SMALL LOCAL businesses, not chains or franchises
- Make search queries specific: prefer "Kathak dance classes" over just "dance"
- Include both English and common vernacular terms when relevant

Respond ONLY with valid JSON. No explanation. No markdown.
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
    """
    Turn a plain-English description into a structured scraping plan.

    Returns:
        {
            "business_type": "classes",
            "cities": ["Mumbai", "Pune"],
            "search_queries": ["yoga classes", "dance studio", ...],
            "sources": ["gmaps", "justdial"],
            "filters": ["small", "independent"]
        }
    """
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
    """Ensure the plan has all required keys and reasonable values."""
    defaults = {
        "business_type": "other",
        "cities": ["Mumbai", "Delhi", "Bangalore"],
        "search_queries": ["local business"],
        "sources": ["gmaps"],
        "filters": [],
    }
    for k, v in defaults.items():
        if k not in plan or not plan[k]:
            plan[k] = v

    # Cap sizes
    plan["cities"] = plan["cities"][:6]
    plan["search_queries"] = plan["search_queries"][:12]
    plan["sources"] = [s for s in plan["sources"] if s in {"gmaps", "justdial", "urbanpro", "zomato"}]
    if not plan["sources"]:
        plan["sources"] = ["gmaps"]

    return plan


def _fallback_plan(user_query: str) -> dict:
    """Minimal fallback if LLM fails."""
    return {
        "business_type": "other",
        "cities": ["Mumbai", "Bangalore", "Delhi"],
        "search_queries": [user_query.strip()[:60]],
        "sources": ["gmaps"],
        "filters": [],
    }
