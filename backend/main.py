"""
LeadScraper Backend — FastAPI
"""
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

from routes.jobs import router as jobs_router

app = FastAPI(title="LeadScraper API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(jobs_router, prefix="/jobs", tags=["jobs"])

@app.get("/health")
def health():
    return {"status": "ok"}
