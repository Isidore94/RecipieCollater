"""Meal-planning / pantry assistant UI (Chat tab, Phase 5c).

Cookie-auth browser routes; every POST is CSRF-guarded. One structured request/response per turn
(no SSE in v1); proposals render as Accept/Dismiss cards, and acceptance is a separate idempotent
request that applies via deterministic services (docs/05 section 3).
"""

from __future__ import annotations

import sqlite3
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData

from app.auth import current_user, require_csrf
from app.deps import get_db
from app.services import assistant
from app.services.users import User
from app.templating import render

router = APIRouter(prefix="/chat")


def _chat_url(db: sqlite3.Connection, proposal_id: int, query: str = "") -> str:
    """Back to the conversation a proposal lives in, not merely the latest one."""
    cid = assistant.proposal_conversation(db, proposal_id)
    params = ([f"c={cid}"] if cid else []) + ([query] if query else [])
    return "/chat" + ("?" + "&".join(params) if params else "")


def _str(form: FormData, key: str) -> str:
    raw = form.get(key)
    return raw.strip() if isinstance(raw, str) else ""


def _render(
    request: Request,
    db: sqlite3.Connection,
    user: User,
    conversation_id: int,
    notice: str | None = None,
    error: str | None = None,
) -> Response:
    return render(
        request,
        "chat/index.html",
        active_nav="chat",
        user=user,
        conversation_id=conversation_id,
        messages=assistant.list_messages(db, conversation_id),
        proposals=assistant.list_proposals(db, conversation_id),
        conversations=assistant.list_conversations(db, user.id),
        notice=notice,
        error=error,
    )


@router.get("")
def index(
    request: Request,
    c: int | None = None,
    notice: str | None = None,
    error: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    if c is not None and assistant.conversation_visible(db, c, user.id):
        conversation_id = c
    else:
        conversation_id = assistant.get_or_create_conversation(db, user_id=user.id)
    return _render(request, db, user, conversation_id, notice=notice, error=error)


@router.post("/message")
async def message(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    async with request.form() as form:
        conversation_raw = _str(form, "conversation_id")
        conversation_id = int(conversation_raw) if conversation_raw.isdigit() else 0
        if not assistant.conversation_visible(db, conversation_id, user.id):
            conversation_id = assistant.get_or_create_conversation(db, user_id=user.id)
        result = assistant.ask(db, conversation_id, _str(form, "message"), user_id=user.id)
    # ask() surfaces setup problems (no AI key, empty message, spend cap) via .error rather
    # than persisting an assistant turn - show it instead of redirecting to a silent page.
    if result.error:
        return RedirectResponse(
            f"/chat?c={conversation_id}&error={quote(result.error)}", status_code=303
        )
    return RedirectResponse(f"/chat?c={conversation_id}", status_code=303)


@router.get("/new")
def new_chat(
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    conversation_id = assistant.new_chat(db, user_id=user.id)
    return RedirectResponse(f"/chat?c={conversation_id}", status_code=303)


@router.post("/proposal/{proposal_id}/accept")
async def accept(
    request: Request,
    proposal_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    try:
        summary = assistant.accept_proposal(db, proposal_id, user_id=user.id)
    except assistant.AssistantError as exc:
        # Silently redirecting left the proposal pending with no sign anything had happened.
        return RedirectResponse(
            _chat_url(db, proposal_id, f"error={quote(str(exc))}"), status_code=303
        )
    notice = "Applied: " + ", ".join(summary) if summary else "Applied"
    return RedirectResponse(_chat_url(db, proposal_id, f"notice={quote(notice)}"), status_code=303)


@router.post("/proposal/{proposal_id}/dismiss")
async def dismiss(
    request: Request,
    proposal_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
) -> Response:
    assistant.dismiss_proposal(db, proposal_id)
    return RedirectResponse(_chat_url(db, proposal_id), status_code=303)
