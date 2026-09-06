"""Conversation persistence. Every read and write is scoped to the owning user.

Ownership is enforced in SQL (WHERE user_id = %s) rather than by filtering
afterwards, so a guessed conversation id returns nothing instead of another
user's transcript.
"""

from __future__ import annotations

import json

from .db import query

MAX_IMPORT_MESSAGES = 500
MAX_MESSAGE_CHARS = 100_000


def list_for_user(user_id, limit=100):
    return query(
        "SELECT id, title, model, updated_at FROM chat_conversations "
        "WHERE user_id = %s ORDER BY updated_at DESC LIMIT %s",
        (user_id, limit),
    )


def create(user_id, title, model=None) -> int:
    row = query(
        "INSERT INTO chat_conversations (user_id, title, model) "
        "VALUES (%s,%s,%s) RETURNING id",
        (user_id, (title or "New chat")[:120], model), fetch="one", commit=True,
    )
    return row["id"]


def owned(conversation_id, user_id):
    return query(
        "SELECT id, title, model FROM chat_conversations "
        "WHERE id = %s AND user_id = %s",
        (conversation_id, user_id), fetch="one",
    )


def messages(conversation_id, user_id):
    """Messages for a conversation the user owns; None if they do not."""
    if not owned(conversation_id, user_id):
        return None
    rows = query(
        "SELECT role, content FROM chat_messages WHERE conversation_id = %s "
        "ORDER BY created_at, id",
        (conversation_id,),
    )
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def add_message(conversation_id, role, content):
    query("INSERT INTO chat_messages (conversation_id, role, content) VALUES (%s,%s,%s)",
          (conversation_id, role, content[:MAX_MESSAGE_CHARS]), fetch=None, commit=True)


def touch(conversation_id, title=None):
    if title is not None:
        query("UPDATE chat_conversations SET updated_at = NOW(), title = %s WHERE id = %s",
              (title[:120], conversation_id), fetch=None, commit=True)
    else:
        query("UPDATE chat_conversations SET updated_at = NOW() WHERE id = %s",
              (conversation_id,), fetch=None, commit=True)


def set_model(conversation_id, model):
    query("UPDATE chat_conversations SET model = %s WHERE id = %s",
          (model, conversation_id), fetch=None, commit=True)


def delete(conversation_id, user_id) -> bool:
    """Delete a conversation the user owns, preserving the audit trail.

    chat_query_log.conversation_id is a NO ACTION foreign key, so deleting a
    conversation that has ever been asked a question fails outright. Cascading
    would be the wrong fix: "every question is logged" is a promise the app
    makes to its users, and the log must outlive the transcript. So the log
    rows are DETACHED (conversation_id -> NULL) and keep their question, SQL,
    timing and user; only the link to the deleted thread goes.

    chat_messages cascades, which is correct -- those ARE the transcript.
    """
    if not owned(conversation_id, user_id):
        return False
    query("UPDATE chat_query_log SET conversation_id = NULL WHERE conversation_id = %s",
          (conversation_id,), fetch=None, commit=True)
    query("DELETE FROM chat_conversations WHERE id = %s AND user_id = %s",
          (conversation_id, user_id), fetch=None, commit=True)
    return True


def export_payload(conversation_id, user_id):
    convo = query(
        "SELECT id, title, model, created_at FROM chat_conversations "
        "WHERE id = %s AND user_id = %s",
        (conversation_id, user_id), fetch="one",
    )
    if not convo:
        return None
    msgs = messages(conversation_id, user_id) or []
    return {
        "title": convo["title"],
        "model": convo["model"],
        "created_at": convo["created_at"].isoformat() if convo["created_at"] else None,
        "messages": msgs,
    }


def import_payload(user_id, title, msgs, model=None) -> int:
    """Create a conversation from an uploaded transcript.

    Validated rather than trusted: unknown roles, oversized files and non-string
    content are rejected before anything is written.
    """
    if not isinstance(msgs, list) or not msgs:
        raise ValueError("No messages found in that file.")
    if len(msgs) > MAX_IMPORT_MESSAGES:
        raise ValueError(f"Too many messages (limit {MAX_IMPORT_MESSAGES}).")
    clean = []
    for m in msgs:
        if not isinstance(m, dict):
            raise ValueError("Each message must be an object.")
        role, content = m.get("role"), m.get("content")
        if role not in ("user", "assistant"):
            raise ValueError("Each message needs role 'user' or 'assistant'.")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Each message needs non-empty text content.")
        clean.append((role, content[:MAX_MESSAGE_CHARS]))
    cid = create(user_id, title or "Imported conversation", model)
    for role, content in clean:
        add_message(cid, role, content)
    touch(cid)
    return cid


def auto_title(first_prompt: str) -> str:
    t = " ".join((first_prompt or "").split())
    return (t[:57] + "…") if len(t) > 58 else (t or "New chat")


def log_query(user_id, question, steps, answer, error, duration_ms,
              conversation_id=None, model=None):
    """Mirror of the Streamlit app's audit log -- every question stays logged."""
    last_sql = steps[-1]["sql"] if steps else None
    row_count = steps[-1]["row_count"] if steps else None
    try:
        query(
            "INSERT INTO chat_query_log (user_id, question, generated_sql, row_count, "
            "error, duration_ms, model, steps, conversation_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (user_id, question, last_sql, row_count, error, duration_ms, model,
             json.dumps(steps or []), conversation_id),
            fetch=None, commit=True,
        )
    except Exception:
        # Logging must never take down an answer the user already received.
        pass
