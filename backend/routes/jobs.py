"""
routes/jobs.py — Job management endpoints
"""
import io
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional

from firebase_config import get_db, verify_token
from workers.job_runner import start_job, request_stop
from output.excel_exporter import build_xlsx

router = APIRouter()


def _get_uid(authorization: str) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.split(" ", 1)[1]
    try:
        decoded = verify_token(token)
        return decoded["uid"]
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


class StartJobRequest(BaseModel):
    query: str
    sources: Optional[list[str]] = None


@router.post("")
def start(body: StartJobRequest, authorization: str = Header(default="")):
    uid = _get_uid(authorization)
    if not body.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    job_id = str(uuid.uuid4())
    db = get_db()
    db.collection("users").document(uid).collection("jobs").document(job_id).set({
        "id": job_id,
        "status": "queued",
        "query": body.query,
        "sources": body.sources or [],
        "leads_count": 0,
        "logs": [],
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })
    start_job(uid, job_id, body.query, body.sources or [])
    return {"job_id": job_id}


@router.delete("/{job_id}")
def stop(job_id: str, authorization: str = Header(default="")):
    uid = _get_uid(authorization)
    request_stop(job_id)
    
    # Safely force the status to 'stopped' in the database immediately.
    # This guarantees the "Download Excel" button will unlock on the frontend
    # even if the server restarts or the worker thread dies unexpectedly.
    try:
        db = get_db()
        db.collection("users").document(uid).collection("jobs").document(job_id).update({
            "status": "stopped",
            "updated_at": datetime.now(timezone.utc)
        })
    except Exception:
        pass
        
    return {"stopped": True}


@router.get("/{job_id}")
def get_job(job_id: str, authorization: str = Header(default="")):
    uid = _get_uid(authorization)
    db = get_db()
    doc = db.collection("users").document(uid).collection("jobs").document(job_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Job not found")
    return doc.to_dict()


@router.get("/{job_id}/download")
def download_excel(job_id: str, authorization: str = Header(default="")):
    """
    Fetch all leads for this job from Firestore, build an Excel file in memory,
    and stream it directly to the browser — no storage service required.
    """
    uid = _get_uid(authorization)
    db = get_db()

    leads_col = (
        db.collection("users").document(uid)
          .collection("jobs").document(job_id)
          .collection("leads")
    )
    docs = leads_col.stream()
    leads = [d.to_dict() for d in docs]

    if not leads:
        raise HTTPException(status_code=404, detail="No leads found for this job")

    xlsx_bytes = build_xlsx(leads)

    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="leads_{job_id[:8]}.xlsx"'},
    )

