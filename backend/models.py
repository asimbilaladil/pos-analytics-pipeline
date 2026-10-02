"""Request/response schemas.

Response models exist so a field can never leak by accident: password_hash and
session tokens are not present on any model the client can receive.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=512)


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str | None = None
    role: str
    is_admin: bool


class ConversationOut(BaseModel):
    id: int
    title: str
    model: str | None = None
    updated_at: datetime


class MessageOut(BaseModel):
    role: str
    content: str


class ConversationDetail(BaseModel):
    id: int
    title: str
    model: str | None = None
    messages: list[MessageOut]


class NewConversationIn(BaseModel):
    title: str | None = None
    model: str | None = None


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    model: str | None = None
    # Per-submission key from the client. A double-tapped Send or a reconnect
    # that resubmits reuses the key and is handed the SAME in-flight job back
    # instead of starting a second generation.
    idempotency_key: str | None = Field(default=None, max_length=100)


class JobOut(BaseModel):
    """What the client polls. Deliberately carries no question text."""
    job_id: str
    conversation_id: int
    status: str
    attempt: int = 1
    created_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None


class AskAcceptedOut(BaseModel):
    """Returned immediately by the messages endpoint.

    The answer is NOT here: generation outlives this response, and the client
    reaches the result through job_id. conversation_id is returned up front so a
    new conversation is addressable even if this response is the last thing the
    client ever receives.
    """
    conversation_id: int
    title: str
    job_id: str
    status: str
    user_message_id: int | None = None


class AskOut(BaseModel):
    conversation_id: int
    title: str
    answer: str
    duration_ms: int


class ModelsOut(BaseModel):
    models: list[str]
    default: str


class AdminUserOut(BaseModel):
    id: int
    email: str
    full_name: str | None = None
    role: str
    is_active: bool
    last_login_at: datetime | None = None
    conversation_count: int
    last_chat_at: datetime | None = None


class ImportIn(BaseModel):
    title: str | None = None
    model: str | None = None
    messages: list[MessageOut]
