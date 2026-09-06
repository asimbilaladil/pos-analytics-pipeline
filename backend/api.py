"""FastAPI application.

Routes are thin: authenticate, authorise, delegate, serialise. All analytics
live in chat_sql; all ownership checks live in SQL.
"""

from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from fastapi import (Cookie, Depends, FastAPI, File, HTTPException, Response,
                     UploadFile, status)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from . import chat as chat_svc
from . import conversations as convo
from .auth import (SESSION_COOKIE, clear_cookie, create_session, current_user,
                   delete_session, set_cookie, verify_login)
from .models import (AskIn, AskOut, ConversationDetail, ConversationOut, ImportIn,
                     LoginIn, MessageOut, ModelsOut, NewConversationIn, UserOut)

app = FastAPI(title="Laynes Intelligence API", docs_url=None, redoc_url=None)

# In production the SPA is served same-origin by nginx and this list is empty.
# It exists so the Vite dev server can talk to the API during development.
_origins = [o for o in os.getenv("API_CORS_ORIGINS", "").split(",") if o]
if _origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=_origins, allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )


def _user_out(u) -> UserOut:
    return UserOut(id=u["id"], email=u["email"], full_name=u.get("full_name"),
                   role=u["role"], is_admin=u["role"] == "super_admin")


# ── auth ────────────────────────────────────────────────────────────────────
@app.post("/api/auth/login", response_model=UserOut)
def login(body: LoginIn, response: Response):
    user = verify_login(body.email, body.password)
    if not user:
        # One message for both "no such account" and "wrong password", so the
        # endpoint cannot be used to enumerate who has an account.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Email or password is incorrect.")
    set_cookie(response, create_session(user["id"]))
    return _user_out(user)


@app.post("/api/auth/logout", status_code=204)
def logout(laynes_session: str | None = Cookie(default=None)):
    # The server-side row is deleted, not just the browser cookie: a token that
    # leaked elsewhere must stop working the moment the user signs out.
    delete_session(laynes_session)
    response = Response(status_code=204)
    clear_cookie(response)
    return response


@app.get("/api/auth/me", response_model=UserOut)
def me(user=Depends(current_user)):
    return _user_out(user)


# ── models ──────────────────────────────────────────────────────────────────
@app.get("/api/models", response_model=ModelsOut)
def models(user=Depends(current_user)):
    return ModelsOut(models=chat_svc.AVAILABLE_MODELS, default=chat_svc.DEFAULT_MODEL)


@app.get("/api/status")
def api_status(user=Depends(current_user)):
    return {"assistant_configured": chat_svc.assistant_configured()}


# ── conversations ───────────────────────────────────────────────────────────
@app.get("/api/conversations", response_model=list[ConversationOut])
def list_conversations(user=Depends(current_user)):
    return [ConversationOut(**r) for r in convo.list_for_user(user["id"])]


@app.post("/api/conversations", response_model=ConversationOut)
def new_conversation(body: NewConversationIn, user=Depends(current_user)):
    model = chat_svc.resolve_model(body.model)
    cid = convo.create(user["id"], body.title or "New chat", model)
    row = convo.owned(cid, user["id"])
    from datetime import datetime, timezone
    return ConversationOut(id=row["id"], title=row["title"], model=row["model"],
                           updated_at=datetime.now(timezone.utc))


@app.get("/api/conversations/{cid}", response_model=ConversationDetail)
def get_conversation(cid: int, user=Depends(current_user)):
    row = convo.owned(cid, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    msgs = convo.messages(cid, user["id"]) or []
    return ConversationDetail(id=row["id"], title=row["title"], model=row["model"],
                              messages=[MessageOut(**m) for m in msgs])


@app.delete("/api/conversations/{cid}", status_code=204)
def delete_conversation(cid: int, user=Depends(current_user)):
    if not convo.delete(cid, user["id"]):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return Response(status_code=204)


# ── chat ────────────────────────────────────────────────────────────────────
@app.post("/api/conversations/{cid}/messages", response_model=AskOut)
def ask(cid: int, body: AskIn, user=Depends(current_user)):
    """Ask a question. cid = 0 starts a new conversation.

    A12 is NOT re-implemented here: chat_sql.answer_question runs the same gate
    it always has, server-side, before any transactional business SQL.
    """
    if not chat_svc.assistant_configured():
        raise HTTPException(status_code=503,
                            detail="The assistant is not configured on the server.")
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    model = chat_svc.resolve_model(body.model)
    is_new = cid == 0
    if is_new:
        cid = convo.create(user["id"], convo.auto_title(question), model)
    else:
        if not convo.owned(cid, user["id"]):
            raise HTTPException(status_code=404, detail="Conversation not found")
        convo.set_model(cid, model)

    history = convo.messages(cid, user["id"]) or []
    convo.add_message(cid, "user", question)

    answer, steps, dur, error = chat_svc.ask(history, question, model)

    convo.add_message(cid, "assistant", answer)
    convo.touch(cid, title=convo.auto_title(question) if is_new else None)
    convo.log_query(user["id"], question, steps, answer, error, dur,
                    conversation_id=cid, model=model)

    row = convo.owned(cid, user["id"])
    return AskOut(conversation_id=cid, title=row["title"], answer=answer,
                  duration_ms=dur)


# ── import / export ─────────────────────────────────────────────────────────
@app.get("/api/conversations/{cid}/export")
def export_conversation(cid: int, user=Depends(current_user)):
    payload = convo.export_payload(cid, user["id"])
    if not payload:
        raise HTTPException(status_code=404, detail="Conversation not found")
    data = json.dumps(payload, indent=2).encode()
    safe = "".join(ch for ch in payload["title"] if ch.isalnum() or ch in " -_")[:60].strip()
    return StreamingResponse(
        io.BytesIO(data), media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="{safe or "conversation"}.json"'},
    )


@app.post("/api/conversations/import", response_model=ConversationOut)
async def import_conversation(file: UploadFile = File(...), user=Depends(current_user)):
    raw = await file.read(2_000_001)
    if len(raw) > 2_000_000:
        raise HTTPException(status_code=413, detail="File is larger than 2 MB.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="That file is not valid JSON.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a conversation object.")
    try:
        cid = convo.import_payload(
            user["id"], payload.get("title"), payload.get("messages"),
            chat_svc.resolve_model(payload.get("model")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    row = convo.owned(cid, user["id"])
    from datetime import datetime, timezone
    return ConversationOut(id=row["id"], title=row["title"], model=row["model"],
                           updated_at=datetime.now(timezone.utc))
