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


class AskOut(BaseModel):
    conversation_id: int
    title: str
    answer: str
    duration_ms: int


class ModelsOut(BaseModel):
    models: list[str]
    default: str


class ImportIn(BaseModel):
    title: str | None = None
    model: str | None = None
    messages: list[MessageOut]
