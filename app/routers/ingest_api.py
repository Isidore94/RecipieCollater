"""The ingest API: POST /api/ingest returns 202 + job id in under 2s (docs/04 section 1.1).

Authenticated only by a scoped ingest Bearer token, never a browser cookie (CONVENTIONS 6).
Parsing runs asynchronously in the worker; this endpoint records the job and returns fast.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.auth import require_ingest_token
from app.deps import get_db
from app.logging_config import get_logger
from app.services import ingest, tokens

router = APIRouter(prefix="/api")
log = get_logger("ingest_api")


def schedule_processing(job_id: int) -> None:
    """Hand a new job to the worker; a queue hiccup must never break the fast 202 response.

    The job is already durably recorded, so on failure it simply waits in 'queued' for a retry.
    """
    try:
        from app.tasks import process_ingest_job

        process_ingest_job(job_id)
    except Exception:
        log.warning("ingest_enqueue_failed", job_id=job_id)


@router.post("/ingest")
async def submit_ingest(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    token: tokens.ApiToken = Depends(require_ingest_token),
) -> JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"detail": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "expected a JSON object with a 'url'"}, status_code=400)
    url = body.get("url")
    if not isinstance(url, str) or not url.strip():
        return JSONResponse({"detail": "missing 'url'"}, status_code=400)
    html = body.get("html")
    html_text = html if isinstance(html, str) and html.strip() else None
    try:
        job, created = ingest.enqueue_job(
            db, url, html=html_text, submitted_by=token.user_id, source="api"
        )
    except ingest.IngestError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    if created:
        schedule_processing(job.id)
    return JSONResponse(
        {
            "job_id": job.id,
            "status": job.status,
            "duplicate": not created,
            "recipe_id": job.recipe_id,
        },
        status_code=202 if created else 200,
    )
