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

import base64
import hashlib

from fastapi import (Cookie, Depends, FastAPI, File, Form, HTTPException, Response,
                     UploadFile, status)
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from . import chat as chat_svc
from . import jobs as jobs_svc

# uvicorn configures its own loggers and leaves the root logger at WARNING, so
# the chat timing line (logger "laynes.chat") was emitted and then dropped --
# the instrumentation existed but never reached the journal. Attach it to
# uvicorn's handlers explicitly so a slow request stays diagnosable from logs.
import logging as _logging

_chat_log = _logging.getLogger("laynes.chat")
_chat_log.setLevel(_logging.INFO)
if not _chat_log.handlers:
    _uv = _logging.getLogger("uvicorn")
    for _h in _uv.handlers:
        _chat_log.addHandler(_h)
    if not _uv.handlers:                      # uvicorn not configured (tests)
        _chat_log.addHandler(_logging.StreamHandler())
_chat_log.propagate = False
log = _chat_log
from . import conversations as convo
from .auth import (SESSION_COOKIE, clear_cookie, create_session, current_user,
                   delete_session, set_cookie, verify_login)
from .models import (
    JobOut,
    AskAcceptedOut,AdminUserOut, AskIn, AskOut, ConversationDetail, ConversationOut, ImportIn,
                     LoginIn, MessageOut, ModelsOut, NewConversationIn, UserOut)

# Wall-clock start of this process, for the startup job sweep (see
# jobs.sweep_interrupted): no job created before it can still be running.
from datetime import datetime as _dt, timezone as _tz  # noqa: E402
_PROCESS_STARTED_AT = _dt.now(_tz.utc)

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
    # Server-approved ids only: Claude's fixed list plus the configured,
    # verified OpenAI models. The client may only send back one of these.
    return ModelsOut(**chat_svc.model_options())


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
    msgs = convo.messages(cid, user["id"], with_model=True) or []
    return ConversationDetail(id=row["id"], title=row["title"], model=row["model"],
                              messages=[_message_out(m) for m in msgs])


def _message_out(m: dict) -> MessageOut:
    """An assistant message carries the model (and so the provider) that
    produced it, taken from the job that wrote it."""
    model = m.get("model") if m["role"] == "assistant" else None
    return MessageOut(role=m["role"], content=m["content"], model=model,
                      provider=chat_svc.provider_of(model) if model else None)


@app.delete("/api/conversations/{cid}", status_code=204)
def delete_conversation(cid: int, user=Depends(current_user)):
    if not convo.delete(cid, user["id"]):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return Response(status_code=204)


# ── chat ────────────────────────────────────────────────────────────────────
@app.post("/api/conversations/{cid}/messages", response_model=AskAcceptedOut,
          status_code=202)
def ask(cid: int, body: AskIn, user=Depends(current_user)):
    """Ask a question. cid = 0 starts a new conversation.

    A12 is NOT re-implemented here: chat_sql.answer_question runs the same gate
    it always has, server-side, before any transactional business SQL.
    """
    return _ask(cid, body.question, body.model, user,
                idempotency_key=body.idempotency_key)


# Images and PDFs go to the model natively; text-like files are inlined.
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_TEXT_EXTS = (".txt", ".csv", ".md", ".json", ".tsv", ".log")
MAX_FILES = 5
MAX_FILE_BYTES = 10_000_000


async def _to_block(f: UploadFile) -> dict:
    raw = await f.read(MAX_FILE_BYTES + 1)
    name = f.filename or "file"
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"{name} is larger than 10 MB.")
    ctype = (f.content_type or "").lower()
    if ctype in _IMAGE_TYPES:
        return {"type": "image", "source": {
            "type": "base64", "media_type": ctype,
            "data": base64.b64encode(raw).decode()}}
    if ctype == "application/pdf" or name.lower().endswith(".pdf"):
        return {"type": "document", "title": name, "source": {
            "type": "base64", "media_type": "application/pdf",
            "data": base64.b64encode(raw).decode()}}
    if ctype.startswith("text/") or name.lower().endswith(_TEXT_EXTS):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail=f"{name} is not UTF-8 text.")
        return {"type": "document", "title": name,
                "source": {"type": "text", "media_type": "text/plain", "data": text}}
    raise HTTPException(
        status_code=415,
        detail=f"{name}: unsupported type. Use an image, PDF, or text/CSV file.")


@app.post("/api/conversations/{cid}/messages/upload",
          response_model=AskAcceptedOut, status_code=202)
async def ask_with_files(cid: int, question: str = Form(...),
                         model: str | None = Form(default=None),
                         idempotency_key: str | None = Form(default=None),
                         files: list[UploadFile] = File(default=[]),
                         user=Depends(current_user)):
    """Same as ask, plus up to MAX_FILES attachments sent with this turn only."""
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"Attach at most {MAX_FILES} files.")
    blocks = [await _to_block(f) for f in files]
    names = [f.filename or "file" for f in files]
    return await run_in_threadpool(_ask, cid, question, model, user, blocks, names,
                                   idempotency_key)


def _ask(cid: int, question: str, model: str | None, user,
         attachments: list[dict] | None = None, names: list[str] | None = None,
         idempotency_key: str | None = None):
    if not chat_svc.assistant_configured():
        raise HTTPException(status_code=503,
                            detail="The assistant is not configured on the server.")
    question = question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
    if len(question) > 4000:
        raise HTTPException(status_code=400, detail="Question is too long.")

    # A resend of a submission the server already has -- the phone locked
    # before the 202 was read, or the user tapped Retry -- is a LOOKUP. Checked
    # before any write, so it creates no conversation, no user message and no
    # second generation, and it hands back the job's own conversation (for a
    # new chat the client never learned that id). Client keys only: a derived
    # key cannot tell a resend from the same question asked again later.
    if idempotency_key:
        prior = jobs_svc.find_for_key(user["id"], idempotency_key)
        if prior:
            prow = convo.owned(prior["conversation_id"], user["id"])
            if prow:
                return AskAcceptedOut(
                    conversation_id=prior["conversation_id"], title=prow["title"],
                    job_id=str(prior["job_id"]), status=prior["status"],
                    user_message_id=prior.get("user_message_id"))

    # Validated AFTER the resend lookup: a submission the server already has
    # keeps the provider it was submitted with, even if that provider has
    # since been disabled -- its answer is still returned. A NEW submission for
    # a model that is not offered is refused, never re-routed to the other
    # provider.
    try:
        model = chat_svc.resolve_model(model, strict=True)
    except chat_svc.ModelUnavailable:
        raise HTTPException(
            status_code=400,
            detail="The selected AI model is not available right now. "
                   "Please choose another model.")
    if not chat_svc.provider_configured(model):
        raise HTTPException(status_code=503,
                            detail="The selected AI provider is not configured on the server.")

    is_new = cid == 0
    if is_new:
        cid = convo.create(user["id"], convo.auto_title(question), model)
    else:
        if not convo.owned(cid, user["id"]):
            raise HTTPException(status_code=404, detail="Conversation not found")
        convo.set_model(cid, model)

    history = convo.messages(cid, user["id"]) or []
    # File bytes are not stored; the transcript records which files were sent.
    stored = question + ("".join(f"\n\U0001F4CE {n}" for n in names) if names else "")

    # One logical submission -> at most one generation. Without a client key we
    # derive a stable one from (conversation, model, text) so even a client that
    # cannot send a key is protected against a double-tapped Send.
    key = idempotency_key or hashlib.sha256(
        f"{cid}|{model}|{stored}".encode()).hexdigest()[:64]

    job, created = jobs_svc.create_or_reuse(
        conversation_id=cid, user_id=user["id"], question=question, model=model,
        idempotency_key=key, user_message_id=None)
    if not created:
        # This exact submission is already generating. Hand back the SAME job
        # and ITS conversation: no duplicate user message, no second model run.
        # A new-chat resend that lost the race created an empty conversation a
        # moment ago; remove it so it never shows up in the sidebar.
        if is_new and job["conversation_id"] != cid:
            convo.delete(cid, user["id"])
        jrow = convo.owned(job["conversation_id"], user["id"])
        return AskAcceptedOut(conversation_id=job["conversation_id"],
                              title=jrow["title"] if jrow else "New chat",
                              job_id=str(job["job_id"]), status=job["status"],
                              user_message_id=job.get("user_message_id"))
    row = convo.owned(cid, user["id"])

    user_message_id = convo.add_message_returning_id(cid, "user", stored)
    jobs_svc.attach_user_message(str(job["job_id"]), user_message_id)
    convo.touch(cid, title=convo.auto_title(question) if is_new else None)

    job_id = str(job["job_id"])
    uid = user["id"]

    def _run():
        """Detached: the HTTP response for this submission has already gone.

        Every existing gate still applies -- chat_svc.ask calls the same
        chat_sql.answer_question, so source-aware gating, the SQL validator,
        HME safety, reconciliation blocking, timing instrumentation and the
        per-call provider bounds are untouched. `model` was fixed at
        submission: changing the picker later never changes this job.
        """
        log.info("chat_job generate job_id=%s provider=%s model=%s",
                 job_id, chat_svc.provider_of(model), model)
        answer, steps, dur, error = chat_svc.ask(history, question, model, attachments)
        assistant_id = convo.add_message_returning_id(cid, "assistant", answer)
        convo.touch(cid)
        convo.log_query(uid, question, steps, answer, error, dur,
                        conversation_id=cid, model=model)
        if error:
            # chat_svc.ask never raises: it returns a user-safe answer plus an
            # error string meant for the log. Record the failure so the client
            # clears its spinner and offers Retry; the answer text is still
            # persisted and readable in the transcript.
            jobs_svc.mark_failed(job_id, chat_svc.failure_code(error), answer, dur)
        else:
            jobs_svc.mark_completed(job_id, assistant_id, dur)

    jobs_svc.spawn(job_id, _run)
    return AskAcceptedOut(conversation_id=cid, title=row["title"],
                          job_id=job_id, status="queued",
                          user_message_id=user_message_id)


# ── chat jobs: recovering an answer after the connection drops ──────────────
# The browser does not need to stay connected for its own answer. It polls here,
# which only READS persisted state -- no metadata extraction, no reconciliation,
# no SQL, no Anthropic call. That is what makes polling cheap enough to do every
# couple of seconds while the tab is visible.
@app.get("/api/chat/jobs/{job_id}", response_model=JobOut)
def chat_job(job_id: str, user=Depends(current_user)):
    job = jobs_svc.get(job_id, user["id"])
    if not job:
        # Scoped by user_id in SQL, so another user's job is indistinguishable
        # from one that does not exist.
        raise HTTPException(status_code=404, detail="Job not found")
    return JobOut(**jobs_svc.to_public(job))


@app.get("/api/conversations/{cid}/active-job", response_model=JobOut | None)
def conversation_active_job(cid: int, user=Depends(current_user)):
    """Lets a reloaded page reattach without the client having kept any state.

    This is the path that fixes "I reloaded and my question vanished": the
    pending job is discoverable from the conversation alone.
    """
    if not convo.owned(cid, user["id"]):
        raise HTTPException(status_code=404, detail="Conversation not found")
    job = jobs_svc.active_for_conversation(cid, user["id"])
    if job:
        log.info("chat_job reattach job_id=%s conversation_id=%s status=%s",
                 job["job_id"], cid, job["status"])
    return JobOut(**jobs_svc.to_public(job)) if job else None


@app.on_event("startup")
def _warm_openai_availability():
    """Check OpenAI model availability in the background, so no page load
    ever waits on OpenAI (a no-op while OpenAI is not configured)."""
    try:
        chat_svc.warm_models()
    except Exception:                                  # noqa: BLE001
        log.exception("openai availability warm-up failed")


@app.on_event("startup")
def _sweep_interrupted_jobs():
    """Generation runs in-process, so a restart orphans in-flight jobs.

    They must not poll as 'processing' forever -- that is the stuck spinner this
    whole change exists to remove. Failing them with error_code='interrupted'
    surfaces the normal Retry affordance instead.
    """
    try:
        # Everything created before THIS process started is dead: jobs run in
        # this single worker's threads. Age alone missed young orphans.
        jobs_svc.sweep_interrupted(started_before=_PROCESS_STARTED_AT)
    except Exception:                                  # noqa: BLE001
        log.exception("chat_job startup sweep failed")


# ── super admin: read other users' chats ────────────────────────────────────
def admin_user(user=Depends(current_user)):
    if user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


@app.get("/api/admin/users", response_model=list[AdminUserOut])
def admin_list_users(admin=Depends(admin_user)):
    return [AdminUserOut(**r) for r in convo.admin_users()]


@app.get("/api/admin/users/{uid}/conversations", response_model=list[ConversationOut])
def admin_user_conversations(uid: int, admin=Depends(admin_user)):
    return [ConversationOut(**r) for r in convo.list_for_user(uid, limit=500)]


@app.get("/api/admin/conversations/{cid}", response_model=ConversationDetail)
def admin_get_conversation(cid: int, admin=Depends(admin_user)):
    found = convo.admin_conversation(cid)
    if not found:
        raise HTTPException(status_code=404, detail="Conversation not found")
    row, msgs = found
    return ConversationDetail(id=row["id"], title=row["title"], model=row["model"],
                              messages=[_message_out(m) for m in msgs])


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
